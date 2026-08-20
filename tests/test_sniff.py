"""Tests for the live UDS decode used by `med17flasher sniff`.

Two levels: a deterministic hand-built multi-talker frame stream feeding the
:class:`LiveUdsTracker`, and an end-to-end pass over a *recorded simulated
flash* (the same rig `sniff` uses on real hardware, minus the adapter).
"""

from __future__ import annotations

import os
import tempfile

from med17flasher import cli
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
    LiveUdsTracker,
    TraceFrame,
    _DirAssembler,
)
from med17flasher.simulator import VirtualEcu, VirtualEcuConfig


def _sf(arb, payload):
    """Single-frame ISO-TP TraceFrame."""
    body = bytes([len(payload)]) + bytes(payload)
    return TraceFrame(0.0, arb, body.ljust(8, b"\x55"))


def _frames(arb, payload):
    """Full multi-frame ISO-TP TraceFrame list for a payload of any length."""
    payload = bytes(payload)
    if len(payload) <= 7:
        return [_sf(arb, payload)]
    first = bytes([0x10 | (len(payload) >> 8), len(payload) & 0xFF]) + payload[:6]
    out = [TraceFrame(0.0, arb, first.ljust(8, b"\x55"))]
    rest, sn = payload[6:], 1
    while rest:
        chunk = rest[:7]
        out.append(TraceFrame(0.0, arb, (bytes([0x20 | (sn & 0x0F)]) + chunk).ljust(8, b"\x55")))
        rest = rest[7:]
        sn += 1
    return out


def test_dir_assembler_multiframe_roundtrip():
    asm = _DirAssembler()
    payload = bytes(range(20))
    out = None
    for f in _frames(0x7E0, payload):
        out = asm.feed(f.data) or out
    assert out == payload


def test_tracker_decodes_full_flow():
    t = LiveUdsTracker(transfer_every=4)
    kinds = []

    def push(arb, payload):
        for f in _frames(arb, payload):
            for ev in t.feed(f):
                kinds.append((ev.kind, ev.detail))

    # programming session
    push(0x7E0, [0x10, 0x02])
    push(0x7E8, [0x50, 0x02])
    # security access: requestSeed -> seed, sendKey
    push(0x7E0, [0x27, 0x11])
    push(0x7E8, [0x67, 0x11, 0xAA, 0xBB, 0xCC, 0xDD])
    push(0x7E0, [0x27, 0x12, 0x01, 0x02, 0x03, 0x04])
    push(0x7E8, [0x67, 0x12])
    # erase routine (ALFID + addr + size)
    push(0x7E0, [0x31, 0x01, 0xFF, 0x00, 0x44, 0x80, 0x04, 0x00, 0x00,
                 0x00, 0x00, 0x20, 0x00])
    # request download 0x80040000 / 0x2000
    push(0x7E0, [0x34, 0x00, 0x44, 0x80, 0x04, 0x00, 0x00, 0x00, 0x00, 0x20, 0x00])
    # 10 transfers -> progress at 4 and 8
    for _ in range(10):
        push(0x7E0, [0x36, 0x01])
    push(0x7E0, [0x37])
    push(0x7E0, [0x11, 0x01])

    kset = {k for k, _ in kinds}
    assert {"session", "seed", "key", "seedkey", "erase", "download",
            "transfer", "exit", "reset"} <= kset

    seedkey = next(d for k, d in kinds if k == "seedkey")
    assert seedkey["seed"] == "aabbccdd"
    assert seedkey["key"] == "01020304"
    dl = next(d for k, d in kinds if k == "download")
    assert dl["address"] == 0x80040000 and dl["size"] == 0x2000
    # transfer_every=4 over 10 transfers -> reported at 4 and 8
    prog = [d["transfers"] for k, d in kinds if k == "transfer"]
    assert prog == [4, 8]


def test_tracker_ignores_response_pending():
    t = LiveUdsTracker()
    evs = []
    for f in _frames(0x7E8, [0x7F, 0x34, 0x78]):  # responsePending
        evs += t.feed(f)
    assert evs == []
    for f in _frames(0x7E8, [0x7F, 0x27, 0x35]):  # invalidKey -> surfaced
        evs += t.feed(f)
    assert any(e.kind == "nrc" for e in evs)


def _record_flash(demo_profile):
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


def test_tracker_over_recorded_flash(demo_profile):
    """Feeding a real recorded flash frame-by-frame yields the same seed/key
    and download blocks the offline analyzer finds."""
    frames = _record_flash(demo_profile)
    t = LiveUdsTracker()
    events = []
    for f in frames:
        events.extend(t.feed(f))

    seedkeys = [e for e in events if e.kind == "seedkey"]
    assert len(seedkeys) == 1
    assert seedkeys[0].detail["seed"] == "11223344"
    downloads = {(e.detail["address"], e.detail["size"])
                 for e in events if e.kind == "download"}
    assert (0x80040000, 0x2000) in downloads
    assert (0x80042000, 0x1000) in downloads


def test_cli_sniff_analyzes_recorded_log(demo_profile, capsys):
    """`sniff` on a candump log path (via --analyze pathway) is covered by
    analyze-trace; here we drive the simulator sniff end-to-end by pre-recording
    a flash to a candump and running the analyzer the way sniff does."""
    frames = _record_flash(demo_profile)
    with tempfile.TemporaryDirectory() as d:
        log = os.path.join(d, "sniff.log")
        from med17flasher.core.trace import write_candump

        write_candump(frames, log)
        rc = cli.main(["analyze-trace", log, "--emit-profile",
                       os.path.join(d, "p.yaml")])
        assert rc == 0
        out = capsys.readouterr().out
        assert "seed=11223344" in out
        assert os.path.isfile(os.path.join(d, "p.yaml"))


def test_guess_uds_ids_prefers_8_apart_pair():
    from med17flasher.cli import _guess_uds_ids

    seen = {0x123: 5, 0x7E0: 40, 0x7E8: 38, 0x201: 100}
    tx, rx = _guess_uds_ids(seen)
    assert (tx, rx) == (0x7E0, 0x7E8)  # the diagnostic pair, not the busiest id


def test_guess_uds_ids_falls_back_to_busiest_two():
    from med17flasher.cli import _guess_uds_ids

    seen = {0x201: 100, 0x305: 60, 0x123: 5}
    tx, rx = _guess_uds_ids(seen)
    assert {tx, rx} == {0x201, 0x305}


def test_cli_sniff_simulator_runs_and_reports_empty(capsys):
    # No other talker on the fresh sim bus -> zero frames, clean exit + hint.
    with tempfile.TemporaryDirectory() as d:
        rc = cli.main(["sniff", "--simulator", "--seconds", "0.05",
                       "-o", os.path.join(d, "s.log"), "--no-analyze"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "sendet NICHTS" in out
        assert os.path.isfile(os.path.join(d, "s.log"))
