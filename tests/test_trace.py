"""CAN-trace analysis tests (record a simulated flash, then reconstruct it)."""

from __future__ import annotations

import os
import tempfile


from med17flasher.core import (
    Flasher,
    IsoTpConfig,
    IsoTpLayer,
    ProfileSeedKey,
    UdsClient,
    UdsTiming,
    VirtualCanNetwork,
)
from med17flasher.core.firmware import FirmwareImage
from med17flasher.core.trace import (
    BusRecorder,
    TraceFrame,
    analyze,
    read_asc,
    read_candump,
    read_csv,
    reassemble,
    write_candump,
)
from med17flasher.simulator import VirtualEcu, VirtualEcuConfig


def _flash_and_record(demo_profile):
    net = VirtualCanNetwork()
    recorder = BusRecorder(net)
    ecu = VirtualEcu(
        net.new_endpoint("ecu"), demo_profile,
        VirtualEcuConfig(security_algorithm="med17",
                         security_params=demo_profile.security.params,
                         max_block_length=0x0102),
    )
    ecu.start()
    tp = IsoTpLayer(net.new_endpoint("tester"),
                    IsoTpConfig(tx_id=0x7E0, rx_id=0x7E8, padding_byte=0x55))
    uds = UdsClient(tp, UdsTiming(p2=1.0, p2_star=2.0))
    img = FirmwareImage()
    img.add_segment(0x80040000, bytes((i * 7) & 0xFF for i in range(0x2000)))
    img.add_segment(0x80042000, bytes((0xC0 + (i & 0x1F)) & 0xFF for i in range(0x1000)))
    img.normalise()
    Flasher(uds, demo_profile, ProfileSeedKey(demo_profile)).flash(img)
    ecu.stop()
    return recorder.frames()


def test_analyze_recorded_flash(demo_profile):
    frames = _flash_and_record(demo_profile)
    assert len(frames) > 100
    report = analyze(frames, tx_id=0x7E0, rx_id=0x7E8)

    assert 0x02 in report.sessions  # programming session
    assert 0x11 in report.security_levels
    assert len(report.seed_key_pairs) == 1
    level, seed, key = report.seed_key_pairs[0]
    assert level == 0x11 and seed == b"\x11\x22\x33\x44"
    assert report.erase_routine == 0xFF00
    assert report.check_memory_routine == 0x0202
    addrs = [(b.address, b.size) for b in report.download_blocks]
    assert (0x80040000, 0x2000) in addrs
    assert (0x80042000, 0x1000) in addrs


def test_report_to_profile(demo_profile):
    frames = _flash_and_record(demo_profile)
    report = analyze(frames)
    profile = report.to_profile()
    assert profile.routines.erase_memory == 0xFF00
    assert profile.routines.check_memory == 0x0202
    assert len(profile.memory_map) == 2
    assert profile.memory_map[0].start == 0x80040000


def test_candump_roundtrip(demo_profile):
    frames = _flash_and_record(demo_profile)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "flash.log")
        write_candump(frames, path)
        reread = read_candump(path)
    assert len(reread) == len(frames)
    assert reread[0].arbitration_id == frames[0].arbitration_id
    assert reread[10].data == frames[10].data


def test_reassemble_multiframe():
    # a hand-built ISO-TP multi-frame message on 0x7E8
    frames = [
        TraceFrame(0.0, 0x7E8, bytes.fromhex("10 14 62 f1 90 57 56 57".replace(" ", ""))),
        TraceFrame(0.1, 0x7E8, bytes.fromhex("21 5a 5a 5a 31 4b 5a 41".replace(" ", ""))),
        TraceFrame(0.2, 0x7E8, bytes.fromhex("22 57 30 30 30 30 30 31".replace(" ", ""))),
    ]
    msgs = reassemble(frames, 0x7E8)
    assert len(msgs) == 1
    assert msgs[0][:3] == bytes.fromhex("62f190")
    assert len(msgs[0]) == 0x14


def test_read_asc():
    sample = (
        "date Tue\n"
        "0.001000 1 7E0 Tx d 8 02 10 03 55 55 55 55 55\n"
        "0.002000 1 7E8 Rx d 8 06 50 03 00 32 01 F4 55\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".asc", delete=False) as fh:
        fh.write(sample)
        path = fh.name
    try:
        frames = read_asc(path)
        assert len(frames) == 2
        assert frames[0].arbitration_id == 0x7E0
        assert frames[0].data[:3] == bytes.fromhex("021003")
    finally:
        os.remove(path)


def test_read_csv():
    sample = "# ts,id,data\n0.001,7E0,021003\n0.002,7E8,0650030032\n"
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
        fh.write(sample)
        path = fh.name
    try:
        frames = read_csv(path)
        assert len(frames) == 2
        assert frames[1].arbitration_id == 0x7E8
    finally:
        os.remove(path)


def test_extracted_pairs_feed_solver(demo_profile):
    # With a randomised-seed ECU we get several distinct pairs -> the solver can
    # unambiguously recover the algorithm from the trace.
    net = VirtualCanNetwork()
    BusRecorder(net)  # attaches to the network; frames are read back below
    ecu = VirtualEcu(
        net.new_endpoint("ecu"), demo_profile,
        VirtualEcuConfig(security_algorithm="med17",
                         security_params=demo_profile.security.params,
                         randomize_seed=True, max_block_length=0x0102),
    )
    ecu.start()
    tp = IsoTpLayer(net.new_endpoint("tester"),
                    IsoTpConfig(tx_id=0x7E0, rx_id=0x7E8, padding_byte=0x55))
    uds = UdsClient(tp, UdsTiming(p2=1.0, p2_star=2.0))
    # perform security access a few times to collect distinct pairs
    from med17flasher.seedkey import SeedKeyPair, SeedKeySolver, compute_key

    pairs = []
    for _ in range(4):
        uds.enter_programming_session()
        seed = uds.request_seed(0x11)
        key = compute_key("med17", seed, level=0x11, params=demo_profile.security.params)
        uds.send_key(0x12, key)
        pairs.append(SeedKeyPair(seed, key))
        uds.ecu_reset(0x01)
    ecu.stop()

    distinct = {p.seed for p in pairs}
    assert len(distinct) >= 2
    best = SeedKeySolver(pairs).best(level=0x11, wordlist=[0x1C5A36B7])
    assert best.is_full_match and best.algorithm == "med17"
