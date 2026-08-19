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
from .solver import (
    SeedKeyPair,
    SeedKeySolver,
    SolveResult,
    load_pairs,
    load_wordlist,
)
from .store import SeedKeyEntry, SeedKeyStore

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
]
