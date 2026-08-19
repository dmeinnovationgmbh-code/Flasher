"""Web backend (FlashService + WebServer) tests."""

from __future__ import annotations

import json
import queue
import urllib.request

import pytest

from med17flasher.core import (
    CanConfig,
    EcuProfile,
    MemoryRegion,
    RoutineConfig,
    SecurityConfig,
    TimingConfig,
)
from med17flasher.webserver import FlashService, WebServer


def _small_profile() -> EcuProfile:
    # Uses the sector block names the service maps (CBOOT/ASW/CAL) but tiny.
    return EcuProfile(
        name="MED17.7.5",
        can=CanConfig(padding_byte=0x55),
        timing=TimingConfig(p2=1.0, p2_star=2.0, tester_present_period=0.3),
        security=SecurityConfig(request_seed_level=0x11, send_key_level=0x12,
                                algorithm="med17",
                                params={"k": "0x1C5A36B7", "rounds": 5, "shift": 5}),
        routines=RoutineConfig(),
        memory_map=[
            MemoryRegion("CBOOT", 0x80008000, 0x1000, checksum="crc32"),
            MemoryRegion("ASW", 0x80038000, 0x4000, checksum="crc32"),
            MemoryRegion("CAL", 0x80188000, 0x1000, checksum="crc32"),
        ],
    )


@pytest.fixture
def service():
    return FlashService(_small_profile(), throttle_kbs=0)


def test_vehicle_and_maps(service):
    v = service.vehicle()
    assert v["name"] == "Mercedes-AMG C63 S"
    assert [s["name"] for s in v["sectors"]][0].startswith("SBOOT")
    maps = service.maps()
    assert len(maps) == 2
    assert all(not m["unlocked"] for m in maps)


def test_buy_unlocks(service):
    res = service.buy_map("s1", addon=True)
    assert res["unlocked"] is True
    assert service.maps()[0]["unlocked"] is True


def test_buy_unknown_map(service):
    with pytest.raises(KeyError):
        service.buy_map("nope")


def test_live_flash_stream(service):
    sub = service.subscribe()
    assert service.start_flash() is True
    # a second start while running is rejected
    assert service.start_flash() is False

    events = []
    deadline_events = 0
    import time

    end = time.time() + 30
    while time.time() < end:
        try:
            ev = sub.q.get(timeout=5)
        except queue.Empty:
            break
        events.append(ev)
        if ev.get("type") in ("done", "error"):
            break
    service.unsubscribe(sub)

    kinds = {e["type"] for e in events}
    assert "progress" in kinds
    assert "done" in kinds
    assert "error" not in kinds
    progress = [e for e in events if e["type"] == "progress"]
    assert progress[-1]["pct"] == pytest.approx(100.0, abs=0.5) or any(
        e["type"] == "done" for e in events
    )
    # sectors are well-formed
    assert all("fill" in s for s in progress[-1]["sectors"])


def test_http_endpoints():
    svc = FlashService(_small_profile(), throttle_kbs=0)
    with WebServer(svc, port=0) as srv:
        host, port = srv.address
        base = f"http://{host}:{port}"

        def get(path):
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return json.loads(r.read())

        assert get("/api/vehicle")["name"] == "Mercedes-AMG C63 S"
        assert len(get("/api/maps")["maps"]) == 2
        assert "volt" in get("/api/telemetry")

        # start a flash via POST
        req = urllib.request.Request(base + "/api/flash", data=b"{}",
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            assert json.loads(r.read())["started"] is True
