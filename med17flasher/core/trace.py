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
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from ..logging_setup import get_logger
from . import uds_const as C
from .can_backends import CanBus, CanFrame, VirtualCanNetwork
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


# --------------------------------------------------------------------------- #
# Live capture from a real bus / adapter
# --------------------------------------------------------------------------- #
def capture_frames(
    bus: CanBus,
    *,
    seconds: Optional[float] = None,
    stop_event: Optional[threading.Event] = None,
    on_frame: Optional[Callable[[TraceFrame], None]] = None,
    max_frames: Optional[int] = None,
) -> List[TraceFrame]:
    """Passively record CAN frames from ``bus`` (a real adapter or the sim bus).

    Records until ``seconds`` elapse, ``stop_event`` is set, or ``max_frames``
    is reached. Feed the returned frames (or a saved candump) to :func:`analyze`.
    This is read-only - the software never transmits (only recv). Note a real
    CAN controller still ACKs received frames at the hardware layer; use a
    listen-only-capable interface if even that must be avoided.
    """

    frames: List[TraceFrame] = []
    deadline = (time.monotonic() + seconds) if seconds else None
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        if deadline is not None and time.monotonic() >= deadline:
            break
        if max_frames is not None and len(frames) >= max_frames:
            break
        remaining = 0.25
        if deadline is not None:
            remaining = max(0.0, min(remaining, deadline - time.monotonic()))
        frame = bus.recv(timeout=remaining or 0.01)
        if frame is None:
            continue
        tf = TraceFrame(frame.timestamp or time.time(), frame.arbitration_id,
                        frame.data, frame.is_extended_id)
        frames.append(tf)
        if on_frame is not None:
            try:
                on_frame(tf)
            except Exception:  # pragma: no cover - a UI callback must not stop capture
                pass
    return frames


class LiveCapture:
    """Background passive capture from a :class:`CanBus`."""

    def __init__(self, bus: CanBus, on_frame: Optional[Callable[[TraceFrame], None]] = None) -> None:
        self.bus = bus
        self._on_frame = on_frame
        self._frames: List[TraceFrame] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _run(self) -> None:
        def collect(tf: TraceFrame) -> None:
            self._frames.append(tf)
            if self._on_frame:
                self._on_frame(tf)

        capture_frames(self.bus, stop_event=self._stop, on_frame=collect)

    def start(self) -> "LiveCapture":
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="live-capture", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> List[TraceFrame]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        return list(self._frames)

    @property
    def frames(self) -> List[TraceFrame]:
        return list(self._frames)


# --------------------------------------------------------------------------- #
# Live UDS decode (for `med17flasher sniff`)
# --------------------------------------------------------------------------- #
@dataclass
class FlowEvent:
    """One decoded, human-readable step observed live while sniffing.

    ``kind`` is a stable machine tag (``session``, ``seed``, ``key``,
    ``seedkey``, ``download``, ``transfer``, ``erase``, ``checkmemory``,
    ``routine``, ``exit``, ``reset``, ``did``, ``nrc``); ``text`` is ready to
    print; ``detail`` carries the parsed fields for a UI.
    """

    timestamp: float
    kind: str
    text: str
    detail: Dict = field(default_factory=dict)


class _DirAssembler:
    """Incremental ISO-TP reassembly for a single CAN id (flow control ignored).

    Fed one CAN payload at a time, it returns a completed UDS message the moment
    the last segment arrives and ``None`` otherwise - the streaming counterpart
    of :func:`reassemble`, which works over a whole frame list at once.
    """

    __slots__ = ("_buf", "_expected")

    def __init__(self) -> None:
        self._buf: Optional[bytearray] = None
        self._expected = 0

    def feed(self, data: bytes) -> Optional[bytes]:
        if not data:
            return None
        pci = data[0] >> 4
        if pci == 0x0:  # Single Frame
            length = data[0] & 0x0F
            if length == 0 and len(data) > 8:  # ISO-TP 2016 escape
                length = data[1]
                out = bytes(data[2 : 2 + length])
            else:
                out = bytes(data[1 : 1 + length])
            self._buf = None
            return out
        if pci == 0x1:  # First Frame
            length = ((data[0] & 0x0F) << 8) | data[1]
            if length == 0:  # 32-bit escape length
                length = int.from_bytes(data[2:6], "big")
                self._buf = bytearray(data[6:])
            else:
                self._buf = bytearray(data[2:])
            self._expected = length
            if len(self._buf) >= self._expected:
                out = bytes(self._buf[: self._expected])
                self._buf = None
                return out
            return None
        if pci == 0x2:  # Consecutive Frame
            if self._buf is None:
                return None
            self._buf.extend(data[1:])
            if len(self._buf) >= self._expected:
                out = bytes(self._buf[: self._expected])
                self._buf = None
                return out
            return None
        return None  # Flow Control - nothing to reassemble


_SESSION_NAMES = {0x01: "Standard", 0x02: "Programmierung", 0x03: "Erweitert",
                  0x04: "Sicherheitssystem"}
_RESET_NAMES = {0x01: "Hard-Reset", 0x02: "Key-Off/On", 0x03: "Soft-Reset"}
_NRC_NAMES = {
    0x10: "generalReject", 0x11: "serviceNotSupported",
    0x22: "conditionsNotCorrect", 0x24: "requestSequenceError",
    0x31: "requestOutOfRange", 0x33: "securityAccessDenied",
    0x35: "invalidKey", 0x36: "exceedNumberOfAttempts",
    0x72: "programmingFailure", 0x73: "wrongBlockSequenceCounter",
}


class LiveUdsTracker:
    """Turn a live stream of raw CAN frames into high-level UDS steps.

    Feed it every frame seen on the bus - both the tester's requests and the
    ECU's responses, no matter which CAN ids they use - and it emits
    :class:`FlowEvent` objects: session changes, the seed and key of each
    Security Access (paired automatically), each RequestDownload
    address/size, transfer progress, the erase / checkMemory routines and
    ECUReset. It keeps one ISO-TP assembler per CAN id, so it does not need to
    be told the request/response ids in advance - ideal for sniffing another
    tool whose addressing you may not know yet.

    This is for *live confidence*, not the system of record: it decodes the
    head of the flow as it happens. The authoritative memory map and seed/key
    pairs still come from :func:`analyze` over the full recording afterwards.
    """

    def __init__(self, *, transfer_every: int = 64) -> None:
        self._asm: Dict[int, _DirAssembler] = {}
        self._pending_seed: Dict[int, bytes] = {}
        self._transfers = 0
        self._transfer_every = max(1, int(transfer_every))
        self._dids_seen: set = set()
        self.frame_count = 0

    def feed(self, frame: TraceFrame) -> List[FlowEvent]:
        self.frame_count += 1
        asm = self._asm.get(frame.arbitration_id)
        if asm is None:
            asm = self._asm[frame.arbitration_id] = _DirAssembler()
        msg = asm.feed(frame.data)
        if not msg:
            return []
        return self._classify(frame.timestamp, msg)

    def _classify(self, ts: float, msg: bytes) -> List[FlowEvent]:
        sid = msg[0]
        ev: List[FlowEvent] = []

        if sid == C.Service.DIAGNOSTIC_SESSION_CONTROL and len(msg) > 1:
            sub = msg[1] & 0x7F
            name = _SESSION_NAMES.get(sub, f"0x{sub:02X}")
            ev.append(FlowEvent(ts, "session", f"Sitzung -> {name} (0x{sub:02X})",
                                {"session": sub}))

        elif sid == 0x67 and len(msg) > 2:  # SecurityAccess: positive seed
            level = msg[1]
            seed = bytes(msg[2:])
            self._pending_seed[level] = seed
            ev.append(FlowEvent(ts, "seed",
                                f"Security Access L0x{level:02X}: Seed = {seed.hex()}",
                                {"level": level, "seed": seed.hex()}))

        elif sid == C.Service.SECURITY_ACCESS and len(msg) > 2 and (msg[1] % 2 == 0):
            level = msg[1]  # even = sendKey
            key = bytes(msg[2:])
            seed = self._pending_seed.get(level - 1)
            ev.append(FlowEvent(ts, "key",
                                f"Security Access L0x{level - 1:02X}: Key  = {key.hex()}",
                                {"level": level - 1, "key": key.hex()}))
            if seed is not None:
                ev.append(FlowEvent(ts, "seedkey",
                                    f"  -> Seed/Key-Paar L0x{level - 1:02X}: "
                                    f"{seed.hex()} / {key.hex()}",
                                    {"level": level - 1, "seed": seed.hex(),
                                     "key": key.hex()}))

        elif sid == C.Service.REQUEST_DOWNLOAD and len(msg) >= 4:
            alfid = msg[2]
            addr_len = alfid & 0x0F
            size_len = (alfid >> 4) & 0x0F
            addr = int.from_bytes(msg[3 : 3 + addr_len], "big")
            size = int.from_bytes(msg[3 + addr_len : 3 + addr_len + size_len], "big")
            self._transfers = 0
            ev.append(FlowEvent(ts, "download",
                                f"RequestDownload -> 0x{addr:08X}  {size} Bytes "
                                f"(0x{size:X})",
                                {"address": addr, "size": size}))

        elif sid == C.Service.REQUEST_UPLOAD and len(msg) >= 4:
            alfid = msg[2]
            addr_len = alfid & 0x0F
            size_len = (alfid >> 4) & 0x0F
            addr = int.from_bytes(msg[3 : 3 + addr_len], "big")
            size = int.from_bytes(msg[3 + addr_len : 3 + addr_len + size_len], "big")
            self._transfers = 0
            ev.append(FlowEvent(ts, "upload",
                                f"RequestUpload (lesen) <- 0x{addr:08X}  {size} Bytes",
                                {"address": addr, "size": size}))

        elif sid == C.Service.TRANSFER_DATA:
            self._transfers += 1
            if self._transfers % self._transfer_every == 0:
                ev.append(FlowEvent(ts, "transfer",
                                    f"  TransferData: {self._transfers} Bloecke ...",
                                    {"transfers": self._transfers}))

        elif sid == C.Service.REQUEST_TRANSFER_EXIT:
            ev.append(FlowEvent(ts, "exit",
                                f"RequestTransferExit ({self._transfers} Bloecke)",
                                {"transfers": self._transfers}))

        elif sid == C.Service.ROUTINE_CONTROL and len(msg) >= 4:
            rid = (msg[2] << 8) | msg[3]
            args = msg[4:]
            parsed = _looks_like_alfid_addr_size(args)
            if parsed and len(args) >= 1 + (args[0] & 0x0F) + ((args[0] >> 4) & 0x0F) + 4:
                ev.append(FlowEvent(ts, "checkmemory",
                                    f"RoutineControl checkMemory (0x{rid:04X})",
                                    {"routine": rid}))
            elif parsed:
                addr, size = parsed
                ev.append(FlowEvent(ts, "erase",
                                    f"RoutineControl eraseMemory (0x{rid:04X}) "
                                    f"0x{addr:08X}  {size} Bytes",
                                    {"routine": rid, "address": addr, "size": size}))
            else:
                ev.append(FlowEvent(ts, "routine",
                                    f"RoutineControl 0x{rid:04X}", {"routine": rid}))

        elif sid == C.Service.ECU_RESET and len(msg) > 1:
            name = _RESET_NAMES.get(msg[1], f"0x{msg[1]:02X}")
            ev.append(FlowEvent(ts, "reset", f"ECUReset ({name})", {"reset": msg[1]}))

        elif sid == C.Service.READ_DATA_BY_IDENTIFIER and len(msg) >= 3:
            did = (msg[1] << 8) | msg[2]
            if did not in self._dids_seen:
                self._dids_seen.add(did)
                ev.append(FlowEvent(ts, "did", f"ReadDataByIdentifier 0x{did:04X}",
                                    {"did": did}))

        elif sid == C.NEGATIVE_RESPONSE_SID and len(msg) >= 3:
            nrc = msg[2]
            if nrc != 0x78:  # responsePending is normal churn - stay quiet
                name = _NRC_NAMES.get(nrc, f"0x{nrc:02X}")
                ev.append(FlowEvent(ts, "nrc",
                                    f"NegativeResponse SID 0x{msg[1]:02X}: {name}",
                                    {"sid": msg[1], "nrc": nrc}))

        return ev
