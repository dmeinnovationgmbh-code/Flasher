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


SEED = bytes.fromhex("11223344")


def test_all_builtins_registered():
    for name in ("xor", "add", "sum", "med17", "vag_crc", "fixed"):
        assert name in list_algorithms()


def test_algorithms_deterministic():
    # sa2 needs a 'script'; pass one so it is exercised alongside the rest.
    params = {"k": "0x1C5A36B7", "script": bytes(_SA2_SCRIPT).hex()}
    for name in list_algorithms():
        k1 = compute_key(name, SEED, level=0x11, params=dict(params))
        k2 = compute_key(name, SEED, level=0x11, params=dict(params))
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


# --- VW/Audi SA2 bytecode seed/key ---------------------------------------- #
_SA2_SCRIPT = [0x68, 0x02, 0x81, 0x49, 0x93, 0xa5, 0x5a, 0x55, 0xaa, 0x4a, 0x05,
               0x87, 0x81, 0x05, 0x95, 0x26, 0x68, 0x05, 0x82, 0x49, 0x84, 0x5a,
               0xa5, 0xaa, 0x55, 0x87, 0x03, 0xf7, 0x80, 0x6a, 0x4c]


def test_sa2_known_answer_vector():
    """The published bri3d vector: seed 0x1A1B1C1D -> key 0x6A37F02E. This is
    the proof the SA2 bytecode VM is the real algorithm, not an approximation."""
    from med17flasher.seedkey import compute_key

    key = compute_key("sa2", (0x1A1B1C1D).to_bytes(4, "big"), params={"script": _SA2_SCRIPT})
    assert key == (0x6A37F02E).to_bytes(4, "big")


def test_sa2_accepts_hex_string_and_spaced_forms():
    from med17flasher.seedkey import compute_key

    seed = (0x1A1B1C1D).to_bytes(4, "big")
    ref = compute_key("sa2", seed, params={"script": _SA2_SCRIPT})
    assert compute_key("sa2", seed, params={"script": bytes(_SA2_SCRIPT).hex()}) == ref
    spaced = " ".join(f"{b:02x}" for b in _SA2_SCRIPT)
    assert compute_key("sa2", seed, params={"script": spaced}) == ref


def test_sa2_missing_script_raises():
    from med17flasher.seedkey import compute_key

    with pytest.raises(ValueError):
        compute_key("sa2", b"\x00\x00\x00\x01", params={})


def test_sa2_unknown_opcode_raises():
    from med17flasher.seedkey import compute_key

    with pytest.raises(ValueError):
        compute_key("sa2", b"\x00\x00\x00\x01", params={"script": [0xFF, 0x4C]})
