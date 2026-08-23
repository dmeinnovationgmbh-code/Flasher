"""A persistent database mapping (ECU, security level) -> algorithm + params.

Real workshops keep a library of seed/key routines. :class:`SeedKeyStore` is a
small JSON-backed catalogue so the flasher, the CLI and the seed/key server can
all resolve "which algorithm and constants do I use for this ECU/level?" from
one place.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from ..exceptions import SeedKeyError
from .base import compute_key, get_algorithm


@dataclass
class SeedKeyEntry:
    """One catalogue row."""

    ecu: str
    level: int
    algorithm: str
    params: Dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def key(self) -> str:
        return f"{self.ecu.lower()}#{self.level}"

    def compute(self, seed: bytes) -> bytes:
        return compute_key(self.algorithm, seed, level=self.level, params=self.params)


class SeedKeyStore:
    """An in-memory catalogue with optional JSON persistence."""

    def __init__(self) -> None:
        self._entries: Dict[str, SeedKeyEntry] = {}

    # ------------------------------------------------------------------ #
    # Mutation
    # ------------------------------------------------------------------ #
    def add(self, entry: SeedKeyEntry) -> None:
        get_algorithm(entry.algorithm)  # validate the algorithm exists
        self._entries[entry.key()] = entry

    def add_many(self, entries: List[SeedKeyEntry]) -> None:
        for entry in entries:
            self.add(entry)

    def remove(self, ecu: str, level: int) -> None:
        self._entries.pop(f"{ecu.lower()}#{level}", None)

    # ------------------------------------------------------------------ #
    # Lookup
    # ------------------------------------------------------------------ #
    def get(self, ecu: str, level: int) -> Optional[SeedKeyEntry]:
        entry = self._entries.get(f"{ecu.lower()}#{level}")
        if entry is not None:
            return entry
        # Fall back to a wildcard entry registered for the ECU with level -1.
        return self._entries.get(f"{ecu.lower()}#-1")

    def resolve(self, ecu: str, level: int) -> SeedKeyEntry:
        entry = self.get(ecu, level)
        if entry is None:
            raise SeedKeyError(
                f"no seed/key entry for ecu={ecu!r} level={level}"
            )
        return entry

    def compute(self, ecu: str, level: int, seed: bytes) -> bytes:
        return self.resolve(ecu, level).compute(seed)

    def entries(self) -> List[SeedKeyEntry]:
        return list(self._entries.values())

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def to_json(self) -> str:
        return json.dumps([asdict(e) for e in self._entries.values()], indent=2)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.to_json())

    @classmethod
    def from_list(cls, rows: List[Dict[str, Any]]) -> "SeedKeyStore":
        store = cls()
        for row in rows:
            store.add(
                SeedKeyEntry(
                    ecu=row["ecu"],
                    level=int(row["level"]),
                    algorithm=row["algorithm"],
                    params=row.get("params", {}),
                    note=row.get("note", ""),
                )
            )
        return store

    @classmethod
    def load(cls, path: str) -> "SeedKeyStore":
        with open(path, "r", encoding="utf-8") as fh:
            rows = json.load(fh)
        return cls.from_list(rows)

    @classmethod
    def default(cls) -> "SeedKeyStore":
        """A store pre-populated with the MED17.7.5 template entry."""

        return cls.from_list(
            [
                {
                    "ecu": "MED17.7.5",
                    "level": 0x11,
                    "algorithm": "med17",
                    "params": {"k": "0x1C5A36B7", "rounds": 5, "shift": 5},
                    "note": "Template - replace k/rounds/shift with your ECU's values",
                },
                {
                    "ecu": "MED17.7.5",
                    "level": 0x01,
                    "algorithm": "med17",
                    "params": {"k": "0x7F3D91A2", "rounds": 3, "shift": 7},
                    "note": "Template extended-diagnostics level",
                },
            ]
        )
