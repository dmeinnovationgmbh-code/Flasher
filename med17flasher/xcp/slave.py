"""An in-process virtual XCP slave for tests and demos.

Answers the subset of XCP the client implements (connect/status, memory read via
SET_MTA+UPLOAD and SHORT_UPLOAD, and a single-list DAQ that streams the
configured entries as DTO frames). Runs on a :class:`VirtualCanBus` endpoint so
the client can drive it entirely in-process.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from ..core.can_backends import CanBus, CanFrame
from ..logging_setup import get_logger
from . import const as X

log = get_logger("xcp.slave")

Provider = Callable[[int, int], Optional[bytes]]


class VirtualXcpSlave:
    def __init__(
        self,
        bus: CanBus,
        cro_id: int = 0x7E0,
        dto_id: int = 0x7E1,
        *,
        byte_order: str = "little",
        max_cto: int = 8,
        max_dto: int = 8,
        provider: Optional[Provider] = None,
        daq_period: float = 0.01,
    ) -> None:
        self.bus = bus
        self.cro_id = cro_id
        self.dto_id = dto_id
        self.byte_order = byte_order
        self.max_cto = max_cto
        self.max_dto = max_dto
        self.provider = provider
        self.daq_period = daq_period

        self._mem: Dict[int, int] = {}
        self._mta = 0
        # DAQ config: list of (address, size, ext) entries for daq 0 / odt 0.
        self._entries: List[Tuple[int, int, int]] = []
        self._ptr = (0, 0, 0)
        self._daq_selected = False
        self._daq_running = threading.Event()

        self._rx = threading.Thread(target=self._serve, name="xcp-slave", daemon=True)
        self._daq_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ #
    # Memory
    # ------------------------------------------------------------------ #
    def set_bytes(self, address: int, data: bytes) -> None:
        for i, b in enumerate(bytes(data)):
            self._mem[address + i] = b

    def _read(self, address: int, size: int) -> bytes:
        if self.provider is not None:
            got = self.provider(address, size)
            if got is not None:
                return bytes(got)[:size].ljust(size, b"\x00")
        return bytes(self._mem.get(address + i, 0) for i in range(size))

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> "VirtualXcpSlave":
        self._rx.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._daq_running.clear()
        self._rx.join(timeout=1.0)
        if self._daq_thread:
            self._daq_thread.join(timeout=1.0)

    def __enter__(self) -> "VirtualXcpSlave":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ------------------------------------------------------------------ #
    # Serving
    # ------------------------------------------------------------------ #
    def _send(self, payload: bytes) -> None:
        self.bus.send(CanFrame(self.dto_id, bytes(payload)[:8]))

    def _res(self, *rest: int) -> None:
        self._send(bytes([X.PID_RES, *rest]))

    def _err(self, code: int) -> None:
        self._send(bytes([X.PID_ERR, code]))

    def _u16(self, v: int) -> bytes:
        return int(v).to_bytes(2, self.byte_order)

    def _serve(self) -> None:
        while not self._stop.is_set():
            frame = self.bus.recv(timeout=0.1)
            if frame is None or frame.arbitration_id != self.cro_id:
                continue
            try:
                self._handle(frame.data)
            except Exception:  # noqa: BLE001 - a slave must never die on bad input
                log.exception("XCP slave error")
                self._err(X.ERR_GENERIC)

    def _handle(self, data: bytes) -> None:
        if not data:
            return
        cmd = data[0]
        if cmd == X.CONNECT:
            cmb = 0x00 if self.byte_order == "little" else X.CMB_BYTE_ORDER_MOTOROLA
            self._res(0x05, cmb, self.max_cto, *self._u16(self.max_dto), 0x01, 0x01)
        elif cmd == X.DISCONNECT:
            self._daq_running.clear()
            self._res()
        elif cmd == X.GET_STATUS:
            self._res(0x00, 0x00, 0x00, *self._u16(0))
        elif cmd == X.SYNCH:
            self._err(X.ERR_CMD_SYNCH)
        elif cmd == X.SET_MTA:
            self._mta = int.from_bytes(data[4:8], self.byte_order)
            self._res()
        elif cmd == X.UPLOAD:
            n = data[1]
            chunk = self._read(self._mta, n)
            self._mta += n
            self._send(bytes([X.PID_RES]) + chunk)
        elif cmd == X.SHORT_UPLOAD:
            size = data[1]
            addr = int.from_bytes(data[4:8], self.byte_order)
            self._send(bytes([X.PID_RES]) + self._read(addr, size))
        elif cmd == X.FREE_DAQ:
            self._entries.clear()
            self._daq_selected = False
            self._daq_running.clear()
            self._res()
        elif cmd in (X.ALLOC_DAQ, X.ALLOC_ODT, X.ALLOC_ODT_ENTRY):
            self._res()
        elif cmd == X.SET_DAQ_PTR:
            daq = int.from_bytes(data[2:4], self.byte_order)
            self._ptr = (daq, data[4], data[5])
            self._res()
        elif cmd == X.WRITE_DAQ:
            size = data[2]
            ext = data[3]
            addr = int.from_bytes(data[4:8], self.byte_order)
            _, _, entry = self._ptr
            if entry >= len(self._entries):
                self._entries.extend([(0, 0, 0)] * (entry + 1 - len(self._entries)))
            self._entries[entry] = (addr, size, ext)
            self._res()
        elif cmd == X.SET_DAQ_LIST_MODE:
            self._res()
        elif cmd == X.START_STOP_DAQ_LIST:
            mode = data[1]
            if mode in (X.DAQ_SELECT, X.DAQ_START):
                self._daq_selected = True
            self._res(0x00)  # FIRST_PID = 0
        elif cmd == X.START_STOP_SYNCH:
            mode = data[1]
            if mode == X.DAQ_START_SELECTED and self._daq_selected:
                self._start_daq()
            elif mode == X.DAQ_STOP_ALL:
                self._daq_running.clear()
            self._res()
        else:
            self._err(X.ERR_CMD_UNKNOWN)

    # ------------------------------------------------------------------ #
    # DAQ streaming
    # ------------------------------------------------------------------ #
    def _start_daq(self) -> None:
        if self._daq_running.is_set():
            return
        self._daq_running.set()
        self._daq_thread = threading.Thread(target=self._daq_loop,
                                            name="xcp-daq", daemon=True)
        self._daq_thread.start()

    def _daq_loop(self) -> None:
        while self._daq_running.is_set() and not self._stop.is_set():
            payload = bytearray([0x00])  # PID / ODT number 0
            for addr, size, _ext in self._entries:
                payload += self._read(addr, size)
            self._send(bytes(payload))
            time.sleep(self.daq_period)
