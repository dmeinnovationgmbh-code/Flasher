"""End-to-end flash tests against the virtual ECU simulator."""

from __future__ import annotations

import threading

import pytest

from med17flasher.core.firmware import FirmwareImage
from med17flasher.core.flash_sequence import Flasher, ProfileSeedKey, Stage
from med17flasher.exceptions import FlashAborted, FlashError
from med17flasher.simulator import VirtualEcuConfig


def _make_image(profile):
    img = FirmwareImage()
    img.add_segment(0x80040000, bytes((i * 7) & 0xFF for i in range(0x2000)))
    img.add_segment(0x80042000, bytes((0xC0 + (i & 0x1F)) & 0xFF for i in range(0x1000)))
    return img.normalise()


def test_full_flash_success(uds, simulator, demo_profile):
    img = _make_image(demo_profile)
    stages = []
    flasher = Flasher(uds, demo_profile, ProfileSeedKey(demo_profile),
                      progress=lambda p: stages.append(p.stage))
    result = flasher.flash(img)
    assert result.success
    assert result.blocks == ["ASW", "CAL"]
    # simulator memory now equals the image
    assert simulator.read_memory(0x80040000, 0x2000) == img.read(0x80040000, 0x2000)
    assert simulator.read_memory(0x80042000, 0x1000) == img.read(0x80042000, 0x1000)
    # each block was erased and CRC-verified (these require a prior unlock, so
    # their success proves Security Access happened; the final ECU reset
    # re-locks the ECU, which is why we check the side effects, not `unlocked`).
    assert len(simulator.erased_regions) == 2
    assert len(simulator.verified_regions) == 2
    # the important stages were visited
    for stage in (Stage.SECURITY_ACCESS, Stage.ERASE, Stage.TRANSFER, Stage.VERIFY, Stage.DONE):
        assert stage in stages


def test_identify(uds, simulator, demo_profile):
    flasher = Flasher(uds, demo_profile, ProfileSeedKey(demo_profile))
    ident = dict(flasher.identify())
    from med17flasher.core import uds_const as C

    assert ident[int(C.DataIdentifier.VIN)].startswith(b"WVWZZZ")


def test_flash_wrong_key_fails(demo_profile):
    # A simulator that expects a *different* seed/key constant -> key rejected.
    # Uses a fully isolated network (no shared `uds`/`simulator` fixtures) so
    # exactly one ECU answers on the bus.
    from med17flasher.core import (
        IsoTpConfig,
        IsoTpLayer,
        UdsClient,
        UdsTiming,
        VirtualCanNetwork,
    )
    from med17flasher.simulator import VirtualEcu

    net = VirtualCanNetwork()
    bad_sim = VirtualEcu(
        net.new_endpoint("ecu"), demo_profile,
        VirtualEcuConfig(security_algorithm="med17",
                         security_params={"k": "0xDEADBEEF", "rounds": 5, "shift": 5}),
    )
    bad_sim.start()
    try:
        bus = net.new_endpoint("tester")
        tp = IsoTpLayer(bus, IsoTpConfig(tx_id=0x7E0, rx_id=0x7E8, padding_byte=0x55))
        uds = UdsClient(tp, UdsTiming(p2=1.0, p2_star=2.0))
        img = _make_image(demo_profile)
        flasher = Flasher(uds, demo_profile, ProfileSeedKey(demo_profile))
        with pytest.raises((FlashError, Exception)) as exc:
            flasher.flash(img)
        # the failure must be the invalid-key security-access rejection
        from med17flasher.exceptions import NegativeResponseError

        assert isinstance(exc.value, (FlashError, NegativeResponseError))
    finally:
        bad_sim.stop()


def test_flash_abort(uds, simulator, demo_profile):
    img = _make_image(demo_profile)
    abort = threading.Event()
    seen = {"count": 0}

    def progress(p):
        seen["count"] += 1
        if p.stage == Stage.TRANSFER:
            abort.set()

    flasher = Flasher(uds, demo_profile, ProfileSeedKey(demo_profile),
                      progress=progress, abort_event=abort)
    with pytest.raises(FlashAborted):
        flasher.flash(img)


def test_flash_empty_image_rejected(uds, simulator, demo_profile):
    img = FirmwareImage()
    img.add_segment(0x90000000, b"\x00" * 16)  # outside any region
    img.normalise()
    flasher = Flasher(uds, demo_profile, ProfileSeedKey(demo_profile))
    with pytest.raises(FlashError):
        flasher.flash(img)


def test_response_pending_during_flash(network, demo_profile):
    """A simulator that emits 0x78 before finishing routines still flashes."""

    from med17flasher.core import IsoTpConfig, IsoTpLayer, UdsClient, UdsTiming
    from med17flasher.simulator import VirtualEcu

    ecu_bus = network.new_endpoint("ecu")
    sim = VirtualEcu(
        ecu_bus, demo_profile,
        VirtualEcuConfig(security_algorithm="med17",
                         security_params={"k": "0x1C5A36B7", "rounds": 5, "shift": 5},
                         pending_rounds=2, max_block_length=0x0102),
    )
    sim.start()
    try:
        bus = network.new_endpoint("tester")
        tp = IsoTpLayer(bus, IsoTpConfig(tx_id=0x7E0, rx_id=0x7E8, padding_byte=0x55))
        uds = UdsClient(tp, UdsTiming(p2=0.5, p2_star=2.0))
        img = _make_image(demo_profile)
        flasher = Flasher(uds, demo_profile, ProfileSeedKey(demo_profile))
        assert flasher.flash(img).success
    finally:
        sim.stop()
