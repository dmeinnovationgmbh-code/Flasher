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


def test_service_starts_when_yaml_profile_cannot_be_loaded(monkeypatch):
    """Frozen desktop builds may bundle a *.yaml profile but no PyYAML.

    load_profile() then raises; the service must fall back to the built-in
    profile instead of crashing the app at startup (the "black window flashes
    and closes" bug on Windows).
    """

    import med17flasher.webserver.service as svc_mod

    def boom(*_a, **_k):
        raise RuntimeError("PyYAML is required to read YAML profiles")

    monkeypatch.setattr(svc_mod, "load_profile", boom)
    # Must not raise, and must come up with a usable profile.
    service = FlashService()
    names = [r.name for r in service.profile.memory_map]
    assert names == ["CBOOT", "ASW", "CAL"]


def test_builtin_profile_is_self_contained():
    from med17flasher.webserver.service import _builtin_c63_profile

    prof = _builtin_c63_profile()
    assert prof.security.request_seed_level == 0x11
    assert [r.name for r in prof.memory_map] == ["CBOOT", "ASW", "CAL"]


def test_desktop_selftest_passes_on_a_good_build():
    import med17flasher.desktop as desktop

    # The real startup path (server + bundled UI + API) must return 0.
    assert desktop._selftest() == 0


def test_desktop_selftest_fails_when_ui_missing(monkeypatch):
    import med17flasher.desktop as desktop
    import med17flasher.webserver.app as appmod

    # Simulate a broken build where webui/dist was not bundled: the selftest
    # must FAIL (return 1) instead of shipping a green CI.
    monkeypatch.setattr(appmod, "_static_root", lambda: None)
    assert desktop._selftest() == 1


def test_desktop_selftest_never_raises(monkeypatch):
    """A windowed Windows build must never let the selftest raise (it would pop
    a blocking MessageBox and hang CI). It returns 1 instead."""

    import med17flasher.desktop as desktop

    monkeypatch.setattr(desktop, "WebServer", None, raising=False)

    def boom(*_a, **_k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("med17flasher.webserver.WebServer", boom)
    assert desktop._selftest() == 1


def test_desktop_main_keeps_console_on_startup_error(monkeypatch):
    """A startup crash must be reported and return 1, never propagate (which
    would just close the console window)."""

    import med17flasher.desktop as desktop

    monkeypatch.setattr(desktop, "_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    # input() on the error path hits EOF under pytest and is swallowed.
    assert desktop.main([]) == 1


def test_list_profiles_includes_bundled(service):
    profs = service.list_profiles()
    ids = {p["id"] for p in profs}
    assert "med17_7_5_med1775" in ids  # the real production flow
    assert "med17_7_5_c63" in ids
    assert all(p["path"] and p["id"] for p in profs)


def test_list_backends(service):
    b = service.list_backends()
    ids = [x["id"] for x in b["backends"]]
    assert "simulator" in ids
    assert any(x["real"] for x in b["backends"])
    assert not b["backends"][0]["real"]  # simulator first, non-real
    assert "med17" in b["seedkeyAlgorithms"]


def test_firmware_upload_summary(service):
    data = bytes(range(256)) * 16  # 4096 bytes, one contiguous segment
    summ = service.set_firmware("stage1.bin", data)
    assert summ["size"] == 4096
    assert summ["name"] == "stage1.bin"
    assert summ["programBytes"] == 4096
    assert summ["crc32"].startswith("0x") and len(summ["crc32"]) == 10
    assert summ["segments"] and summ["segments"][0]["size"] == 4096
    assert service.firmware_summary()["size"] == 4096


def test_firmware_upload_rejects_empty(service):
    with pytest.raises(ValueError):
        service.set_firmware("x.bin", b"")


def test_configure_expert_unknown_profile(service):
    with pytest.raises(KeyError):
        service.configure_expert(profile_id="does-not-exist")


def test_build_resolver_sources(service):
    from med17flasher.webserver.service import _builtin_c63_profile

    prof = _builtin_c63_profile()
    assert hasattr(service._build_resolver(prof, {"source": "profile"}), "compute")
    assert hasattr(service._build_resolver(prof, {"source": "server",
                                                  "url": "http://h:1/"}), "compute")
    for bad in ({"source": "server"}, {"source": "dll"}, {"source": "exe"},
                {"source": "store"}, {"source": "bogus"}):
        with pytest.raises(ValueError):
            service._build_resolver(prof, bad)


def _drain(sub, kinds=("done", "error"), timeout=30):
    import time

    events = []
    end = time.time() + timeout
    while time.time() < end:
        try:
            ev = sub.q.get(timeout=5)
        except queue.Empty:
            break
        events.append(ev)
        if ev.get("type") in kinds:
            break
    return events


def test_expert_flash_refuses_real_write_without_optin():
    """A real (non-sim) backend without the explicit opt-in must refuse before
    ever opening the bus (so it is safe even with no hardware present)."""

    svc = FlashService(throttle_kbs=0)
    svc.set_firmware("fw.bin", bytes(0x1000))
    svc.configure_expert(profile_id="med17_7_5_demo", backend="socketcan:can0",
                         allow_write=False)
    assert svc.expert_config()["willWrite"] is False
    sub = svc.subscribe()
    assert svc.start_expert_flash() is True
    events = _drain(sub, kinds=("error", "done"), timeout=6)
    svc.unsubscribe(sub)
    errs = [e for e in events if e["type"] == "error"]
    assert errs and "disabled" in errs[0]["msg"]
    assert not any(e["type"] == "done" for e in events)


def test_expert_flash_simulator_end_to_end():
    svc = FlashService(throttle_kbs=0)
    # The 'demo' profile has two tiny regions at 0x80040000 (0x2000) and
    # 0x80042000 (0x1000); a 0x3000 image anchored at the base covers both.
    image = bytes([0x60]) + bytes(0x2FFE) + bytes([0xDE])
    svc.set_firmware("cal.bin", image)
    cfg = svc.configure_expert(profile_id="med17_7_5_demo", backend="simulator",
                               seedkey={"source": "profile"})
    assert cfg["ready"] is True
    assert cfg["willWrite"] is True          # simulator always "writes"
    assert len(cfg["regions"]) == 2

    sub = svc.subscribe()
    assert svc.start_expert_flash() is True
    events = _drain(sub)
    svc.unsubscribe(sub)

    kinds = {e["type"] for e in events}
    assert "done" in kinds, kinds
    assert "error" not in kinds
    prog = [e for e in events if e["type"] == "progress"]
    # sector bars come from the demo profile (two regions), not the C63 demo (four)
    assert len(prog[-1]["sectors"]) == 2


def test_expert_endpoints_over_http():
    svc = FlashService(throttle_kbs=0)
    with WebServer(svc, port=0) as srv:
        host, port = srv.address
        base = f"http://{host}:{port}"

        def get(path):
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return json.loads(r.read())

        def post(path, data, ctype="application/json"):
            req = urllib.request.Request(base + path, data=data,
                                         headers={"Content-Type": ctype}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())

        assert any(p["id"] == "med17_7_5_med1775" for p in get("/api/profiles")["profiles"])
        assert "simulator" in [b["id"] for b in get("/api/backends")["backends"]]

        up = post("/api/firmware?name=cal.bin", bytes(0x1000), "application/octet-stream")
        assert up["firmware"]["size"] == 0x1000

        cfg = post("/api/expert/config", json.dumps(
            {"profileId": "med17_7_5_demo", "backend": "simulator"}).encode())
        assert cfg["ready"] is True
        assert get("/api/expert")["profileId"] == "med17_7_5_demo"


def test_scan_reports_ecu(service):
    rep = service.scan()
    assert rep["online"] is True
    assert rep["txId"].startswith("0x")
    assert rep["identification"], "scan should read at least one DID"
    assert rep["programmingLevel"] is not None


def test_read_memory_and_guards(service):
    out = service.read_memory(0x80008000, 16)
    assert out["size"] == 16
    assert len(out["hex"]) == 32
    assert out["crc32"].startswith("0x")
    for bad in (0, -1, 0x20000):
        with pytest.raises(ValueError):
            service.read_memory(0x80008000, bad)


def test_checksum_requires_firmware(service):
    with pytest.raises(ValueError):
        service.checksum_report()
    with pytest.raises(ValueError):
        service.checksum_correct()


def test_checksum_report_on_upload(service):
    service.set_firmware("cal.bin", bytes(0x1000))
    rep = service.checksum_report()
    assert rep["name"] == "cal.bin"
    # A blank calibration has no MEDC17 descriptor blocks; that must not error.
    assert rep["blocks"] == 0
    assert rep["regions"] == []


def test_measurement_streams_samples(service):
    sub = service.subscribe()
    assert service.start_measure(rate=100) is True
    assert service.start_measure() is False       # already running
    events = _drain(sub, kinds=("sample",), timeout=10)
    # let a few more arrive, then stop
    import time as _t

    _t.sleep(0.4)
    service.stop_measure()
    _t.sleep(0.4)
    service.unsubscribe(sub)

    assert any(e["type"] == "sample" for e in events)
    sample = next(e for e in events if e["type"] == "sample")
    assert {"rpm", "coolant", "battery"} <= set(sample["values"])
    csv = service.measure_csv()
    assert csv.splitlines()[0] == "time_s,rpm,coolant,battery"
    assert len(csv.splitlines()) > 1
    assert service.measure_config()["running"] is False


def test_measurement_rejects_bad_signal(service):
    with pytest.raises(ValueError):
        service.start_measure(signals=["this-is-not-a-spec"])


def test_diagnostic_endpoints_over_http():
    svc = FlashService(throttle_kbs=0)
    with WebServer(svc, port=0) as srv:
        host, port = srv.address
        base = f"http://{host}:{port}"

        def get(path):
            with urllib.request.urlopen(base + path, timeout=30) as r:
                return r.read()

        assert json.loads(get("/api/scan"))["online"] is True
        mem = json.loads(get("/api/memory?address=0x80008000&size=32"))
        assert mem["size"] == 32
        assert json.loads(get("/api/measure"))["running"] is False
        # CSV export is served as a downloadable file even when empty.
        assert get("/api/measure/csv").startswith(b"time_s")


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


# --------------------------------------------------------------------------- #
# Firmware repository (file server) wired into the app
# --------------------------------------------------------------------------- #
def test_repo_pull_stages_firmware_for_flashing():
    """A workshop keeps one firmware library; the app pulls from it.

    Driven against a real running file server, so the whole chain is exercised:
    configure -> health check -> list -> download -> parse -> staged.
    """

    import os
    import tempfile

    from med17flasher.server import FileServer, FirmwareRepository

    data = bytes((i * 3) & 0xFF for i in range(0x800))
    with tempfile.TemporaryDirectory() as d:
        repo = FirmwareRepository(os.path.join(d, "repo"))
        meta = repo.add(data, "asw_stage1.bin", ecu="MED17.7.5", sw_version="1779032500")
        with FileServer(repo, port=0, token="secret") as srv:
            svc = FlashService(throttle_kbs=0)
            assert svc.repo_config() == {"url": "", "hasToken": False}

            cfg = svc.set_repo(url=srv.url, token="secret")
            assert cfg["reachable"] is True
            assert cfg["hasToken"] is True
            # The token is never handed back out to the browser.
            assert "secret" not in str(cfg)

            listing = svc.repo_list()
            assert [f["id"] for f in listing["firmwares"]] == [meta.id]

            summary = svc.repo_use(meta.id)
            assert summary["source"] == "repo"
            assert summary["name"] == "asw_stage1.bin"
            assert summary["size"] == len(data)
            # Staged exactly like an uploaded file, so a flash can use it.
            assert svc.firmware_summary()["name"] == "asw_stage1.bin"
            assert svc.expert_config()["repo"]["url"] == srv.url


def test_repo_reports_an_unreachable_server_instead_of_failing_later():
    svc = FlashService(throttle_kbs=0)
    cfg = svc.set_repo(url="http://127.0.0.1:9")  # nothing listens there
    assert cfg["reachable"] is False
    assert cfg["error"]
    with pytest.raises(Exception):
        svc.repo_list()


def test_repo_use_needs_a_configured_server():
    svc = FlashService(throttle_kbs=0)
    with pytest.raises(ValueError):
        svc.repo_use("")
    svc.set_repo(url="")
    with pytest.raises(ValueError):
        svc.repo_use("some-id")


# --------------------------------------------------------------------------- #
# Seed/key: a 32-bit vendor DLL must work from this 64-bit app
# --------------------------------------------------------------------------- #
def test_seedkey_dll_source_loads_in_process_when_bitness_matches(tmp_path):
    """`source: dll` resolves through the real ctypes path when it can."""

    import shutil
    import subprocess

    cc = shutil.which("gcc") or shutil.which("cc")
    if not cc:
        pytest.skip("no C compiler for the mock seed-key lib")
    src = tmp_path / "mock.c"
    src.write_text(
        "typedef unsigned char u8; typedef unsigned int u32;\n"
        "long GenerateKeyExOpt(u8* s, u32 n, const char* o, u8* k, u32* kn){\n"
        "  for (u32 i=0;i<4;i++) k[i] = s[i] ^ 0xA5; *kn=4; return 0; }\n"
    )
    so = tmp_path / "mock_seedkey.so"
    subprocess.run([cc, "-shared", "-fPIC", "-o", str(so), str(src)], check=True)

    svc = FlashService(_small_profile(), throttle_kbs=0)
    resolver = svc._build_resolver(_small_profile(), {"source": "dll", "path": str(so)})
    key = resolver.compute("MED17.7.5", 0x05, bytes([0x11, 0x22, 0x33, 0x44]))
    assert key == bytes([0x11 ^ 0xA5, 0x22 ^ 0xA5, 0x33 ^ 0xA5, 0x44 ^ 0xA5])


def test_seedkey_dll_falls_back_to_the_32bit_bridge(monkeypatch):
    """A 64-bit app must still drive a 32-bit vendor DLL, not just give up."""

    import med17flasher.seedkey.bridge as bridge_mod
    from med17flasher.exceptions import SeedKeyError

    calls = {}

    def fake_load(self):
        raise SeedKeyError(
            "cannot load seed-key DLL: [WinError 193] %1 is not a valid Win32 "
            "application. It is a 32-bit Windows DLL")

    class FakeBridge:
        def __init__(self, path, **kw):
            calls["path"] = path
            calls["kw"] = kw

    monkeypatch.setattr("med17flasher.seedkey.dll.DllSeedKey._load", fake_load)
    monkeypatch.setattr(bridge_mod, "SeedKeyBridge", FakeBridge)

    svc = FlashService(_small_profile(), throttle_kbs=0)
    resolver = svc._build_resolver(
        _small_profile(), {"source": "dll", "path": "MED1775_12_42_00.dll"})
    assert isinstance(resolver, FakeBridge)
    assert calls["path"] == "MED1775_12_42_00.dll"


def test_seedkey_dll_error_that_is_not_bitness_is_not_swallowed(monkeypatch):
    from med17flasher.exceptions import SeedKeyError

    def fake_load(self):
        raise SeedKeyError("seed-key DLL not found: 'nope.dll'")

    monkeypatch.setattr("med17flasher.seedkey.dll.DllSeedKey._load", fake_load)
    svc = FlashService(_small_profile(), throttle_kbs=0)
    with pytest.raises(SeedKeyError, match="not found"):
        svc._build_resolver(_small_profile(), {"source": "dll", "path": "nope.dll"})


def test_seedkey_source_bridge_is_selectable(monkeypatch):
    import med17flasher.seedkey.bridge as bridge_mod

    seen = {}

    class FakeBridge:
        def __init__(self, path, **kw):
            seen["path"], seen["kw"] = path, kw

    monkeypatch.setattr(bridge_mod, "SeedKeyBridge", FakeBridge)
    monkeypatch.setattr("med17flasher.seedkey.SeedKeyBridge", FakeBridge)
    svc = FlashService(_small_profile(), throttle_kbs=0)
    resolver = svc._build_resolver(
        _small_profile(),
        {"source": "bridge", "path": "v.dll", "python32": "C:\\py32\\python.exe"})
    assert isinstance(resolver, FakeBridge)
    assert seen["kw"]["python32"] == "C:\\py32\\python.exe"


def _drain_until_sniff_stopped(sub, timeout=30):
    import time
    events = []
    end = time.time() + timeout
    while time.time() < end:
        try:
            ev = sub.q.get(timeout=5)
        except queue.Empty:
            break
        events.append(ev)
        if ev.get("type") == "sniff" and ev.get("event") == "stopped":
            break
    return events


def test_sniff_simulator_end_to_end():
    """The Sniffer tab's backend: sniff a self-driven demo flash and derive the
    seed/key + download blocks purely from the (passively observed) traffic."""
    svc = FlashService(throttle_kbs=0)
    sub = svc.subscribe()
    try:
        assert svc.start_sniff(backend="simulator",
                               profile_id="med17_7_5_demo") is True
        assert svc.start_sniff(backend="simulator") is False   # already running
        events = _drain_until_sniff_stopped(sub)
    finally:
        svc.unsubscribe(sub)

    sniff = [e for e in events if e.get("type") == "sniff"]
    stages = {e["event"] for e in sniff}
    assert {"started", "flow", "report", "stopped"} <= stages

    flow_kinds = {e["kind"] for e in sniff if e["event"] == "flow"}
    assert {"seedkey", "download"} <= flow_kinds

    report = next(e for e in sniff if e["event"] == "report")
    pairs = report["seedKeyPairs"]
    assert any(p["seed"] == "11223344" for p in pairs)
    addrs = {b["address"] for b in report["downloadBlocks"]}
    assert "0x80040000" in addrs and "0x80042000" in addrs

    # the derived artefacts are downloadable
    name, yaml_text = svc.sniff_download("profile")
    assert name.endswith(".yaml") and "MED17" in yaml_text
    _, pairs_text = svc.sniff_download("pairs")
    assert "11223344" in pairs_text


def test_sniff_config_reports_idle():
    svc = FlashService(throttle_kbs=0)
    cfg = svc.sniff_config()
    assert cfg["running"] is False
    assert cfg["report"] is None
