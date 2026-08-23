"""UDS client tests, including the responsePending (0x78) loop and NRC decode."""

from __future__ import annotations

import pytest

from med17flasher.core import uds_const as C
from med17flasher.core.uds import UdsClient, UdsTiming
from med17flasher.exceptions import NegativeResponseError, UnexpectedResponseError


class FakeIsoTp:
    """A scripted ISO-TP stand-in: queues of responses per test."""

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
        if not self._responses:
            from med17flasher.exceptions import IsoTpTimeoutError

            raise IsoTpTimeoutError("no scripted response")
        return self._responses.pop(0)


def test_positive_response_stripping():
    tp = FakeIsoTp([bytes([0x50, 0x02, 0x00, 0x32, 0x01, 0xF4])])
    uds = UdsClient(tp)
    record = uds.diagnostic_session_control(0x02)
    assert record == bytes([0x00, 0x32, 0x01, 0xF4])
    assert tp.sent[0] == bytes([0x10, 0x02])


def test_negative_response_raises():
    tp = FakeIsoTp([bytes([0x7F, 0x27, 0x35])])
    uds = UdsClient(tp)
    with pytest.raises(NegativeResponseError) as exc:
        uds.request_seed(0x11)
    assert exc.value.nrc == 0x35
    assert "Invalid key" in str(exc.value)


def test_response_pending_loop():
    # two 0x78 responses, then the real positive response
    pending = bytes([0x7F, 0x31, 0x78])
    final = bytes([0x71, 0x01, 0xFF, 0x00, 0x00])
    tp = FakeIsoTp([pending, pending, final])
    uds = UdsClient(tp, UdsTiming(p2=0.5, p2_star=0.5))
    result = uds.start_routine(0xFF00)
    assert result == bytes([0x00])


def test_unexpected_response():
    tp = FakeIsoTp([bytes([0x62, 0xF1, 0x90])])  # wrong SID for a 0x11 request
    uds = UdsClient(tp)
    with pytest.raises(UnexpectedResponseError):
        uds.ecu_reset(0x01)


def test_encode_request_download():
    tp = FakeIsoTp([bytes([0x74, 0x20, 0x04, 0x02])])
    uds = UdsClient(tp)
    max_block = uds.request_download(0x80040000, 0x2000)
    assert max_block == 0x0402
    # 0x34, dataFormat=0x00, ALFID=0x44, addr(4), size(4)
    assert tp.sent[0] == bytes([0x34, 0x00, 0x44, 0x80, 0x04, 0x00, 0x00, 0x00, 0x00, 0x20, 0x00])


def test_nrc_names():
    assert "Security access denied" in C.nrc_name(0x33)
    assert "response pending" in C.nrc_name(0x78).lower()
    assert "manufacturer specific" in C.nrc_name(0xF0).lower()


def test_read_data_by_identifier_against_sim(uds):
    uds.enter_extended_session()
    vin = uds.read_data_by_identifier(int(C.DataIdentifier.VIN))
    assert vin.startswith(b"WVWZZZ")


def test_security_access_against_sim(uds):
    uds.enter_programming_session()
    seed = uds.request_seed(0x11)
    assert len(seed) == 4
    from med17flasher.seedkey import compute_key

    key = compute_key("med17", seed, level=0x11, params={"k": "0x1C5A36B7", "rounds": 5, "shift": 5})
    uds.send_key(0x12, key)  # must not raise


def test_security_access_wrong_key(uds):
    uds.enter_programming_session()
    uds.request_seed(0x11)
    with pytest.raises(NegativeResponseError) as exc:
        uds.send_key(0x12, b"\xde\xad\xbe\xef")
    assert exc.value.nrc == int(C.NRC.INVALID_KEY)
