"""A dependency-free HTTP firmware file server (REST API).

Endpoints
---------
``GET  /health``                      -> ``{"status": "ok"}``
``GET  /firmwares``                    -> list metadata (``?ecu=`` / ``?q=`` filters)
``GET  /firmwares/<id>``               -> one metadata object
``GET  /firmwares/<id>/download``      -> raw firmware bytes
``POST /firmwares``                    -> upload (body = bytes, metadata in
                                          query string or ``X-Firmware-*`` headers)
``DELETE /firmwares/<id>``             -> remove

An optional bearer token (``Authorization: Bearer <token>``) protects write
operations (and, if ``protect_reads`` is set, reads too).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

from ..exceptions import RepositoryError
from ..logging_setup import get_logger
from .repository import FirmwareRepository

log = get_logger("server.http")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # populated on the server instance
    repo: FirmwareRepository
    token: Optional[str]
    protect_reads: bool

    def log_message(self, fmt: str, *args) -> None:
        log.debug("HTTP %s - " + fmt, self.address_string(), *args)

    # -- helpers -------------------------------------------------------- #
    @property
    def _repo(self) -> FirmwareRepository:
        return self.server.repo  # type: ignore[attr-defined]

    def _authorized(self, *, write: bool) -> bool:
        token = self.server.token  # type: ignore[attr-defined]
        if token is None:
            return True
        if not write and not self.server.protect_reads:  # type: ignore[attr-defined]
            return True
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {token}"

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, code: int, data: bytes, filename: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _deny(self) -> None:
        self._json(401, {"error": "unauthorized"})

    # -- routing -------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if not self._authorized(write=False):
            self._deny()
            return
        try:
            if parsed.path == "/health":
                self._json(200, {"status": "ok"})
            elif parsed.path == "/firmwares":
                params = parse_qs(parsed.query)
                items = self._repo.list(
                    ecu=(params.get("ecu", [None])[0]),
                    query=(params.get("q", [None])[0]),
                )
                self._json(200, {"firmwares": [m.to_dict() for m in items]})
            elif len(parts) == 2 and parts[0] == "firmwares":
                self._json(200, self._repo.get_meta(parts[1]).to_dict())
            elif len(parts) == 3 and parts[0] == "firmwares" and parts[2] == "download":
                meta = self._repo.get_meta(parts[1])
                self._bytes(200, self._repo.get_data(parts[1]), meta.filename)
            else:
                self._json(404, {"error": "not found"})
        except RepositoryError as exc:
            self._json(404, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/firmwares":
            self._json(404, {"error": "not found"})
            return
        if not self._authorized(write=True):
            self._deny()
            return
        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length) if length else b""
        if not data:
            self._json(400, {"error": "empty body"})
            return
        params = parse_qs(parsed.query)

        def field(name: str, default: str = "") -> str:
            if name in params:
                return params[name][0]
            return self.headers.get(f"X-Firmware-{name}", default)

        try:
            base_addr_raw = field("base", "0")
            meta = self._repo.add(
                data,
                filename=field("filename", "firmware.bin"),
                ecu=field("ecu", "MED17.7.5"),
                sw_version=field("sw", ""),
                part_number=field("part", ""),
                hw_version=field("hw", ""),
                fmt=field("fmt", "bin"),
                base_address=int(base_addr_raw, 0) if base_addr_raw else 0,
                description=field("description", ""),
            )
        except Exception as exc:  # noqa: BLE001
            self._json(422, {"error": str(exc)})
        else:
            self._json(201, meta.to_dict())

    def do_DELETE(self) -> None:  # noqa: N802
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if not (len(parts) == 2 and parts[0] == "firmwares"):
            self._json(404, {"error": "not found"})
            return
        if not self._authorized(write=True):
            self._deny()
            return
        try:
            self._repo.delete(parts[1])
        except RepositoryError as exc:
            self._json(404, {"error": str(exc)})
        else:
            self._json(200, {"deleted": parts[1]})


class FileServer:
    """A threaded HTTP firmware file server."""

    def __init__(
        self,
        repository: FirmwareRepository,
        host: str = "127.0.0.1",
        port: int = 8080,
        *,
        token: Optional[str] = None,
        protect_reads: bool = False,
    ) -> None:
        self.repository = repository
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.repo = repository  # type: ignore[attr-defined]
        self._httpd.token = token  # type: ignore[attr-defined]
        self._httpd.protect_reads = protect_reads  # type: ignore[attr-defined]
        self._thread: Optional[threading.Thread] = None

    @property
    def address(self) -> Tuple[str, int]:
        return self._httpd.server_address  # type: ignore[return-value]

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}"

    def start(self, block: bool = False) -> None:
        log.info("firmware file server listening on %s", self.url)
        if block:
            self._httpd.serve_forever()
        else:
            self._thread = threading.Thread(
                target=self._httpd.serve_forever, name="fileserver", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "FileServer":
        self.start(block=False)
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
