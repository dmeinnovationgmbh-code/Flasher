"""ISO 15765-2 (ISO-TP) transport layer over classic CAN.

The implementation is symmetric: the same :class:`IsoTpLayer` object is used by
the flasher (tester side) *and* by the ECU simulator, so both directions -
segmented send with flow control, and segmented receive that issues flow
control - are fully implemented.

Supported:

* Single Frame (SF), First Frame (FF), Consecutive Frame (CF), Flow Control (FC)
* Normal 11-bit addressing with distinct TX/RX ids, and optional extended
  addressing (a leading address byte)
* Block Size (BS) and Separation Time (STmin), both when sending (honouring the
  peer's FC) and when receiving (advertising our own)
* The 32-bit "escape" First Frame for messages longer than 4095 bytes
* Configurable frame padding (MED17 ECUs expect 8-byte padded frames)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from ..exceptions import IsoTpError, IsoTpTimeoutError
from ..logging_setup import get_logger
from .can_backends import CanBus, CanFrame

log = get_logger("core.isotp")

# PCI type nibbles (high nibble of the first PCI byte).
_PCI_SF = 0x0
_PCI_FF = 0x1
_PCI_CF = 0x2
_PCI_FC = 0x3

# Flow status values (low nibble of an FC frame's PCI byte).
_FS_CTS = 0x0  # clear to send
_FS_WAIT = 0x1
_FS_OVFLW = 0x2


@dataclass
class IsoTpConfig:
    """Addressing and timing parameters for one ISO-TP channel."""

    tx_id: int  # CAN id the tester transmits on (request)
    rx_id: int  # CAN id the tester listens on (response)
    is_extended_id: bool = False

    # Extended addressing: when set, this address byte is prepended to every
    # frame's data field (and expected on receive). ``None`` = normal addressing.
    address_extension: Optional[int] = None

    # Padding: pad every frame to 8 bytes with this byte. ``None`` disables
    # padding (frames use their natural length). MED17 expects padded frames.
    padding_byte: Optional[int] = 0x55

    # Flow control parameters *we* advertise while receiving.
    block_size: int = 0  # 0 = the sender may send everything at once
    st_min: int = 0  # our requested separation time (raw STmin encoding)

    # Timeouts (seconds).
    n_bs: float = 1.0  # waiting for a Flow Control after our First Frame
    n_cr: float = 1.0  # waiting for the next Consecutive Frame
    timeout: float = 2.0  # overall per-single-frame wait

    def data_length(self) -> int:
        """Usable CAN data bytes after any address-extension byte."""

        return 8 - (1 if self.address_extension is not None else 0)


def _decode_stmin(st_min: int) -> float:
    """Convert a raw STmin byte to seconds."""

    if 0x00 <= st_min <= 0x7F:
        return st_min / 1000.0
    if 0xF1 <= st_min <= 0xF9:
        return (st_min - 0xF0) / 10000.0
    # Reserved values are treated as the maximum (127 ms) per ISO 15765-2.
    return 0x7F / 1000.0


class IsoTpLayer:
    """Send and receive arbitrarily long payloads over a :class:`CanBus`."""

    def __init__(self, bus: CanBus, config: IsoTpConfig) -> None:
        self.bus = bus
        self.cfg = config

    # ------------------------------------------------------------------ #
    # Frame helpers
    # ------------------------------------------------------------------ #
    def _ae_prefix(self) -> bytes:
        ae = self.cfg.address_extension
        return bytes([ae]) if ae is not None else b""

    def _pad(self, payload: bytes) -> bytes:
        if self.cfg.padding_byte is None:
            return payload
        if len(payload) >= 8:
            return payload
        return payload + bytes([self.cfg.padding_byte]) * (8 - len(payload))

    def _tx(self, payload: bytes) -> None:
        frame = CanFrame(
            self.cfg.tx_id,
            self._pad(self._ae_prefix() + payload),
            self.cfg.is_extended_id,
        )
        log.debug("ISO-TP TX %s", frame)
        self.bus.send(frame)

    def _rx_matching(self, deadline: float) -> Optional[bytes]:
        """Return the data (minus any AE byte) of the next frame on rx_id."""

        ae = self.cfg.address_extension
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            frame = self.bus.recv(timeout=remaining)
            if frame is None:
                continue
            if frame.arbitration_id != self.cfg.rx_id:
                continue
            data = frame.data
            if ae is not None:
                if not data or data[0] != ae:
                    continue
                data = data[1:]
            log.debug("ISO-TP RX %s", frame)
            return data

    # ------------------------------------------------------------------ #
    # Sending
    # ------------------------------------------------------------------ #
    def send(self, data: bytes, timeout: Optional[float] = None) -> None:
        """Transmit ``data`` as one ISO-TP message (segmenting as needed)."""

        data = bytes(data)
        cap = self.cfg.data_length()
        sf_max = cap - 1  # one PCI byte

        if len(data) <= sf_max:
            self._send_single_frame(data)
            return
        self._send_multi_frame(data, timeout if timeout is not None else self.cfg.n_bs)

    def _send_single_frame(self, data: bytes) -> None:
        self._tx(bytes([(_PCI_SF << 4) | len(data)]) + data)

    def _send_multi_frame(self, data: bytes, fc_timeout: float) -> None:
        total = len(data)
        cap = self.cfg.data_length()

        # ---- First Frame -------------------------------------------------
        if total <= 0xFFF:
            header = bytes([(_PCI_FF << 4) | ((total >> 8) & 0x0F), total & 0xFF])
        else:
            # 32-bit escape form: 0x10 0x00 <len:4 bytes>
            header = bytes([_PCI_FF << 4, 0x00]) + total.to_bytes(4, "big")
        ff_payload = cap - len(header)
        self._tx(header + data[:ff_payload])
        offset = ff_payload

        sn = 1
        while offset < total:
            fs, block_size, st_min = self._await_flow_control(fc_timeout)
            if fs == _FS_OVFLW:
                raise IsoTpError("receiver reported buffer overflow (FC OVFLW)")
            if fs == _FS_WAIT:
                # Peer asked us to wait; loop back and wait for a fresh FC.
                continue

            sep = _decode_stmin(st_min)
            frames_in_block = 0
            while offset < total:
                chunk = data[offset : offset + (cap - 1)]
                self._tx(bytes([(_PCI_CF << 4) | (sn & 0x0F)]) + chunk)
                offset += len(chunk)
                sn = (sn + 1) & 0x0F
                frames_in_block += 1
                if offset >= total:
                    break
                if block_size and frames_in_block >= block_size:
                    break  # wait for the next FC
                if sep:
                    time.sleep(sep)

    def _await_flow_control(self, fc_timeout: float):
        deadline = time.monotonic() + fc_timeout
        while True:
            data = self._rx_matching(deadline)
            if data is None:
                raise IsoTpTimeoutError("timeout waiting for Flow Control frame")
            if not data:
                continue
            if (data[0] >> 4) != _PCI_FC:
                # Not an FC (could be a stray frame); keep waiting.
                continue
            fs = data[0] & 0x0F
            block_size = data[1] if len(data) > 1 else 0
            st_min = data[2] if len(data) > 2 else 0
            return fs, block_size, st_min

    # ------------------------------------------------------------------ #
    # Receiving
    # ------------------------------------------------------------------ #
    def recv(self, timeout: Optional[float] = None) -> bytes:
        """Receive one complete ISO-TP message and return its payload."""

        overall = timeout if timeout is not None else self.cfg.timeout
        deadline = time.monotonic() + overall

        data = self._rx_matching(deadline)
        if data is None:
            raise IsoTpTimeoutError("timeout waiting for the first frame")
        if not data:
            raise IsoTpError("received an empty CAN frame")

        pci_type = data[0] >> 4
        if pci_type == _PCI_SF:
            length = data[0] & 0x0F
            if length == 0 and len(data) > 8:
                # SF escape (CAN-FD only) - the real length is in the next byte.
                # Classic CAN frames are <= 8 bytes, so a zero nibble there just
                # means an empty single frame.
                length = data[1]
                return data[2 : 2 + length]
            return data[1 : 1 + length]

        if pci_type == _PCI_FF:
            return self._recv_multi_frame(data)

        raise IsoTpError(f"unexpected PCI type 0x{pci_type:X} for first frame")

    def _recv_multi_frame(self, ff: bytes) -> bytes:
        cap = self.cfg.data_length()
        length = ((ff[0] & 0x0F) << 8) | ff[1]
        if length == 0:
            # 32-bit escape First Frame.
            length = int.from_bytes(ff[2:6], "big")
            payload = bytearray(ff[6:cap])
        else:
            payload = bytearray(ff[2:cap])

        payload = payload[:length]

        # Acknowledge with a Flow Control (Clear To Send).
        self._send_flow_control(_FS_CTS)

        expected_sn = 1
        frames_since_fc = 0
        while len(payload) < length:
            deadline = time.monotonic() + self.cfg.n_cr
            data = self._rx_matching(deadline)
            if data is None:
                raise IsoTpTimeoutError("timeout waiting for a Consecutive Frame")
            if (data[0] >> 4) != _PCI_CF:
                raise IsoTpError("expected a Consecutive Frame")
            sn = data[0] & 0x0F
            if sn != expected_sn:
                raise IsoTpError(
                    f"consecutive frame sequence error: expected {expected_sn}, got {sn}"
                )
            expected_sn = (expected_sn + 1) & 0x0F
            payload.extend(data[1:cap])
            frames_since_fc += 1

            if len(payload) >= length:
                break
            if self.cfg.block_size and frames_since_fc >= self.cfg.block_size:
                self._send_flow_control(_FS_CTS)
                frames_since_fc = 0

        return bytes(payload[:length])

    def _send_flow_control(self, flow_status: int) -> None:
        self._tx(
            bytes(
                [
                    (_PCI_FC << 4) | (flow_status & 0x0F),
                    self.cfg.block_size & 0xFF,
                    self.cfg.st_min & 0xFF,
                ]
            )
        )
