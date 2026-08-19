"""XCP master + measurement tests, driven against the in-process virtual slave."""

from __future__ import annotations

import struct

import pytest

from med17flasher.core.can_backends import VirtualCanNetwork
from med17flasher.exceptions import XcpNegativeResponseError, XcpTimeoutError
from med17flasher.xcp import (
    DaqMeasurement,
    PollingMeasurement,
    Signal,
    XcpClient,
    XcpOnCan,
    VirtualXcpSlave,
    configure_daq,
    parse_signal,
)

CRO, DTO = 0x7E0, 0x7E1


def _pair(provider=None, **slave_kw):
    net = VirtualCanNetwork()
    slave = VirtualXcpSlave(net.new_endpoint("slave"), CRO, DTO,
                            provider=provider, **slave_kw)
    slave.start()
    tp = XcpOnCan(net.new_endpoint("tester"), CRO, DTO, timeout=1.0)
    return slave, XcpClient(tp)


def test_connect_and_status():
    slave, client = _pair()
    try:
        info = client.connect()
        assert info["maxCto"] == 8
        assert info["byteOrder"] == "little"
        assert client.connected
        st = client.get_status()
        assert "sessionStatus" in st
        client.disconnect()
        assert not client.connected
    finally:
        slave.stop()


def test_read_memory_short_and_block():
    slave, client = _pair()
    try:
        slave.set_bytes(0x80005000, (1234).to_bytes(2, "little"))
        client.connect()
        assert int.from_bytes(client.short_upload(0x80005000, 2), "little") == 1234
        # A read larger than MAX_CTO-1 must fall back to SET_MTA + UPLOAD.
        big = bytes(range(20))
        slave.set_bytes(0x1000, big)
        assert client.read(0x1000, 20) == big
        # short_upload guards its size limit
        with pytest.raises(ValueError):
            client.short_upload(0x1000, 20)
    finally:
        slave.stop()


def test_polling_measurement_and_csv(tmp_path):
    slave, client = _pair()
    try:
        slave.set_bytes(0x2000, (800).to_bytes(2, "little"))            # rpm raw
        slave.set_bytes(0x2002, (900).to_bytes(2, "little", signed=False))  # temp raw
        client.connect()
        sigs = [
            Signal("rpm", 0x2000, "u16", factor=0.25),
            Signal("temp", 0x2002, "s16", factor=0.1, offset=-40.0, unit="degC"),
        ]
        m = PollingMeasurement(client, sigs)
        s = m.sample()
        assert s.values["rpm"] == pytest.approx(200.0)   # 800 * 0.25
        assert s.values["temp"] == pytest.approx(50.0)    # 900 * 0.1 - 40

        csv = tmp_path / "log.csv"
        rows = m.run(rate_hz=500, max_samples=5, csv_path=str(csv))
        assert len(rows) == 5
        lines = csv.read_text().splitlines()
        assert lines[0] == "time_s,rpm,temp"
        assert len(lines) == 6  # header + 5 samples
    finally:
        slave.stop()


def test_daq_measurement_streams_live_values():
    counter = {"v": 0}

    def provider(addr, size):
        if addr == 0x3000:
            counter["v"] += 1
            return (counter["v"] & 0xFFFF).to_bytes(2, "little")
        return None

    slave, client = _pair(provider=provider, daq_period=0.004)
    try:
        client.connect()
        sigs = [Signal("n", 0x3000, "u16")]
        layout = configure_daq(client, sigs, event=0)
        assert layout.offsets == [0]
        daq = DaqMeasurement(client, layout)
        rows = daq.run(max_samples=8, duration=3.0)
        assert len(rows) >= 3
        vals = [r.values["n"] for r in rows]
        # The counter only ever grows -> strictly increasing samples.
        assert vals == sorted(vals)
        assert vals[-1] > vals[0]
    finally:
        slave.stop()


def test_unknown_command_raises_negative_response():
    slave, client = _pair()
    try:
        client.connect()
        with pytest.raises(XcpNegativeResponseError) as ei:
            client._command(0x01)  # not a command the slave implements
        assert "UNKNOWN" in ei.value.error_name
    finally:
        slave.stop()


def test_timeout_when_no_slave():
    net = VirtualCanNetwork()
    tp = XcpOnCan(net.new_endpoint("lonely"), CRO, DTO, timeout=0.2)
    client = XcpClient(tp)
    with pytest.raises(XcpTimeoutError):
        client.connect()


def test_parse_signal_and_decoding():
    s = parse_signal("rpm@0x80005000:u16:0.25")
    assert s.name == "rpm" and s.address == 0x80005000
    assert s.dtype == "u16" and s.factor == 0.25

    s2 = parse_signal("coolant@0xD0001234:s16:0.1:-40:degC")
    assert s2.offset == -40.0 and s2.unit == "degC"

    assert Signal("x", 0, "f32").decode(struct.pack("<f", 1.5), "little") == 1.5
    assert Signal("x", 0, "u32").decode((0x01020304).to_bytes(4, "big"), "big") == 0x01020304
    assert Signal("x", 0, "s8").decode(b"\xff", "little") == -1

    with pytest.raises(ValueError):
        Signal("x", 0, "weird")


def test_big_endian_slave_round_trip():
    slave, client = _pair(byte_order="big")
    try:
        info = client.connect()
        assert info["byteOrder"] == "big"
        slave.set_bytes(0x4000, (0xBEEF).to_bytes(2, "big"))
        assert int.from_bytes(client.short_upload(0x4000, 2), "big") == 0xBEEF
    finally:
        slave.stop()
