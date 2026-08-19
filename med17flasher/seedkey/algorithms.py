"""Reference seed -> key algorithms.

These are complete, deterministic and self-consistent (the ECU simulator uses
the very same code to validate a key), which makes the whole tool-chain
end-to-end testable. They model the *shapes* of algorithms used by real
diagnostic security access - XOR/add masks, and an LFSR-style bit-mixing round
function parameterised by constants - but they are **not** any manufacturer's
secret routine.

To flash a real ECU, register that ECU's routine as a plug-in (see
:func:`med17flasher.seedkey.base.load_plugin`) or select the matching algorithm
and supply the correct ``params`` in the ECU profile / seed-key store.
"""

from __future__ import annotations

from typing import Any

from .base import SeedKeyAlgorithm, register


def _as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, str):
        return int(value, 0)
    return int(value)


class XorAlgorithm(SeedKeyAlgorithm):
    name = "xor"
    description = "key = seed XOR constant (params: k)"

    def compute(self, seed, *, level=0, params=None):
        params = params or {}
        k = _as_int(params.get("k"), 0x1234ABCD)
        length = int(params.get("length", len(seed) or 4))
        value = self.seed_to_int(seed) ^ k
        return self.int_to_key(value, length)


class AddAlgorithm(SeedKeyAlgorithm):
    name = "add"
    description = "key = (seed + constant) mod 2^n (params: k)"

    def compute(self, seed, *, level=0, params=None):
        params = params or {}
        k = _as_int(params.get("k"), 0x5A5A5A5A)
        length = int(params.get("length", len(seed) or 4))
        value = (self.seed_to_int(seed) + k) & ((1 << (8 * length)) - 1)
        return self.int_to_key(value, length)


class SumAlgorithm(SeedKeyAlgorithm):
    name = "sum"
    description = "byte-wise (seed[i] + k) & 0xFF (params: k)"

    def compute(self, seed, *, level=0, params=None):
        params = params or {}
        k = _as_int(params.get("k"), 0x3D) & 0xFF
        return bytes(((b + k) & 0xFF) for b in seed) or bytes([k])


class Med17Algorithm(SeedKeyAlgorithm):
    """An LFSR / Galois feedback transform in the style of MED17 security access.

    The transform is fully parameterised so the same class serves many ECUs:

    * ``k``       - 32-bit feedback constant
    * ``rounds``  - number of shift/feedback iterations (default 5)
    * ``shift``   - final rotate-left amount (default 5)
    * ``xor_out`` - constant XORed into the result (default 0)
    * ``length``  - key length in bytes (default = seed length or 4)

    It is deterministic and reproducible; supply the constants that match your
    ECU. It is *not* a captured production routine.
    """

    name = "med17"
    description = "LFSR-style MED17 seed/key (params: k, rounds, shift, xor_out)"

    def compute(self, seed, *, level=0, params=None):
        params = params or {}
        length = int(params.get("length", len(seed) or 4))
        bits = 8 * length
        mask = (1 << bits) - 1
        top_bit = 1 << (bits - 1)

        k = _as_int(params.get("k"), 0x1C5A36B7) & mask
        rounds = int(params.get("rounds", 5))
        shift = int(params.get("shift", 5)) % bits
        xor_out = _as_int(params.get("xor_out"), 0) & mask
        # An optional per-level tweak so different access levels give different
        # keys from the same seed, as production ECUs often do.
        k ^= (level * 0x01010101) & mask

        key = self.seed_to_int(seed) & mask
        for _ in range(rounds):
            if key & top_bit:
                key = ((key << 1) ^ k) & mask
            else:
                key = (key << 1) & mask
        key = (key + k) & mask
        if shift:
            key = ((key << shift) | (key >> (bits - shift))) & mask
        key ^= xor_out
        return self.int_to_key(key, length)


class VagCrcAlgorithm(SeedKeyAlgorithm):
    """A CRC-mix style transform (another common seed/key shape).

    ``key = crc-style-mix(seed, poly) ^ app_key``. Parameters: ``poly``,
    ``init``, ``app_key``, ``length``.
    """

    name = "vag_crc"
    description = "CRC-mix seed/key (params: poly, init, app_key)"

    def compute(self, seed, *, level=0, params=None):
        params = params or {}
        length = int(params.get("length", len(seed) or 4))
        bits = 8 * length
        mask = (1 << bits) - 1
        poly = _as_int(params.get("poly"), 0x04C11DB7) & mask
        crc = _as_int(params.get("init"), 0xFFFFFFFF) & mask
        app_key = _as_int(params.get("app_key"), 0x00000000) & mask
        top = 1 << (bits - 1)

        for byte in seed:
            crc ^= (byte << (bits - 8)) & mask
            for _ in range(8):
                if crc & top:
                    crc = ((crc << 1) ^ poly) & mask
                else:
                    crc = (crc << 1) & mask
        crc ^= app_key
        return self.int_to_key(crc, length)


class FixedAlgorithm(SeedKeyAlgorithm):
    """Returns a constant key regardless of seed.

    Useful for ECUs/sessions whose 'seed' is zero (security disabled) or for
    bench mocking. Parameter: ``key`` (hex string or int) and ``length``.
    """

    name = "fixed"
    description = "constant key (params: key)"

    def compute(self, seed, *, level=0, params=None):
        params = params or {}
        raw = params.get("key", "0x00000000")
        length = int(params.get("length", len(seed) or 4))
        if isinstance(raw, str) and all(c in "0123456789abcdefABCDEFxX" for c in raw):
            value = int(raw, 0)
            return self.int_to_key(value, length)
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw)
        return self.int_to_key(_as_int(raw), length)


# Register the built-in reference algorithms.
register(XorAlgorithm())
register(AddAlgorithm())
register(SumAlgorithm())
register(Med17Algorithm())
register(VagCrcAlgorithm())
register(FixedAlgorithm())


def _load_builtins() -> None:
    """Idempotent import hook (importing this module already registers)."""

    return None
