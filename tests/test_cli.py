"""CLI smoke tests driving med17flasher.cli.main()."""

from __future__ import annotations

import os
import tempfile


from med17flasher import cli


def _make_fw(path, base_len=0x2000, cal_len=0x1000):
    data = bytes((i * 7) & 0xFF for i in range(base_len)) + bytes(
        (0xC0 + (i & 0x1F)) & 0xFF for i in range(cal_len)
    )
    with open(path, "wb") as fh:
        fh.write(data)
    return data


def test_cli_backends(capsys):
    assert cli.main(["backends"]) == 0
    out = capsys.readouterr().out
    assert "virtual" in out
    assert "med17" in out


def test_cli_seedkey(capsys):
    rc = cli.main(["seedkey", "11223344", "--algorithm", "med17", "--level", "0x11",
                   "--param", "k=0x1C5A36B7", "--param", "rounds=5", "--param", "shift=5"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    from med17flasher.seedkey import compute_key

    expected = compute_key("med17", bytes.fromhex("11223344"), level=0x11,
                           params={"k": "0x1C5A36B7", "rounds": "5", "shift": "5"}).hex()
    assert out == expected


def test_cli_profile(capsys):
    assert cli.main(["profile", "--profile", "config/med17_7_5_demo.yaml"]) == 0
    out = capsys.readouterr().out
    assert '"MED17.7.5"' in out


def test_cli_flash_simulator(capsys):
    with tempfile.TemporaryDirectory() as d:
        fw = os.path.join(d, "fw.bin")
        _make_fw(fw)
        rc = cli.main([
            "flash", "--simulator",
            "--profile", "config/med17_7_5_demo.yaml",
            "--base", "0x80040000",
            fw,
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "SUCCESS" in out
        assert "ASW" in out and "CAL" in out


def test_cli_flash_dry_run(capsys):
    with tempfile.TemporaryDirectory() as d:
        fw = os.path.join(d, "fw.bin")
        _make_fw(fw)
        rc = cli.main([
            "flash", "--simulator", "--dry-run",
            "--profile", "config/med17_7_5_demo.yaml",
            "--base", "0x80040000", fw,
        ])
        assert rc == 0
        out = capsys.readouterr().out
        # --dry-run now runs the live rehearsal (preflight): it must have
        # entered the programming session and completed Security Access, and
        # written nothing. The old behaviour only printed the planned blocks.
        assert "Preflight" in out
        assert "security access granted" in out


def test_cli_identify_simulator(capsys):
    rc = cli.main(["identify", "--simulator", "--profile", "config/med17_7_5_demo.yaml"])
    assert rc == 0
    assert "DID 0x" in capsys.readouterr().out


def test_cli_analyze_firmware(capsys):
    with tempfile.TemporaryDirectory() as d:
        fw = os.path.join(d, "dump.bin")
        blob = bytearray(b"\xff" * 0x8000)
        blob[0x1000:0x1800] = bytes((i * 3) & 0xFF for i in range(0x800))
        blob[0x5000:0x5400] = bytes((0x40 + (i & 0x1F)) & 0xFF for i in range(0x400))
        with open(fw, "wb") as fh:
            fh.write(blob)
        prof = os.path.join(d, "out.yaml")
        rc = cli.main(["analyze-firmware", fw, "--base", "0x80000000", "--emit-profile", prof])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Detected 2 region(s)" in out
        assert os.path.isfile(prof)


def test_cli_analyze_trace(capsys):
    # a minimal hand-written candump with one session-control exchange
    with tempfile.TemporaryDirectory() as d:
        log = os.path.join(d, "t.log")
        with open(log, "w") as fh:
            fh.write("(0.001000) can0 7E0#0210025555555555\n")
            fh.write("(0.002000) can0 7E8#06500300320 1F455\n".replace(" ", ""))
        rc = cli.main(["analyze-trace", log])
        assert rc == 0
        assert "sessions" in capsys.readouterr().out


def test_cli_ingest(capsys):
    with tempfile.TemporaryDirectory() as d:
        # a firmware dump and a seed/key pairs file
        with open(os.path.join(d, "fw.bin"), "wb") as fh:
            fh.write(b"\xff" * 0x100 + b"\x5a" * 0x200 + b"\xff" * 0x2000 + b"\xa5" * 0x100)
        with open(os.path.join(d, "pairs.txt"), "w") as fh:
            from med17flasher.seedkey import compute_key

            for s in ("11223344", "deadbeef", "00000001"):
                k = compute_key("xor", bytes.fromhex(s), params={"k": 0x1234ABCD}).hex()
                fh.write(f"{s} {k}\n")
        rc = cli.main(["ingest", d, "--base", "0x80000000"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Ingest complete" in out
        assert "recovered seed/key" in out  # 3 distinct-seed pairs -> xor recovered
