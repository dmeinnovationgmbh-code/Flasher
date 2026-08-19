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

import queue
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
    load_profile,
)
from ..core.ecu_profile import EcuProfile
from ..core.firmware import FirmwareImage
from ..core.flash_sequence import Flasher, FlashProgress, ProfileSeedKey, Stage
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
    ) -> None:
        self.profile = profile or load_profile(_default_profile_path())
        self.throttle_kbs = throttle_kbs
        self.backend = backend

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
        """Read identification DIDs live from the (simulated) ECU."""

        net = VirtualCanNetwork()
        ecu = self._make_ecu(net)
        ecu.start()
        try:
            uds = self._make_uds(net)
            out = []
            for did, value in Flasher(uds, self.profile, ProfileSeedKey(self.profile)).identify():
                out.append({"did": f"0x{did:04X}",
                            "value": value.decode("latin-1", "replace").strip("\x00 ")})
            return out
        finally:
            ecu.stop()

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

    def _make_ecu(self, net: VirtualCanNetwork) -> VirtualEcu:
        return VirtualEcu(
            net.new_endpoint("ecu"), self.profile,
            VirtualEcuConfig(security_algorithm=self.profile.security.algorithm,
                             security_params=self.profile.security.params,
                             max_block_length=0x0FFE),
        )

    def _make_uds(self, net: VirtualCanNetwork) -> UdsClient:
        tp = IsoTpLayer(net.new_endpoint("tester"),
                        IsoTpConfig(tx_id=self.profile.can.tx_id, rx_id=self.profile.can.rx_id,
                                    padding_byte=self.profile.can.padding_byte))
        return UdsClient(tp, UdsTiming(p2=self.profile.timing.p2, p2_star=self.profile.timing.p2_star))

    def _run_flash(self, map_id: Optional[str]) -> None:
        net = VirtualCanNetwork()
        ecu = self._make_ecu(net)
        ecu.start()
        start = time.time()
        self._broadcast({"type": "log", "cls": "accent", "msg": "Schreibvorgang gestartet"})

        # Build a full 2 MB image covering the flashable regions.
        image = FirmwareImage()
        for region in self.profile.memory_map:
            image.add_segment(region.start,
                              bytes((region.start >> 12) + i * 7 & 0xFF for i in range(region.size)))
        image.normalise()

        throttle_state = {"t0": time.time(), "bytes": 0}

        def progress(p: FlashProgress) -> None:
            if p.stage == Stage.TRANSFER:
                self._throttle(p.overall_done, throttle_state)
            self._broadcast(self._progress_event(p, start))
            self._emit_stage_log(p)

        try:
            uds = self._make_uds(net)
            flasher = Flasher(uds, self.profile, ProfileSeedKey(self.profile),
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
            ecu.stop()
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
        cur_addr = BASE_ADDR + int(FLASH_SIZE * pct / 100.0)
        if p.stage == Stage.TRANSFER and p.block_name:
            region = next((r for r in self.profile.memory_map if r.name == p.block_name), None)
            if region:
                cur_addr = region.start + p.bytes_done

        return {
            "type": "progress",
            "pct": round(pct, 2),
            "stage": p.stage.value,
            "block": p.block_name,
            "sectors": self._sector_state(p),
            "address": cur_addr,
            "bytesDone": int(FLASH_SIZE * pct / 100.0),
            "bytesTotal": FLASH_SIZE,
            "speed": speed,
            "eta": eta,
            "volt": round(self._volt, 1),
            "running": self._running and p.stage not in (Stage.DONE, Stage.FAILED),
        }

    def _sector_state(self, p: FlashProgress) -> List[dict]:
        """Compute per-sector fill/writing/done from the real per-block progress."""

        order = [s["block"] for s in SECTORS]
        # index of the block currently transferring
        active = p.block_name if p.stage in (Stage.ERASE, Stage.TRANSFER,
                                             Stage.DOWNLOAD, Stage.VERIFY) else None
        out = []
        seen_active = False
        for s in SECTORS:
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


def _default_profile_path() -> str:
    import os

    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(here, "config", "med17_7_5_c63.yaml")
    return path if os.path.isfile(path) else "config/med17_7_5_c63.yaml"
