"""Pluggable Security Access (UDS 0x27) seed -> key framework.

Real ECUs guard flash programming with a challenge/response: the tester asks
for a *seed* (0x27 requestSeed), transforms it into a *key* with a secret
algorithm, and sends the key back (0x27 sendKey). The transform is
ECU/variant specific and, on production ECUs, confidential.

This module provides the *framework* - an algorithm interface, a registry and a
loader for user-supplied plug-ins - plus a handful of well-understood reference
algorithms (see :mod:`.algorithms`). Drop your ECU-specific routine in as a
plug-in; nothing here hard-codes a manufacturer's secret.
"""

from __future__ import annotations

import importlib.util
import os
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from ..exceptions import AlgorithmNotFoundError, SeedKeyError
from ..logging_setup import get_logger

log = get_logger("seedkey")


class SeedKeyAlgorithm(ABC):
    """Base class for a seed -> key transform.

    Subclasses set :attr:`name` and implement :meth:`compute`. ``params`` carries
    the per-ECU constants (from the ECU profile or the seed/key store), so one
    algorithm class can serve many variants.
    """

    #: Unique registry name.
    name: str = ""
    #: Short human description shown in the UI.
    description: str = ""

    @abstractmethod
    def compute(
        self, seed: bytes, *, level: int = 0, params: Optional[Dict[str, Any]] = None
    ) -> bytes:
        """Return the key bytes for ``seed``."""

    # Handy helpers for integer-oriented algorithms ------------------- #
    @staticmethod
    def seed_to_int(seed: bytes, big_endian: bool = True) -> int:
        return int.from_bytes(seed, "big" if big_endian else "little")

    @staticmethod
    def int_to_key(value: int, length: int, big_endian: bool = True) -> bytes:
        value &= (1 << (8 * length)) - 1
        return value.to_bytes(length, "big" if big_endian else "little")


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
_REGISTRY: Dict[str, SeedKeyAlgorithm] = {}


def register(algorithm: SeedKeyAlgorithm) -> SeedKeyAlgorithm:
    """Register an algorithm instance under its ``name``."""

    if not algorithm.name:
        raise SeedKeyError("algorithm has no name")
    _REGISTRY[algorithm.name.lower()] = algorithm
    return algorithm


def register_function(
    name: str,
    func: Callable[..., bytes],
    description: str = "",
) -> SeedKeyAlgorithm:
    """Register a plain function ``func(seed, level, params) -> bytes``."""

    class _FunctionAlgorithm(SeedKeyAlgorithm):
        def compute(self, seed, *, level=0, params=None):
            return func(seed, level=level, params=params or {})

    algo = _FunctionAlgorithm()
    algo.name = name
    algo.description = description or f"function {name}"
    return register(algo)


def get_algorithm(name: str) -> SeedKeyAlgorithm:
    try:
        return _REGISTRY[name.lower()]
    except KeyError:
        raise AlgorithmNotFoundError(
            f"no seed/key algorithm named {name!r}; available: "
            f"{', '.join(sorted(_REGISTRY)) or '(none)'}"
        ) from None


def list_algorithms() -> List[str]:
    return sorted(_REGISTRY)


def compute_key(
    name: str,
    seed: bytes,
    *,
    level: int = 0,
    params: Optional[Dict[str, Any]] = None,
) -> bytes:
    """Convenience: look up ``name`` and compute the key for ``seed``."""

    return get_algorithm(name).compute(seed, level=level, params=params or {})


def load_plugin(path: str) -> List[str]:
    """Import a Python plug-in file so it can ``register`` its algorithms.

    The file is executed as a module; anything it registers via
    :func:`register` becomes available afterwards. Returns the names that
    appeared as a result.
    """

    before = set(_REGISTRY)
    spec = importlib.util.spec_from_file_location(
        f"med17flasher_seedkey_plugin_{os.path.basename(path)}", path
    )
    if spec is None or spec.loader is None:
        raise SeedKeyError(f"cannot import seed/key plug-in {path!r}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    new = sorted(set(_REGISTRY) - before)
    log.info("loaded seed/key plug-in %s (added: %s)", path, ", ".join(new) or "none")
    return new
