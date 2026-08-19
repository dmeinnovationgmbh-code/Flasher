"""Seed/key security-access subpackage.

Importing this package registers the built-in reference algorithms so
:func:`get_algorithm` / :func:`compute_key` work out of the box.
"""

from . import algorithms as _algorithms  # noqa: F401  (side-effect: registration)
from .base import (
    AlgorithmNotFoundError,
    SeedKeyAlgorithm,
    compute_key,
    get_algorithm,
    list_algorithms,
    load_plugin,
    register,
    register_function,
)
from .bridge import SeedKeyBridge, find_python32
from .dll import DllSeedKey, ExeSeedKey, make_backend


from .solver import (
    SeedKeyPair,
    SeedKeySolver,
    SolveResult,
    load_pairs,
    load_wordlist,
)
from .store import SeedKeyEntry, SeedKeyStore


class AlgorithmResolver:
    """Adapt a :class:`SeedKeyAlgorithm` to the flasher's seed/key resolver
    interface (``compute(ecu, level, seed)``)."""

    def __init__(self, algorithm: SeedKeyAlgorithm, params=None) -> None:
        self.algorithm = algorithm
        self.params = params or {}

    def compute(self, ecu: str, level: int, seed: bytes) -> bytes:
        return self.algorithm.compute(seed, level=level, params=self.params)

__all__ = [
    "SeedKeyAlgorithm",
    "AlgorithmNotFoundError",
    "compute_key",
    "get_algorithm",
    "list_algorithms",
    "load_plugin",
    "register",
    "register_function",
    "SeedKeyStore",
    "SeedKeyEntry",
    "SeedKeySolver",
    "SeedKeyPair",
    "SolveResult",
    "load_pairs",
    "load_wordlist",
    "DllSeedKey",
    "ExeSeedKey",
    "make_backend",
    "AlgorithmResolver",
    "SeedKeyBridge",
    "find_python32",
]
