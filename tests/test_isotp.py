"""ISO-TP (ISO 15765-2) transport tests."""

from __future__ import annotations

import threading

import pytest

from med17flasher.core import IsoTpConfig, IsoTpLayer, VirtualCanNetwork
from med17flasher.exceptions import IsoTpTimeoutError


def _pair(padding=0x55, block_size=0, st_min=0):
    net = VirtualCanNetwork()
    a = IsoTpLayer(
        net.new_endpoint("a"),
        IsoTpConfig(tx_id=0x7E0, rx_id=0x7E8, padding_byte=padding,
                    block_size=block_size, st_min=st_min),
    )
    b = IsoTpLayer(
        net.new_endpoint("b"),
        IsoTpConfig(tx_id=0x7E8, rx_id=0x7E0, padding_byte=padding,
                    block_size=block_size, st_min=st_min),
    )
    return a, b


def _roundtrip(sender: IsoTpLayer, receiver: IsoTpLayer, payload: bytes) -> bytes:
    got = {}

    def rx():
        got["data"] = receiver.recv(timeout=5)

    t = threading.Thread(target=rx)
    t.start()
    # give the receiver a moment to start waiting
    import time

    time.sleep(0.02)
    sender.send(payload)
    t.join(timeout=5)
    return got["data"]


@pytest.mark.parametrize("size", [0, 1, 6, 7])
def test_single_frame(size):
    a, b = _pair()
    payload = bytes(range(size))
    assert _roundtrip(a, b, payload) == payload


@pytest.mark.parametrize("size", [8, 9, 63, 64, 127, 256, 1000, 4095])
def test_multi_frame(size):
    a, b = _pair()
    payload = bytes((i * 7) & 0xFF for i in range(size))
    assert _roundtrip(a, b, payload) == payload


def test_escape_first_frame_over_4095():
    a, b = _pair()
    payload = bytes((i * 3) & 0xFF for i in range(5000))
    assert _roundtrip(a, b, payload) == payload


def test_block_size_flow_control():
    # Receiver only accepts 4 consecutive frames per flow-control window.
    a, b = _pair(block_size=4)
    payload = bytes((i) & 0xFF for i in range(2000))
    assert _roundtrip(a, b, payload) == payload


def test_stmin_separation_time():
    a, b = _pair(st_min=1)  # 1 ms between CFs advertised by the receiver
    payload = bytes(range(200))
    assert _roundtrip(a, b, payload) == payload


def test_no_padding():
    a, b = _pair(padding=None)
    payload = bytes(range(20))
    assert _roundtrip(a, b, payload) == payload


def test_extended_addressing():
    net = VirtualCanNetwork()
    a = IsoTpLayer(
        net.new_endpoint("a"),
        IsoTpConfig(tx_id=0x7E0, rx_id=0x7E8, address_extension=0xF1, padding_byte=0xAA),
    )
    b = IsoTpLayer(
        net.new_endpoint("b"),
        IsoTpConfig(tx_id=0x7E8, rx_id=0x7E0, address_extension=0xF1, padding_byte=0xAA),
    )
    payload = bytes(range(30))
    assert _roundtrip(a, b, payload) == payload


def test_recv_timeout():
    a, _ = _pair()
    with pytest.raises(IsoTpTimeoutError):
        a.recv(timeout=0.1)
