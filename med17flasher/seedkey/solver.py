"""Recover a seed -> key algorithm from captured seed/key pairs.

The honest way to obtain an ECU's Security Access routine is to *derive* it from
example pairs you capture from an ECU you own (or from your licensed tool): send
``0x27 requestSeed``, note the seed, let the trusted tool produce the key, and
record the ``(seed, key)`` pair. A handful of pairs is usually enough.

Given those pairs, :class:`SeedKeySolver`:

* directly solves the simple families - ``xor`` (k = seed ^ key),
  ``add`` (k = key - seed) and ``sum`` (per-byte offset);
* brute-forces the structured families (``med17``, ``vag_crc``) over a parameter
  grid, optionally seeded with a *wordlist* of candidate 32-bit constants;
* tests any registered algorithm (including your own plug-ins) against a
  parameter grid and reports which reproduces every pair.

It never invents an algorithm - it only reports one that provably reproduces
all the pairs you supply. Recovering an unknown 32-bit-keyed proprietary routine
by brute force alone is generally infeasible; supply a wordlist of likely
constants, or plug the routine in directly (see :func:`.base.load_plugin`).
"""

from __future__ import annotations

import itertools
import json
import os
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

from ..logging_setup import get_logger
from .base import compute_key, get_algorithm

log = get_logger("seedkey.solver")


@dataclass
class SeedKeyPair:
    seed: bytes
    key: bytes

    @classmethod
    def from_hex(cls, seed_hex: str, key_hex: str) -> "SeedKeyPair":
        return cls(
            bytes.fromhex(seed_hex.replace(" ", "")),
            bytes.fromhex(key_hex.replace(" ", "")),
        )


@dataclass
class SolveResult:
    algorithm: str
    params: Dict[str, object]
    matches: int
    total: int
    level: int = 0

    @property
    def is_full_match(self) -> bool:
        return self.total > 0 and self.matches == self.total

    def __str__(self) -> str:
        status = "FULL" if self.is_full_match else f"{self.matches}/{self.total}"
        return f"[{status}] {self.algorithm} params={self.params} level=0x{self.level:X}"


def _int(b: bytes) -> int:
    return int.from_bytes(b, "big")


# --------------------------------------------------------------------------- #
# Direct solvers (one pair suffices; the rest confirm)
# --------------------------------------------------------------------------- #
def solve_xor(pairs: Sequence[SeedKeyPair]) -> Optional[Dict[str, object]]:
    length = len(pairs[0].key)
    if any(len(p.key) != length or len(p.seed) != length for p in pairs):
        return None
    k = _int(pairs[0].seed) ^ _int(pairs[0].key)
    if all((_int(p.seed) ^ _int(p.key)) == k for p in pairs):
        return {"k": hex(k), "length": length}
    return None


def solve_add(pairs: Sequence[SeedKeyPair]) -> Optional[Dict[str, object]]:
    length = len(pairs[0].key)
    if any(len(p.key) != length or len(p.seed) != length for p in pairs):
        return None
    mask = (1 << (8 * length)) - 1
    k = (_int(pairs[0].key) - _int(pairs[0].seed)) & mask
    if all(((_int(p.seed) + k) & mask) == _int(p.key) for p in pairs):
        return {"k": hex(k), "length": length}
    return None


def solve_sum(pairs: Sequence[SeedKeyPair]) -> Optional[Dict[str, object]]:
    if any(len(p.seed) != len(p.key) for p in pairs):
        return None
    k = (pairs[0].key[0] - pairs[0].seed[0]) & 0xFF
    for p in pairs:
        for s, ky in zip(p.seed, p.key):
            if ((s + k) & 0xFF) != ky:
                return None
    return {"k": hex(k)}


# --------------------------------------------------------------------------- #
# Grid brute force for structured / arbitrary algorithms
# --------------------------------------------------------------------------- #
def _score(algorithm: str, params: Dict[str, object], pairs, level: int) -> int:
    matches = 0
    for p in pairs:
        try:
            if compute_key(algorithm, p.seed, level=level, params=params) == p.key:
                matches += 1
        except Exception:  # noqa: BLE001 - bad param combo just scores 0
            return matches
    return matches


def brute_force(
    pairs: Sequence[SeedKeyPair],
    algorithm: str,
    grid: Dict[str, Iterable],
    level: int = 0,
    stop_on_full: bool = True,
) -> Optional[SolveResult]:
    """Search ``grid`` (a cartesian product of parameter values) for a match."""

    names = list(grid.keys())
    value_lists = [list(grid[n]) for n in names]
    best: Optional[SolveResult] = None
    total = len(pairs)
    for combo in itertools.product(*value_lists):
        params = dict(zip(names, combo))
        matches = _score(algorithm, params, pairs, level)
        if best is None or matches > best.matches:
            best = SolveResult(algorithm, params, matches, total, level)
        if matches == total and stop_on_full:
            return best
    return best


def default_grids(wordlist: Optional[Sequence[int]] = None) -> Dict[str, Dict[str, Iterable]]:
    """Parameter grids for the structured built-ins.

    Without a ``wordlist`` the 32-bit constant ``k`` cannot be brute-forced, so
    the structured grids are only useful when you pass likely constants.
    """

    ks = list(wordlist) if wordlist else []
    grids: Dict[str, Dict[str, Iterable]] = {}
    if ks:
        grids["med17"] = {"k": ks, "rounds": range(1, 9), "shift": range(0, 32)}
        grids["vag_crc"] = {"poly": ks, "init": [0xFFFFFFFF, 0x00000000], "app_key": [0] + ks}
    return grids


class SeedKeySolver:
    """Recover the algorithm + parameters that reproduce a set of pairs."""

    def __init__(self, pairs: Sequence[SeedKeyPair]) -> None:
        if not pairs:
            raise ValueError("need at least one seed/key pair")
        self.pairs = list(pairs)

    def solve(
        self,
        *,
        level: int = 0,
        wordlist: Optional[Sequence[int]] = None,
        extra_grids: Optional[Dict[str, Dict[str, Iterable]]] = None,
        include_algorithms: Optional[Sequence[str]] = None,
    ) -> List[SolveResult]:
        """Return candidate solutions, best (full matches) first."""

        results: List[SolveResult] = []
        total = len(self.pairs)

        # 1. direct algebraic solvers
        for name, fn in (("xor", solve_xor), ("add", solve_add), ("sum", solve_sum)):
            params = fn(self.pairs)
            if params is not None:
                results.append(SolveResult(name, params, total, total, level))

        # 2. structured grids (need a wordlist for the 32-bit constants)
        grids = dict(default_grids(wordlist))
        if extra_grids:
            grids.update(extra_grids)
        if include_algorithms:
            grids = {k: v for k, v in grids.items() if k in include_algorithms}
        for algorithm, grid in grids.items():
            res = brute_force(self.pairs, algorithm, grid, level=level)
            if res and res.matches > 0:
                results.append(res)

        # de-duplicate + sort: full matches first, then by match count
        seen = set()
        unique: List[SolveResult] = []
        for r in sorted(results, key=lambda r: (r.is_full_match, r.matches), reverse=True):
            key = (r.algorithm, json.dumps(r.params, sort_keys=True, default=str))
            if key not in seen:
                seen.add(key)
                unique.append(r)
        return unique

    def best(self, **kwargs) -> Optional[SolveResult]:
        results = self.solve(**kwargs)
        if results and results[0].is_full_match:
            return results[0]
        return results[0] if results else None

    def verify(self, algorithm: str, params: Dict[str, object], level: int = 0) -> bool:
        """Check that ``algorithm``/``params`` reproduces every pair."""

        get_algorithm(algorithm)  # raise early on unknown algorithm
        return _score(algorithm, params, self.pairs, level) == len(self.pairs)


# --------------------------------------------------------------------------- #
# Loading pairs from files
# --------------------------------------------------------------------------- #
def load_pairs(path: str) -> List[SeedKeyPair]:
    """Load seed/key pairs from a JSON or whitespace/CSV text file.

    JSON: ``[{"seed": "11223344", "key": "8369ee49"}, ...]``
    Text: one ``<seedhex> <keyhex>`` pair per line (``#`` comments allowed).
    """

    ext = os.path.splitext(path)[1].lower()
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if ext == ".json":
        rows = json.loads(text)
        return [SeedKeyPair.from_hex(r["seed"], r["key"]) for r in rows]
    pairs: List[SeedKeyPair] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.replace(",", " ").split()
        if len(parts) >= 2:
            pairs.append(SeedKeyPair.from_hex(parts[0], parts[1]))
    return pairs


def load_wordlist(path: str) -> List[int]:
    """Load candidate 32-bit constants (one hex/int per line) for brute force."""

    values: List[int] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                values.append(int(line, 0))
            except ValueError:
                continue
    return values
