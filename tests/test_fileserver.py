"""Firmware repository + HTTP file server + client tests."""

from __future__ import annotations

import os
import tempfile

import pytest

from med17flasher.exceptions import RepositoryError
from med17flasher.server import FileServer, FileServerClient, FirmwareRepository


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as d:
        yield FirmwareRepository(os.path.join(d, "repo"))


def test_repository_add_get_delete(repo):
    data = bytes(range(256)) * 4
    meta = repo.add(data, "asw.bin", ecu="MED17.7.5", sw_version="123", base_address=0x80040000)
    assert meta.size == len(data)
    assert repo.get_data(meta.id) == data
    assert repo.verify(meta.id)
    assert len(repo.list(ecu="MED17.7.5")) == 1
    repo.delete(meta.id)
    with pytest.raises(RepositoryError):
        repo.get_meta(meta.id)


def test_repository_dedup(repo):
    data = b"identical"
    m1 = repo.add(data, "a.bin")
    m2 = repo.add(data, "a.bin")
    assert m1.id == m2.id  # same content+name deduped


def test_repository_persists_index(repo):
    meta = repo.add(b"abc", "x.bin", sw_version="v1")
    repo2 = FirmwareRepository(repo.root)  # reopen same dir
    assert repo2.get_meta(meta.id).sw_version == "v1"


def test_http_server_full_cycle(repo):
    fw = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
    fw.write(bytes(range(256)) * 8)
    fw.close()
    try:
        with FileServer(repo, port=0, token="secret") as srv:
            client = FileServerClient(srv.url, token="secret")
            assert client.health()
            meta = client.upload(fw.name, ecu="MED17.7.5", sw_version="9",
                                  base_address=0x80040000)
            listing = client.list(ecu="MED17.7.5")
            assert len(listing) == 1
            image = client.download_image(meta["id"])
            assert image.read(0x80040000, 256 * 8) == bytes(range(256)) * 8
            assert image.metadata["sw_version"] == "9"
            client.delete(meta["id"])
            assert client.list() == []
    finally:
        os.remove(fw.name)


def test_http_auth_enforced(repo):
    fw = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
    fw.write(b"data")
    fw.close()
    try:
        with FileServer(repo, port=0, token="secret") as srv:
            bad = FileServerClient(srv.url)  # no token
            with pytest.raises(RepositoryError):
                bad.upload(fw.name)
            # reads are allowed without a token by default
            assert bad.list() == []
    finally:
        os.remove(fw.name)
