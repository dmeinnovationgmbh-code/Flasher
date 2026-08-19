"""ECU scan (recon) + live capture tests, against the simulator."""

from __future__ import annotations

import time

from med17flasher.core import ProfileSeedKey
from med17flasher.core.flash_sequence import Flasher
from med17flasher.core.firmware import FirmwareImage
from med17flasher.core.trace import LiveCapture, analyze, capture_frames
from med17flasher.recon import EcuScanner


def test_scan_reports_ecu(uds, simulator, demo_profile):
    scanner = EcuScanner(uds, demo_profile.can.tx_id, demo_profile.can.rx_id)
    report = scanner.scan(probe_programming_session=True, read_memory_at=0x80040000)

    assert report.online
    assert 0x03 in report.sessions_supported  # extended
    assert 0x02 in report.sessions_supported  # programming
    from med17flasher.core import uds_const as C

    assert int(C.DataIdentifier.VIN) in report.identification
    assert report.identification[int(C.DataIdentifier.VIN)].startswith(b"WVWZZZ")
    # the simulator returns a seed for the programming level
    assert "seed" in report.seeds.get(0x11, "")
    assert report.memory_read is not None


def test_scan_to_profile_skeleton(uds, simulator, demo_profile):
    report = EcuScanner(uds, 0x7E0, 0x7E8).scan()
    profile = report.to_profile()
    # 0x11 seed was seen -> programming level guessed
    assert profile.security.request_seed_level == 0x11
    assert profile.can.tx_id == 0x7E0
    assert profile.memory_map == []  # memory map is unknown from a scan alone


def test_scan_text_summary(uds, simulator, demo_profile):
    report = EcuScanner(uds, 0x7E0, 0x7E8).scan()
    text = report.text_summary()
    assert "ECU online" in text
    assert "identification" in text


def test_live_capture_during_flash(network, demo_profile, simulator, uds):
    sniffer = network.new_endpoint("sniffer")
    cap = LiveCapture(sniffer).start()

    img = FirmwareImage()
    img.add_segment(0x80040000, bytes((i * 7) & 0xFF for i in range(0x2000)))
    img.add_segment(0x80042000, bytes((0xC0 + (i & 0x1F)) & 0xFF for i in range(0x1000)))
    img.normalise()
    Flasher(uds, demo_profile, ProfileSeedKey(demo_profile)).flash(img)
    time.sleep(0.15)
    frames = cap.stop()

    assert len(frames) > 50
    report = analyze(frames, tx_id=0x7E0, rx_id=0x7E8)
    assert report.request_count > 0
    assert report.erase_routine == 0xFF00
    assert len(report.seed_key_pairs) == 1


def test_capture_frames_timeout(network):
    # a quiet bus yields nothing within the window (and must not hang)
    bus = network.new_endpoint("quiet")
    t0 = time.monotonic()
    frames = capture_frames(bus, seconds=0.3)
    assert frames == []
    assert time.monotonic() - t0 < 2.0
