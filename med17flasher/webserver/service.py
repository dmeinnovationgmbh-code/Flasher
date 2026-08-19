"""The application service behind the web UI.

:class:`FlashService` wires the real flashing engine (a UDS client driving the
in-process :class:`~med17flasher.simulator.VirtualEcu`, or real hardware) to the
web front-end. It runs a genuine UDS flash on a worker thread, throttled to a
realistic CAN data rate so the on-screen KB/s and ETA are truthful, and pushes
structured progress/log events to any number of SSE subscribers.

The OTS-map purchase flow is *simulated* exactly as in the design (no real
payment provider, no real tuning files) - it just unlocks a map for the VIN
after a short delay.
"""

from __future__ import annotations

import glob
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..core import (
    IsoTpConfig,
    IsoTpLayer,
    UdsClient,
    UdsTiming,
    VirtualCanNetwork,
    create_bus,
    load_firmware,
    load_profile,
)
from ..core.can_backends import available_backends
from ..core.ecu_profile import EcuProfile
from ..core.firmware import FirmwareImage
from ..core.flash_sequence import (
    Flasher,
    FlashProgress,
    ProfileSeedKey,
    SeedKeyResolver,
    Stage,
)
from ..logging_setup import get_logger
from ..simulator import VirtualEcu, VirtualEcuConfig

log = get_logger("webserver.service")


# --------------------------------------------------------------------------- #
# Static demo content (matches the design)
# --------------------------------------------------------------------------- #
VEHICLE = {
    "name": "Mercedes-AMG C63 S",
    "chassis": "W205",
    "engine": "M177 · 4.0 V8 Biturbo",
    "ecu": "Bosch MED17.7.5",
    "processor": "TriCore TC1767",
    "vin": "WDD2050871F704112",
    "swNumber": "1779032500",
    "hwNumber": "1779010600",
    "flashSize": "2 MB PFLASH",
    "protection": "Nicht erkannt",
    "connection": {"mode": "OBD", "interface": "PCM-Flash v1.9",
                   "protocol": "UDS · ISO 14229", "bus": "CAN 500k"},
    "file": {"name": "c63_w205_m177_stage1.bin", "size": "2 MB", "checksum": "OK"},
}

# Display sectors (SBOOT is protected and shown pre-filled).
SECTORS = [
    {"name": "SBOOT · 32K", "flex": 1.0, "protected": True, "block": None},
    {"name": "CBOOT · 192K", "flex": 1.6, "protected": False, "block": "CBOOT"},
    {"name": "ASW · 1.3M", "flex": 5.0, "protected": False, "block": "ASW"},
    {"name": "CAL · 480K", "flex": 2.4, "protected": False, "block": "CAL"},
]

MAPS = [
    {"id": "s1", "name": "Stage 1", "sub": "98–102 Oktan · Serienhardware", "base": 499,
     "ps": 590, "psGain": "+80", "nm": 850, "nmGain": "+150",
     "features": ["Ladedruck & Zündung optimiert", "Vmax-Aufhebung",
                  "Prüfsumme automatisch korrigiert"]},
    {"id": "s2", "name": "Stage 2", "sub": "Downpipes ohne Kat erforderlich", "base": 799,
     "ps": 640, "psGain": "+130", "nm": 900, "nmGain": "+200",
     "features": ["Alle Stage-1-Inhalte", "Abgestimmt auf Downpipes ohne Kat",
                  "Prüfsumme automatisch korrigiert"]},
]

BASE_ADDR = 0x80000000
FLASH_SIZE = 0x200000  # 2 MB


@dataclass
class _Subscriber:
    q: "queue.Queue[dict]" = field(default_factory=lambda: queue.Queue(maxsize=1000))


class FlashService:
    def __init__(
        self,
        profile: Optional[EcuProfile] = None,
        *,
        throttle_kbs: float = 180.0,
        backend: str = "simulator",
        firmware_path: Optional[str] = None,
        allow_write: bool = False,
    ) -> None:
        self.profile = profile or _load_default_profile()
        self.throttle_kbs = throttle_kbs
        self.backend = backend
        self.is_simulator = backend == "simulator"
        self.firmware_path = firmware_path
        # Writing to REAL hardware is refused unless explicitly enabled AND a
        # real firmware image is supplied - the demo pattern must never be
        # written to a real ECU.
        self.allow_write = allow_write and not self.is_simulator and bool(firmware_path)

        self._subscribers: List[_Subscriber] = []
        self._sub_lock = threading.Lock()
        self._flash_lock = threading.Lock()
        self._abort = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._running = False

        # purchase state (simulated), keyed by map id
        self.unlocked: Dict[str, bool] = {}

        # last known telemetry
        self._volt = 13.8
        self._speed = 0

        # Display state for the ACTIVE flash. Progress events derive the sector
        # bars and address window from these; they default to the C63 demo
        # layout and are overridden for an expert ("real") flash.
        self._disp_sectors: List[dict] = SECTORS
        self._disp_base = BASE_ADDR
        self._disp_size = FLASH_SIZE
        self._active_profile = self.profile
        self._active_backend = backend
        self._active_is_sim = self.is_simulator
        self._active_resolver: SeedKeyResolver = ProfileSeedKey(self.profile)
        self._active_image_builder = self._build_image
        self._active_allow_write = False

        # Expert ("real flash") configuration, set from the UI.
        self._expert_profile: Optional[EcuProfile] = None
        self._expert_profile_id: Optional[str] = None
        self._expert_backend = "simulator"
        self._expert_seedkey: Dict[str, Any] = {"source": "profile"}
        self._expert_allow_write = False
        self._uploaded: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ #
    # Vehicle / maps / telemetry
    # ------------------------------------------------------------------ #
    def vehicle(self) -> dict:
        data = dict(VEHICLE)
        data["sectors"] = SECTORS
        data["running"] = self._running
        data["volt"] = round(self._volt, 1)
        return data

    def maps(self) -> List[dict]:
        out = []
        for m in MAPS:
            d = dict(m)
            d["unlocked"] = self.unlocked.get(m["id"], False)
            out.append(d)
        return out

    def telemetry(self) -> dict:
        # Idle board voltage wanders a little, like the design.
        if not self._running:
            import math

            self._volt = 13.7 + 0.1 * math.sin(time.time() / 3.0)
        return {"volt": round(self._volt, 1), "speed": self._speed, "running": self._running}

    def buy_map(self, map_id: str, addon: bool = False) -> dict:
        """Simulate a purchase: unlock the map for the VIN after a short delay."""

        if not any(m["id"] == map_id for m in MAPS):
            raise KeyError(map_id)
        time.sleep(1.6)  # mimic the Stripe redirect/confirmation delay
        self.unlocked[map_id] = True
        name = next(m["name"] for m in MAPS if m["id"] == map_id)
        if addon:
            name += " + Pops & Bangs"
        self._broadcast({"type": "log", "cls": "ok",
                         "msg": f"Zahlung bestätigt · {name} freigeschaltet für VIN …4112"})
        return {"unlocked": True, "id": map_id}

    def identify(self) -> List[dict]:
        """Read identification DIDs live from the ECU (simulated or real)."""

        uds, close = self._open_session()
        try:
            out = []
            for did, value in Flasher(uds, self.profile, ProfileSeedKey(self.profile)).identify():
                out.append({"did": f"0x{did:04X}",
                            "value": value.decode("latin-1", "replace").strip("\x00 ")})
            return out
        finally:
            close()

    # ------------------------------------------------------------------ #
    # Real ("expert") flash: profile + backend + firmware + seed/key
    # ------------------------------------------------------------------ #
    def list_profiles(self) -> List[dict]:
        """Bundled ECU profiles the UI can pick (incl. the real med1775 flow)."""

        return list_profile_files()

    def list_backends(self) -> dict:
        """Usable transports + seed/key algorithms for the expert-flash panel."""

        avail = available_backends()
        backends = [{"id": "simulator", "name": "Simulator (virtuelle ECU)",
                     "available": True, "real": False}]
        backends.append({"id": "socketcan:can0", "name": "SocketCAN · can0",
                         "available": bool(avail.get("socketcan")), "real": True})
        for spec, nm in (("pcan:PCAN_USBBUS1", "PEAK PCAN-USB"),
                         ("slcan:/dev/ttyUSB0", "SLCAN (seriell)"),
                         ("vector:0", "Vector"),
                         ("kvaser:0", "Kvaser")):
            backends.append({"id": spec, "name": nm,
                             "available": bool(avail.get("python-can")), "real": True})
        from ..seedkey import list_algorithms

        return {"backends": backends, "seedkeyAlgorithms": list_algorithms()}

    def set_firmware(self, name: Optional[str], data: bytes) -> dict:
        """Accept an uploaded firmware file (raw .bin / Intel-HEX / S-Record)."""

        if not data:
            raise ValueError("leere Firmware-Datei")
        name = os.path.basename(name or "firmware.bin")
        image = _parse_firmware_bytes(name, data, base_address=0)
        self._uploaded = {"name": name, "raw": bytes(data), "size": len(data),
                          "image0": image}
        log.info("firmware uploaded: %s (%d bytes, %d program bytes)",
                 name, len(data), image.total_size)
        return self.firmware_summary()

    def firmware_summary(self) -> Optional[dict]:
        if not self._uploaded:
            return None
        u = self._uploaded
        img: FirmwareImage = u["image0"]
        lo, hi = img.span
        return {
            "name": u["name"],
            "size": u["size"],
            "programBytes": img.total_size,
            "crc32": f"0x{img.checksum('crc32'):08X}",
            "segments": [{"start": f"0x{s.address:08X}", "size": len(s)}
                         for s in img.segments],
            "span": [f"0x{lo:08X}", f"0x{hi:08X}"] if img.segments else None,
            "ext": os.path.splitext(u["name"])[1].lower() or ".bin",
        }

    def _load_profile_by_id(self, profile_id: str) -> EcuProfile:
        for p in list_profile_files():
            if p["id"] == profile_id:
                return load_profile(p["path"])
        raise KeyError(profile_id)

    def configure_expert(self, *, profile_id: Optional[str] = None,
                         backend: Optional[str] = None,
                         seedkey: Optional[dict] = None,
                         allow_write: Optional[bool] = None) -> dict:
        """Set the expert-flash configuration from the UI."""

        if profile_id is not None:
            self._expert_profile = self._load_profile_by_id(profile_id)
            self._expert_profile_id = profile_id
        if backend is not None:
            self._expert_backend = backend
        if seedkey is not None:
            self._expert_seedkey = dict(seedkey)
        if allow_write is not None:
            self._expert_allow_write = bool(allow_write)
        return self.expert_config()

    def expert_config(self) -> dict:
        prof = self._expert_profile
        is_sim = self._expert_backend == "simulator"
        return {
            "profileId": self._expert_profile_id,
            "profileName": prof.name if prof else None,
            "regions": [{"name": r.name, "start": f"0x{r.start:08X}",
                         "size": r.size} for r in prof.memory_map] if prof else [],
            "backend": self._expert_backend,
            "isSimulator": is_sim,
            "seedkey": self._expert_seedkey,
            "allowWrite": self._expert_allow_write,
            "firmware": self.firmware_summary(),
            "ready": bool(prof and self._uploaded),
            # A real (non-sim) write additionally needs the explicit opt-in.
            "willWrite": bool(prof and self._uploaded and (is_sim or self._expert_allow_write)),
        }

    def _build_resolver(self, profile: EcuProfile, cfg: dict) -> SeedKeyResolver:
        src = (cfg or {}).get("source", "profile")
        if src == "profile":
            return ProfileSeedKey(profile)
        if src == "server":
            from ..seedkey.server import SeedKeyClient

            url = (cfg.get("url") or "").strip()
            if not url:
                raise ValueError("Seed/Key-Server-URL fehlt")
            return SeedKeyClient(url)
        if src in ("dll", "exe"):
            from ..seedkey import AlgorithmResolver, make_backend

            path = (cfg.get("path") or "").strip()
            if not path:
                raise ValueError("Pfad zur Seed/Key-DLL/EXE fehlt")
            params = {"options": cfg["options"]} if cfg.get("options") else {}
            return AlgorithmResolver(make_backend(f"{src}:{path}"), params)
        if src == "store":
            from ..seedkey import SeedKeyStore

            path = (cfg.get("path") or "").strip()
            if not path:
                raise ValueError("Pfad zur Seed/Key-Datei fehlt")
            return SeedKeyStore.load(path)
        raise ValueError(f"unbekannte Seed/Key-Quelle: {src!r}")

    def _build_uploaded_image(self, profile: EcuProfile) -> FirmwareImage:
        u = self._uploaded
        if not u:
            raise ValueError("keine Firmware geladen")
        base = profile.memory_map[0].start if profile.memory_map else 0
        return _parse_firmware_bytes(u["name"], u["raw"], base_address=base)

    def start_expert_flash(self) -> bool:
        """Start a real flash using the configured profile/backend/firmware."""

        prof = self._expert_profile
        if prof is None:
            raise ValueError("kein Profil gewählt")
        if not self._uploaded:
            raise ValueError("keine Firmware geladen")

        backend = self._expert_backend
        is_sim = backend == "simulator"
        if is_sim:
            # A real profile may specify a multi-second post-reset settle (e.g.
            # med1775's 10 s); that only makes sense on hardware. Don't make a
            # simulator run wait for it.
            try:
                prof.settle_delay = 0.0
            except Exception:  # noqa: BLE001 - profile is read-only? keep going
                pass
        resolver = self._build_resolver(prof, self._expert_seedkey)
        image = self._build_uploaded_image(prof)  # build up front (validates parse)
        disp = _display_from_profile(prof)

        self._active_profile = prof
        self._active_backend = backend
        self._active_is_sim = is_sim
        self._active_resolver = resolver
        self._active_allow_write = bool(self._expert_allow_write) and not is_sim
        self._active_image_builder = lambda: image
        self._disp_sectors = disp["sectors"]
        self._disp_base = disp["base"]
        self._disp_size = disp["size"]
        return self._begin(None)

    # ------------------------------------------------------------------ #
    # SSE subscription
    # ------------------------------------------------------------------ #
    def subscribe(self) -> _Subscriber:
        sub = _Subscriber()
        with self._sub_lock:
            self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        with self._sub_lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)

    def _broadcast(self, event: dict) -> None:
        with self._sub_lock:
            subs = list(self._subscribers)
        for sub in subs:
            try:
                sub.q.put_nowait(event)
            except queue.Full:  # slow client - drop the event
                pass

    # ------------------------------------------------------------------ #
    # Flash control
    # ------------------------------------------------------------------ #
    @property
    def running(self) -> bool:
        return self._running

    def start_flash(self, map_id: Optional[str] = None) -> bool:
        """Start the C63 *demo* flash (simulated ECU, generated image)."""

        # Reset the active-flash display to the demo layout.
        self._disp_sectors = SECTORS
        self._disp_base = BASE_ADDR
        self._disp_size = FLASH_SIZE
        self._active_profile = self.profile
        self._active_backend = self.backend
        self._active_is_sim = self.is_simulator
        self._active_resolver = ProfileSeedKey(self.profile)
        self._active_image_builder = self._build_image
        self._active_allow_write = self.allow_write
        return self._begin(map_id)

    def _begin(self, map_id: Optional[str] = None) -> bool:
        with self._flash_lock:
            if self._running:
                return False
            self._running = True
            self._abort = threading.Event()
            self._worker = threading.Thread(target=self._run_flash, args=(map_id,), daemon=True)
            self._worker.start()
            return True

    def abort_flash(self) -> None:
        self._abort.set()

    def _make_uds_on(self, bus, profile: Optional[EcuProfile] = None) -> UdsClient:
        profile = profile or self.profile
        tp = IsoTpLayer(bus, IsoTpConfig(tx_id=profile.can.tx_id, rx_id=profile.can.rx_id,
                                         padding_byte=profile.can.padding_byte))
        return UdsClient(tp, UdsTiming(p2=profile.timing.p2, p2_star=profile.timing.p2_star))

    def _open_session(self, profile: Optional[EcuProfile] = None,
                      backend: Optional[str] = None, is_sim: Optional[bool] = None):
        """Open a UDS session against the simulator or the real adapter.

        Returns ``(uds, close)`` where ``close()`` releases the ECU/bus. Defaults
        to the service's configured profile/backend (used by ``identify``); the
        flash worker passes the active flash's profile/backend explicitly.
        """

        profile = profile or self.profile
        backend = backend if backend is not None else self.backend
        is_sim = self.is_simulator if is_sim is None else is_sim

        if is_sim:
            net = VirtualCanNetwork()
            ecu = VirtualEcu(
                net.new_endpoint("ecu"), profile,
                VirtualEcuConfig(security_algorithm=profile.security.algorithm,
                                 security_params=profile.security.params,
                                 max_block_length=0x0FFE),
            )
            ecu.start()
            uds = self._make_uds_on(net.new_endpoint("tester"), profile)
            return uds, ecu.stop
        bus = create_bus(backend)
        return self._make_uds_on(bus, profile), bus.close

    def _build_image(self) -> FirmwareImage:
        """Image for the demo / launch-configured flash (not the expert flash).

        Simulator -> a synthetic demo pattern across the memory map. A real,
        launch-configured backend -> the firmware supplied via ``--firmware``,
        anchored at the first program region for a raw .bin.
        """

        if not self.is_simulator:
            base = self.profile.memory_map[0].start if self.profile.memory_map else 0
            return load_firmware(self.firmware_path, base_address=base)
        image = FirmwareImage()
        for region in self.profile.memory_map:
            image.add_segment(region.start,
                              bytes((region.start >> 12) + i * 7 & 0xFF for i in range(region.size)))
        return image.normalise()

    def _run_flash(self, map_id: Optional[str]) -> None:
        # Safety gate: never write to real hardware without an explicit opt-in.
        if not self._active_is_sim and not self._active_allow_write:
            self._broadcast({"type": "log", "cls": "err",
                             "msg": "Echtes Schreiben gesperrt · verifiziertes Profil + Firmware "
                                    "und Schreibfreigabe erforderlich"})
            self._broadcast({"type": "error", "msg": "real write disabled"})
            self._running = False
            return

        # Reset the per-flash stage-log de-dup so every flash emits its log lines
        # (not just the first flash of this service's lifetime).
        self._logged = set()
        close = None
        start = time.time()
        throttle_state = {"t0": start, "bytes": 0}

        def progress(p: FlashProgress) -> None:
            if p.stage == Stage.TRANSFER:
                self._throttle(p.overall_done, throttle_state)
            self._broadcast(self._progress_event(p, start))
            self._emit_stage_log(p)

        try:
            # Open the session INSIDE the try so a failure to open the bus/ECU
            # surfaces an error and always clears _running in finally.
            uds, close = self._open_session(self._active_profile, self._active_backend,
                                            self._active_is_sim)
            self._broadcast({"type": "log", "cls": "accent", "msg": "Schreibvorgang gestartet"})
            image = self._active_image_builder()
            flasher = Flasher(uds, self._active_profile, self._active_resolver,
                              progress=progress, abort_event=self._abort)
            result = flasher.flash(image)
            self._broadcast({"type": "log", "cls": "ok",
                             "msg": "Schreibvorgang abgeschlossen · Verifikation erfolgreich"})
            self._broadcast({"type": "log", "cls": "",
                             "msg": "ECU-Reset (0x11 01) · Steuergerät startet neu"})
            self._broadcast({"type": "done", "blocks": result.blocks,
                             "duration": round(result.duration, 1)})
        except Exception as exc:  # noqa: BLE001
            self._broadcast({"type": "log", "cls": "err", "msg": f"Fehler: {exc}"})
            self._broadcast({"type": "error", "msg": str(exc)})
        finally:
            if close is not None:
                close()
            self._running = False
            self._speed = 0

    def _throttle(self, bytes_done: int, state: dict) -> None:
        if self.throttle_kbs <= 0:
            return
        target = state["t0"] + (bytes_done / 1024.0) / self.throttle_kbs
        delay = target - time.time()
        if delay > 0:
            time.sleep(min(delay, 0.5))

    def _progress_event(self, p: FlashProgress, start: float) -> dict:
        pct = p.percent
        elapsed = max(1e-3, time.time() - start)
        speed = int(p.overall_done / 1024.0 / elapsed) if p.overall_done else 0
        self._speed = speed
        self._volt = 13.6 + (0.4 * ((int(time.time() * 7) % 10) / 10.0))
        remaining_bytes = max(0, p.overall_total - p.overall_done)
        eta = int(remaining_bytes / 1024.0 / speed) if speed else 0

        # current absolute address
        base, size = self._disp_base, self._disp_size
        cur_addr = base + int(size * pct / 100.0)
        if p.stage == Stage.TRANSFER and p.block_name:
            region = next((r for r in self._active_profile.memory_map if r.name == p.block_name), None)
            if region:
                cur_addr = region.start + p.bytes_done

        return {
            "type": "progress",
            "pct": round(pct, 2),
            "stage": p.stage.value,
            "block": p.block_name,
            "sectors": self._sector_state(p),
            "address": cur_addr,
            "bytesDone": int(size * pct / 100.0),
            "bytesTotal": size,
            "speed": speed,
            "eta": eta,
            "volt": round(self._volt, 1),
            "running": self._running and p.stage not in (Stage.DONE, Stage.FAILED),
        }

    def _sector_state(self, p: FlashProgress) -> List[dict]:
        """Compute per-sector fill/writing/done from the real per-block progress."""

        sectors = self._disp_sectors
        # index of the block currently transferring
        active = p.block_name if p.stage in (Stage.ERASE, Stage.TRANSFER,
                                             Stage.DOWNLOAD, Stage.VERIFY) else None
        out = []
        seen_active = False
        for s in sectors:
            block = s["block"]
            if block is None:  # protected SBOOT
                out.append({"name": s["name"], "flex": s["flex"], "fill": 100,
                            "writing": False, "done": True})
                continue
            if block == active:
                fill = int(100 * p.bytes_done / p.bytes_total) if p.bytes_total else 0
                out.append({"name": s["name"], "flex": s["flex"], "fill": fill,
                            "writing": p.stage == Stage.TRANSFER, "done": False})
                seen_active = True
            elif not seen_active and active is not None:
                # earlier blocks already complete
                out.append({"name": s["name"], "flex": s["flex"], "fill": 100,
                            "writing": False, "done": True})
            elif active is None and p.stage in (Stage.DONE,):
                out.append({"name": s["name"], "flex": s["flex"], "fill": 100,
                            "writing": False, "done": True})
            else:
                out.append({"name": s["name"], "flex": s["flex"], "fill": 0,
                            "writing": False, "done": False})
        return out

    def _emit_stage_log(self, p: FlashProgress) -> None:
        """Emit a few meaningful, de-duplicated log lines mirroring the design."""

        if not hasattr(self, "_logged"):
            self._logged = set()

        def once(key: str, cls: str, msg: str) -> None:
            if key not in self._logged:
                self._logged.add(key)
                self._broadcast({"type": "log", "cls": cls, "msg": msg})

        if p.stage == Stage.SECURITY_ACCESS:
            once("sec", "", "SecurityAccess · Seed/Key erfolgreich")
        elif p.stage == Stage.PROGRAMMING_SESSION:
            once("prog", "", "Programmiersession aktiv (0x10 02)")
        elif p.stage == Stage.ERASE and p.block_name:
            once(f"erase:{p.block_name}", "accent",
                 f"Sektor {p.block_name} · Löschen (0x31 FF00)")
        elif p.stage == Stage.VERIFY and p.block_name:
            once(f"verify:{p.block_name}", "ok",
                 f"Sektor {p.block_name} · Prüfsumme verifiziert")


def _config_dirs() -> List[str]:
    """All directories that may hold bundled ECU profiles, most-specific first."""

    dirs = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:  # PyInstaller bundle
        dirs.append(os.path.join(meipass, "config"))
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    dirs.append(os.path.join(here, "config"))
    dirs.append("config")
    seen = set()
    out = []
    for d in dirs:
        ad = os.path.abspath(d)
        if ad not in seen and os.path.isdir(ad):
            seen.add(ad)
            out.append(ad)
    return out


def _default_profile_path() -> Optional[str]:
    for d in _config_dirs():
        path = os.path.join(d, "med17_7_5_c63.yaml")
        if os.path.isfile(path):
            return path
    return None


def list_profile_files() -> List[Dict[str, str]]:
    """Discover the bundled ECU profiles (``config/*.yaml``|``*.json``).

    Returns ``[{id, name, description, path}]`` sorted by id, de-duplicated by
    filename (the first config dir wins).
    """

    found: Dict[str, Dict[str, str]] = {}
    for d in _config_dirs():
        for path in sorted(glob.glob(os.path.join(d, "*.yaml"))
                           + glob.glob(os.path.join(d, "*.yml"))
                           + glob.glob(os.path.join(d, "*.json"))):
            pid = os.path.splitext(os.path.basename(path))[0]
            if pid in found:
                continue
            name, desc = pid, ""
            try:
                prof = load_profile(path)
                name = prof.name or pid
                desc = getattr(prof, "description", "") or ""
            except Exception as exc:  # noqa: BLE001 - list even if unparsable here
                desc = f"(nicht ladbar: {exc})"
            found[pid] = {"id": pid, "name": name, "description": desc, "path": path}
    return [found[k] for k in sorted(found)]


def _human_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f}M".replace(".0M", "M")
    if n >= 1024:
        return f"{n // 1024}K"
    return f"{n}B"


def _display_from_profile(profile: EcuProfile) -> Dict[str, Any]:
    """Build UI sector bars + address window from a profile's memory map."""

    regions = list(profile.memory_map)
    if not regions:
        return {"sectors": [{"name": "FLASH", "flex": 1.0, "protected": False,
                             "block": None}], "base": 0, "size": 0x1000}
    base = min(r.start for r in regions)
    end = max(r.start + r.size for r in regions)
    total = sum(r.size for r in regions) or 1
    sectors = [
        {"name": f"{r.name} · {_human_size(r.size)}",
         "flex": max(1.0, 6.0 * r.size / total),
         "protected": False, "block": r.name}
        for r in regions
    ]
    return {"sectors": sectors, "base": base, "size": max(end - base, 1)}


def _parse_firmware_bytes(name: str, data: bytes, base_address: int = 0) -> FirmwareImage:
    """Parse uploaded firmware bytes, dispatching on the filename extension.

    Reuses the file loaders (Intel-HEX / S-Record / raw binary) by staging the
    bytes in a temp file with the original extension.
    """

    import tempfile

    ext = os.path.splitext(name or "")[1].lower() or ".bin"
    fd, tmp = tempfile.mkstemp(suffix=ext)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return load_firmware(tmp, base_address=base_address)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _load_default_profile() -> EcuProfile:
    path = _default_profile_path()
    if path:
        try:
            return load_profile(path)
        except Exception as exc:  # noqa: BLE001
            # The most common cause in a frozen build is a bundled *.yaml
            # profile with no PyYAML available. Never let that crash startup -
            # fall back to the equivalent built-in profile so the app still runs.
            log.warning(
                "could not load bundled profile %s (%s); using the built-in "
                "C63 profile instead", path, exc,
            )
    return _builtin_c63_profile()


def _builtin_c63_profile() -> EcuProfile:
    """A C63-shaped profile baked into code (no YAML/JSON needed).

    Used when no profile file is present, or when a bundled YAML profile cannot
    be parsed (e.g. a PyInstaller build without PyYAML).
    """

    from ..core.ecu_profile import (
        CanConfig,
        MemoryRegion,
        RoutineConfig,
        SecurityConfig,
    )

    return EcuProfile(
        name="MED17.7.5",
        can=CanConfig(tx_id=0x7E0, rx_id=0x7E8, padding_byte=0x55),
        security=SecurityConfig(request_seed_level=0x11, send_key_level=0x12,
                                algorithm="med17",
                                params={"k": "0x1C5A36B7", "rounds": 5, "shift": 5}),
        routines=RoutineConfig(),
        memory_map=[
            MemoryRegion("CBOOT", 0x80008000, 0x30000),
            MemoryRegion("ASW", 0x80038000, 0x150000),
            MemoryRegion("CAL", 0x80188000, 0x78000),
        ],
    )
