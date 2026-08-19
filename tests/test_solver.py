"""Seed/key solver tests: recover an algorithm from captured pairs."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from med17flasher.seedkey import compute_key
from med17flasher.seedkey.solver import (
    SeedKeyPair,
    SeedKeySolver,
    load_pairs,
    load_wordlist,
    solve_add,
    solve_xor,
)


def _pairs_for(algorithm, params, seeds, level=0):
    return [
        SeedKeyPair(seed, compute_key(algorithm, seed, level=level, params=params))
        for seed in seeds
    ]


SEEDS = [
    bytes.fromhex("11223344"),
    bytes.fromhex("deadbeef"),
    bytes.fromhex("00000001"),
    bytes.fromhex("a5a5a5a5"),
]


def test_recover_xor():
    pairs = _pairs_for("xor", {"k": 0x1234ABCD}, SEEDS)
    solver = SeedKeySolver(pairs)
    best = solver.best()
    assert best.is_full_match
    assert best.algorithm == "xor"
    assert int(best.params["k"], 0) == 0x1234ABCD


def test_recover_add():
    pairs = _pairs_for("add", {"k": 0x5A5A5A5A}, SEEDS)
    best = SeedKeySolver(pairs).best()
    assert best.is_full_match and best.algorithm == "add"
    assert int(best.params["k"], 0) == 0x5A5A5A5A


def test_recover_sum():
    pairs = _pairs_for("sum", {"k": 0x3D}, SEEDS)
    results = SeedKeySolver(pairs).solve()
    assert any(r.algorithm == "sum" and r.is_full_match for r in results)


def test_recover_med17_with_wordlist():
    params = {"k": 0x1C5A36B7, "rounds": 5, "shift": 5}
    pairs = _pairs_for("med17", params, SEEDS, level=0x11)
    # provide the real constant among some decoys in the wordlist
    wordlist = [0x11111111, 0x1C5A36B7, 0xDEADBEEF]
    best = SeedKeySolver(pairs).best(level=0x11, wordlist=wordlist)
    assert best.is_full_match
    assert best.algorithm == "med17"
    assert best.params["rounds"] == 5 and best.params["shift"] == 5


def test_no_match_without_wordlist_for_structured():
    params = {"k": 0x1C5A36B7, "rounds": 5, "shift": 5}
    pairs = _pairs_for("med17", params, SEEDS, level=0x11)
    # No wordlist -> structured algorithms cannot be brute forced; and the
    # direct solvers won't match a med17 output. So: no full match.
    best = SeedKeySolver(pairs).best(level=0x11)
    assert best is None or not best.is_full_match


def test_verify():
    pairs = _pairs_for("xor", {"k": 0xCAFEBABE}, SEEDS)
    solver = SeedKeySolver(pairs)
    assert solver.verify("xor", {"k": "0xCAFEBABE"})
    assert not solver.verify("xor", {"k": "0x00000000"})


def test_direct_solvers_reject_inconsistent():
    pairs = [
        SeedKeyPair(bytes.fromhex("11111111"), bytes.fromhex("22222222")),
        SeedKeyPair(bytes.fromhex("11111111"), bytes.fromhex("33333333")),  # contradicts
    ]
    assert solve_xor(pairs) is None
    assert solve_add(pairs) is None


def test_load_pairs_json_and_text():
    with tempfile.TemporaryDirectory() as d:
        jpath = os.path.join(d, "p.json")
        with open(jpath, "w") as fh:
            json.dump([{"seed": "11223344", "key": "8369ee49"}], fh)
        pj = load_pairs(jpath)
        assert pj[0].seed == bytes.fromhex("11223344")

        tpath = os.path.join(d, "p.txt")
        with open(tpath, "w") as fh:
            fh.write("# comment\n11223344 8369ee49\ndeadbeef,01020304\n")
        pt = load_pairs(tpath)
        assert len(pt) == 2
        assert pt[1].key == bytes.fromhex("01020304")

        wpath = os.path.join(d, "w.txt")
        with open(wpath, "w") as fh:
            fh.write("0x1C5A36B7\n# c\n305419896\n")
        wl = load_wordlist(wpath)
        assert 0x1C5A36B7 in wl and 305419896 in wl


def test_cli_seedkey_solve(capsys):
    from med17flasher import cli

    # capture a real pair from the med17 reference algorithm, then solve it
    seed = "11223344"
    key = compute_key("xor", bytes.fromhex(seed), params={"k": 0x1234ABCD}).hex()
    rc = cli.main(["seedkey-solve", "--pair", f"{seed}:{key}",
                   "--pair", f"deadbeef:{compute_key('xor', bytes.fromhex('deadbeef'), params={'k':0x1234ABCD}).hex()}"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "RECOVERED" in out and "xor" in out
