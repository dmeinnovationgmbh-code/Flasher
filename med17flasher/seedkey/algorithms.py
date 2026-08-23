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


# --------------------------------------------------------------------------- #
# VW / Audi SA2 seed/key
# --------------------------------------------------------------------------- #
# SA2 is the real Volkswagen-Group security-access mechanism: the ECU's flash
# container (FRF/ODX/.sgo) carries a short *bytecode* — the "SA2 script" — that a
# tiny stack machine runs over the seed to produce the key. Unlike a fixed
# formula, the script differs per ECU, so this one interpreter unlocks every VAG
# ECU whose SA2 script you have — no vendor DLL needed.
#
# The opcode semantics below are a clean re-implementation of the well-known,
# MIT-licensed reference by bri3d (github.com/bri3d/sa2_seed_key); the algorithm
# itself is a documented, interoperability fact. Validated against that
# project's published vector (seed 0x1A1B1C1D -> key 0x6A37F02E), see tests.

_SA2_MAX_STEPS = 100000  # guard against a malformed script looping forever


def _parse_sa2_script(raw: Any) -> bytes:
    """Accept the SA2 bytecode as bytes, an int list, or hex ('68 02', '6802',
    '0x68,0x02')."""

    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    if isinstance(raw, (list, tuple)):
        return bytes(int(x) & 0xFF for x in raw)
    s = str(raw or "").strip().replace("0x", "").replace(",", " ")
    if not s:
        return b""
    return bytes(int(t, 16) for t in s.split()) if " " in s else bytes.fromhex(s)


def sa2_execute(script: bytes, seed: int) -> int:
    """Run an SA2 bytecode script over a 32-bit ``seed``; return the 32-bit key."""

    from collections import deque

    reg = seed & 0xFFFFFFFF
    carry = 0
    ip = 0
    for_ptr: deque = deque()
    for_it: deque = deque()
    steps = 0
    n = len(script)

    def u32(i: int) -> int:
        return (script[i] << 24) | (script[i + 1] << 16) | (script[i + 2] << 8) | script[i + 3]

    while ip < n:
        steps += 1
        if steps > _SA2_MAX_STEPS:
            raise ValueError("sa2: script did not terminate (loop guard)")
        op = script[ip]
        if op == 0x81:                       # rotate left through carry
            carry = reg & 0x80000000
            reg = ((reg << 1) | (1 if carry else 0)) & 0xFFFFFFFF
            ip += 1
        elif op == 0x82:                     # rotate right through carry
            carry = reg & 0x1
            reg >>= 1
            if carry:
                reg |= 0x80000000
            ip += 1
        elif op == 0x93:                     # add 32-bit immediate
            v = reg + u32(ip + 1)
            carry = 1 if v > 0xFFFFFFFF else 0
            reg = v & 0xFFFFFFFF
            ip += 5
        elif op == 0x84:                     # subtract 32-bit immediate
            v = reg - u32(ip + 1)
            carry = 1 if v < 0 else 0
            reg = v & 0xFFFFFFFF
            ip += 5
        elif op == 0x87:                     # xor 32-bit immediate
            reg = (reg ^ u32(ip + 1)) & 0xFFFFFFFF
            ip += 5
        elif op == 0x68:                     # for-loop begin (count)
            for_it.appendleft(script[ip + 1] - 1)
            ip += 2
            for_ptr.appendleft(ip)
        elif op == 0x49:                     # loop end / next
            if for_it and for_it[0] > 0:
                for_it[0] -= 1
                ip = for_ptr[0]
            else:
                if for_it:
                    for_it.popleft()
                    for_ptr.popleft()
                ip += 1
        elif op == 0x4A:                     # branch if carry clear
            ip += (script[ip + 1] + 2) if carry == 0 else 2
        elif op == 0x6B:                     # unconditional branch
            ip += script[ip + 1] + 2
        elif op == 0x4C:                     # finish
            ip += 1
        else:
            raise ValueError(f"sa2: unknown opcode 0x{op:02X} at offset {ip}")
    return reg & 0xFFFFFFFF


class Sa2Algorithm(SeedKeyAlgorithm):
    """VW/Audi **SA2** seed/key — runs the ECU's SA2 bytecode over the seed.

    Parameter ``script``: the SA2 bytecode (hex, e.g. ``"6802819349..."``, or an
    int list). It comes from the ECU's flash container / flashdaten. With it,
    this computes the key for *any* seed — the DLL-free VAG unlock.
    """

    name = "sa2"
    description = "VW/Audi SA2 bytecode seed/key (param: script = SA2 bytecode hex)"

    def compute(self, seed, *, level=0, params=None):
        params = params or {}
        script = _parse_sa2_script(params.get("script") or params.get("sa2") or "")
        if not script:
            raise ValueError("sa2: 'script' (SA2-Bytecode als Hex) fehlt")
        seed_int = self.seed_to_int(bytes(seed)[:4] if len(seed) >= 4 else bytes(seed))
        length = int(params.get("length", 4))
        return self.int_to_key(sa2_execute(script, seed_int), length)


# Register the built-in reference algorithms.
register(XorAlgorithm())
register(AddAlgorithm())
register(SumAlgorithm())
register(Med17Algorithm())
register(VagCrcAlgorithm())
register(FixedAlgorithm())
register(Sa2Algorithm())


def _load_builtins() -> None:
    """Idempotent import hook (importing this module already registers)."""

    return None
