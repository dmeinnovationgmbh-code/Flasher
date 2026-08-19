"""A small, dependency-free XCP master (client).

Implements the commands needed to measure a MED17-class ECU over CAN:
connection/identification, memory read (``SHORT_UPLOAD`` and ``SET_MTA`` +
``UPLOAD``), and synchronous data acquisition (DAQ) list configuration +
start/stop so live values can be streamed.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..exceptions import XcpNegativeResponseError
from ..logging_setup import get_logger
from . import const as X
from .transport import XcpOnCan

log = get_logger("xcp.client")


class XcpClient:
    def __init__(self, transport: XcpOnCan) -> None:
        self.tp = transport
        self.connected = False
        self.byte_order = "little"      # refined by CONNECT
        self.max_cto = 8
        self.max_dto = 8
        self.resource = 0

    # ------------------------------------------------------------------ #
    # Low-level command helper
    # ------------------------------------------------------------------ #
    def _command(self, code: int, payload: bytes = b"", *,
                 timeout: Optional[float] = None) -> bytes:
        self.tp.send_command(bytes([code]) + bytes(payload))
        resp = self.tp.recv_response(timeout)
        if resp[0] == X.PID_ERR:
            err = resp[1] if len(resp) > 1 else 0xFF
            raise XcpNegativeResponseError(code, err)
        return resp  # resp[0] == PID_RES

    def _addr(self, address: int) -> bytes:
        return int(address).to_bytes(4, self.byte_order)

    def _u16(self, value: int) -> bytes:
        return int(value).to_bytes(2, self.byte_order)

    # ------------------------------------------------------------------ #
    # Connection / status
    # ------------------------------------------------------------------ #
    def connect(self, mode: int = 0) -> dict:
        resp = self._command(X.CONNECT, bytes([mode]))
        self.resource = resp[1] if len(resp) > 1 else 0
        cmb = resp[2] if len(resp) > 2 else 0
        self.byte_order = "big" if (cmb & X.CMB_BYTE_ORDER_MOTOROLA) else "little"
        self.max_cto = resp[3] if len(resp) > 3 else 8
        if len(resp) >= 6:
            self.max_dto = int.from_bytes(resp[4:6], self.byte_order)
        self.connected = True
        info = {
            "resource": self.resource,
            "byteOrder": self.byte_order,
            "maxCto": self.max_cto,
            "maxDto": self.max_dto,
            "protocolLayer": resp[6] if len(resp) > 6 else None,
            "transportLayer": resp[7] if len(resp) > 7 else None,
        }
        log.info("XCP connected: %s", info)
        return info

    def disconnect(self) -> None:
        try:
            self._command(X.DISCONNECT)
        finally:
            self.connected = False

    def get_status(self) -> dict:
        resp = self._command(X.GET_STATUS)
        return {
            "sessionStatus": resp[1] if len(resp) > 1 else 0,
            "protectionStatus": resp[2] if len(resp) > 2 else 0,
            "sessionConfigId": int.from_bytes(resp[4:6], self.byte_order)
            if len(resp) >= 6 else 0,
        }

    def synch(self) -> None:
        # SYNCH always answers with ERR CMD_SYNCH by design; swallow it.
        try:
            self._command(X.SYNCH)
        except XcpNegativeResponseError:
            pass

    # ------------------------------------------------------------------ #
    # Memory read
    # ------------------------------------------------------------------ #
    def set_mta(self, address: int, ext: int = 0) -> None:
        self._command(X.SET_MTA, bytes([0, 0, ext]) + self._addr(address))

    def upload(self, size: int) -> bytes:
        out = bytearray()
        chunk = max(1, self.max_cto - 1)
        while len(out) < size:
            n = min(size - len(out), chunk)
            resp = self._command(X.UPLOAD, bytes([n]))
            out += resp[1:1 + n]
        return bytes(out)

    def short_upload(self, address: int, size: int, ext: int = 0) -> bytes:
        if size > self.max_cto - 1:
            raise ValueError(
                f"SHORT_UPLOAD size {size} exceeds MAX_CTO-1 ({self.max_cto - 1}); "
                "use read()"
            )
        resp = self._command(X.SHORT_UPLOAD, bytes([size, 0, ext]) + self._addr(address))
        return resp[1:1 + size]

    def read(self, address: int, size: int, ext: int = 0) -> bytes:
        """Read ``size`` bytes, picking SHORT_UPLOAD or SET_MTA+UPLOAD."""

        if size <= self.max_cto - 1:
            return self.short_upload(address, size, ext)
        self.set_mta(address, ext)
        return self.upload(size)

    # ------------------------------------------------------------------ #
    # DAQ (synchronous data acquisition)
    # ------------------------------------------------------------------ #
    def get_daq_processor_info(self) -> dict:
        resp = self._command(X.GET_DAQ_PROCESSOR_INFO)
        return {
            "properties": resp[1] if len(resp) > 1 else 0,
            "maxDaq": int.from_bytes(resp[2:4], self.byte_order) if len(resp) >= 4 else 0,
            "maxEventChannel": int.from_bytes(resp[4:6], self.byte_order)
            if len(resp) >= 6 else 0,
            "minDaq": resp[6] if len(resp) > 6 else 0,
        }

    def free_daq(self) -> None:
        self._command(X.FREE_DAQ)

    def alloc_daq(self, daq_count: int) -> None:
        self._command(X.ALLOC_DAQ, bytes([0]) + self._u16(daq_count))

    def alloc_odt(self, daq: int, odt_count: int) -> None:
        self._command(X.ALLOC_ODT, bytes([0]) + self._u16(daq) + bytes([odt_count]))

    def alloc_odt_entry(self, daq: int, odt: int, entry_count: int) -> None:
        self._command(X.ALLOC_ODT_ENTRY,
                      bytes([0]) + self._u16(daq) + bytes([odt, entry_count]))

    def set_daq_ptr(self, daq: int, odt: int, entry: int) -> None:
        self._command(X.SET_DAQ_PTR, bytes([0]) + self._u16(daq) + bytes([odt, entry]))

    def write_daq(self, bit_offset: int, size: int, ext: int, address: int) -> None:
        self._command(X.WRITE_DAQ,
                      bytes([bit_offset & 0xFF, size, ext]) + self._addr(address))

    def set_daq_list_mode(self, mode: int, daq: int, event: int,
                          prescaler: int = 1, priority: int = 0) -> None:
        self._command(X.SET_DAQ_LIST_MODE,
                      bytes([mode]) + self._u16(daq) + self._u16(event)
                      + bytes([prescaler, priority]))

    def start_stop_daq_list(self, mode: int, daq: int) -> int:
        resp = self._command(X.START_STOP_DAQ_LIST, bytes([mode]) + self._u16(daq))
        return resp[1] if len(resp) > 1 else 0  # FIRST_PID

    def start_stop_synch(self, mode: int) -> None:
        self._command(X.START_STOP_SYNCH, bytes([mode]))

    # ------------------------------------------------------------------ #
    def __enter__(self) -> "XcpClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.connected:
                self.disconnect()
        except Exception:  # noqa: BLE001
            pass
