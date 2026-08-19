"""A **32-bit seed/key bridge**: run the vendor DLL in a helper process.

Why this exists
---------------
Vendor J2534 seed/key DLLs (``MED1775_12_42_00.dll`` and friends) are **32-bit
Windows stdcall** libraries. ``ctypes.WinDLL`` can only load a DLL whose bitness
matches the *running interpreter*, so a 64-bit process - which is what the
PyInstaller desktop build is - fails with ``OSError: [WinError 193] %1 is not a
valid Win32 application``. No amount of ctypes trickery fixes that: the loader
simply refuses to map a 32-bit image into a 64-bit address space.

The standard remedy is out-of-process: a small **32-bit Python** loads the DLL
and answers key requests over a pipe. Until now that meant the user had to start
a separate seed/key server by hand and point the app at its URL. This module
automates it - :class:`SeedKeyBridge` finds a 32-bit interpreter, spawns the
worker on first use, and speaks to it over stdin/stdout, so 64-bit callers can
use a 32-bit DLL transparently:

    bridge = SeedKeyBridge("MED1775_12_42_00.dll")
    key = bridge.compute("MED17.7.5", 0x05, bytes.fromhex("11223344"))

The process plumbing (interpreter discovery, spawning, the line-JSON exchange)
lives in :mod:`med17flasher.core.procbridge` and is shared with the J2534
bridge, which has exactly the same 32-bit problem.

The protocol
------------
One JSON object per line in each direction. The parent writes a request, the
child writes exactly one reply::

    -> {"cmd": "key", "level": 5, "seed": "11223344"}
    <- {"ok": true, "key": "0316653c"}

    -> {"cmd": "info"}
    <- {"ok": true, "info": {"ecu_name": "...", "seed_length": 4, ...}}

    -> {"cmd": "quit"}
    <- {"ok": true}

    (any failure)
    <- {"ok": false, "error": "..."}

The child is started as ``<python32> -m med17flasher.seedkey.bridge --dll PATH``
and reuses :class:`~med17flasher.seedkey.dll.DllSeedKey` for the actual ctypes
work, so there is exactly one implementation of the ``GenerateKeyEx`` call.
"""

from __future__ import annotations

import os
import struct
import sys
from typing import Any, Dict, List, Optional

from ..core.procbridge import LineJsonBridge, find_python32, serve
from ..exceptions import SeedKeyError
from ..logging_setup import get_logger
from .base import SeedKeyAlgorithm

log = get_logger("seedkey.bridge")

__all__ = ["SeedKeyBridge", "BridgeAlgorithm", "find_python32",
           "open_seedkey_dll"]


# --------------------------------------------------------------------------- #
# Parent side
# --------------------------------------------------------------------------- #
class SeedKeyBridge(LineJsonBridge):
    """Compute keys with a 32-bit seed/key DLL from a 64-bit process.

    Implements the flasher's resolver interface (``compute(ecu, level, seed)``),
    so it can be handed straight to :class:`~med17flasher.flasher.Flasher` in
    place of a :class:`~med17flasher.seedkey.store.SeedKeyStore` or a
    :class:`~med17flasher.seedkey.server.SeedKeyClient`.

    Parameters
    ----------
    dll_path:
        The vendor seed-key DLL (32-bit Windows).
    python32:
        Interpreter used to run the worker. Auto-discovered when omitted. It is
        deliberately *not* bitness-checked when given explicitly - that lets the
        tests (and a Wine-based setup) point at any interpreter they like.
    options:
        ``pOptionData`` / ``pDllData`` config string for the DLL. Fixed for the
        lifetime of the child, since it is passed on its command line.
    timeout:
        Seconds to wait for one reply. A wedged DLL must not hang the flash.
    """

    label = "seed/key bridge"
    error_type = SeedKeyError

    def __init__(
        self,
        dll_path: str,
        *,
        python32: Optional[str] = None,
        options: str = "",
        timeout: float = 10.0,
    ) -> None:
        super().__init__(python32=python32, timeout=timeout)
        # Resolve now: the child inherits our cwd, but POSIX ``dlopen`` refuses
        # to search it for a bare relative name, and an absolute path is what we
        # want in error messages anyway. A name that is *not* a file on disk is
        # left alone so Windows' own DLL search order still applies.
        self.dll_path = os.path.abspath(dll_path) if os.path.isfile(dll_path) else dll_path
        self.options = options or ""

    def _child_argv(self) -> List[str]:
        argv = [self.python32]
        # ``med17flasher.seedkey`` imports this module, so runpy would warn that
        # it was already in sys.modules before running it as __main__.
        argv += self._module_warning_filter("med17flasher.seedkey.bridge")
        argv += ["-m", "med17flasher.seedkey.bridge", "--dll", self.dll_path]
        if self.options:
            argv += ["--options", self.options]
        return argv

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def compute(self, ecu: str, level: int, seed: bytes) -> bytes:
        """Resolver interface: turn ``seed`` into a key via the 32-bit DLL.

        ``ecu`` is accepted for interface compatibility; a seed-key DLL is
        already ECU specific, so it is not part of the wire request.
        """

        reply = self.request(
            {"cmd": "key", "level": int(level), "seed": bytes(seed).hex()}
        )
        key_hex = reply.get("key", "")
        try:
            key = bytes.fromhex(key_hex)
        except ValueError as exc:
            raise SeedKeyError(
                f"the 32-bit seed/key bridge returned non-hex key {key_hex!r}"
            ) from exc
        log.debug("bridge key for ecu=%s level=0x%02X: %s", ecu, int(level), key.hex())
        return key

    def info(self) -> Dict[str, Any]:
        """DLL introspection: ECU name, seed/key lengths, access types."""

        reply = self.request({"cmd": "info"})
        info = reply.get("info")
        if not isinstance(info, dict):
            raise SeedKeyError("the 32-bit seed/key bridge sent a malformed info reply")
        return info

    def as_algorithm(self) -> SeedKeyAlgorithm:
        """Adapt to the :class:`SeedKeyAlgorithm` interface.

        Needed by :class:`~med17flasher.seedkey.server.SeedKeyService`, which
        calls ``compute(seed, level=..., params=...)`` rather than the resolver
        signature this class implements.
        """

        return BridgeAlgorithm(self)


class BridgeAlgorithm(SeedKeyAlgorithm):
    """``SeedKeyAlgorithm`` facade over a :class:`SeedKeyBridge`."""

    name = "bridge"
    description = "32-bit seed-key DLL via a helper process"

    def __init__(self, bridge: SeedKeyBridge) -> None:
        self.bridge = bridge

    def compute(self, seed, *, level=0, params=None):
        # ``params['options']`` is deliberately ignored: the option string is
        # baked into the child's command line when it is spawned.
        return self.bridge.compute(str((params or {}).get("ecu", "")), level, bytes(seed))


# --------------------------------------------------------------------------- #
# Child side (``python -m med17flasher.seedkey.bridge``)
# --------------------------------------------------------------------------- #
def _handle(algo: Any, request: Dict[str, Any]) -> Dict[str, Any]:
    cmd = request.get("cmd")
    if cmd == "key":
        seed = bytes.fromhex(str(request["seed"]).replace(" ", ""))
        level = int(request.get("level", 0))
        return {"ok": True, "key": algo.compute(seed, level=level).hex()}
    if cmd == "info":
        return {
            "ok": True,
            "info": {
                "dll": algo.path,
                "options": algo.options,
                "ecu_name": algo.ecu_name(),
                "seed_length": algo.seed_length(),
                "key_length": algo.key_length(),
                "access_types": algo.access_types(),
                "python": sys.executable,
                # 32 or 64 - proves which interpreter actually loaded the DLL.
                "bits": 8 * struct.calcsize("P"),
            },
        }
    if cmd == "ping":
        return {"ok": True}
    return {"ok": False, "error": f"unknown command: {cmd!r}"}


def _worker_main(argv: List[str]) -> int:
    """The helper process: load the DLL, answer JSON requests on stdin."""

    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m med17flasher.seedkey.bridge",
        description="32-bit seed/key DLL worker (line-based JSON on stdin/stdout)",
    )
    parser.add_argument("--dll", required=True, help="path to the seed-key DLL")
    parser.add_argument("--options", default="", help="DLL option/config string")
    args = parser.parse_args(argv)

    from .dll import DllSeedKey  # reuse the one ctypes implementation

    algo = DllSeedKey(args.dll, options=args.options)
    try:
        # Fail fast and loudly: a wrong-bitness or missing DLL should surface as
        # the loader's own message on stderr (which the parent reports) rather
        # than as an error on every single key request.
        algo._load()
    except Exception as exc:  # noqa: BLE001 - anything here is fatal
        sys.stderr.write(f"{exc}\n")
        sys.stderr.flush()
        return 2

    return serve(lambda request: _handle(algo, request))


if __name__ == "__main__":
    sys.exit(_worker_main(sys.argv[1:]))


# --------------------------------------------------------------------------- #
# Entry point with automatic 32-bit fallback
# --------------------------------------------------------------------------- #
def _is_bitness_error(exc: Exception) -> bool:
    """Is this the loader refusing a wrong-architecture image?

    WinError 193 (``%1 is not a valid Win32 application``) is the Windows
    signal; the ELF loader says "wrong ELF class" instead.
    """

    text = str(exc).lower()
    return ("193" in text or "not a valid win32 application" in text
            or "wrong elf class" in text or "invalid win32" in text)


def open_seedkey_dll(path: str, *, options: str = "",
                     python32: Optional[str] = None, params: Optional[Dict[str, Any]] = None):
    """Open a seed/key DLL as a **resolver**, bridging out-of-process if needed.

    Vendor seed-key DLLs are 32-bit, and the desktop build is 64-bit, so loading
    one in-process fails outright. This tries in-process first (fast path: the
    bitness already matches) and falls back to :class:`SeedKeyBridge` on exactly
    that loader error - the same shape as
    :func:`med17flasher.core.j2534.open_j2534`.

    The DLL is loaded eagerly rather than on the first key request: a wrong
    interpreter must surface while configuring, not halfway through a flash with
    the ECU already unlocked and erased.
    """

    from . import AlgorithmResolver  # local: the package imports this module
    from .dll import DllSeedKey

    algo = DllSeedKey(path, options=options)
    try:
        algo._load()
    except SeedKeyError as exc:
        if not _is_bitness_error(exc):
            raise
        log.info("seed/key DLL %r cannot be loaded in-process (%s); "
                 "falling back to the 32-bit bridge", path, exc)
        return SeedKeyBridge(path, options=options, python32=python32)
    return AlgorithmResolver(algo, params or {})
