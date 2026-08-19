"""HTTP server for the MED17 Flash Tool web UI.

Serves the built React app plus a small JSON/SSE API backed by
:class:`~med17flasher.webserver.service.FlashService`. Dependency free
(``http.server`` only), so ``med17flasher webserver`` just works.

API
---
``GET  /api/vehicle``        vehicle + ECU + sector metadata
``GET  /api/maps``           OTS maps (with unlocked state)
``GET  /api/telemetry``      live board voltage / speed
``GET  /api/identify``       identification DIDs (live from the ECU)
``POST /api/flash``          start the C63 demo flash -> {started: bool}
``POST /api/flash/abort``    abort the running flash
``GET  /api/flash/stream``   Server-Sent Events: progress / log / done / error
``POST /api/maps/<id>/buy``  simulate a purchase      -> {unlocked: true}

Expert / real flash:
``GET  /api/profiles``       bundled ECU profiles (incl. med1775)
``GET  /api/backends``       usable CAN transports + seed/key algorithms
``GET  /api/expert``         current expert-flash configuration
``POST /api/firmware?name=`` upload a firmware file (raw body) -> {firmware:…}
``POST /api/expert/config``  set profile/backend/seedkey/allowWrite
``POST /api/expert/flash``   start the configured real flash  -> {started: bool}
"""

from __future__ import annotations

import json
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

from ..logging_setup import get_logger
from .service import FlashService

log = get_logger("webserver.http")

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ico": "image/x-icon",
    ".map": "application/json",
}


def _static_root() -> Optional[str]:
    """Locate the built web UI (webui/dist), if present."""

    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "static"),  # packaged copy
        os.path.normpath(os.path.join(here, "..", "..", "webui", "dist")),  # repo build
    ]
    # PyInstaller one-file bundle extracts data files under sys._MEIPASS.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.insert(0, os.path.join(meipass, "webui", "dist"))
    for path in candidates:
        if os.path.isdir(path) and os.path.isfile(os.path.join(path, "index.html")):
            return path
    return None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    service: FlashService
    static_root: Optional[str]

    def log_message(self, fmt: str, *args) -> None:
        log.debug("HTTP %s - " + fmt, self.address_string(), *args)

    # -- helpers -------------------------------------------------------- #
    @property
    def _svc(self) -> FlashService:
        return self.server.service  # type: ignore[attr-defined]

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def _csv(self, text: str, filename: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    # -- verbs ---------------------------------------------------------- #
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/vehicle":
            self._json(200, self._svc.vehicle())
        elif path == "/api/maps":
            self._json(200, {"maps": self._svc.maps()})
        elif path == "/api/telemetry":
            self._json(200, self._svc.telemetry())
        elif path == "/api/identify":
            try:
                self._json(200, {"dids": self._svc.identify()})
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
        elif path == "/api/profiles":
            self._json(200, {"profiles": self._svc.list_profiles()})
        elif path == "/api/backends":
            self._json(200, self._svc.list_backends())
        elif path == "/api/expert":
            self._json(200, self._svc.expert_config())
        elif path == "/api/scan":
            try:
                deep = parse_qs(urlparse(self.path).query).get("deep", ["0"])[0] == "1"
                self._json(200, self._svc.scan(deep=deep))
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
        elif path == "/api/memory":
            q = parse_qs(urlparse(self.path).query)
            try:
                addr = int(q.get("address", ["0"])[0], 0)
                size = int(q.get("size", ["256"])[0], 0)
                self._json(200, self._svc.read_memory(addr, size))
            except Exception as exc:  # noqa: BLE001
                self._json(400, {"error": str(exc)})
        elif path == "/api/checksum":
            try:
                self._json(200, self._svc.checksum_report())
            except Exception as exc:  # noqa: BLE001
                self._json(400, {"error": str(exc)})
        elif path == "/api/measure":
            self._json(200, self._svc.measure_config())
        elif path == "/api/measure/csv":
            self._csv(self._svc.measure_csv(), "messung.csv")
        elif path == "/api/flash/stream":
            self._stream()
        elif path.startswith("/api/"):
            self._json(404, {"error": "not found"})
        else:
            self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/flash":
            body = self._read_json()
            started = self._svc.start_flash(body.get("mapId"))
            self._json(200 if started else 409, {"started": started})
        elif path == "/api/flash/abort":
            self._svc.abort_flash()
            self._json(200, {"aborted": True})
        elif path == "/api/firmware":
            name = (parse_qs(parsed.query).get("name") or ["firmware.bin"])[0]
            data = self._read_body()
            try:
                self._json(200, {"firmware": self._svc.set_firmware(name, data)})
            except Exception as exc:  # noqa: BLE001
                self._json(400, {"error": str(exc)})
        elif path == "/api/expert/config":
            body = self._read_json()
            try:
                self._json(200, self._svc.configure_expert(
                    profile_id=body.get("profileId"),
                    backend=body.get("backend"),
                    seedkey=body.get("seedkey"),
                    allow_write=body.get("allowWrite"),
                ))
            except KeyError as exc:
                self._json(404, {"error": f"unbekanntes Profil: {exc}"})
            except Exception as exc:  # noqa: BLE001
                self._json(400, {"error": str(exc)})
        elif path == "/api/expert/flash":
            try:
                started = self._svc.start_expert_flash()
                self._json(200 if started else 409, {"started": started})
            except Exception as exc:  # noqa: BLE001
                self._json(400, {"error": str(exc)})
        elif path == "/api/measure/start":
            body = self._read_json()
            try:
                started = self._svc.start_measure(
                    signals=body.get("signals"),
                    backend=body.get("backend"),
                    rate=float(body.get("rate", 10.0)),
                    cro=(int(body["cro"], 0) if isinstance(body.get("cro"), str)
                         else body.get("cro")),
                    dto=(int(body["dto"], 0) if isinstance(body.get("dto"), str)
                         else body.get("dto")),
                    use_daq=bool(body.get("daq")),
                )
                self._json(200 if started else 409, {"started": started})
            except Exception as exc:  # noqa: BLE001
                self._json(400, {"error": str(exc)})
        elif path == "/api/measure/stop":
            self._svc.stop_measure()
            self._json(200, {"stopped": True})
        elif path == "/api/checksum/correct":
            try:
                self._json(200, self._svc.checksum_correct())
            except Exception as exc:  # noqa: BLE001
                self._json(400, {"error": str(exc)})
        elif path.startswith("/api/maps/") and path.endswith("/buy"):
            map_id = path[len("/api/maps/"):-len("/buy")]
            body = self._read_json()
            try:
                self._json(200, self._svc.buy_map(map_id, bool(body.get("addon"))))
            except KeyError:
                self._json(404, {"error": "unknown map"})
        else:
            self._json(404, {"error": "not found"})

    # -- SSE ------------------------------------------------------------ #
    def _stream(self) -> None:
        sub = self._svc.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._cors()
        self.end_headers()
        try:
            # Prime the stream so the client renders immediately.
            self._sse({"type": "hello", "running": self._svc.running})
            while True:
                try:
                    event = sub.q.get(timeout=10.0)
                    self._sse(event)
                except queue.Empty:
                    # heartbeat comment keeps the connection alive
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self._svc.unsubscribe(sub)

    def _sse(self, event: dict) -> None:
        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
        self.wfile.flush()

    # -- static --------------------------------------------------------- #
    def _serve_static(self, path: str) -> None:
        root = self.server.static_root  # type: ignore[attr-defined]
        if not root:
            self._fallback()
            return
        rel = path.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(root, rel))
        if not full.startswith(os.path.abspath(root)):
            self._json(403, {"error": "forbidden"})
            return
        if not os.path.isfile(full):
            # SPA fallback -> index.html
            full = os.path.join(root, "index.html")
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            self._json(404, {"error": "not found"})
            return
        ext = os.path.splitext(full)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", _MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _fallback(self) -> None:
        html = (
            "<!doctype html><meta charset=utf-8><title>MED17 Flash Tool</title>"
            "<body style='font-family:sans-serif;max-width:640px;margin:60px auto;color:#1D1D1F'>"
            "<h1>MED17 Flash Tool</h1>"
            "<p>The web UI has not been built yet. Build it once with:</p>"
            "<pre style='background:#F5F5F7;padding:14px;border-radius:10px'>"
            "cd webui &amp;&amp; npm install &amp;&amp; npm run build</pre>"
            "<p>The JSON/SSE API is already live under <code>/api/…</code>.</p>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)


class WebServer:
    def __init__(
        self,
        service: Optional[FlashService] = None,
        host: str = "127.0.0.1",
        port: int = 8090,
        static_root: Optional[str] = None,
    ) -> None:
        self.service = service or FlashService()
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.service = self.service  # type: ignore[attr-defined]
        self._httpd.static_root = static_root or _static_root()  # type: ignore[attr-defined]
        self._thread: Optional[threading.Thread] = None

    @property
    def address(self) -> Tuple[str, int]:
        return self._httpd.server_address  # type: ignore[return-value]

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}"

    def start(self, block: bool = False) -> None:
        root = self._httpd.static_root  # type: ignore[attr-defined]
        log.info("web UI on %s (static: %s)", self.url, root or "not built")
        if block:
            self._httpd.serve_forever()
        else:
            self._thread = threading.Thread(target=self._httpd.serve_forever,
                                            name="webserver", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "WebServer":
        self.start(block=False)
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
