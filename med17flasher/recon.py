"""Read-only ECU reconnaissance ("scan").

With just an OBD/CAN adapter and no profile or seed/key yet, the first thing you
do is *characterise* the ECU safely: confirm communication, see which
diagnostic sessions it accepts, read every identification DID it exposes, and
observe how Security Access behaves (request seeds - never send keys).

:class:`EcuScanner` does exactly that and nothing that writes to the ECU. From
the result it can emit a profile *skeleton* (CAN ids, the security level that
gates programming, the DIDs found) and the seeds it collected - the starting
point for building a real profile and, once you can also capture the matching
keys, recovering the seed/key algorithm.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .core import uds_const as C
from .core.ecu_profile import CanConfig, EcuProfile, SecurityConfig
from .core.uds import UdsClient
from .exceptions import NegativeResponseError, UdsTimeoutError
from .logging_setup import get_logger

log = get_logger("recon")

# A curated set of identification DIDs worth reading on a VAG/Bosch ECU.
_IDENT_DIDS: List[Tuple[int, str]] = [
    (0xF187, "VW spare part number"),
    (0xF189, "Vehicle manufacturer ECU SW version"),
    (0xF190, "VIN"),
    (0xF191, "System supplier ECU HW number"),
    (0xF192, "System supplier ECU HW version"),
    (0xF194, "System supplier ECU SW number"),
    (0xF195, "System supplier ECU SW version"),
    (0xF197, "System name / engine code"),
    (0xF19E, "ASAM/ODX file identifier"),
    (0xF1A2, "ODX file version"),
    (0xF186, "Active diagnostic session"),
    (0xF18C, "ECU serial number"),
    (0xF180, "Boot software identification"),
    (0xF181, "Application software identification"),
    (0xF182, "Application data identification"),
    (0xF17C, "FAZIT identification string"),
    (0xF1AA, "Vehicle manufacturer ECU SW/HW number"),
]

# Security-access request-seed sub-functions to probe (odd = requestSeed).
_SEED_LEVELS = [0x01, 0x03, 0x05, 0x07, 0x09, 0x11, 0x13]


@dataclass
class ScanReport:
    tx_id: int
    rx_id: int
    online: bool = False
    sessions_supported: List[int] = field(default_factory=list)
    identification: Dict[int, bytes] = field(default_factory=dict)
    identification_names: Dict[int, str] = field(default_factory=dict)
    seeds: Dict[int, str] = field(default_factory=dict)  # level -> seed hex or "NRC 0x.."
    seed_lengths: Dict[int, int] = field(default_factory=dict)
    memory_read: Optional[str] = None  # sample hex or an NRC note
    notes: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    def text_summary(self) -> str:
        lines = [
            f"ECU {'online' if self.online else 'OFFLINE'} (tx=0x{self.tx_id:03X} rx=0x{self.rx_id:03X})",
            f"  sessions accepted : {', '.join('0x%02X' % s for s in self.sessions_supported) or 'none'}",
            f"  identification    : {len(self.identification)} DID(s)",
        ]
        for did, val in self.identification.items():
            name = self.identification_names.get(did, "")
            printable = val.decode("latin-1", "replace").strip("\x00 ")
            lines.append(f"      0x{did:04X} {name:<38} {printable!r}")
        lines.append("  security access   :")
        for level, info in self.seeds.items():
            lines.append(f"      requestSeed 0x{level:02X} -> {info}")
        if self.memory_read is not None:
            lines.append(f"  readMemoryByAddress: {self.memory_read}")
        for note in self.notes:
            lines.append(f"  note: {note}")
        return "\n".join(lines)

    def programming_security_level(self) -> Optional[int]:
        """Best guess at the level that guards programming (highest that gave a seed)."""

        got = [lvl for lvl, info in self.seeds.items() if info.startswith("seed ")]
        if not got:
            return None
        # The programming level is conventionally the highest one that returns a
        # non-zero seed; 0x11 is the usual MED17 value.
        if 0x11 in got:
            return 0x11
        return max(got)

    def to_profile(self, name: str = "MED17.7.5") -> EcuProfile:
        sec = SecurityConfig()
        lvl = self.programming_security_level()
        if lvl is not None:
            sec = SecurityConfig(request_seed_level=lvl, send_key_level=lvl + 1,
                                 algorithm="med17", params={})
        profile = EcuProfile(
            name=name,
            description="Skeleton derived from an ECU scan (memory map still unknown - "
                        "add it from an ODX/flash description or a flash trace)",
            can=CanConfig(tx_id=self.tx_id, rx_id=self.rx_id),
            security=sec,
            memory_map=[],
        )
        return profile


class EcuScanner:
    """Perform a safe, read-only characterisation of an ECU."""

    def __init__(self, uds: UdsClient, tx_id: int, rx_id: int) -> None:
        self.uds = uds
        self.tx_id = tx_id
        self.rx_id = rx_id

    def scan(
        self,
        *,
        probe_programming_session: bool = False,
        seed_levels: Optional[List[int]] = None,
        read_memory_at: Optional[int] = None,
        dids: Optional[List[Tuple[int, str]]] = None,
    ) -> ScanReport:
        report = ScanReport(tx_id=self.tx_id, rx_id=self.rx_id)

        # 1. Is anyone home? A TesterPresent that gets any answer means online.
        try:
            self.uds.tester_present(suppress_response=False)
            report.online = True
        except (UdsTimeoutError, NegativeResponseError):
            # A negative response still proves the ECU is there.
            report.online = True
        except Exception as exc:  # noqa: BLE001
            report.notes.append(f"no response to TesterPresent: {exc}")
            report.online = False
            return report

        # 2. Which sessions does it accept?
        sessions = [("default", C.Session.DEFAULT), ("extended", C.Session.EXTENDED_DIAGNOSTIC)]
        if probe_programming_session:
            sessions.append(("programming", C.Session.PROGRAMMING))
        for _label, sess in sessions:
            try:
                self.uds.diagnostic_session_control(int(sess))
                report.sessions_supported.append(int(sess))
            except (NegativeResponseError, UdsTimeoutError):
                pass

        # Make sure we are in extended session for DID/seed probing.
        try:
            self.uds.enter_extended_session()
        except (NegativeResponseError, UdsTimeoutError):
            pass

        # 3. Identification DIDs.
        for did, dname in (dids or _IDENT_DIDS):
            try:
                value = self.uds.read_data_by_identifier(did)
                report.identification[did] = value
                report.identification_names[did] = dname
            except (NegativeResponseError, UdsTimeoutError):
                continue

        # 4. Security access - request seeds only (never send a key).
        for level in (seed_levels or _SEED_LEVELS):
            try:
                seed = self.uds.request_seed(level)
                if any(seed):
                    report.seeds[level] = f"seed {seed.hex()}"
                    report.seed_lengths[level] = len(seed)
                else:
                    report.seeds[level] = "seed all-zero (already unlocked?)"
            except NegativeResponseError as exc:
                report.seeds[level] = f"NRC 0x{exc.nrc:02X} ({exc.nrc_name})"
            except UdsTimeoutError:
                report.seeds[level] = "no response"

        # 5. Optional tiny read-only memory probe.
        if read_memory_at is not None:
            try:
                data = self.uds.read_memory_by_address(read_memory_at, 16)
                report.memory_read = f"0x{read_memory_at:08X}: {data.hex()}"
            except NegativeResponseError as exc:
                report.memory_read = f"0x{read_memory_at:08X}: NRC 0x{exc.nrc:02X} ({exc.nrc_name})"
            except UdsTimeoutError:
                report.memory_read = f"0x{read_memory_at:08X}: no response"

        # Back to default session so we leave the ECU as we found it.
        try:
            self.uds.enter_default_session()
        except (NegativeResponseError, UdsTimeoutError):
            pass

        if not report.identification and not report.seeds:
            report.notes.append("ECU answered but exposed no DIDs/seeds on the probed set")
        return report
