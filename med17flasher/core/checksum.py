"""Checksum / CRC helpers used for firmware verification and transfer routines.

MED17 flash containers are protected by several kinds of check value depending
on the region: a plain 32-bit additive sum, a 16-bit CCITT CRC over program
segments, and the standard 32-bit CRC used by many ``checkMemory`` routines.
All of them live here so the flash sequence and the simulator agree byte for
byte.
"""

from __future__ import annotations

import zlib
from typing import Iterable

# --------------------------------------------------------------------------- #
# CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection)
# --------------------------------------------------------------------------- #
_CRC16_TABLE = []


def _build_crc16_table() -> None:
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
        _CRC16_TABLE.append(crc)


_build_crc16_table()


def crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE, as used by several MED17 program checks."""

    crc = init
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC16_TABLE[((crc >> 8) ^ byte) & 0xFF]
    return crc & 0xFFFF


# --------------------------------------------------------------------------- #
# CRC-32 (IEEE 802.3, the one used by zlib and most checkMemory routines)
# --------------------------------------------------------------------------- #
def crc32(data: bytes, init: int = 0) -> int:
    """Standard CRC-32 (reflected, poly 0xEDB88320)."""

    return zlib.crc32(data, init) & 0xFFFFFFFF


# --------------------------------------------------------------------------- #
# Additive / summation checksums
# --------------------------------------------------------------------------- #
def sum32(data: bytes) -> int:
    """32-bit wrapping additive sum of every byte."""

    return sum(data) & 0xFFFFFFFF


def sum16(data: bytes) -> int:
    """16-bit wrapping additive sum of every byte."""

    return sum(data) & 0xFFFF


def word_sum32(data: bytes, *, big_endian: bool = False) -> int:
    """Sum the payload interpreted as 32-bit words (padded with zeros)."""

    order = "big" if big_endian else "little"
    total = 0
    padded = data + b"\x00" * ((-len(data)) % 4)
    for i in range(0, len(padded), 4):
        total = (total + int.from_bytes(padded[i : i + 4], order)) & 0xFFFFFFFF
    return total


def xor_checksum(data: bytes) -> int:
    """Simple 8-bit XOR of every byte (used by some legacy blocks)."""

    acc = 0
    for byte in data:
        acc ^= byte
    return acc & 0xFF


# --------------------------------------------------------------------------- #
# Dispatch by name so profiles/routines can pick an algorithm declaratively.
# --------------------------------------------------------------------------- #
_ALGOS = {
    "crc16": lambda d: crc16_ccitt(d),
    "crc16_ccitt": lambda d: crc16_ccitt(d),
    "crc32": lambda d: crc32(d),
    "sum16": sum16,
    "sum32": sum32,
    "word_sum32": word_sum32,
    "xor": xor_checksum,
}


def compute(algorithm: str, data: bytes) -> int:
    """Compute a named checksum over ``data``."""

    try:
        return _ALGOS[algorithm.lower()](data)
    except KeyError:
        raise ValueError(f"unknown checksum algorithm {algorithm!r}") from None


def available_algorithms() -> Iterable[str]:
    return tuple(sorted(_ALGOS))
