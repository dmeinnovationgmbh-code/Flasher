"""Seed/key backends that call an external routine: a J2534 seed-key **DLL** or
a seed-key **executable**.

Professional flashing keeps the secret seed->key routine in a vendor-supplied
DLL (the SAE/J2534 ``GenerateKeyEx`` interface) or a small ``*-seed-key.exe``,
and exposes it through a local "seed/key server". This module provides both
backends so they slot into :class:`~med17flasher.seedkey.base.SeedKeyAlgorithm`
and, through the server, match a production ``getKey`` API.

* :class:`DllSeedKey` loads a J2534 seed-key DLL and calls ``GenerateKeyExOpt``
  (falling back to ``GenerateKeyEx``), using ``GetSeedLength`` / ``GetKeyLength``
  / ``GetECUName`` / ``GetConfiguredAccessTypes`` for introspection.
* :class:`ExeSeedKey` runs ``<tool> <seedhex>`` and reads the key from stdout
  (the ``cpcng-seed-key.exe`` pattern).

The DLL is 32-bit stdcall (WINAPI): load it with **32-bit Python on Windows**
(``ctypes.WinDLL``), or under Wine. The DLL itself is never bundled here - you
point at your own licensed copy at runtime.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

from ..exceptions import SeedKeyError
from ..logging_setup import get_logger
from .base import SeedKeyAlgorithm

log = get_logger("seedkey.dll")


def _hexish(value: str) -> bytes:
    return bytes.fromhex(value.strip().replace(" ", "").replace("0x", ""))


class DllSeedKey(SeedKeyAlgorithm):
    """Wrap a J2534 / SAE seed-key DLL (``GenerateKeyEx`` family).

    Parameters
    ----------
    path:
        Path to the DLL (e.g. ``MED1775_12_42_00.dll``).
    options:
        The ``pOptionData`` / ``pDllData`` config string passed to the DLL
        (empty for most single-purpose ECU DLLs).
    loader:
        ctypes loader override for testing (``ctypes.CDLL`` against a mock .so);
        defaults to ``WinDLL`` on Windows, ``CDLL`` elsewhere.
    """

    name = "dll"
    description = "J2534 seed-key DLL (GenerateKeyExOpt)"

    def __init__(
        self,
        path: str,
        *,
        options: str = "",
        loader: Optional[Any] = None,
        seed_length: Optional[int] = None,
        key_length: Optional[int] = None,
    ) -> None:
        self.path = path
        self.options = options
        self._lib = None
        self._loader = loader
        self._seed_length = seed_length
        self._key_length = key_length

    # ------------------------------------------------------------------ #
    def _load(self):
        if self._lib is not None:
            return self._lib
        if not os.path.isfile(self.path):
            raise SeedKeyError(f"seed-key DLL not found: {self.path!r}")
        loader = self._loader
        if loader is None:
            loader = ctypes.WinDLL if sys.platform == "win32" else ctypes.CDLL
        try:
            self._lib = loader(self.path)
        except OSError as exc:  # wrong bitness / missing deps
            raise SeedKeyError(
                f"cannot load seed-key DLL {self.path!r}: {exc}. "
                "It is a 32-bit Windows DLL - use 32-bit Python on Windows (or Wine)."
            ) from exc
        return self._lib

    def _func(self, name: str):
        lib = self._load()
        try:
            return getattr(lib, name)
        except AttributeError:
            return None

    # ------------------------------------------------------------------ #
    # Introspection helpers (best-effort; DLLs vary slightly)
    # ------------------------------------------------------------------ #
    def ecu_name(self) -> Optional[str]:
        fn = self._func("GetECUName")
        if fn is None:
            return None
        buf = ctypes.create_string_buffer(256)
        try:
            fn.restype = ctypes.c_long
            fn.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_ulong)]
            size = ctypes.c_ulong(256)
            fn(buf, ctypes.byref(size))
            return buf.value.decode("latin-1", "replace")
        except Exception:  # noqa: BLE001
            return None

    def seed_length(self) -> int:
        if self._seed_length:
            return self._seed_length
        return self._length("GetSeedLength", default=4)

    def key_length(self) -> int:
        if self._key_length:
            return self._key_length
        return self._length("GetKeyLength", default=4)

    def _length(self, name: str, default: int) -> int:
        fn = self._func(name)
        if fn is None:
            return default
        try:
            fn.restype = ctypes.c_ulong
            fn.argtypes = [ctypes.c_char_p]
            val = int(fn(self.options.encode("latin-1")))
            return val if 1 <= val <= 64 else default
        except Exception:  # noqa: BLE001
            return default

    def access_types(self) -> List[int]:
        fn = self._func("GetConfiguredAccessTypes")
        if fn is None:
            return []
        try:
            arr = (ctypes.c_ubyte * 32)()
            count = ctypes.c_ulong(32)
            fn.restype = ctypes.c_long
            fn.argtypes = [ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_ulong)]
            fn(arr, ctypes.byref(count))
            return [arr[i] for i in range(min(count.value, 32))]
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------------ #
    def compute(self, seed, *, level=0, params=None):
        params = params or {}
        options = str(params.get("options", self.options))
        key_len = int(params.get("key_length", self.key_length()))
        seed = bytes(seed)

        key_buf = (ctypes.c_ubyte * max(key_len, 8))()
        key_size = ctypes.c_ulong(len(key_buf))
        seed_arr = (ctypes.c_ubyte * len(seed))(*seed)

        opt_rc = None  # remember an Opt failure so we report it, not a false
        #                 "exports neither" error, when GenerateKeyEx is absent.

        # Preferred: GenerateKeyExOpt(seed, seedSize, options, key, keySize)
        fn = self._func("GenerateKeyExOpt")
        if fn is not None:
            fn.restype = ctypes.c_long
            fn.argtypes = [ctypes.POINTER(ctypes.c_ubyte), ctypes.c_ulong,
                           ctypes.c_char_p, ctypes.POINTER(ctypes.c_ubyte),
                           ctypes.POINTER(ctypes.c_ulong)]
            rc = fn(seed_arr, len(seed), options.encode("latin-1"), key_buf,
                    ctypes.byref(key_size))
            if rc == 0:
                return bytes(key_buf[: key_size.value])
            opt_rc = rc
            log.debug("GenerateKeyExOpt returned %d, trying GenerateKeyEx", rc)

        # Fallback: GenerateKeyEx(seed, seedSize, dllData, key, keySize)
        fn = self._func("GenerateKeyEx")
        if fn is not None:
            key_size = ctypes.c_ulong(len(key_buf))
            fn.restype = ctypes.c_long
            fn.argtypes = [ctypes.POINTER(ctypes.c_ubyte), ctypes.c_ulong,
                           ctypes.c_char_p, ctypes.POINTER(ctypes.c_ubyte),
                           ctypes.POINTER(ctypes.c_ulong)]
            rc = fn(seed_arr, len(seed), options.encode("latin-1"), key_buf,
                    ctypes.byref(key_size))
            if rc == 0:
                return bytes(key_buf[: key_size.value])
            raise SeedKeyError(f"seed-key DLL GenerateKeyEx failed (rc={rc})")

        if opt_rc is not None:
            raise SeedKeyError(f"seed-key DLL GenerateKeyExOpt failed (rc={opt_rc})")
        raise SeedKeyError(
            f"DLL {self.path!r} exports neither GenerateKeyExOpt nor GenerateKeyEx"
        )


class ExeSeedKey(SeedKeyAlgorithm):
    """Run an external ``<tool> <seedhex>`` and read the key hex from stdout.

    Mirrors the ``execa('cpcng-seed-key.exe', [seed])`` pattern. ``argv_template``
    lets you customise the command line: ``{seed}`` and ``{level}`` are filled in.
    """

    name = "exe"
    description = "external seed-key executable (stdout = key hex)"

    def __init__(self, path: str, *, argv_template: Optional[List[str]] = None,
                 timeout: float = 10.0) -> None:
        self.path = path
        self.argv_template = argv_template or ["{seed}"]
        self.timeout = timeout

    def compute(self, seed, *, level=0, params=None):
        seed_hex = bytes(seed).hex()
        argv = [self.path] + [a.format(seed=seed_hex, level=level) for a in self.argv_template]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=self.timeout, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SeedKeyError(f"seed-key exe failed ({argv}): {exc}") from exc
        out = proc.stdout.strip().split()
        if not out:
            raise SeedKeyError(f"seed-key exe produced no output ({argv})")
        try:
            return _hexish(out[-1])
        except ValueError as exc:
            raise SeedKeyError(f"seed-key exe output is not hex: {proc.stdout!r}") from exc


def make_backend(spec: str, **kwargs: Any) -> SeedKeyAlgorithm:
    """Build a DLL/EXE backend from a ``dll:path`` / ``exe:path`` (or bare path).

    ``.dll`` -> :class:`DllSeedKey`, ``.exe``/other -> :class:`ExeSeedKey`.
    """

    kind, _, path = spec.partition(":")
    if not path and os.path.exists(kind):  # bare path
        path, kind = kind, ""
    if kind == "dll" or path.lower().endswith(".dll"):
        return DllSeedKey(path, **kwargs)
    if kind == "exe" or path.lower().endswith(".exe") or os.path.exists(path):
        return ExeSeedKey(path, **kwargs)
    raise SeedKeyError(f"cannot build seed-key backend from {spec!r}")
