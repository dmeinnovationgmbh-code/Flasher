"""A seed/key network server.

Flashing setups traditionally isolate the secret seed->key routine behind a
small network service (the classic "seed/key DLL server"), so the routine lives
in one place and the flasher merely asks it for a key. This module implements
that service two ways, both dependency free:

* an **HTTP/JSON** API (``POST /seedkey`` with ``{ecu, level, seed}``)
* a tiny **line-based TCP** protocol (``<ecu> <level> <seedhex>\\n`` ->
  ``<keyhex>\\n``)

Both are backed by a :class:`~med17flasher.seedkey.store.SeedKeyStore`, so the
same catalogue drives the flasher and the server.
"""

from __future__ import annotations

import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple

from ..logging_setup import get_logger
from .base import compute_key, list_algorithms
from .store import SeedKeyStore

log = get_logger("seedkey.server")


class SeedKeyService:
    """The transport-agnostic core used by both the HTTP and TCP servers.

    With ``algorithm`` set (e.g. a :class:`~med17flasher.seedkey.dll.DllSeedKey`
    or :class:`~med17flasher.seedkey.dll.ExeSeedKey`), every request is served by
    that backend regardless of the ECU name - which is how you front a vendor
    seed-key DLL. Otherwise requests resolve through the :class:`SeedKeyStore`.
    """

    def __init__(
        self,
        store: Optional[SeedKeyStore] = None,
        algorithm=None,
        default_ecu: str = "MED17.7.5",
    ) -> None:
        self.store = store or SeedKeyStore.default()
        self.algorithm = algorithm
        self.default_ecu = default_ecu

    def compute_hex(self, ecu: str, level: int, seed_hex: str) -> str:
        seed = bytes.fromhex(seed_hex.replace(" ", ""))
        if self.algorithm is not None:
            key = self.algorithm.compute(seed, level=level, params={})
        else:
            key = self.store.compute(ecu, level, seed)
        return key.hex()

    def key_for(self, level: int, seed_hex: str) -> str:
        """Compute a key for the default ECU / configured backend (for GET /key)."""

        return self.compute_hex(self.default_ecu, level, seed_hex)

    def compute_with_algorithm(
        self, algorithm: str, level: int, seed_hex: str, params: Optional[dict] = None
    ) -> str:
        seed = bytes.fromhex(seed_hex.replace(" ", ""))
        key = compute_key(algorithm, seed, level=level, params=params or {})
        return key.hex()


# --------------------------------------------------------------------------- #
# HTTP/JSON server
# --------------------------------------------------------------------------- #
class _HttpHandler(BaseHTTPRequestHandler):
    service: SeedKeyService  # injected on the server instance

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # silence default stderr spam
        log.debug("HTTP %s - " + fmt, self.address_string(), *args)

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        svc: SeedKeyService = self.server.service  # type: ignore[attr-defined]
        # Compat route: GET /key/<level>/<seedhex> -> {"key": "..."} (matches the
        # `curl http://host:5000/key/05/183fd11c` production pattern).
        parts = [p for p in self.path.split("/") if p]
        if len(parts) == 3 and parts[0] == "key":
            try:
                level = int(parts[1], 16)
                self._send_json(200, {"key": svc.key_for(level, parts[2])})
            except Exception as exc:  # noqa: BLE001
                self._send_json(422, {"error": str(exc)})
            return
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        elif self.path == "/algorithms":
            self._send_json(200, {"algorithms": list_algorithms()})
        elif self.path == "/entries":
            self._send_json(
                200,
                {
                    "entries": [
                        {"ecu": e.ecu, "level": e.level, "algorithm": e.algorithm}
                        for e in svc.store.entries()
                    ]
                },
            )
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        svc: SeedKeyService = self.server.service  # type: ignore[attr-defined]
        if self.path != "/seedkey":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": f"bad JSON: {exc}"})
            return
        try:
            seed_hex = request["seed"]
            level = int(request.get("level", 0))
            if "algorithm" in request:
                key = svc.compute_with_algorithm(
                    request["algorithm"], level, seed_hex, request.get("params")
                )
            else:
                key = svc.compute_hex(request["ecu"], level, seed_hex)
        except KeyError as exc:
            self._send_json(400, {"error": f"missing field: {exc}"})
        except Exception as exc:  # bad hex, unknown ecu/algorithm, ...
            self._send_json(422, {"error": str(exc)})
        else:
            self._send_json(200, {"key": key, "level": level})


class SeedKeyHttpServer:
    """A threaded HTTP seed/key server."""

    def __init__(
        self,
        service: Optional[SeedKeyService] = None,
        host: str = "127.0.0.1",
        port: int = 8377,
    ) -> None:
        self.service = service or SeedKeyService()
        self._httpd = ThreadingHTTPServer((host, port), _HttpHandler)
        self._httpd.service = self.service  # type: ignore[attr-defined]
        self._thread: Optional[threading.Thread] = None

    @property
    def address(self) -> Tuple[str, int]:
        return self._httpd.server_address  # type: ignore[return-value]

    def start(self, block: bool = False) -> None:
        host, port = self.address
        log.info("seed/key HTTP server listening on http://%s:%d", host, port)
        if block:
            self._httpd.serve_forever()
        else:
            self._thread = threading.Thread(
                target=self._httpd.serve_forever, name="seedkey-http", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "SeedKeyHttpServer":
        self.start(block=False)
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


# --------------------------------------------------------------------------- #
# Raw TCP line protocol
# --------------------------------------------------------------------------- #
class _TcpHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        svc: SeedKeyService = self.server.service  # type: ignore[attr-defined]
        for raw in self.rfile:
            line = raw.decode("ascii", errors="replace").strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit"):
                break
            parts = line.split()
            try:
                if len(parts) == 3:
                    ecu, level_s, seed_hex = parts
                    key = svc.compute_hex(ecu, int(level_s, 0), seed_hex)
                elif len(parts) == 2:
                    # "<seedhex> <level>" against the default ecu
                    seed_hex, level_s = parts
                    key = svc.compute_hex("MED17.7.5", int(level_s, 0), seed_hex)
                else:
                    self.wfile.write(b"ERR expected: <ecu> <level> <seedhex>\n")
                    continue
            except Exception as exc:  # noqa: BLE001
                self.wfile.write(f"ERR {exc}\n".encode("ascii", errors="replace"))
            else:
                self.wfile.write(f"OK {key}\n".encode("ascii"))


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class SeedKeyTcpServer:
    """A threaded line-based TCP seed/key server."""

    def __init__(
        self,
        service: Optional[SeedKeyService] = None,
        host: str = "127.0.0.1",
        port: int = 8378,
    ) -> None:
        self.service = service or SeedKeyService()
        self._srv = _ThreadingTCPServer((host, port), _TcpHandler)
        self._srv.service = self.service  # type: ignore[attr-defined]
        self._thread: Optional[threading.Thread] = None

    @property
    def address(self) -> Tuple[str, int]:
        return self._srv.server_address  # type: ignore[return-value]

    def start(self, block: bool = False) -> None:
        host, port = self.address
        log.info("seed/key TCP server listening on %s:%d", host, port)
        if block:
            self._srv.serve_forever()
        else:
            self._thread = threading.Thread(
                target=self._srv.serve_forever, name="seedkey-tcp", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        if self._thread:
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "SeedKeyTcpServer":
        self.start(block=False)
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


# --------------------------------------------------------------------------- #
# A minimal client for the HTTP API (used by the flasher when configured to
# offload seed/key to a remote server).
# --------------------------------------------------------------------------- #
class SeedKeyClient:
    """Ask a remote :class:`SeedKeyHttpServer` to turn a seed into a key."""

    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def compute(self, ecu: str, level: int, seed: bytes) -> bytes:
        import urllib.request

        body = json.dumps(
            {"ecu": ecu, "level": level, "seed": seed.hex()}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/seedkey",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read())
        return bytes.fromhex(payload["key"])
