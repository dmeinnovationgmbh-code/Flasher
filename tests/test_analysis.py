"""Firmware region detection + UDS transient-NRC retry tests."""

from __future__ import annotations

import pytest

from med17flasher.core.firmware import FirmwareImage, detect_regions
from med17flasher.core.uds import UdsClient, UdsTiming
from med17flasher.core import uds_const as C
from med17flasher.exceptions import NegativeResponseError


# --------------------------------------------------------------------------- #
# Region detection
# --------------------------------------------------------------------------- #
def test_detect_two_regions():
    img = FirmwareImage()
    img.add_segment(0x80040000, b"\x5a" * 0x800)          # region 1
    img.add_segment(0x80040000 + 0x800, b"\xff" * 0x4000)  # big erased gap
    img.add_segment(0x80044800, b"\xa5" * 0x400)          # region 2
    img.normalise()
    regions = detect_regions(img, min_gap=0x1000, align=0x100)
    assert len(regions) == 2
    assert regions[0][0] == 0x80040000
    assert regions[1][0] == 0x80044800


def test_detect_merges_small_gaps():
    img = FirmwareImage()
    img.add_segment(0x1000, b"\x11" * 0x100)
    img.add_segment(0x1100, b"\xff" * 0x10)   # tiny gap -> merged
    img.add_segment(0x1110, b"\x22" * 0x100)
    img.normalise()
    regions = detect_regions(img, min_gap=0x1000, align=0x10)
    assert len(regions) == 1


def test_detect_empty_image():
    assert detect_regions(FirmwareImage()) == []


# --------------------------------------------------------------------------- #
# UDS transient-NRC retry
# --------------------------------------------------------------------------- #
class _ScriptedIsoTp:
    class _Bus:
        def flush_rx(self):
            pass

    def __init__(self, responses):
        self._responses = list(responses)
        self.sent = []
        self.bus = self._Bus()

    def send(self, data, timeout=None):
        self.sent.append(bytes(data))

    def recv(self, timeout=None):
        return self._responses.pop(0)


def _fast_timing():
    return UdsTiming(p2=0.5, p2_star=0.5, busy_delay=0.001, delay_not_expired_wait=0.001)


def test_retry_on_busy():
    busy = bytes([0x7F, 0x10, int(C.NRC.BUSY_REPEAT_REQUEST)])
    ok = bytes([0x50, 0x02, 0x00, 0x32, 0x01, 0xF4])
    tp = _ScriptedIsoTp([busy, busy, ok])
    uds = UdsClient(tp, _fast_timing())
    record = uds.diagnostic_session_control(0x02)
    assert record == bytes([0x00, 0x32, 0x01, 0xF4])
    assert len(tp.sent) == 3  # two retries + the successful send


def test_retry_on_required_time_delay():
    delay = bytes([0x7F, 0x27, int(C.NRC.REQUIRED_TIME_DELAY_NOT_EXPIRED)])
    seed_ok = bytes([0x67, 0x11, 0xDE, 0xAD, 0xBE, 0xEF])
    tp = _ScriptedIsoTp([delay, seed_ok])
    uds = UdsClient(tp, _fast_timing())
    seed = uds.request_seed(0x11)
    assert seed == bytes.fromhex("deadbeef")
    assert len(tp.sent) == 2


def test_retry_exhausted_raises():
    busy = bytes([0x7F, 0x11, int(C.NRC.BUSY_REPEAT_REQUEST)])
    tp = _ScriptedIsoTp([busy, busy, busy, busy, busy])
    uds = UdsClient(tp, _fast_timing())  # busy_retries default 3 -> 4 sends then raise
    with pytest.raises(NegativeResponseError) as exc:
        uds.ecu_reset(0x01)
    assert exc.value.nrc == int(C.NRC.BUSY_REPEAT_REQUEST)


def test_non_transient_nrc_not_retried():
    denied = bytes([0x7F, 0x27, int(C.NRC.SECURITY_ACCESS_DENIED)])
    tp = _ScriptedIsoTp([denied])
    uds = UdsClient(tp, _fast_timing())
    with pytest.raises(NegativeResponseError):
        uds.request_seed(0x11)
    assert len(tp.sent) == 1  # not retried
