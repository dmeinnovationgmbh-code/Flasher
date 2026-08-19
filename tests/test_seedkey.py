"""Seed/key framework, algorithms, store and server tests."""

from __future__ import annotations

import os
import socket
import tempfile

import pytest

from med17flasher.exceptions import AlgorithmNotFoundError, SeedKeyError
from med17flasher.seedkey import (
    SeedKeyStore,
    compute_key,
    get_algorithm,
    list_algorithms,
    load_plugin,
    register_function,
)
from med17flasher.seedkey.server import (
    SeedKeyClient,
    SeedKeyHttpServer,
    SeedKeyService,
    SeedKeyTcpServer,
)
from med17flasher.seedkey.store import SeedKeyEntry


SEED = bytes.fromhex("11223344")


def test_all_builtins_registered():
    for name in ("xor", "add", "sum", "med17", "vag_crc", "fixed"):
        assert name in list_algorithms()


def test_algorithms_deterministic():
    for name in list_algorithms():
        k1 = compute_key(name, SEED, level=0x11, params={"k": "0x1C5A36B7"})
        k2 = compute_key(name, SEED, level=0x11, params={"k": "0x1C5A36B7"})
        assert k1 == k2


def test_xor_roundtrip():
    key = compute_key("xor", SEED, params={"k": 0x00000000})
    assert key == SEED  # XOR with zero is identity


def test_med17_level_dependence():
    a = compute_key("med17", SEED, level=0x01, params={"k": "0x1C5A36B7"})
    b = compute_key("med17", SEED, level=0x11, params={"k": "0x1C5A36B7"})
    assert a != b  # different access levels yield different keys


def test_unknown_algorithm():
    with pytest.raises(AlgorithmNotFoundError):
        get_algorithm("does-not-exist")


def test_store_roundtrip():
    store = SeedKeyStore.default()
    key = store.compute("MED17.7.5", 0x11, SEED)
    assert key == compute_key("med17", SEED, level=0x11,
                              params={"k": "0x1C5A36B7", "rounds": 5, "shift": 5})
    # persistence
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "store.json")
        store.save(path)
        reloaded = SeedKeyStore.load(path)
        assert reloaded.compute("MED17.7.5", 0x11, SEED) == key


def test_store_missing_entry():
    store = SeedKeyStore()
    with pytest.raises(SeedKeyError):
        store.compute("NoSuchEcu", 0x11, SEED)


def test_register_function_plugin():
    register_function("t_double", lambda seed, level, params: bytes((b * 2) & 0xFF for b in seed))
    assert compute_key("t_double", b"\x01\x02") == b"\x02\x04"


def test_load_plugin_file():
    src = (
        "from med17flasher.seedkey import register_function\n"
        "register_function('plugin_addone', lambda seed, level, params: "
        "bytes(((b+1)&0xFF) for b in seed))\n"
    )
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "plug.py")
        with open(path, "w") as fh:
            fh.write(src)
        added = load_plugin(path)
        assert "plugin_addone" in added
        assert compute_key("plugin_addone", b"\x00\x01") == b"\x01\x02"


def test_http_server_client():
    svc = SeedKeyService(SeedKeyStore.default())
    with SeedKeyHttpServer(svc, port=0) as srv:
        host, port = srv.address
        client = SeedKeyClient(f"http://{host}:{port}")
        key = client.compute("MED17.7.5", 0x11, SEED)
        assert key == svc.store.compute("MED17.7.5", 0x11, SEED)


def test_tcp_server():
    svc = SeedKeyService(SeedKeyStore.default())
    with SeedKeyTcpServer(svc, port=0) as srv:
        host, port = srv.address
        s = socket.create_connection((host, port), timeout=3)
        s.sendall(b"MED17.7.5 0x11 11223344\n")
        resp = s.recv(100).decode().strip()
        s.close()
        assert resp.startswith("OK ")
        assert resp.split()[1] == svc.store.compute("MED17.7.5", 0x11, SEED).hex()
