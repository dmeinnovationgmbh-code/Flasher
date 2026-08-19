"""A UDS (ISO 14229-1) client running on top of :class:`IsoTpLayer`.

The client only implements what a MED17.7.5 flash and diagnostic workflow
needs, but it does so completely: negative-response decoding, the mandatory
``responsePending`` (NRC 0x78) wait loop, suppress-positive-response handling,
and a background TesterPresent keep-alive.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..exceptions import (
    NegativeResponseError,
    UdsTimeoutError,
    UnexpectedResponseError,
)
from ..logging_setup import get_logger
from .isotp import IsoTpLayer
from . import uds_const as C

log = get_logger("core.uds")


@dataclass
class UdsTiming:
    """UDS session timing parameters (seconds)."""

    p2: float = 1.0  # normal response time
    p2_star: float = 5.0  # extended time granted after a 0x78 response
    max_pending: int = 30  # cap on consecutive 0x78 responses before giving up


class UdsClient:
    """Send UDS requests and decode the responses."""

    def __init__(self, isotp: IsoTpLayer, timing: Optional[UdsTiming] = None) -> None:
        self.tp = isotp
        self.timing = timing or UdsTiming()
        self._tester_present_stop: Optional[threading.Event] = None
        self._tester_present_thread: Optional[threading.Thread] = None
        self._io_lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # Core request/response
    # ------------------------------------------------------------------ #
    def request(
        self,
        payload: bytes,
        *,
        expect_response: bool = True,
        suppress_positive: bool = False,
        p2: Optional[float] = None,
        p2_star: Optional[float] = None,
    ) -> bytes:
        """Send a raw UDS request and return the raw positive response.

        ``payload[0]`` is the service id. The returned bytes include the
        response SID. Negative responses raise :class:`NegativeResponseError`;
        ``responsePending`` (0x78) is transparently awaited.
        """

        payload = bytes(payload)
        sid = payload[0]
        p2 = p2 if p2 is not None else self.timing.p2
        p2_star = p2_star if p2_star is not None else self.timing.p2_star

        with self._io_lock:
            self.tp.bus.flush_rx()
            self.tp.send(payload)

            if not expect_response or suppress_positive:
                # Still watch briefly for a negative response, which a server
                # may send even when a positive one was suppressed.
                try:
                    response = self.tp.recv(timeout=min(p2, 0.2))
                except UdsTimeoutError:
                    return b""
                except Exception:
                    return b""
                return self._interpret(sid, response, allow_empty=True)

            pending = 0
            timeout = p2
            while True:
                try:
                    response = self.tp.recv(timeout=timeout)
                except Exception as exc:  # isotp timeout / framing
                    raise UdsTimeoutError(
                        f"no response to service 0x{sid:02X}: {exc}"
                    ) from exc

                if self._is_response_pending(sid, response):
                    pending += 1
                    if pending > self.timing.max_pending:
                        raise UdsTimeoutError(
                            f"ECU kept responding 'pending' (0x78) more than "
                            f"{self.timing.max_pending} times for service 0x{sid:02X}"
                        )
                    log.debug("responsePending (%d) for service 0x%02X", pending, sid)
                    timeout = p2_star
                    continue

                return self._interpret(sid, response)

    def _interpret(self, sid: int, response: bytes, allow_empty: bool = False) -> bytes:
        if not response:
            if allow_empty:
                return b""
            raise UnexpectedResponseError("empty UDS response")

        if response[0] == C.NEGATIVE_RESPONSE_SID:
            # 0x7F <requestSid> <nrc>
            req_sid = response[1] if len(response) > 1 else sid
            nrc = response[2] if len(response) > 2 else 0
            raise NegativeResponseError(req_sid, nrc)

        expected = sid + C.POSITIVE_RESPONSE_OFFSET
        if response[0] != expected:
            raise UnexpectedResponseError(
                f"expected positive response 0x{expected:02X} to service "
                f"0x{sid:02X}, got 0x{response[0]:02X} ({response.hex(' ')})"
            )
        return response

    @staticmethod
    def _is_response_pending(sid: int, response: bytes) -> bool:
        return (
            len(response) >= 3
            and response[0] == C.NEGATIVE_RESPONSE_SID
            and response[1] == sid
            and response[2] == C.RESPONSE_PENDING
        )

    # ------------------------------------------------------------------ #
    # 0x10 DiagnosticSessionControl
    # ------------------------------------------------------------------ #
    def diagnostic_session_control(self, session: int) -> bytes:
        resp = self.request(bytes([C.Service.DIAGNOSTIC_SESSION_CONTROL, session]))
        return resp[2:]  # sessionParameterRecord (P2/P2* timing, if present)

    def enter_default_session(self) -> None:
        self.diagnostic_session_control(C.Session.DEFAULT)

    def enter_extended_session(self) -> None:
        self.diagnostic_session_control(C.Session.EXTENDED_DIAGNOSTIC)

    def enter_programming_session(self) -> None:
        self.diagnostic_session_control(C.Session.PROGRAMMING)

    # ------------------------------------------------------------------ #
    # 0x11 ECUReset
    # ------------------------------------------------------------------ #
    def ecu_reset(self, reset_type: int = C.ResetType.HARD_RESET) -> None:
        self.request(bytes([C.Service.ECU_RESET, reset_type]))

    # ------------------------------------------------------------------ #
    # 0x27 SecurityAccess
    # ------------------------------------------------------------------ #
    def request_seed(self, level: int) -> bytes:
        """Return the seed for the given requestSeed sub-function ``level``."""

        resp = self.request(bytes([C.Service.SECURITY_ACCESS, level]))
        # 0x67 <level> <seed...>
        return resp[2:]

    def send_key(self, level: int, key: bytes) -> bytes:
        resp = self.request(bytes([C.Service.SECURITY_ACCESS, level]) + bytes(key))
        return resp[2:]

    # ------------------------------------------------------------------ #
    # 0x28 CommunicationControl
    # ------------------------------------------------------------------ #
    def communication_control(
        self,
        control_type: int,
        communication_type: int = C.CommunicationType.NORMAL_AND_NM,
    ) -> None:
        self.request(
            bytes([C.Service.COMMUNICATION_CONTROL, control_type, communication_type])
        )

    def disable_normal_communication(self) -> None:
        self.communication_control(C.CommunicationControlType.DISABLE_RX_AND_TX)

    def enable_normal_communication(self) -> None:
        self.communication_control(C.CommunicationControlType.ENABLE_RX_AND_TX)

    # ------------------------------------------------------------------ #
    # 0x85 ControlDTCSetting
    # ------------------------------------------------------------------ #
    def control_dtc_setting(self, setting_type: int) -> None:
        self.request(bytes([C.Service.CONTROL_DTC_SETTING, setting_type]))

    def disable_dtc_setting(self) -> None:
        self.control_dtc_setting(C.DtcSettingType.OFF)

    def enable_dtc_setting(self) -> None:
        self.control_dtc_setting(C.DtcSettingType.ON)

    # ------------------------------------------------------------------ #
    # 0x22 / 0x2E ReadDataByIdentifier / WriteDataByIdentifier
    # ------------------------------------------------------------------ #
    def read_data_by_identifier(self, did: int) -> bytes:
        resp = self.request(
            bytes([C.Service.READ_DATA_BY_IDENTIFIER, (did >> 8) & 0xFF, did & 0xFF])
        )
        # 0x62 <did hi> <did lo> <data...>
        return resp[3:]

    def write_data_by_identifier(self, did: int, data: bytes) -> None:
        self.request(
            bytes([C.Service.WRITE_DATA_BY_IDENTIFIER, (did >> 8) & 0xFF, did & 0xFF])
            + bytes(data)
        )

    # ------------------------------------------------------------------ #
    # 0x31 RoutineControl
    # ------------------------------------------------------------------ #
    def routine_control(
        self,
        control_type: int,
        routine_id: int,
        data: bytes = b"",
    ) -> bytes:
        resp = self.request(
            bytes(
                [
                    C.Service.ROUTINE_CONTROL,
                    control_type,
                    (routine_id >> 8) & 0xFF,
                    routine_id & 0xFF,
                ]
            )
            + bytes(data)
        )
        # 0x71 <ctrl> <rid hi> <rid lo> <routineInfo + statusRecord...>
        return resp[4:]

    def start_routine(self, routine_id: int, data: bytes = b"") -> bytes:
        return self.routine_control(C.RoutineControlType.START_ROUTINE, routine_id, data)

    def stop_routine(self, routine_id: int, data: bytes = b"") -> bytes:
        return self.routine_control(C.RoutineControlType.STOP_ROUTINE, routine_id, data)

    def routine_results(self, routine_id: int, data: bytes = b"") -> bytes:
        return self.routine_control(
            C.RoutineControlType.REQUEST_ROUTINE_RESULTS, routine_id, data
        )

    # ------------------------------------------------------------------ #
    # 0x34 / 0x36 / 0x37 Download
    # ------------------------------------------------------------------ #
    def request_download(
        self,
        address: int,
        size: int,
        *,
        data_format: int = 0x00,
        address_length_format: Optional[int] = None,
    ) -> int:
        """RequestDownload (0x34). Returns the negotiated max block length.

        ``address_length_format`` is the ALFID byte; when omitted it is derived
        from 4-byte address + 4-byte size (0x44), which is what MED17 expects.
        """

        addr_bytes, size_bytes, alfid = _encode_addr_size(
            address, size, address_length_format
        )
        resp = self.request(
            bytes([C.Service.REQUEST_DOWNLOAD, data_format, alfid])
            + addr_bytes
            + size_bytes
        )
        # 0x74 <lengthFormatIdentifier> <maxNumberOfBlockLength...>
        length_format = (resp[1] >> 4) & 0x0F if len(resp) > 1 else 2
        max_block = int.from_bytes(resp[2 : 2 + length_format], "big")
        return max_block

    def request_upload(
        self,
        address: int,
        size: int,
        *,
        data_format: int = 0x00,
        address_length_format: Optional[int] = None,
    ) -> int:
        addr_bytes, size_bytes, alfid = _encode_addr_size(
            address, size, address_length_format
        )
        resp = self.request(
            bytes([C.Service.REQUEST_UPLOAD, data_format, alfid])
            + addr_bytes
            + size_bytes
        )
        length_format = (resp[1] >> 4) & 0x0F if len(resp) > 1 else 2
        return int.from_bytes(resp[2 : 2 + length_format], "big")

    def transfer_data(self, block_sequence_counter: int, data: bytes = b"") -> bytes:
        resp = self.request(
            bytes([C.Service.TRANSFER_DATA, block_sequence_counter & 0xFF]) + bytes(data)
        )
        # 0x76 <blockSequenceCounter> <transferResponseParameterRecord...>
        return resp[2:]

    def request_transfer_exit(self, data: bytes = b"") -> bytes:
        resp = self.request(bytes([C.Service.REQUEST_TRANSFER_EXIT]) + bytes(data))
        return resp[1:]

    # ------------------------------------------------------------------ #
    # 0x23 / 0x3D ReadMemoryByAddress / WriteMemoryByAddress
    # ------------------------------------------------------------------ #
    def read_memory_by_address(
        self, address: int, size: int, *, address_length_format: Optional[int] = None
    ) -> bytes:
        addr_bytes, size_bytes, alfid = _encode_addr_size(
            address, size, address_length_format
        )
        resp = self.request(
            bytes([C.Service.READ_MEMORY_BY_ADDRESS, alfid]) + addr_bytes + size_bytes
        )
        return resp[1:]

    def write_memory_by_address(
        self, address: int, data: bytes, *, address_length_format: Optional[int] = None
    ) -> None:
        addr_bytes, size_bytes, alfid = _encode_addr_size(
            address, len(data), address_length_format
        )
        self.request(
            bytes([C.Service.WRITE_MEMORY_BY_ADDRESS, alfid])
            + addr_bytes
            + size_bytes
            + bytes(data)
        )

    # ------------------------------------------------------------------ #
    # 0x3E TesterPresent + keep-alive
    # ------------------------------------------------------------------ #
    def tester_present(self, suppress_response: bool = True) -> None:
        subfunction = 0x80 if suppress_response else 0x00
        self.request(
            bytes([C.Service.TESTER_PRESENT, subfunction]),
            suppress_positive=suppress_response,
        )

    def start_tester_present(self, period: float = 2.0) -> None:
        """Spawn a background thread sending TesterPresent every ``period`` s."""

        if self._tester_present_thread and self._tester_present_thread.is_alive():
            return
        stop = threading.Event()

        def _loop() -> None:
            while not stop.wait(period):
                try:
                    self.tester_present(suppress_response=True)
                except Exception as exc:  # keep-alive must never crash the flash
                    log.debug("tester-present keep-alive failed: %s", exc)

        thread = threading.Thread(target=_loop, name="uds-tester-present", daemon=True)
        self._tester_present_stop = stop
        self._tester_present_thread = thread
        thread.start()

    def stop_tester_present(self) -> None:
        if self._tester_present_stop:
            self._tester_present_stop.set()
        if self._tester_present_thread:
            self._tester_present_thread.join(timeout=1.0)
        self._tester_present_stop = None
        self._tester_present_thread = None

    # ------------------------------------------------------------------ #
    # High level convenience
    # ------------------------------------------------------------------ #
    def read_did_text(self, did: int, encoding: str = "latin-1") -> str:
        raw = self.read_data_by_identifier(did)
        return raw.decode(encoding, errors="replace").strip("\x00 ")

    def identify(self, dids: Optional[List[int]] = None) -> "List[Tuple[int, bytes]]":
        """Read a set of identification DIDs, skipping unsupported ones."""

        if dids is None:
            dids = [
                C.DataIdentifier.VW_SPARE_PART_NUMBER,
                C.DataIdentifier.VEHICLE_MANUFACTURER_ECU_SW_NUMBER,
                C.DataIdentifier.SYSTEM_SUPPLIER_ECU_HW_NUMBER,
                C.DataIdentifier.SYSTEM_SUPPLIER_ECU_SW_NUMBER,
                C.DataIdentifier.VIN,
                C.DataIdentifier.BOOT_SOFTWARE_IDENTIFICATION,
            ]
        out: List[Tuple[int, bytes]] = []
        for did in dids:
            try:
                out.append((int(did), self.read_data_by_identifier(int(did))))
            except NegativeResponseError:
                continue
            except UdsTimeoutError:
                continue
        return out


def _encode_addr_size(
    address: int, size: int, address_length_format: Optional[int]
) -> Tuple[bytes, bytes, int]:
    """Encode an address/size pair and build the ALFID byte.

    The ALFID high nibble is the number of size bytes, the low nibble the
    number of address bytes. MED17 uses 4/4 (0x44).
    """

    if address_length_format is not None:
        addr_len = address_length_format & 0x0F
        size_len = (address_length_format >> 4) & 0x0F
        alfid = address_length_format
    else:
        addr_len = 4
        size_len = 4
        alfid = 0x44
    addr_bytes = address.to_bytes(addr_len, "big")
    size_bytes = size.to_bytes(size_len, "big")
    return addr_bytes, size_bytes, alfid
