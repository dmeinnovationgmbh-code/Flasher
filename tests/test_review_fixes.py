"""Regression tests for the defects found by the adversarial review workflow."""

from __future__ import annotations

import pytest

from med17flasher.core import (
    CanConfig,
    EcuProfile,
    Flasher,
    FirmwareImage,
    IsoTpConfig,
    IsoTpLayer,
    MemoryRegion,
    ProfileSeedKey,
    RoutineConfig,
    SecurityConfig,
    TimingConfig,
    UdsClient,
    UdsTiming,
    VirtualCanNetwork,
)
from med17flasher.core.ecu_profile import FingerprintWrite
from med17flasher.exceptions import FlashError, Med17FlasherError
from med17flasher.simulator import VirtualEcu, VirtualEcuConfig

SEC = {"k": "0x1C5A36B7", "rounds": 5, "shift": 5}


def _flash(profile, segments, **fcfg):
    net = VirtualCanNetwork()
    sim = VirtualEcu(net.new_endpoint("ecu"), profile,
                     VirtualEcuConfig(security_algorithm="med17", security_params=SEC,
                                      max_block_length=0x0FFE))
    sim.start()
    try:
        tp = IsoTpLayer(net.new_endpoint("tester"),
                        IsoTpConfig(tx_id=0x7E0, rx_id=0x7E8, padding_byte=0x00))
        uds = UdsClient(tp, UdsTiming(p2=1, p2_star=2))
        img = FirmwareImage()
        for addr, data in segments:
            img.add_segment(addr, data)
        img.normalise()
        result = Flasher(uds, profile, ProfileSeedKey(profile), **fcfg).flash(img)
        return result, sim
    finally:
        sim.stop()


def test_erase_none_multiblock_preserves_all_blocks():
    # HIGH: whole-flash erase ("none") must run once, not per block, or block 2's
    # erase would wipe block 1.
    profile = EcuProfile(
        name="MED17.7.5",
        can=CanConfig(tx_id=0x7E0, rx_id=0x7E8, padding_byte=0x00),
        timing=TimingConfig(p2=1, p2_star=2, tester_present_period=0.5),
        security=SecurityConfig(request_seed_level=0x05, send_key_level=0x06,
                                algorithm="med17", params=SEC),
        routines=RoutineConfig(erase_argument="none"),
        verify_after_write=False,
        memory_map=[
            MemoryRegion("B1", 0x80040000, 0x1000, checksum="crc32"),
            MemoryRegion("B2", 0x80042000, 0x1000, checksum="crc32"),
        ],
    )
    d1 = bytes((i * 7) & 0xFF for i in range(0x1000))
    d2 = bytes((0xC0 + (i & 0x1F)) & 0xFF for i in range(0x1000))
    result, sim = _flash(profile, [(0x80040000, d1), (0x80042000, d2)])
    assert result.success
    # both blocks must survive - a single whole-flash erase happened
    assert sim.read_memory(0x80040000, 0x1000) == d1
    assert sim.read_memory(0x80042000, 0x1000) == d2
    assert len(sim.erased_regions) == 1  # erased exactly once


def test_fingerprints_null_key_does_not_crash():
    # MEDIUM: `fingerprints:` present but null must not raise TypeError.
    p = EcuProfile.from_dict({"name": "X", "fingerprints": None})
    assert p.fingerprints == []


def test_fingerprint_numeric_value_is_hex_not_decimal():
    # MEDIUM: a numeric YAML value must become even-length hex, not decimal text.
    p = EcuProfile.from_dict({"name": "X", "fingerprints": [{"did": 0xF15A, "value": 0x020004}]})
    assert p.fingerprints[0].value == "020004"
    bytes.fromhex(p.fingerprints[0].value)  # must be valid hex


def test_fingerprint_bad_hex_raises_flasherror():
    # LOW: a malformed fingerprint hex must raise a typed FlashError, not a raw
    # ValueError escaping flash().
    profile = EcuProfile(
        name="MED17.7.5",
        can=CanConfig(tx_id=0x7E0, rx_id=0x7E8, padding_byte=0x00),
        timing=TimingConfig(p2=1, p2_star=2, tester_present_period=0.5),
        security=SecurityConfig(request_seed_level=0x05, send_key_level=0x06,
                                algorithm="med17", params=SEC),
        routines=RoutineConfig(erase_argument="none"),
        verify_after_write=False,
        memory_map=[MemoryRegion("B1", 0x80040000, 0x1000)],
        fingerprints=[FingerprintWrite(0xF15A, "ZZZZ", "after_security")],
    )
    with pytest.raises(FlashError):
        _flash(profile, [(0x80040000, b"\x00" * 0x1000)])


def test_gateway_missing_field_friendly_error():
    # LOW: a partial gateway config raises a typed error, not a raw KeyError.
    with pytest.raises(Med17FlasherError):
        EcuProfile.from_dict({"name": "X", "gateway": {"tx_id": 0x607}})


def test_build_image_uses_region_base_for_real_firmware(tmp_path):
    # MEDIUM: a raw .bin flashed to real hardware must anchor at the first region.
    from med17flasher.webserver.service import FlashService

    profile = EcuProfile(
        name="MED17.7.5",
        memory_map=[MemoryRegion("CAL", 0x84002000, 0x1000)],
    )
    fw = tmp_path / "cal.bin"
    fw.write_bytes(bytes(0x1000))
    svc = FlashService(profile, throttle_kbs=0, backend="socketcan:doesnotexist",
                       firmware_path=str(fw), allow_write=True)
    image = svc._build_image()
    # the segment is anchored at the region start, not address 0
    assert image.segments[0].address == 0x84002000
