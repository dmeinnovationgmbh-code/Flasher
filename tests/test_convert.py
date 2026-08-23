"""convert / inflate / extract-calibration tests."""

from __future__ import annotations

import os
import tempfile
import zlib

import pytest

from med17flasher import cli
from med17flasher.core.compression import inflate, inflate_raw, inflate_sections
from med17flasher.exceptions import FirmwareError


def test_inflate_raw_roundtrip():
    payload = bytes((i * 7) & 0xFF for i in range(5000))
    co = zlib.compressobj(9, zlib.DEFLATED, -15)  # raw deflate
    raw = co.compress(payload) + co.flush()
    assert inflate_raw(raw) == payload
    out, mode = inflate(raw)
    assert out == payload and mode == "raw"


def test_inflate_raw_with_offset_and_trailing():
    payload = b"CALIBRATION-DATA" * 100
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    raw = co.compress(payload) + co.flush()
    blob = b"\x00" * 0x30 + raw + b"\xff" * 16  # header + stream + trailing
    assert inflate_raw(blob, offset=0x30) == payload
    out, mode = inflate(blob, offset=0x30)
    assert out == payload


def test_inflate_zlib_and_gzip():
    import gzip

    payload = b"hello world" * 50
    assert inflate(zlib.compress(payload))[0] == payload
    assert inflate(gzip.compress(payload))[0] == payload


def test_inflate_sections():
    p1 = b"AAAA" * 100
    p2 = b"BBBB" * 200
    co1 = zlib.compressobj(9, zlib.DEFLATED, -15)
    co2 = zlib.compressobj(9, zlib.DEFLATED, -15)
    blob = (co1.compress(p1) + co1.flush()) + (co2.compress(p2) + co2.flush())
    parts = inflate_sections(blob)
    assert parts == [p1, p2]


def test_inflate_bad_data():
    with pytest.raises(FirmwareError):
        inflate(b"\x00\x01\x02\x03not-compressed")


def test_cli_convert_hex_to_bin():
    with tempfile.TemporaryDirectory() as d:
        hexf = os.path.join(d, "a.hex")
        with open(hexf, "w") as fh:
            fh.write(":10000000000102030405060708090A0B0C0D0E0F78\n:00000001FF\n")
        out = os.path.join(d, "a.bin")
        assert cli.main(["convert", hexf, "-o", out]) == 0
        assert open(out, "rb").read() == bytes(range(16))


def test_cli_inflate_raw():
    payload = bytes(range(256)) * 4
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    raw = co.compress(payload) + co.flush()
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "s.cff")
        with open(src, "wb") as fh:
            fh.write(b"\x00" * 0x30 + raw)
        out = os.path.join(d, "s.bin")
        assert cli.main(["inflate", src, "-o", out, "--offset", "0x30"]) == 0
        assert open(out, "rb").read() == payload


def test_cli_extract_calibration():
    length = 0x2000
    cal = bytes([0x60]) + bytes((i * 3) & 0xFF for i in range(length - 2)) + bytes([0xDE])
    offset = 0x1000
    full = b"\xff" * offset + cal + b"\xff" * 0x100
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "full.bin")
        with open(src, "wb") as fh:
            fh.write(full)
        out = os.path.join(d, "cal.bin")
        rc = cli.main(["extract-calibration", src, "-o", out,
                       "--address", "0x84002000", "--offset", hex(offset),
                       "--length", hex(length)])
        assert rc == 0
        result = open(out, "rb").read()
        assert result == cal
        assert result[0] == 0x60 and result[-1] == 0xDE
