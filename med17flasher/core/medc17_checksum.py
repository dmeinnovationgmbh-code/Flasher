"""MEDC17 / EDC17 flash-checksum verification and correction.

After a legitimate calibration change a Bosch MED17/EDC17 image no longer
matches its internal checksums, so the ECU rejects it. This module recomputes
and corrects those checksums.

It understands the on-flash checksum descriptor format (each block carries one
or more checksum structures marked by the ``FADECAFE``/``CAFEAFFE`` magic pair),
and supports the three checksum algorithms: **CRC32** (zlib/IEEE-802.3),
**ADD32** and **ADD16**. CRC32 is corrected *directly* (no brute force) by
solving, over GF(2), which bits of a compensation dword drive the CRC to its
target - CRC is affine, so ``CRC(a xor b) = CRC(a) xor CRC(b) xor CRC(0)``.

This is an **independent, clean-room implementation** (MIT). It deliberately
does **not** implement RSA signature forgery or CVN "stock value" spoofing:
recomputing internal integrity checksums so a modified-but-honest image is
accepted is legitimate; defeating cryptographic authenticity or masking a
calibration's true CVN from diagnostics/emissions checks is not. The real CVN
can be computed and reported, never spoofed to a different value.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..logging_setup import get_logger

log = get_logger("core.medc17")

# Magic pair that marks a checksum structure: seed / expected.
SEED_MAGIC = 0xFADECAFE
EXPECTED_MAGIC = 0xCAFEAFFE
# CRC32 target is the bit-complement of the expected magic.
CRC_TARGET = (~EXPECTED_MAGIC) & 0xFFFFFFFF  # 0x35015001

ALGO_CRC32 = 0x00
ALGO_ADD32 = 0x01
ALGO_ADD16 = 0x10
ALGO_NAMES = {ALGO_CRC32: "CRC32", ALGO_ADD32: "ADD32", ALGO_ADD16: "ADD16"}

# "erased"/unprogrammed fill values -> skip such regions.
ERASED = (0xFFFFFFFF, 0xC3C3C3C3)


# --------------------------------------------------------------------------- #
# Address helpers
# --------------------------------------------------------------------------- #
def canonical(addr: int) -> int:
    """Collapse TriCore cached/uncached aliases to one canonical address."""

    return (addr & 0x0FFFFFFF) | 0x80000000


# --------------------------------------------------------------------------- #
# Checksum primitives
# --------------------------------------------------------------------------- #
def crc32(data: bytes) -> int:
    """zlib CRC-32 (reflected, init/xorout 0xFFFFFFFF)."""

    return zlib.crc32(data) & 0xFFFFFFFF


def add32(data: bytes, seed: int = SEED_MAGIC) -> int:
    """Sum of little-endian dwords with 32-bit wrap, initialised with ``seed``."""

    acc = seed & 0xFFFFFFFF
    for i in range(0, len(data) - (len(data) % 4), 4):
        acc = (acc + int.from_bytes(data[i : i + 4], "little")) & 0xFFFFFFFF
    return acc


def add16(data: bytes, seed: int = SEED_MAGIC) -> int:
    """ADD16: fold each dword (low16+high16); the final dword counts in full.

    The full-value final dword is what makes a single compensation dword able to
    drive the sum to any target.
    """

    acc = seed & 0xFFFFFFFF
    n = len(data) // 4
    for i in range(n):
        dw = int.from_bytes(data[i * 4 : i * 4 + 4], "little")
        if i == n - 1:
            acc = (acc + dw) & 0xFFFFFFFF
        else:
            acc = (acc + (dw & 0xFFFF) + (dw >> 16)) & 0xFFFFFFFF
    return acc


def compute(algo: int, data: bytes, seed: int = SEED_MAGIC) -> int:
    if algo == ALGO_CRC32:
        return crc32(data)
    if algo == ALGO_ADD32:
        return add32(data, seed)
    if algo == ALGO_ADD16:
        return add16(data, seed)
    raise ValueError(f"unknown checksum algorithm 0x{algo:02X}")


def target_for(algo: int) -> int:
    """The value the algorithm should produce over a correct region."""

    return CRC_TARGET if algo == ALGO_CRC32 else EXPECTED_MAGIC


# --------------------------------------------------------------------------- #
# GF(2) CRC patch solver
# --------------------------------------------------------------------------- #
def _gf2_solve(basis: List[int], rhs: int, nbits: int = 32) -> Optional[int]:
    """Solve ``XOR of selected basis[i] == rhs`` for the selection bits.

    ``basis[i]`` is the 32-bit CRC delta produced by setting input bit ``i``.
    Returns the 32-bit selection (input dword) or ``None`` if unsolvable.
    """

    # Equation per output bit o: sum_i x_i * bit_o(basis[i]) = bit_o(rhs)
    # Represent each equation as an int: low nbits = coefficients, bit nbits = rhs.
    rows = []
    for o in range(nbits):
        coeff = 0
        for i in range(nbits):
            if (basis[i] >> o) & 1:
                coeff |= 1 << i
        rows.append(coeff | (((rhs >> o) & 1) << nbits))

    # Gaussian elimination over GF(2).
    pivots = []
    r = 0
    for col in range(nbits):
        pivot = None
        for k in range(r, len(rows)):
            if (rows[k] >> col) & 1:
                pivot = k
                break
        if pivot is None:
            continue
        rows[r], rows[pivot] = rows[pivot], rows[r]
        for k in range(len(rows)):
            if k != r and (rows[k] >> col) & 1:
                rows[k] ^= rows[r]
        pivots.append(col)
        r += 1

    # Consistency: any row with all-zero coeffs but rhs=1 -> no solution.
    for k in range(len(rows)):
        if (rows[k] & ((1 << nbits) - 1)) == 0 and (rows[k] >> nbits) & 1:
            return None

    x = 0
    for idx, col in enumerate(pivots):
        if (rows[idx] >> nbits) & 1:
            x |= 1 << col
    return x


def solve_crc32_patch(region: bytes, patch_offset: int, target: int = CRC_TARGET) -> bytes:
    """Return the 4 bytes to write at ``patch_offset`` (LE dword) in ``region``
    so that ``crc32(region)`` becomes ``target``.

    Raises ``ValueError`` if the patch location cannot reach the target (the
    compensation dword is rank-deficient - pick another offset).
    """

    if patch_offset < 0 or patch_offset + 4 > len(region):
        raise ValueError("patch offset out of range")

    base = bytearray(region)
    base[patch_offset : patch_offset + 4] = b"\x00\x00\x00\x00"
    crc_base = crc32(bytes(base))

    basis = []
    for bit in range(32):
        probe = bytearray(base)
        probe[patch_offset : patch_offset + 4] = (1 << bit).to_bytes(4, "little")
        basis.append(crc32(bytes(probe)) ^ crc_base)

    x = _gf2_solve(basis, target ^ crc_base)
    if x is None:
        raise ValueError("CRC target unreachable with a dword at this offset")
    return x.to_bytes(4, "little")


def correct_add(region: bytes, comp_offset: int, algo: int,
                target: int = EXPECTED_MAGIC, seed: int = SEED_MAGIC) -> bytes:
    """Return the 4 compensation bytes so the ADD checksum hits ``target``.

    Adjusts the dword at ``comp_offset`` (which the algorithm counts in full).
    """

    patched = bytearray(region)
    patched[comp_offset : comp_offset + 4] = b"\x00\x00\x00\x00"
    current = compute(algo, bytes(patched), seed)
    delta = (target - current) & 0xFFFFFFFF
    return delta.to_bytes(4, "little")


# --------------------------------------------------------------------------- #
# Descriptor model
# --------------------------------------------------------------------------- #
@dataclass
class ChecksumRegion:
    struct_offset: int  # file offset of the 32-byte checksum structure
    start_mem: int
    end_mem: int
    algo: int
    seed: int
    expected: int
    start_file: int = 0
    end_file: int = 0

    @property
    def algo_name(self) -> str:
        return ALGO_NAMES.get(self.algo, f"0x{self.algo:02X}")

    @property
    def length(self) -> int:
        return self.end_file - self.start_file


@dataclass
class ChecksumBlock:
    header_offset: int  # file offset of the block header
    block_type: int
    regions: List[ChecksumRegion] = field(default_factory=list)


@dataclass
class RegionResult:
    region: ChecksumRegion
    computed: int
    target: int
    ok: bool


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def _u32(data: bytes, off: int) -> int:
    return int.from_bytes(data[off : off + 4], "little")


def find_blocks(data: bytes) -> List[ChecksumBlock]:
    """Locate checksum blocks by their FADECAFE/CAFEAFFE magic pair.

    A checksum *structure* holds the seed at +0x0C and the expected value at
    +0x10, so the magic pair appears as ``FADECAFE CAFEAFFE`` at struct+0x0C.
    The block header starts 0x34 before its first structure. This is a best-
    effort parse of the documented layout - validate against a real dump.
    """

    magic = SEED_MAGIC.to_bytes(4, "little") + EXPECTED_MAGIC.to_bytes(4, "little")
    blocks: List[ChecksumBlock] = []
    seen_headers = set()
    pos = 0
    while True:
        idx = data.find(magic, pos)
        if idx < 0:
            break
        pos = idx + 1
        struct_off = idx - 0x0C  # magic sits at struct+0x0C
        header_off = struct_off - 0x34  # structures begin at header+0x34
        if header_off < 0 or header_off in seen_headers:
            continue
        block = _parse_block(data, header_off)
        if block and block.regions:
            seen_headers.add(header_off)
            blocks.append(block)
    return blocks


def _parse_block(data: bytes, header_off: int) -> Optional[ChecksumBlock]:
    if header_off + 0x34 > len(data):
        return None
    block_type = data[header_off]
    count = _u32(data, header_off + 0x2C)
    if not (1 <= count <= 8):
        return None
    # The block's canonical base address is taken from its first region so file
    # offsets can be derived (best effort: assume region data is contiguous from
    # the block header in the file).
    regions: List[ChecksumRegion] = []
    base_struct = header_off + 0x34
    for i in range(count):
        so = base_struct + i * 32
        if so + 32 > len(data):
            break
        seed = _u32(data, so + 0x0C)
        expected = _u32(data, so + 0x10)
        if seed != SEED_MAGIC or expected != EXPECTED_MAGIC:
            continue
        start_mem = _u32(data, so + 0x04)
        end_mem = _u32(data, so + 0x08)
        algo = int.from_bytes(data[so + 0x1C : so + 0x1E], "little")
        if (start_mem in ERASED) or (end_mem in ERASED) or end_mem <= start_mem:
            continue
        regions.append(ChecksumRegion(so, start_mem, end_mem, algo, seed, expected))
    if not regions:
        return None

    # Map memory addresses to file offsets. Use the lowest region start as the
    # block's canonical base, mapped to a file anchor at the block header.
    base_mem = min(canonical(r.start_mem) for r in regions)
    for r in regions:
        r.start_file = canonical(r.start_mem) - base_mem + header_off
        r.end_file = canonical(r.end_mem) - base_mem + header_off
        # clamp to the file
        r.end_file = min(r.end_file, len(data))
    return ChecksumBlock(header_off, block_type, regions)


# --------------------------------------------------------------------------- #
# Verify / correct
# --------------------------------------------------------------------------- #
def verify(data: bytes) -> List[RegionResult]:
    results: List[RegionResult] = []
    for block in find_blocks(data):
        for region in block.regions:
            if region.start_file < 0 or region.end_file > len(data) or region.length <= 0:
                continue
            chunk = data[region.start_file : region.end_file]
            computed = compute(region.algo, chunk, region.seed)
            tgt = target_for(region.algo)
            results.append(RegionResult(region, computed, tgt, computed == tgt))
    return results


def correct(data: bytes, *, comp_at_region_end: int = 4) -> Tuple[bytes, List[RegionResult]]:
    """Correct all discoverable checksums; return (new_bytes, before_results).

    The compensation dword is taken as the last dword of each region
    (``comp_at_region_end`` = 4). For CRC32 this dword is solved via GF(2); for
    ADD it is adjusted arithmetically. Provide the exact adjust-slot offset for
    a specific ECU if it differs.
    """

    out = bytearray(data)
    before = verify(bytes(out))
    for res in before:
        r = res.region
        chunk = bytearray(out[r.start_file : r.end_file])
        comp_off = len(chunk) - comp_at_region_end
        if comp_off < 0:
            continue
        try:
            if r.algo == ALGO_CRC32:
                patch = solve_crc32_patch(bytes(chunk), comp_off, CRC_TARGET)
            else:
                patch = correct_add(bytes(chunk), comp_off, r.algo, EXPECTED_MAGIC, r.seed)
        except ValueError as exc:
            log.warning("cannot correct region @0x%08X (%s): %s",
                        r.start_mem, r.algo_name, exc)
            continue
        chunk[comp_off : comp_off + 4] = patch
        out[r.start_file : r.end_file] = chunk
    return bytes(out), before


def compute_cvn(regions: List[Tuple[int, int]], data: bytes,
                base_mem: int, base_file: int) -> int:
    """Compute the Calibration Verification Number (zlib CRC-32 over regions).

    Read-only: reports the CVN for the actual content. Never spoofs it.
    """

    parts = []
    for start_mem, end_mem in regions:
        sf = canonical(start_mem) - base_mem + base_file
        ef = canonical(end_mem) - base_mem + base_file
        parts.append(data[sf:ef])
    return zlib.crc32(b"".join(parts)) & 0xFFFFFFFF
