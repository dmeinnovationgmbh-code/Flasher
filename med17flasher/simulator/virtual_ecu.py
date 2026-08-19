"""An in-process virtual MED17.7.5 ECU that speaks real UDS.

The simulator sits on a :class:`~med17flasher.core.can_backends.VirtualCanBus`
and answers UDS requests exactly like a real ECU would: session control,
Security Access with a genuine seed/key challenge (using the *same* algorithm
the flasher computes against), erase, requestDownload / transferData /
requestTransferExit, and a real ``checkMemory`` that CRC-checks the bytes it
actually received.

That makes it possible to exercise - and regression-test - the whole flashing
tool-chain end to end with no hardware. It is also a great sandbox for learning
the protocol.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from ..core import checksum as _cs
from ..core import uds_const as C
from ..core.can_backends import CanBus
from ..core.ecu_profile import EcuProfile, builtin_med17_7_5
from ..core.isotp import IsoTpConfig, IsoTpLayer
from ..exceptions import IsoTpError, IsoTpTimeoutError
from ..logging_setup import get_logger
from ..seedkey import compute_key

log = get_logger("simulator")


@dataclass
class _DownloadState:
    address: int = 0
    size: int = 0
    received: int = 0
    next_bsc: int = 1
    active: bool = False


@dataclass
class VirtualEcuConfig:
    """Knobs controlling the simulated ECU's behaviour."""

    seed: bytes = b"\x11\x22\x33\x44"  # fixed seed for reproducible tests
    randomize_seed: bool = False
    security_algorithm: str = "med17"
    security_params: Dict[str, object] = field(
        default_factory=lambda: {"k": "0x1C5A36B7", "rounds": 5, "shift": 5}
    )
    max_block_length: int = 0x0402  # -> 1024 payload bytes per transferData
    #: How many responsePending (0x78) frames to emit before finishing a slow
    #: routine (erase / checkMemory). Exercises the client's 0x78 wait loop.
    pending_rounds: int = 0
    pending_delay: float = 0.01
    require_security_for_flash: bool = True


class VirtualEcu:
    """A threaded virtual MED17.7.5."""

    def __init__(
        self,
        bus: CanBus,
        profile: Optional[EcuProfile] = None,
        config: Optional[VirtualEcuConfig] = None,
    ) -> None:
        self.profile = profile or builtin_med17_7_5()
        self.config = config or VirtualEcuConfig()
        # The ECU listens on the tester's TX id and answers on the RX id.
        self.tp = IsoTpLayer(
            bus,
            IsoTpConfig(
                tx_id=self.profile.can.rx_id,
                rx_id=self.profile.can.tx_id,
                is_extended_id=self.profile.can.is_extended_id,
                padding_byte=self.profile.can.padding_byte,
            ),
        )

        # Flash memory model spanning the profile's memory map.
        starts = [r.start for r in self.profile.memory_map] or [0]
        ends = [r.end for r in self.profile.memory_map] or [0x1000]
        self._base = min(starts)
        self._mem = bytearray(b"\xff" * (max(ends) - self._base))

        self.session = int(C.Session.DEFAULT)
        self.unlocked = False
        self._current_seed = bytes(self.config.seed)
        self._download = _DownloadState()
        self.identification: Dict[int, bytes] = self._default_identification()
        self.erased_regions: list = []
        self.verified_regions: list = []

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seed_counter = 0

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> "VirtualEcu":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="virtual-ecu", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "VirtualEcu":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ------------------------------------------------------------------ #
    # Memory helpers (for tests / inspection)
    # ------------------------------------------------------------------ #
    def read_memory(self, address: int, size: int) -> bytes:
        off = address - self._base
        return bytes(self._mem[off : off + size])

    def _write_memory(self, address: int, data: bytes) -> None:
        off = address - self._base
        self._mem[off : off + len(data)] = data

    def _fill_memory(self, address: int, size: int, value: int = 0xFF) -> None:
        off = address - self._base
        self._mem[off : off + size] = bytes([value]) * size

    # ------------------------------------------------------------------ #
    # Serve loop
    # ------------------------------------------------------------------ #
    def _serve(self) -> None:
        log.info("virtual MED17.7.5 online (rx=0x%03X tx=0x%03X)",
                 self.profile.can.tx_id, self.profile.can.rx_id)
        while not self._stop.is_set():
            try:
                request = self.tp.recv(timeout=0.3)
            except IsoTpTimeoutError:
                continue
            except IsoTpError as exc:
                log.debug("simulator framing error: %s", exc)
                continue
            except Exception as exc:  # bus closed etc.
                log.debug("simulator recv error: %s", exc)
                break
            if not request:
                continue
            try:
                response = self._dispatch(request)
            except Exception:  # noqa: BLE001 - never kill the ECU thread
                log.exception("simulator dispatch error")
                response = self._nrc(request[0], C.NRC.GENERAL_REJECT)
            if response is not None:
                try:
                    self.tp.send(response)
                except Exception as exc:  # noqa: BLE001
                    log.debug("simulator send error: %s", exc)

    # ------------------------------------------------------------------ #
    # Request dispatch
    # ------------------------------------------------------------------ #
    def _dispatch(self, req: bytes) -> Optional[bytes]:
        sid = req[0]
        handler = {
            C.Service.DIAGNOSTIC_SESSION_CONTROL: self._h_session,
            C.Service.ECU_RESET: self._h_reset,
            C.Service.SECURITY_ACCESS: self._h_security,
            C.Service.COMMUNICATION_CONTROL: self._h_comm_control,
            C.Service.CONTROL_DTC_SETTING: self._h_dtc,
            C.Service.TESTER_PRESENT: self._h_tester_present,
            C.Service.READ_DATA_BY_IDENTIFIER: self._h_read_did,
            C.Service.WRITE_DATA_BY_IDENTIFIER: self._h_write_did,
            C.Service.ROUTINE_CONTROL: self._h_routine,
            C.Service.REQUEST_DOWNLOAD: self._h_request_download,
            C.Service.TRANSFER_DATA: self._h_transfer_data,
            C.Service.REQUEST_TRANSFER_EXIT: self._h_transfer_exit,
            C.Service.READ_MEMORY_BY_ADDRESS: self._h_read_memory,
            C.Service.CLEAR_DIAGNOSTIC_INFORMATION: self._h_clear_dtc,
        }.get(sid)
        if handler is None:
            return self._nrc(sid, C.NRC.SERVICE_NOT_SUPPORTED)
        return handler(req)

    # ---- helpers ------------------------------------------------------ #
    @staticmethod
    def _nrc(sid: int, nrc: int) -> bytes:
        return bytes([C.NEGATIVE_RESPONSE_SID, sid & 0xFF, int(nrc)])

    @staticmethod
    def _positive(sid: int) -> bytes:
        return bytes([sid + C.POSITIVE_RESPONSE_OFFSET])

    def _emit_pending(self, sid: int) -> None:
        """Send N responsePending frames, honoured by the client's 0x78 loop."""

        for _ in range(self.config.pending_rounds):
            self.tp.send(self._nrc(sid, C.NRC.REQUEST_CORRECTLY_RECEIVED_RESPONSE_PENDING))
            time.sleep(self.config.pending_delay)

    # ---- 0x10 --------------------------------------------------------- #
    def _h_session(self, req: bytes) -> bytes:
        session = req[1]
        self.session = session
        if session == int(C.Session.DEFAULT):
            self.unlocked = False
        # positive response carries P2 and P2* (2 bytes each, ms and 10ms units)
        return self._positive(req[0]) + bytes([session, 0x00, 0x32, 0x01, 0xF4])

    # ---- 0x11 --------------------------------------------------------- #
    def _h_reset(self, req: bytes) -> bytes:
        self.session = int(C.Session.DEFAULT)
        self.unlocked = False
        self._download = _DownloadState()
        return self._positive(req[0]) + bytes([req[1] if len(req) > 1 else 0x01])

    # ---- 0x27 --------------------------------------------------------- #
    def _h_security(self, req: bytes) -> bytes:
        level = req[1]
        if level % 2 == 1:  # requestSeed
            if self.unlocked:
                # Already unlocked -> zero seed by convention.
                return self._positive(req[0]) + bytes([level]) + b"\x00\x00\x00\x00"
            if self.config.randomize_seed:
                self._seed_counter += 1
                base = int.from_bytes(self.config.seed, "big") + self._seed_counter
                self._current_seed = (base & 0xFFFFFFFF).to_bytes(4, "big")
            return self._positive(req[0]) + bytes([level]) + self._current_seed
        # sendKey (even level)
        key = req[2:]
        expected = compute_key(
            self.config.security_algorithm,
            self._current_seed,
            level=level - 1,
            params=self.config.security_params,
        )
        if key == expected:
            self.unlocked = True
            return self._positive(req[0]) + bytes([level])
        return self._nrc(req[0], C.NRC.INVALID_KEY)

    # ---- 0x28 / 0x85 -------------------------------------------------- #
    def _h_comm_control(self, req: bytes) -> bytes:
        return self._positive(req[0]) + bytes([req[1]])

    def _h_clear_dtc(self, req: bytes) -> bytes:
        # 0x14 <groupOfDTC:3> -> 0x54
        return self._positive(req[0])

    def _h_dtc(self, req: bytes) -> bytes:
        return self._positive(req[0]) + bytes([req[1]])

    # ---- 0x3E --------------------------------------------------------- #
    def _h_tester_present(self, req: bytes) -> Optional[bytes]:
        subfunction = req[1] if len(req) > 1 else 0
        if subfunction & 0x80:  # suppressPositiveResponse
            return None
        return self._positive(req[0]) + bytes([subfunction & 0x7F])

    # ---- 0x22 / 0x2E -------------------------------------------------- #
    def _h_read_did(self, req: bytes) -> bytes:
        did = (req[1] << 8) | req[2]
        data = self.identification.get(did)
        if data is None:
            return self._nrc(req[0], C.NRC.REQUEST_OUT_OF_RANGE)
        return self._positive(req[0]) + bytes([req[1], req[2]]) + data

    def _h_write_did(self, req: bytes) -> bytes:
        did = (req[1] << 8) | req[2]
        self.identification[did] = req[3:]
        return self._positive(req[0]) + bytes([req[1], req[2]])

    # ---- 0x31 --------------------------------------------------------- #
    def _h_routine(self, req: bytes) -> bytes:
        control = req[1]
        routine_id = (req[2] << 8) | req[3]
        data = req[4:]
        if control != int(C.RoutineControlType.START_ROUTINE):
            # We only model startRoutine; report OK for stop/results.
            return self._positive(req[0]) + bytes([control, req[2], req[3], 0x00])

        rt = self.profile.routines
        if routine_id == rt.erase_memory:
            return self._routine_erase(req, data)
        if routine_id == rt.check_memory:
            return self._routine_check_memory(req, data)
        if routine_id == rt.check_programming_dependencies:
            return self._positive(req[0]) + bytes([control, req[2], req[3], 0x00])
        # Unknown routine: acknowledge positively with status 0.
        return self._positive(req[0]) + bytes([control, req[2], req[3], 0x00])

    def _routine_erase(self, req: bytes, data: bytes) -> bytes:
        if self.config.require_security_for_flash and not self.unlocked:
            return self._nrc(req[0], C.NRC.SECURITY_ACCESS_DENIED)
        address, size = self._parse_addr_size(data)
        if address is None:
            if not data:
                # No argument -> whole-flash erase (matches erase_argument "none").
                self._emit_pending(req[0])
                self._mem[:] = b"\xff" * len(self._mem)
                self.erased_regions.append((self._base, len(self._mem)))
                return self._positive(req[0]) + bytes([req[1], req[2], req[3], 0x00])
            # block_id form: a single byte index into the memory map.
            if data[0] < len(self.profile.memory_map):
                region = self.profile.memory_map[data[0]]
                address, size = region.start, region.size
            else:
                return self._nrc(req[0], C.NRC.REQUEST_OUT_OF_RANGE)
        self._emit_pending(req[0])
        self._fill_memory(address, size, 0xFF)
        self.erased_regions.append((address, size))
        return self._positive(req[0]) + bytes([req[1], req[2], req[3], 0x00])

    def _routine_check_memory(self, req: bytes, data: bytes) -> bytes:
        # ALFID + address(4) + size(4) + expected checksum(4)
        address, size = self._parse_addr_size(data)
        if address is None or len(data) < 1 + 4 + 4 + 4:
            return self._nrc(req[0], C.NRC.INCORRECT_MESSAGE_LENGTH_OR_INVALID_FORMAT)
        expected = int.from_bytes(data[9:13], "big")
        self._emit_pending(req[0])
        actual = _cs.crc32(self.read_memory(address, size))
        status = 0x00 if actual == expected else 0x01
        if status == 0x00:
            self.verified_regions.append((address, size))
        return self._positive(req[0]) + bytes([req[1], req[2], req[3], status])

    # ---- 0x34 / 0x36 / 0x37 ------------------------------------------ #
    def _h_request_download(self, req: bytes) -> bytes:
        if self.session != int(self.profile.programming_session):
            return self._nrc(req[0], C.NRC.SERVICE_NOT_SUPPORTED_IN_ACTIVE_SESSION)
        if self.config.require_security_for_flash and not self.unlocked:
            return self._nrc(req[0], C.NRC.SECURITY_ACCESS_DENIED)
        # req = 0x34 <dataFormat> <ALFID> <addr...> <size...>
        alfid = req[2]
        addr_len = alfid & 0x0F
        size_len = (alfid >> 4) & 0x0F
        address = int.from_bytes(req[3 : 3 + addr_len], "big")
        size = int.from_bytes(req[3 + addr_len : 3 + addr_len + size_len], "big")
        self._download = _DownloadState(address=address, size=size, next_bsc=1, active=True)
        mbl = self.config.max_block_length
        # 0x74 <lengthFormatIdentifier=0x20> <maxBlockLength:2>
        return self._positive(req[0]) + bytes([0x20]) + mbl.to_bytes(2, "big")

    def _h_transfer_data(self, req: bytes) -> bytes:
        if not self._download.active:
            return self._nrc(req[0], C.NRC.REQUEST_SEQUENCE_ERROR)
        bsc = req[1]
        payload = req[2:]
        if bsc != self._download.next_bsc:
            return self._nrc(req[0], C.NRC.WRONG_BLOCK_SEQUENCE_COUNTER)
        addr = self._download.address + self._download.received
        if self._download.received + len(payload) > self._download.size:
            return self._nrc(req[0], C.NRC.REQUEST_OUT_OF_RANGE)
        self._write_memory(addr, payload)
        self._download.received += len(payload)
        nxt = (bsc + 1) & 0xFF
        if nxt == 0:
            nxt = 1
        self._download.next_bsc = nxt
        return self._positive(req[0]) + bytes([bsc])

    def _h_transfer_exit(self, req: bytes) -> bytes:
        self._download.active = False
        return self._positive(req[0])

    # ---- 0x23 --------------------------------------------------------- #
    def _h_read_memory(self, req: bytes) -> bytes:
        alfid = req[1]
        addr_len = alfid & 0x0F
        size_len = (alfid >> 4) & 0x0F
        address = int.from_bytes(req[2 : 2 + addr_len], "big")
        size = int.from_bytes(req[2 + addr_len : 2 + addr_len + size_len], "big")
        return self._positive(req[0]) + self.read_memory(address, size)

    # ---- misc --------------------------------------------------------- #
    @staticmethod
    def _parse_addr_size(data: bytes):
        """Parse an ALFID-prefixed address+size argument.

        Returns ``(address, size)`` or ``(None, None)`` if the data is not in
        that form.
        """

        if len(data) < 1:
            return None, None
        alfid = data[0]
        addr_len = alfid & 0x0F
        size_len = (alfid >> 4) & 0x0F
        if addr_len == 0 or size_len == 0 or len(data) < 1 + addr_len + size_len:
            return None, None
        address = int.from_bytes(data[1 : 1 + addr_len], "big")
        size = int.from_bytes(data[1 + addr_len : 1 + addr_len + size_len], "big")
        return address, size

    def _default_identification(self) -> Dict[int, bytes]:
        return {
            int(C.DataIdentifier.VW_SPARE_PART_NUMBER): b"03L906022RF",
            int(C.DataIdentifier.VEHICLE_MANUFACTURER_ECU_SW_NUMBER): b"8V0906264",
            int(C.DataIdentifier.SYSTEM_SUPPLIER_ECU_HW_NUMBER): b"5WK8 6789",
            int(C.DataIdentifier.SYSTEM_SUPPLIER_ECU_SW_NUMBER): b"1037551234",
            int(C.DataIdentifier.VIN): b"WVWZZZ1KZAW000001",
            int(C.DataIdentifier.BOOT_SOFTWARE_IDENTIFICATION): b"MED17.7.5-BOOT-01",
            int(C.DataIdentifier.ACTIVE_DIAGNOSTIC_SESSION): bytes([self.session]),
        }
