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

The protocol
------------
One JSON object per line in each direction (line based so a partial write can
never be mistaken for a complete message, and so the child can be driven by hand
for debugging). The parent writes a request, the child writes exactly one reply:

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

import glob
import json
import os
import queue
import struct
import subprocess
import sys
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from ..exceptions import SeedKeyError
from ..logging_setup import get_logger
from .base import SeedKeyAlgorithm

log = get_logger("seedkey.bridge")

#: How long to wait for the ``struct.calcsize`` bitness probe of a candidate
#: interpreter. Generous enough for a cold start, short enough that scanning a
#: handful of stale install paths cannot stall the UI.
_PROBE_TIMEOUT = 10.0

#: Sentinel pushed on the reply queue by the reader thread when the child's
#: stdout reaches EOF (i.e. the child exited).
_EOF = None

# Discovery is a few subprocess launches, so cache it process-wide. ``_searched``
# distinguishes "not looked yet" from "looked, found nothing".
_py32_cache: Optional[str] = None
_py32_searched = False
_py32_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# 32-bit interpreter discovery
# --------------------------------------------------------------------------- #
def _pointer_size(argv: List[str]) -> Optional[int]:
    """Run ``argv`` as a Python and return ``struct.calcsize('P')``, or None.

    4 means a 32-bit interpreter, 8 a 64-bit one. Anything unexpected (missing
    executable, a crash, a launcher that prints a banner) yields None so the
    caller just moves on to the next candidate.
    """

    try:
        proc = subprocess.run(
            argv + ["-c", "import struct;print(struct.calcsize('P'))"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("bitness probe failed for %s: %s", argv, exc)
        return None
    out = (proc.stdout or "").strip().splitlines()
    if not out:
        return None
    try:
        return int(out[-1])
    except ValueError:
        return None


def _is_32bit(python: str) -> bool:
    return _pointer_size([python]) == 4


def _candidate_paths() -> List[str]:
    """Common Windows install locations of a 32-bit CPython, newest first."""

    patterns = [
        r"C:\Python3*-32\python.exe",
        os.path.join(
            os.environ.get("LOCALAPPDATA", r"C:\Users\Default\AppData\Local"),
            "Programs", "Python", "Python3*-32", "python.exe",
        ),
        os.path.join(
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            "Python3*-32", "python.exe",
        ),
    ]
    found: List[str] = []
    for pattern in patterns:
        # Reverse-sorted so Python312-32 wins over Python38-32.
        found.extend(sorted(glob.glob(pattern), reverse=True))
    return found


def find_python32(refresh: bool = False) -> Optional[str]:
    """Locate a 32-bit Python interpreter, or return None.

    Windows only in practice: the DLLs this bridge exists for are Windows
    libraries. The result is cached because discovery costs a few subprocess
    launches and the answer cannot change while the app runs.
    """

    global _py32_cache, _py32_searched

    with _py32_lock:
        if _py32_searched and not refresh:
            return _py32_cache
        _py32_cache, _py32_searched = None, True

        if sys.platform != "win32":
            # Nothing to find: there is no "32-bit python that loads .dll files"
            # on Linux/macOS. Callers must pass ``python32`` explicitly (the
            # tests do exactly that, and a Wine setup can too).
            log.debug("32-bit Python discovery skipped on %s", sys.platform)
            return None

        # 1. The `py` launcher is the reliable, version-agnostic route.
        if _pointer_size(["py", "-3-32"]) == 4:
            # The launcher is not a plain executable path, and the worker is
            # spawned as ``<python> -m ...``, so ask it where the real
            # interpreter lives and use that path directly.
            try:
                proc = subprocess.run(
                    ["py", "-3-32", "-c", "import sys;print(sys.executable)"],
                    capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                exe = (proc.stdout or "").strip().splitlines()
                if exe and os.path.isfile(exe[-1]):
                    _py32_cache = exe[-1]
            except (OSError, subprocess.SubprocessError) as exc:
                log.debug("py launcher could not report sys.executable: %s", exc)

        # 2. Fall back to the usual install locations.
        if _py32_cache is None:
            for path in _candidate_paths():
                if os.path.isfile(path) and _is_32bit(path):
                    _py32_cache = path
                    break

        if _py32_cache:
            log.info("32-bit Python for the seed/key bridge: %s", _py32_cache)
        else:
            log.debug("no 32-bit Python found")
        return _py32_cache


# --------------------------------------------------------------------------- #
# Parent side
# --------------------------------------------------------------------------- #
class SeedKeyBridge:
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
        tests (and a Wine/…-based setup) point at any interpreter they like.
    options:
        ``pOptionData`` / ``pDllData`` config string for the DLL. Fixed for the
        lifetime of the child, since it is passed on its command line.
    timeout:
        Seconds to wait for one reply. A wedged DLL must not hang the flash.
    """

    def __init__(
        self,
        dll_path: str,
        *,
        python32: Optional[str] = None,
        options: str = "",
        timeout: float = 10.0,
    ) -> None:
        # Resolve now: the child inherits our cwd, but POSIX ``dlopen`` refuses
        # to search it for a bare relative name, and an absolute path is what we
        # want in error messages anyway. A name that is *not* a file on disk is
        # left alone so Windows' own DLL search order still applies.
        self.dll_path = os.path.abspath(dll_path) if os.path.isfile(dll_path) else dll_path
        self.options = options or ""
        self.timeout = float(timeout)
        self._python32 = python32
        self._proc: Optional[subprocess.Popen] = None
        self._replies: "queue.Queue[Optional[str]]" = queue.Queue()
        # Keep only the tail: a broken DLL can be very chatty and the tail is
        # what carries the actual error (e.g. the WinError 193 message).
        self._stderr: Deque[str] = deque(maxlen=50)
        self._threads: List[threading.Thread] = []
        self._err_thread: Optional[threading.Thread] = None
        # Re-entrant: the timeout path calls close() while already holding it.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    @property
    def python32(self) -> str:
        """The interpreter used for the worker (discovered on first access)."""

        if self._python32:
            return self._python32
        found = find_python32()
        if not found:
            raise SeedKeyError(
                "no 32-bit Python found for the seed/key bridge. The vendor DLL "
                "is a 32-bit Windows library, so it needs a 32-bit interpreter: "
                + (
                    "install one from python.org (the 32-bit installer) or pass "
                    "python32=<path to python.exe>."
                    if sys.platform == "win32"
                    else "the bridge is Windows-only unless you pass "
                    "python32=<interpreter> explicitly (e.g. a Wine python.exe)."
                )
            )
        self._python32 = found
        return found

    def _argv(self) -> List[str]:
        argv = [
            self.python32,
            # ``med17flasher.seedkey`` imports this module, so runpy warns that
            # it was already in sys.modules before running it as __main__. It is
            # harmless here (the worker keeps no module state), but it would
            # otherwise show up in the stderr tail of every error we report.
            # -W message patterns are literal prefixes, not regexes.
            "-W", "ignore:'med17flasher.seedkey.bridge' found in sys.modules"
                  ":RuntimeWarning",
            "-m", "med17flasher.seedkey.bridge",
            "--dll", self.dll_path,
        ]
        if self.options:
            argv += ["--options", self.options]
        return argv

    def _child_env(self) -> Dict[str, str]:
        """Environment for the worker.

        The 32-bit interpreter almost certainly does not have ``med17flasher``
        installed (it is a bare python.org install, or the app is frozen), so
        put this checkout on its ``PYTHONPATH``. Unbuffered I/O keeps replies
        from sitting in a pipe buffer.
        """

        env = dict(os.environ)
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        if os.path.isdir(os.path.join(root, "med17flasher")):
            env["PYTHONPATH"] = os.pathsep.join(
                [root] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
            )
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def start(self) -> None:
        """Spawn the worker (idempotent)."""

        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            argv = self._argv()
            log.debug("starting seed/key bridge: %s", argv)
            try:
                self._proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,  # line buffered
                    env=self._child_env(),
                    # No console window flashing up in the GUI build. The
                    # attribute only exists on Windows, hence the getattr.
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as exc:
                self._proc = None
                raise SeedKeyError(
                    f"cannot start the 32-bit seed/key bridge ({argv[0]!r}): {exc}"
                ) from exc

            self._replies = queue.Queue()
            self._stderr.clear()
            self._err_thread = threading.Thread(
                target=self._pump_stderr, name="seedkey-bridge-err",
                args=(self._proc.stderr,), daemon=True)
            self._threads = [
                threading.Thread(target=self._pump_stdout, name="seedkey-bridge-out",
                                 args=(self._proc.stdout,), daemon=True),
                self._err_thread,
            ]
            for thread in self._threads:
                thread.start()

    def _pump_stdout(self, pipe) -> None:
        """Feed replies into a queue.

        A plain ``readline()`` on the pipe would block forever if the child
        hangs inside the DLL, and ``select`` does not work on Windows pipes -
        a reader thread plus ``Queue.get(timeout=...)`` is the portable way to
        put a deadline on a reply.
        """

        try:
            while True:
                line = pipe.readline()
                if not line:
                    break
                self._replies.put(line)
        except (OSError, ValueError):  # pipe closed under us
            pass
        finally:
            self._replies.put(_EOF)

    def _pump_stderr(self, pipe) -> None:
        try:
            while True:
                line = pipe.readline()
                if not line:
                    break
                line = line.rstrip()
                if line:
                    self._stderr.append(line)
                    log.debug("bridge child: %s", line)
        except (OSError, ValueError):
            pass

    def _stderr_tail(self, wait: float = 1.0) -> str:
        """The child's last stderr lines - the diagnosis when it dies.

        The pump runs in its own thread, so give it a moment to drain: without
        this the interesting message (``%1 is not a valid Win32 application``)
        regularly loses the race against our error reporting.
        """

        thread = self._err_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=wait)
        return " | ".join(self._stderr) if self._stderr else "(no output)"

    def close(self) -> None:
        """Ask the worker to quit, then make sure it is gone. Idempotent."""

        with self._lock:
            proc, self._proc = self._proc, None
            threads, self._threads = self._threads, []
            self._err_thread = None
            if proc is None:
                return
            try:
                # Polite shutdown first, then EOF on stdin (which ends the
                # worker's read loop even if the "quit" never made it).
                if proc.poll() is None and proc.stdin is not None:
                    try:
                        proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                        proc.stdin.flush()
                    except (OSError, ValueError):
                        pass
                try:
                    if proc.stdin is not None:
                        proc.stdin.close()
                except (OSError, ValueError):
                    pass
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:  # pragma: no cover
                        log.warning("seed/key bridge child did not die")
            finally:
                # The pumps end by themselves once the child's pipes hit EOF, so
                # join *before* closing them - closing a pipe out from under a
                # blocked reader is what makes this kind of code flaky.
                for thread in threads:
                    thread.join(timeout=2.0)
                for stream in (proc.stdout, proc.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except (OSError, ValueError):
                        pass

    def __enter__(self) -> "SeedKeyBridge":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Protocol
    # ------------------------------------------------------------------ #
    def _request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Send one request and return its (successful) reply object.

        Serialised by ``self._lock``: replies are matched to requests purely by
        order, so two concurrent callers must never interleave writes.
        """

        with self._lock:
            self.start()  # lazy: nothing is spawned until a key is asked for
            proc = self._proc
            assert proc is not None  # start() raises otherwise

            try:
                proc.stdin.write(json.dumps(payload) + "\n")  # type: ignore[union-attr]
                proc.stdin.flush()  # type: ignore[union-attr]
            except (OSError, ValueError) as exc:
                tail = self._stderr_tail()
                self.close()
                raise SeedKeyError(
                    f"the 32-bit seed/key bridge died while sending a request "
                    f"({exc}); child said: {tail}"
                ) from exc

            try:
                raw = self._replies.get(timeout=self.timeout)
            except queue.Empty:
                self.close()  # a wedged child would desync the reply stream
                raise SeedKeyError(
                    f"the 32-bit seed/key bridge did not answer within "
                    f"{self.timeout:g}s (DLL {self.dll_path!r})"
                ) from None

            if raw is _EOF:
                code = proc.poll()
                tail = self._stderr_tail()
                self.close()
                raise SeedKeyError(
                    f"the 32-bit seed/key bridge exited (code={code}) while "
                    f"handling {payload.get('cmd')!r}; child said: {tail}"
                )

            try:
                reply = json.loads(raw)
            except ValueError as exc:
                raise SeedKeyError(
                    f"the 32-bit seed/key bridge sent a non-JSON reply: {raw!r}"
                ) from exc
            if not isinstance(reply, dict):
                raise SeedKeyError(
                    f"the 32-bit seed/key bridge sent an unexpected reply: {raw!r}"
                )
            if not reply.get("ok"):
                raise SeedKeyError(
                    f"seed/key DLL error: {reply.get('error') or 'unknown error'}"
                )
            return reply

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def compute(self, ecu: str, level: int, seed: bytes) -> bytes:
        """Resolver interface: turn ``seed`` into a key via the 32-bit DLL.

        ``ecu`` is accepted for interface compatibility; a seed-key DLL is
        already ECU specific, so it is not part of the wire request.
        """

        reply = self._request(
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

        reply = self._request({"cmd": "info"})
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
def _emit(payload: Dict[str, Any]) -> None:
    """Write exactly one reply line and flush (stdout is a pipe: block buffered)."""

    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


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

    while True:
        line = sys.stdin.readline()
        if not line:  # parent closed the pipe / went away
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            if request.get("cmd") == "quit":
                _emit({"ok": True})
                return 0
            reply = _handle(algo, request)
        except Exception as exc:  # noqa: BLE001 - never die on bad input
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        _emit(reply)


if __name__ == "__main__":
    sys.exit(_worker_main(sys.argv[1:]))
