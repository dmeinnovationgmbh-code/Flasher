"""Real MED17.7.5 (med1775) flow: level 0x05/0x06, whole-flash erase, no
checkMemory, fingerprint writes, clearDTC, and calibration validation."""

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
    load_profile,
)
from med17flasher.core.ecu_profile import FingerprintWrite
from med17flasher.core.firmware import validate_calibration
from med17flasher.exceptions import FirmwareError
from med17flasher.simulator import VirtualEcu, VirtualEcuConfig

SEC = {"k": "0x1C5A36B7", "rounds": 5, "shift": 5}


def _med1775_profile(size=0x4000):
    return EcuProfile(
        name="MED17.7.5",
        can=CanConfig(tx_id=0x7E0, rx_id=0x7E8, padding_byte=0x00),
        timing=TimingConfig(p2=1, p2_star=2, tester_present_period=0.5),
        security=SecurityConfig(request_seed_level=0x05, send_key_level=0x06,
                                algorithm="med17", params=SEC),
        routines=RoutineConfig(erase_argument="none"),
        memory_map=[MemoryRegion("CAL", 0x84002000, size, checksum="crc32")],
        pre_hard_reset=True, settle_delay=0.0, verify_after_write=False,
        clear_dtc_after=True, final_session=0x03,
        fingerprints=[
            FingerprintWrite(0xF15A, "020004110A0B00191471", "after_security"),
            FingerprintWrite(0xF15A, "0000030D090600000000", "after_download"),
        ],
    )


def _flash(profile, data):
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
        img.add_segment(0x84002000, data)
        img.normalise()
        result = Flasher(uds, profile, ProfileSeedKey(profile)).flash(img)
        return result, sim
    finally:
        sim.stop()


def test_med1775_full_flow():
    profile = _med1775_profile()
    data = bytes([0x60]) + bytes((i * 7) & 0xFF for i in range(0x4000 - 2)) + bytes([0xDE])
    result, sim = _flash(profile, data)
    assert result.success
    assert result.blocks == ["CAL"]
    assert sim.read_memory(0x84002000, 0x4000) == data
    # whole-flash erase (no address argument)
    assert sim.erased_regions and sim.erased_regions[0][1] == len(sim._mem)
    # NO checkMemory ran (verify_after_write False)
    assert sim.verified_regions == []
    # fingerprints were written (last one persists at DID 0xF15A)
    assert sim.identification.get(0xF15A) == bytes.fromhex("0000030D090600000000")


def test_calibration_validation():
    good = bytes([0x60]) + b"\x00" * 10 + bytes([0xDE])
    validate_calibration(good, length=12)  # ok
    with pytest.raises(FirmwareError):
        validate_calibration(b"\x00" + b"\x00" * 10 + bytes([0xDE]))  # bad start
    with pytest.raises(FirmwareError):
        validate_calibration(bytes([0x60]) + b"\x00" * 10 + b"\x00")  # bad end
    with pytest.raises(FirmwareError):
        validate_calibration(good, length=99)  # wrong length


def test_med1775_profile_yaml_loads():
    p = load_profile("config/med17_7_5_med1775.yaml")
    assert p.security.request_seed_level == 0x05
    assert p.security.send_key_level == 0x06
    assert p.routines.erase_argument == "none"
    assert p.verify_after_write is False
    assert p.clear_dtc_after is True
    assert p.final_session == 0x03
    assert p.pre_hard_reset is True
    assert len(p.fingerprints) == 2
    assert p.fingerprints[0].did == 0xF15A
    assert p.gateway is None  # commented out in the shipped profile
    assert p.memory_map[0].start == 0x84002000
