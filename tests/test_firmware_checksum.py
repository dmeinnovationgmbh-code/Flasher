"""Firmware container, checksum and ECU profile tests."""

from __future__ import annotations

import os
import tempfile

import pytest

from med17flasher.core import checksum as cs
from med17flasher.core.ecu_profile import EcuProfile, builtin_med17_7_5, load_profile
from med17flasher.core.firmware import (
    FirmwareImage,
    load_intel_hex,
    load_srecord,
)


# --------------------------------------------------------------------------- #
# checksums
# --------------------------------------------------------------------------- #
def test_crc32_matches_zlib():
    import zlib

    data = bytes(range(256))
    assert cs.crc32(data) == zlib.crc32(data) & 0xFFFFFFFF


def test_crc16_ccitt_known_vector():
    # CRC-16/CCITT-FALSE of "123456789" is 0x29B1.
    assert cs.crc16_ccitt(b"123456789") == 0x29B1


def test_sum_and_xor():
    assert cs.sum32(b"\x01\x02\x03") == 6
    assert cs.xor_checksum(b"\x0f\xf0") == 0xFF
    assert cs.compute("sum16", b"\xff\xff") == 0x01FE


def test_unknown_checksum():
    with pytest.raises(ValueError):
        cs.compute("nope", b"")


# --------------------------------------------------------------------------- #
# firmware container
# --------------------------------------------------------------------------- #
def test_segment_merge_and_read():
    img = FirmwareImage()
    img.add_segment(0x1000, b"\xaa" * 16)
    img.add_segment(0x1010, b"\xbb" * 16)  # adjacent -> should merge
    img.normalise()
    assert len(img.segments) == 1
    assert img.read(0x1000, 32) == b"\xaa" * 16 + b"\xbb" * 16
    # gap fill
    img2 = FirmwareImage()
    img2.add_segment(0x1000, b"\x11")
    img2.add_segment(0x1004, b"\x22")
    img2.normalise()
    assert img2.read(0x1000, 5) == b"\x11\xff\xff\xff\x22"


def test_covers():
    img = FirmwareImage()
    img.add_segment(0x1000, b"\x00" * 100)
    img.normalise()
    assert img.covers(0x1000, 100)
    assert not img.covers(0x1000, 101)
    assert not img.covers(0x0FFF, 2)


def test_intel_hex_roundtrip():
    # :10 0000 00 <16 bytes> checksum ; with an extended linear address record
    records = [
        ":020000040800F2",  # upper = 0x08000000
        ":10000000000102030405060708090A0B0C0D0E0F78",
        ":00000001FF",
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".hex", delete=False) as fh:
        fh.write("\n".join(records))
        path = fh.name
    try:
        img = load_intel_hex(path)
        assert img.read(0x08000000, 16) == bytes(range(16))
    finally:
        os.remove(path)


def test_srecord_roundtrip():
    # S3 record with a 32-bit address 0x08000000 and 4 data bytes
    # length = addr(4) + data(4) + csum(1) = 9 -> 0x09
    payload = bytes([0x09]) + bytes.fromhex("08000000") + bytes.fromhex("DEADBEEF")
    csum = (~sum(payload)) & 0xFF
    line = "S3" + (payload + bytes([csum])).hex().upper()
    with tempfile.NamedTemporaryFile("w", suffix=".s19", delete=False) as fh:
        fh.write(line + "\n")
        path = fh.name
    try:
        img = load_srecord(path)
        assert img.read(0x08000000, 4) == bytes.fromhex("DEADBEEF")
    finally:
        os.remove(path)


def test_load_binary_and_blocks_for():
    img = FirmwareImage()
    img.add_segment(0x80040000, b"\x5a" * 0x2000)
    img.add_segment(0x80042000, b"\xa5" * 0x1000)
    img.normalise()
    profile = load_profile("config/med17_7_5_demo.yaml")
    blocks = img.blocks_for(profile.memory_map)
    assert [b.name for b in blocks] == ["ASW", "CAL"]
    assert blocks[0].size == 0x2000 and blocks[1].size == 0x1000


# --------------------------------------------------------------------------- #
# ECU profile
# --------------------------------------------------------------------------- #
def test_builtin_profile():
    p = builtin_med17_7_5()
    assert p.name == "MED17.7.5"
    assert p.can.tx_id == 0x7E0
    assert any(r.name == "ASW1" for r in p.memory_map)


def test_profile_from_dict_hex_strings():
    data = {
        "name": "X",
        "can": {"tx_id": "0x700", "rx_id": "0x708"},
        "security": {"request_seed_level": "0x11", "send_key_level": "0x12"},
        "memory_map": [{"name": "A", "start": "0x80040000", "size": "0x1000"}],
    }
    p = EcuProfile.from_dict(data)
    assert p.can.tx_id == 0x700
    assert p.memory_map[0].start == 0x80040000


def test_profile_yaml_json_roundtrip():
    p = builtin_med17_7_5()
    with tempfile.TemporaryDirectory() as d:
        import json

        path = os.path.join(d, "p.json")
        with open(path, "w") as fh:
            json.dump(p.to_dict(), fh, default=str)
        reloaded = load_profile(path)
        assert reloaded.name == p.name
        assert len(reloaded.memory_map) == len(p.memory_map)
