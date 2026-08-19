"""XCP-on-CAN transport.

XCP on classic CAN puts one XCP packet directly into one CAN frame (no ISO-TP):
the master transmits command packets on the **CRO** id and the slave answers on
the **DTO** id. Responses start with ``0xFF`` (RES) or ``0xFE`` (ERR); ``0xFD``
(EV) / ``0xFC`` (SERV) are asynchronous notifications; anything below ``0xFC`` on
the DTO id is DAQ data (an ODT/PID number).
"""

from __future__ import annotations

import time
from typing import Optional

from ..core.can_backends import CanBus, CanFrame
from ..exceptions import XcpTimeoutError
from ..logging_setup import get_logger
from . import const as X

log = get_logger("xcp.transport")


class XcpOnCan:
    """Frame the XCP packet layer onto a :class:`CanBus`."""

    def __init__(
        self,
        bus: CanBus,
        cro_id: int = 0x7E0,
        dto_id: int = 0x7E1,
        *,
        is_extended_id: bool = False,
        timeout: float = 1.0,
        pad_to: Optional[int] = None,
    ) -> None:
        self.bus = bus
        self.cro_id = cro_id
        self.dto_id = dto_id
        self.is_extended_id = is_extended_id
        self.timeout = timeout
        self.pad_to = pad_to  # e.g. 8 to always send full-length CTO frames

    # ------------------------------------------------------------------ #
    def send_command(self, packet: bytes) -> None:
        data = bytes(packet)
        if self.pad_to and len(data) < self.pad_to:
            data = data + b"\x00" * (self.pad_to - len(data))
        if len(data) > 8:
            raise ValueError("XCP-on-CAN CTO packet exceeds 8 bytes (classic CAN)")
        log.debug("CRO %03X <- %s", self.cro_id, data.hex(" "))
        self.bus.send(CanFrame(self.cro_id, data, self.is_extended_id))

    def _recv_dto(self, timeout: float) -> Optional[bytes]:
        """Return the payload of the next frame on the DTO id, or None."""

        end = time.monotonic() + max(0.0, timeout)
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return None
            frame = self.bus.recv(timeout=remaining)
            if frame is None:
                return None
            if frame.arbitration_id != self.dto_id:
                continue  # not ours (bus may carry other traffic)
            log.debug("DTO %03X -> %s", self.dto_id, frame.data.hex(" "))
            return frame.data

    def recv_response(self, timeout: Optional[float] = None) -> bytes:
        """Wait for the next command response (RES or ERR), skipping EV/SERV/DAQ.

        Returns the full packet (including the leading PID byte). Raises
        :class:`XcpTimeoutError` if nothing arrives in time.
        """

        t = self.timeout if timeout is None else timeout
        end = time.monotonic() + t
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise XcpTimeoutError("no XCP response within %.2fs" % t)
            data = self._recv_dto(remaining)
            if data is None:
                raise XcpTimeoutError("no XCP response within %.2fs" % t)
            if not data:
                continue
            pid = data[0]
            if pid in (X.PID_RES, X.PID_ERR):
                return data
            if pid in (X.PID_EV, X.PID_SERV):
                log.debug("skipping async XCP packet pid=0x%02X", pid)
                continue
            # A DTO/DAQ frame arrived while we awaited a CTO response; ignore it.
            continue

    def collect_daq(self, timeout: float) -> Optional[bytes]:
        """Return the next DAQ/DTO frame payload (PID < 0xFC), or None."""

        end = time.monotonic() + max(0.0, timeout)
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return None
            data = self._recv_dto(remaining)
            if data is None:
                return None
            if data and data[0] < X.PID_SERV:
                return data
            # RES/ERR/EV/SERV are not DAQ data; keep waiting.

    def flush(self) -> None:
        self.bus.flush_rx()
