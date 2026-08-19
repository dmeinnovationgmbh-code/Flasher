"""Client for the firmware :class:`~med17flasher.server.app.FileServer`.

Lets the flasher (CLI or GUI) browse the repository, download a firmware into a
:class:`~med17flasher.core.firmware.FirmwareImage` and upload new images -
using only the standard library.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from ..core.firmware import FirmwareImage, load_firmware
from ..exceptions import RepositoryError


class FileServerClient:
    def __init__(
        self, base_url: str, token: Optional[str] = None, timeout: float = 15.0
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # ------------------------------------------------------------------ #
    def _headers(self) -> Dict[str, str]:
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, path: str, data: Optional[bytes] = None, headers=None):
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, data=data, method=method)
        for key, value in self._headers().items():
            req.add_header(key, value)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RepositoryError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RepositoryError(f"cannot reach file server at {url}: {exc}") from exc

    # ------------------------------------------------------------------ #
    def health(self) -> bool:
        with self._request("GET", "/health") as resp:
            return json.loads(resp.read()).get("status") == "ok"

    def list(self, ecu: Optional[str] = None, query: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {}
        if ecu:
            params["ecu"] = ecu
        if query:
            params["q"] = query
        qs = ("?" + urllib.parse.urlencode(params)) if params else ""
        with self._request("GET", f"/firmwares{qs}") as resp:
            return json.loads(resp.read()).get("firmwares", [])

    def get_meta(self, fw_id: str) -> Dict[str, Any]:
        with self._request("GET", f"/firmwares/{fw_id}") as resp:
            return json.loads(resp.read())

    def download_bytes(self, fw_id: str) -> bytes:
        with self._request("GET", f"/firmwares/{fw_id}/download") as resp:
            return resp.read()

    def download_image(self, fw_id: str) -> FirmwareImage:
        """Download a firmware and parse it into a :class:`FirmwareImage`."""

        meta = self.get_meta(fw_id)
        data = self.download_bytes(fw_id)
        suffix = os.path.splitext(meta.get("filename", "firmware.bin"))[1] or ".bin"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            image = load_firmware(tmp_path, base_address=int(meta.get("base_address", 0)))
            image.metadata.update(
                {
                    "id": meta.get("id", ""),
                    "filename": meta.get("filename", ""),
                    "ecu": meta.get("ecu", ""),
                    "sw_version": meta.get("sw_version", ""),
                    "part_number": meta.get("part_number", ""),
                }
            )
            return image
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def upload(
        self,
        path: str,
        *,
        ecu: str = "MED17.7.5",
        sw_version: str = "",
        part_number: str = "",
        hw_version: str = "",
        base_address: int = 0,
        description: str = "",
    ) -> Dict[str, Any]:
        with open(path, "rb") as fh:
            data = fh.read()
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        fmt = "hex" if ext in ("hex", "ihex", "ihx") else (
            "srec" if ext in ("s19", "srec", "mot", "sre") else "bin"
        )
        params = {
            "filename": os.path.basename(path),
            "ecu": ecu,
            "sw": sw_version,
            "part": part_number,
            "hw": hw_version,
            "fmt": fmt,
            "base": hex(base_address),
            "description": description,
        }
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v != ""})
        with self._request(
            "POST",
            f"/firmwares?{qs}",
            data=data,
            headers={"Content-Type": "application/octet-stream"},
        ) as resp:
            return json.loads(resp.read())

    def delete(self, fw_id: str) -> None:
        with self._request("DELETE", f"/firmwares/{fw_id}") as resp:
            resp.read()
