"""MEDC17 checksum engine tests: algorithms, GF(2) CRC solver, discovery,
verify/correct round-trips."""

from __future__ import annotations

import os
import struct
import tempfile

import pytest

from med17flasher import cli
from med17flasher.core import medc17_checksum as mc


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #
def test_crc_target_is_complement():
    assert mc.CRC_TARGET == (~mc.EXPECTED_MAGIC) & 0xFFFFFFFF == 0x35015001


def test_gf2_crc_solver_hits_target():
    for i in range(4):
        region = bytearray(os.urandom(2048 + i * 512))
        off = 0x40 * (i + 1)
        patch = mc.solve_crc32_patch(bytes(region), off, mc.CRC_TARGET)
        region[off : off + 4] = patch
        assert mc.crc32(bytes(region)) == mc.CRC_TARGET


def test_gf2_crc_solver_arbitrary_target():
    region = bytearray(os.urandom(1000))
    patch = mc.solve_crc32_patch(bytes(region), 100, 0xDEADBEEF)
    region[100:104] = patch
    assert mc.crc32(bytes(region)) == 0xDEADBEEF


def test_add32_compensation():
    region = bytearray(os.urandom(1024))
    comp = len(region) - 4
    region[comp : comp + 4] = mc.correct_add(bytes(region), comp, mc.ALGO_ADD32,
                                             mc.EXPECTED_MAGIC)
    assert mc.add32(bytes(region)) == mc.EXPECTED_MAGIC


def test_add16_compensation():
    region = bytearray(os.urandom(1024))
    comp = len(region) - 4
    region[comp : comp + 4] = mc.correct_add(bytes(region), comp, mc.ALGO_ADD16,
                                             mc.EXPECTED_MAGIC)
    assert mc.add16(bytes(region)) == mc.EXPECTED_MAGIC


def test_canonical_alias():
    assert mc.canonical(0xA0100000) == mc.canonical(0x80100000) == 0x80100000


# --------------------------------------------------------------------------- #
# discovery + verify + correct (synthetic block)
# --------------------------------------------------------------------------- #
def _build_block(algo, H=0x100, L=0x400):
    total = H + L + 0x100
    data = bytearray(os.urandom(total))
    start_mem = 0x80100000
    end_mem = start_mem + L
    data[H] = 0x40  # block_type ASW
    data[H + 1] = 0x00
    data[H + 3] = 0x00
    struct.pack_into("<I", data, H + 0x2C, 1)  # count
    so = H + 0x34
    struct.pack_into("<I", data, so + 0x04, start_mem)
    struct.pack_into("<I", data, so + 0x08, end_mem)
    struct.pack_into("<I", data, so + 0x0C, mc.SEED_MAGIC)
    struct.pack_into("<I", data, so + 0x10, mc.EXPECTED_MAGIC)
    struct.pack_into("<H", data, so + 0x1C, algo)
    return bytes(data)


@pytest.mark.parametrize("algo", [mc.ALGO_CRC32, mc.ALGO_ADD32, mc.ALGO_ADD16])
def test_discover_verify_correct(algo):
    img = _build_block(algo)
    blocks = mc.find_blocks(img)
    assert len(blocks) == 1
    assert len(blocks[0].regions) == 1
    assert blocks[0].regions[0].algo == algo

    before = mc.verify(img)
    assert len(before) == 1 and not before[0].ok  # random data -> wrong checksum

    fixed, _ = mc.correct(img)
    after = mc.verify(fixed)
    assert after[0].ok
    # exactly the compensation dword changed
    changed = [i for i in range(len(img)) if img[i] != fixed[i]]
    assert len(changed) <= 4


def test_no_blocks_in_random_data():
    assert mc.verify(os.urandom(4096)) == []


def test_cli_checksum_verify_and_correct(capsys):
    img = _build_block(mc.ALGO_CRC32)
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.bin")
        out = os.path.join(d, "out.bin")
        with open(src, "wb") as fh:
            fh.write(img)
        # verify -> mismatch -> rc 2
        rc = cli.main(["checksum", "verify", src])
        assert rc == 2
        assert "BAD" in capsys.readouterr().out
        # correct -> rc 0
        rc = cli.main(["checksum", "correct", src, "-o", out])
        assert rc == 0
        # re-verify the output -> all OK -> rc 0
        rc = cli.main(["checksum", "verify", out])
        assert rc == 0
        assert "BAD" not in capsys.readouterr().out
