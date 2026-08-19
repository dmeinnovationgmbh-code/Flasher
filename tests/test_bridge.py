"""32-bit seed/key bridge tests (against a compiled mock shared library).

The bridge exists because vendor seed-key DLLs are 32-bit Windows libraries a
64-bit interpreter cannot load. The *mechanism* - spawn a helper interpreter,
speak line-based JSON to it, let it do the ctypes call - is platform neutral, so
here it is exercised end to end on Linux by passing ``sys.executable`` as the
"32-bit" Python and a compiled mock ``.so`` as the "DLL". That covers the spawn,
the protocol and the DLL call path; only the actual bitness cannot be faked.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading

import pytest

from med17flasher.exceptions import SeedKeyError
from med17flasher.seedkey import SeedKeyBridge
from med17flasher.seedkey.bridge import _worker_main, find_python32

MOCK_C = r"""
#include <string.h>
typedef unsigned char u8; typedef unsigned long u32;
long GenerateKeyExOpt(u8* seed, u32 seedSize, const char* opt, u8* key, u32* keySize){
  unsigned int v=0; for(u32 i=0;i<seedSize && i<4;i++) v=(v<<8)|seed[i];
  v ^= 0x12345678u;
  key[0]=(v>>24)&0xFF; key[1]=(v>>16)&0xFF; key[2]=(v>>8)&0xFF; key[3]=v&0xFF;
  *keySize=4; return 0;
}
u32 GetSeedLength(const char* d){return 4;}
u32 GetKeyLength(const char* d){return 4;}
long GetECUName(char* buf, u32* size){ strcpy(buf,"MED1775-MOCK"); *size=12; return 0;}
long GetConfiguredAccessTypes(u8* arr, u32* count){ arr[0]=0x05; *count=1; return 0;}
"""

HAS_CC = bool(shutil.which("gcc") or shutil.which("cc"))
needs_cc = pytest.mark.skipif(not HAS_CC, reason="no C compiler for the mock seed-key lib")


def expected_key(seed_hex: str) -> bytes:
    """The mock's transform, in Python: key = seed XOR 0x12345678."""

    return ((int(seed_hex, 16) ^ 0x12345678) & 0xFFFFFFFF).to_bytes(4, "big")


@pytest.fixture(scope="module")
def mock_dll():
    gcc = shutil.which("gcc") or shutil.which("cc")
    if not gcc:
        pytest.skip("no C compiler to build the mock seed-key lib")
    d = tempfile.mkdtemp()
    c = os.path.join(d, "mock.c")
    so = os.path.join(d, "mock_seedkey.so")
    with open(c, "w") as fh:
        fh.write(MOCK_C)
    subprocess.run([gcc, "-shared", "-fPIC", "-o", so, c], check=True)
    yield so
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def bridge(mock_dll):
    """A bridge whose "32-bit Python" is simply the interpreter running us."""

    b = SeedKeyBridge(mock_dll, python32=sys.executable, timeout=30.0)
    yield b
    b.close()


# --------------------------------------------------------------------------- #
# End-to-end: spawn a child, talk JSON, get a key out of a real shared library
# --------------------------------------------------------------------------- #
@needs_cc
def test_bridge_computes_a_key(bridge):
    assert bridge.compute("MED17.7.5", 0x05, bytes.fromhex("11223344")) == expected_key("11223344")


@needs_cc
def test_bridge_info(bridge):
    info = bridge.info()
    assert info["ecu_name"] == "MED1775-MOCK"
    assert info["seed_length"] == 4
    assert info["key_length"] == 4
    assert info["access_types"] == [0x05]
    # The child reports its own bitness - on a real setup this is how you check
    # the bridge really is running a 32-bit interpreter.
    assert info["bits"] in (32, 64)
    assert info["python"]


@needs_cc
def test_bridge_many_requests_reuse_one_child(bridge):
    seeds = ["00000000", "11223344", "deadbeef", "ffffffff", "0000ffff", "a5a5a5a5"]
    for seed in seeds:
        assert bridge.compute("MED17.7.5", 0x05, bytes.fromhex(seed)) == expected_key(seed)
    # One long-lived worker, not one process per key.
    assert bridge._proc is not None
    pid = bridge._proc.pid
    bridge.compute("MED17.7.5", 0x05, bytes.fromhex("11223344"))
    assert bridge._proc.pid == pid


@needs_cc
def test_bridge_is_thread_safe(bridge):
    """Concurrent callers must not interleave request/response pairs."""

    seeds = ["00000001", "00000002", "00000003", "0000dead", "0000beef", "cafebabe"]
    errors = []
    results = {}
    lock = threading.Lock()

    def worker(seed):
        try:
            key = bridge.compute("MED17.7.5", 0x05, bytes.fromhex(seed))
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)
        else:
            with lock:
                results[seed] = key

    threads = [threading.Thread(target=worker, args=(s,)) for s in seeds * 3]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    assert results == {s: expected_key(s) for s in seeds}


@needs_cc
def test_bridge_as_algorithm(mock_dll):
    """The SeedKeyAlgorithm facade used by the seed/key server."""

    with SeedKeyBridge(mock_dll, python32=sys.executable) as b:
        algo = b.as_algorithm()
        assert algo.compute(bytes.fromhex("11223344"), level=0x05) == expected_key("11223344")


@needs_cc
def test_bridge_context_manager_and_close(mock_dll):
    with SeedKeyBridge(mock_dll, python32=sys.executable) as b:
        assert b.compute("MED17.7.5", 0x05, bytes.fromhex("11223344")) == expected_key("11223344")
        proc = b._proc
    assert proc is not None and proc.poll() is not None, "child still running after __exit__"
    assert b._proc is None
    b.close()  # idempotent
    # A closed bridge simply starts a fresh child on the next use.
    assert b.compute("MED17.7.5", 0x05, bytes.fromhex("11223344")) == expected_key("11223344")
    b.close()


@needs_cc
def test_bridge_reports_dll_errors_from_the_child(mock_dll):
    """A bad option/seed becomes a SeedKeyError, and the child survives it."""

    with SeedKeyBridge(mock_dll, python32=sys.executable) as b:
        b.compute("MED17.7.5", 0x05, bytes.fromhex("11223344"))  # start the child
        with pytest.raises(SeedKeyError):
            b._request({"cmd": "key", "level": 5, "seed": "not-hex"})
        # still usable afterwards
        assert b.compute("MED17.7.5", 0x05, bytes.fromhex("11223344")) == expected_key("11223344")


# --------------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------------- #
def test_bridge_bad_interpreter_raises_seedkey_error():
    b = SeedKeyBridge("/no/such/file.dll", python32="/no/such/python32.exe")
    with pytest.raises(SeedKeyError) as exc:
        b.compute("MED17.7.5", 0x05, b"\x11\x22\x33\x44")
    assert "bridge" in str(exc.value)
    b.close()


def test_bridge_missing_dll_reports_the_childs_stderr():
    """The worker exits when the DLL cannot be loaded; the parent must say why."""

    b = SeedKeyBridge("/no/such/seedkey.dll", python32=sys.executable, timeout=30.0)
    with pytest.raises(SeedKeyError) as exc:
        b.compute("MED17.7.5", 0x05, b"\x11\x22\x33\x44")
    assert "seedkey.dll" in str(exc.value)
    # The runpy double-import RuntimeWarning is filtered out in the child, so
    # the reported tail is the actual diagnosis and nothing else.
    assert "RuntimeWarning" not in str(exc.value)
    b.close()


@needs_cc
def test_bridge_accepts_a_relative_dll_path(mock_dll, monkeypatch):
    """dlopen does not search the cwd, so the path must be resolved for the child."""

    monkeypatch.chdir(os.path.dirname(mock_dll))
    with SeedKeyBridge(os.path.basename(mock_dll), python32=sys.executable) as b:
        assert b.compute("MED17.7.5", 0x05, bytes.fromhex("11223344")) == expected_key("11223344")


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX shell stub")
def test_bridge_timeout(tmp_path):
    """A child that never answers must not hang the caller."""

    stub = tmp_path / "sleepy-python"
    stub.write_text("#!/bin/sh\nexec sleep 30\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    b = SeedKeyBridge("/no/such/file.dll", python32=str(stub), timeout=0.5)
    with pytest.raises(SeedKeyError) as exc:
        b.compute("MED17.7.5", 0x05, b"\x11\x22\x33\x44")
    assert "did not answer" in str(exc.value)
    # The wedged child is killed, otherwise the next reply would be off by one.
    assert b._proc is None
    b.close()


@pytest.mark.skipif(sys.platform == "win32", reason="discovery does find a python on Windows")
def test_find_python32_is_none_off_windows():
    assert find_python32(refresh=True) is None
    with pytest.raises(SeedKeyError) as exc:
        SeedKeyBridge("/no/such/file.dll").python32
    assert "Windows-only" in str(exc.value)


# --------------------------------------------------------------------------- #
# The worker itself: it must never die on bad input
# --------------------------------------------------------------------------- #
@needs_cc
def test_worker_survives_malformed_requests(mock_dll):
    proc = subprocess.Popen(
        [sys.executable, "-m", "med17flasher.seedkey.bridge", "--dll", mock_dll],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    try:
        def ask(line):
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())

        assert ask("this is not json")["ok"] is False
        assert ask("[1, 2, 3]")["ok"] is False           # JSON, but not an object
        assert ask("{}")["ok"] is False                  # no cmd
        assert ask('{"cmd": "nope"}')["ok"] is False     # unknown cmd
        assert ask('{"cmd": "key"}')["ok"] is False      # no seed
        assert ask('{"cmd": "key", "seed": "zz"}')["ok"] is False  # bad hex
        assert ask('{"cmd": "ping"}')["ok"] is True

        # ...and it still works after all of that
        reply = ask('{"cmd": "key", "level": 5, "seed": "11223344"}')
        assert reply["ok"] is True
        assert bytes.fromhex(reply["key"]) == expected_key("11223344")

        assert ask('{"cmd": "quit"}')["ok"] is True
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()


@needs_cc
def test_worker_main_exits_cleanly_on_eof(mock_dll, monkeypatch):
    """``_worker_main`` returns 0 when stdin closes (parent went away)."""

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert _worker_main(["--dll", mock_dll]) == 0


def test_worker_main_reports_a_bad_dll(capsys):
    assert _worker_main(["--dll", "/no/such/seedkey.dll"]) == 2
    assert "seedkey.dll" in capsys.readouterr().err
