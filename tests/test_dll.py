"""Seed-key DLL / EXE backend tests (against a compiled mock shared lib)."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

import pytest

from med17flasher.seedkey import AlgorithmResolver, make_backend
from med17flasher.seedkey.dll import DllSeedKey, ExeSeedKey
from med17flasher.seedkey.server import SeedKeyHttpServer, SeedKeyService

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

EXPECTED = (0x11223344 ^ 0x12345678).to_bytes(4, "big")


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


def test_dll_compute(mock_dll):
    algo = DllSeedKey(mock_dll, loader=ctypes.CDLL)
    assert algo.compute(bytes.fromhex("11223344"), level=0x05) == EXPECTED


def test_dll_introspection(mock_dll):
    algo = DllSeedKey(mock_dll, loader=ctypes.CDLL)
    assert algo.ecu_name() == "MED1775-MOCK"
    assert algo.seed_length() == 4
    assert algo.key_length() == 4
    assert algo.access_types() == [0x05]


def test_dll_missing_file():
    from med17flasher.exceptions import SeedKeyError

    algo = DllSeedKey("/no/such/file.dll", loader=ctypes.CDLL)
    with pytest.raises(SeedKeyError):
        algo.compute(b"\x00\x00\x00\x00")


def test_algorithm_resolver(mock_dll):
    resolver = AlgorithmResolver(DllSeedKey(mock_dll, loader=ctypes.CDLL))
    assert resolver.compute("MED17.7.5", 0x05, bytes.fromhex("11223344")) == EXPECTED


def test_dll_seedkey_server(mock_dll):
    algo = DllSeedKey(mock_dll, loader=ctypes.CDLL)
    svc = SeedKeyService(algorithm=algo)
    with SeedKeyHttpServer(svc, port=0) as srv:
        host, port = srv.address
        base = f"http://{host}:{port}"
        # production-compatible GET /key/<level>/<seed>
        with urllib.request.urlopen(f"{base}/key/05/11223344", timeout=3) as r:
            assert json.loads(r.read())["key"] == EXPECTED.hex()
        # POST /seedkey
        body = json.dumps({"ecu": "MED17.7.5", "level": 5, "seed": "11223344"}).encode()
        req = urllib.request.Request(f"{base}/seedkey", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            assert json.loads(r.read())["key"] == EXPECTED.hex()


def test_exe_backend():
    # a tiny "seed-key exe": prints key = seed ^ 0x12345678
    script = (
        "import sys\n"
        "seed=int(sys.argv[1],16)\n"
        "print(format(seed ^ 0x12345678, '08x'))\n"
    )
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sk.py")
        with open(path, "w") as fh:
            fh.write(script)
        exe = ExeSeedKey(sys.executable, argv_template=[path, "{seed}"])
        assert exe.compute(bytes.fromhex("11223344")) == EXPECTED


def test_make_backend_dispatch(mock_dll):
    assert isinstance(make_backend(f"dll:{mock_dll}", loader=ctypes.CDLL), DllSeedKey)
    assert isinstance(make_backend("exe:/bin/true"), ExeSeedKey)
