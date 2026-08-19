"""Offline CAN-trace analysis.

Feed in a CAN log captured during a real flash (or any diagnostic session) and
this module reconstructs the UDS exchange and *derives* the things you would
otherwise have to configure by hand:

* the **ECU profile** - CAN ids, the security level(s) used, the erase /
  checkMemory routine ids, the RequestDownload address/size of every program
  block (i.e. the memory map), and the DIDs that were read;
* the **seed/key pairs** exchanged during Security Access, ready to feed to the
  :mod:`med17flasher.seedkey.solver`.

Supported log formats:

* ``candump`` / ``candump -L`` (Linux SocketCAN)      ``(ts) can0 7E0#0210...``
* Vector **ASC** (``.asc``)                            ``0.1 1 7E0 Tx d 8 02 10 03 ...``
* a permissive **CSV / hex** format                    ``ts,id,hexdata``

It also provides a :class:`BusRecorder` that captures a run on a
:class:`~med17flasher.core.can_backends.VirtualCanNetwork` and writes it out as
a candump log - which is how the tests generate realistic traces from the
simulator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..logging_setup import get_logger
from . import uds_const as C
from .can_backends import CanFrame, VirtualCanNetwork
from .ecu_profile import (
    CanConfig,
    EcuProfile,
    MemoryRegion,
    RoutineConfig,
    SecurityConfig,
)

log = get_logger("core.trace")


@dataclass
class TraceFrame:
    timestamp: float
    arbitration_id: int
    data: bytes
    is_extended_id: bool = False


# --------------------------------------------------------------------------- #
# Log readers / writers
# --------------------------------------------------------------------------- #
_CANDUMP_RE = re.compile(
    r"\(?(?P<ts>\d+\.\d+)\)?\s+\S+\s+(?P<id>[0-9A-Fa-f]+)#(?P<data>[0-9A-Fa-f]*)"
)
# Vector ASC data line: "<ts> <chan> <id> Rx|Tx d <dlc> <b0> <b1> ..."
_ASC_RE = re.compile(
    r"^\s*(?P<ts>\d+\.\d+)\s+\d+\s+(?P<id>[0-9A-Fa-fx]+)\s+(?P<dir>Rx|Tx)\s+d\s+"
    r"(?P<dlc>\d+)\s+(?P<data>[0-9A-Fa-f ]*)"
)


def read_candump(path: str) -> List[TraceFrame]:
    frames: List[TraceFrame] = []
    with open(path, "r", encoding="ascii", errors="replace") as fh:
        for line in fh:
            m = _CANDUMP_RE.search(line)
            if not m:
                continue
            arb = int(m.group("id"), 16)
            data = bytes.fromhex(m.group("data"))
            frames.append(TraceFrame(float(m.group("ts")), arb, data, arb > 0x7FF))
    return frames


def read_asc(path: str) -> List[TraceFrame]:
    frames: List[TraceFrame] = []
    with open(path, "r", encoding="ascii", errors="replace") as fh:
        for line in fh:
            m = _ASC_RE.match(line)
            if not m:
                continue
            raw_id = m.group("id").lower().replace("x", "")
            if not raw_id:
                continue
            arb = int(raw_id, 16)
            data = bytes.fromhex(m.group("data").replace(" ", ""))
            frames.append(TraceFrame(float(m.group("ts")), arb, data, arb > 0x7FF))
    return frames


def read_csv(path: str) -> List[TraceFrame]:
    """Permissive ``timestamp, id, hexdata`` (comma/semicolon/space separated)."""

    frames: List[TraceFrame] = []
    with open(path, "r", encoding="ascii", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line[0] in "#;/":
                continue
            parts = re.split(r"[,;\s]+", line)
            if len(parts) < 3:
                continue
            try:
                ts = float(parts[0])
                arb = int(parts[1], 16) if not parts[1].isdigit() else int(parts[1], 0)
                data = bytes.fromhex(parts[2])
            except ValueError:
                continue
            frames.append(TraceFrame(ts, arb, data, arb > 0x7FF))
    return frames


def read_trace(path: str) -> List[TraceFrame]:
    """Dispatch on extension / content to read any supported log."""

    lower = path.lower()
    if lower.endswith(".asc"):
        return read_asc(path)
    if lower.endswith((".csv", ".txt")):
        # try candump first (it also uses .txt/.log), then csv
        frames = read_candump(path)
        return frames if frames else read_csv(path)
    return read_candump(path)


def write_candump(frames: List[TraceFrame], path: str, channel: str = "can0") -> None:
    with open(path, "w", encoding="ascii") as fh:
        for f in frames:
            fh.write(f"({f.timestamp:.6f}) {channel} {f.arbitration_id:03X}#{f.data.hex().upper()}\n")


# --------------------------------------------------------------------------- #
# Offline ISO-TP reassembly
# --------------------------------------------------------------------------- #
def reassemble(frames: List[TraceFrame], arbitration_id: int) -> List[bytes]:
    """Reassemble the ISO-TP messages carried on one CAN id (flow control ignored)."""

    messages: List[bytes] = []
    buf: Optional[bytearray] = None
    expected = 0
    for f in frames:
        if f.arbitration_id != arbitration_id or not f.data:
            continue
        data = f.data
        pci = data[0] >> 4
        if pci == 0x0:  # Single Frame
            length = data[0] & 0x0F
            if length == 0 and len(data) > 8:
                length = data[1]
                messages.append(bytes(data[2 : 2 + length]))
            else:
                messages.append(bytes(data[1 : 1 + length]))
            buf = None
        elif pci == 0x1:  # First Frame
            length = ((data[0] & 0x0F) << 8) | data[1]
            if length == 0:
                length = int.from_bytes(data[2:6], "big")
                buf = bytearray(data[6:])
            else:
                buf = bytearray(data[2:])
            expected = length
            if len(buf) >= expected:
                messages.append(bytes(buf[:expected]))
                buf = None
        elif pci == 0x2:  # Consecutive Frame
            if buf is not None:
                buf.extend(data[1:])
                if len(buf) >= expected:
                    messages.append(bytes(buf[:expected]))
                    buf = None
        # pci == 0x3 (Flow Control) is ignored for reconstruction
    return messages


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
@dataclass
class DownloadBlock:
    address: int
    size: int
    transfers: int = 0


@dataclass
class TraceReport:
    tx_id: int
    rx_id: int
    sessions: List[int] = field(default_factory=list)
    security_levels: List[int] = field(default_factory=list)
    seed_key_pairs: List[Tuple[int, bytes, bytes]] = field(default_factory=list)  # (level, seed, key)
    erase_routine: Optional[int] = None
    check_memory_routine: Optional[int] = None
    check_dependencies_routine: Optional[int] = None
    download_blocks: List[DownloadBlock] = field(default_factory=list)
    dids: Dict[int, bytes] = field(default_factory=dict)
    request_count: int = 0
    response_count: int = 0

    def to_profile(self, name: str = "MED17.7.5") -> EcuProfile:
        """Build an EcuProfile skeleton from what the trace revealed."""

        regions = [
            MemoryRegion(f"BLOCK{i+1}", b.address, b.size, checksum="crc32")
            for i, b in enumerate(self.download_blocks)
        ]
        sec = SecurityConfig()
        if self.security_levels:
            req = min(self.security_levels)  # requestSeed levels are odd
            sec = SecurityConfig(request_seed_level=req, send_key_level=req + 1,
                                 algorithm="med17", params={})
        routines = RoutineConfig()
        if self.erase_routine is not None:
            routines.erase_memory = self.erase_routine
        if self.check_memory_routine is not None:
            routines.check_memory = self.check_memory_routine
        if self.check_dependencies_routine is not None:
            routines.check_programming_dependencies = self.check_dependencies_routine
        return EcuProfile(
            name=name,
            description=f"Derived from CAN trace ({len(regions)} block(s))",
            can=CanConfig(tx_id=self.tx_id, rx_id=self.rx_id),
            security=sec,
            routines=routines,
            memory_map=regions,
        )

    def seed_key_pairs_for_solver(self):
        from ..seedkey.solver import SeedKeyPair

        return [SeedKeyPair(seed, key) for _lvl, seed, key in self.seed_key_pairs]


def _looks_like_alfid_addr_size(data: bytes) -> Optional[Tuple[int, int]]:
    if len(data) < 1:
        return None
    alfid = data[0]
    addr_len = alfid & 0x0F
    size_len = (alfid >> 4) & 0x0F
    if addr_len == 0 or size_len == 0 or len(data) < 1 + addr_len + size_len:
        return None
    address = int.from_bytes(data[1 : 1 + addr_len], "big")
    size = int.from_bytes(data[1 + addr_len : 1 + addr_len + size_len], "big")
    return address, size


def analyze(frames: List[TraceFrame], tx_id: int = 0x7E0, rx_id: int = 0x7E8) -> TraceReport:
    """Reconstruct the UDS flow and derive a :class:`TraceReport`."""

    requests = reassemble(frames, tx_id)
    responses = reassemble(frames, rx_id)
    report = TraceReport(tx_id=tx_id, rx_id=rx_id)
    report.request_count = len(requests)
    report.response_count = len(responses)

    # Pair each request with the next positive/negative response in order,
    # skipping suppressed-response TesterPresent requests.
    ri = 0

    def next_response() -> Optional[bytes]:
        nonlocal ri
        if ri < len(responses):
            resp = responses[ri]
            ri += 1
            return resp
        return None

    pending_seed: Dict[int, bytes] = {}
    dl_current: Optional[DownloadBlock] = None

    for req in requests:
        if not req:
            continue
        sid = req[0]

        # TesterPresent with suppressPositiveResponse consumes no response.
        if sid == C.Service.TESTER_PRESENT:
            if not (len(req) > 1 and req[1] & 0x80):
                next_response()
            continue

        resp = next_response()

        if sid == C.Service.DIAGNOSTIC_SESSION_CONTROL and len(req) > 1:
            if req[1] not in report.sessions:
                report.sessions.append(req[1])

        elif sid == C.Service.SECURITY_ACCESS and len(req) > 1:
            level = req[1]
            if level % 2 == 1:  # requestSeed
                if level not in report.security_levels:
                    report.security_levels.append(level)
                if resp and resp[0] == 0x67 and len(resp) > 2:
                    pending_seed[level] = bytes(resp[2:])
            else:  # sendKey
                key = bytes(req[2:])
                seed = pending_seed.get(level - 1)
                if seed is not None and resp and resp[0] == 0x67:
                    report.seed_key_pairs.append((level - 1, seed, key))

        elif sid == C.Service.ROUTINE_CONTROL and len(req) >= 4:
            rid = (req[2] << 8) | req[3]
            args = req[4:]
            parsed = _looks_like_alfid_addr_size(args)
            if parsed and len(args) >= 1 + (args[0] & 0x0F) + ((args[0] >> 4) & 0x0F) + 4:
                # ALFID+addr+size+checksum -> checkMemory
                report.check_memory_routine = report.check_memory_routine or rid
            elif parsed:
                report.erase_routine = report.erase_routine or rid
            elif not args:
                report.check_dependencies_routine = report.check_dependencies_routine or rid

        elif sid == C.Service.REQUEST_DOWNLOAD and len(req) >= 3:
            alfid = req[2]
            addr_len = alfid & 0x0F
            size_len = (alfid >> 4) & 0x0F
            address = int.from_bytes(req[3 : 3 + addr_len], "big")
            size = int.from_bytes(req[3 + addr_len : 3 + addr_len + size_len], "big")
            dl_current = DownloadBlock(address, size)
            report.download_blocks.append(dl_current)

        elif sid == C.Service.TRANSFER_DATA:
            if dl_current is not None:
                dl_current.transfers += 1

        elif sid == C.Service.REQUEST_TRANSFER_EXIT:
            dl_current = None

        elif sid == C.Service.READ_DATA_BY_IDENTIFIER and len(req) >= 3:
            did = (req[1] << 8) | req[2]
            if resp and resp[0] == 0x62 and len(resp) >= 3:
                report.dids[did] = bytes(resp[3:])

    return report


# --------------------------------------------------------------------------- #
# Recording a virtual-bus run to a candump log
# --------------------------------------------------------------------------- #
class BusRecorder:
    """Passively capture every frame on a :class:`VirtualCanNetwork`.

    Attaches a sniffer endpoint; because a virtual endpoint's receive queue is
    unbounded, frames accumulate and are collected in order when you call
    :meth:`frames` / :meth:`save`.
    """

    def __init__(self, network: VirtualCanNetwork, name: str = "sniffer") -> None:
        self._endpoint = network.new_endpoint(name)
        self._collected: List[TraceFrame] = []

    def _drain(self) -> None:
        while True:
            frame: Optional[CanFrame] = self._endpoint.recv(timeout=0.0)
            if frame is None:
                break
            self._collected.append(
                TraceFrame(frame.timestamp, frame.arbitration_id, frame.data, frame.is_extended_id)
            )

    def frames(self) -> List[TraceFrame]:
        self._drain()
        return list(self._collected)

    def save(self, path: str, channel: str = "can0") -> None:
        write_candump(self.frames(), path, channel)

    def close(self) -> None:
        self._drain()
        self._endpoint.close()

    def __enter__(self) -> "BusRecorder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
