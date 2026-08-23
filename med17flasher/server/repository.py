"""On-disk firmware repository with a JSON index.

The repository is the storage layer behind the file server: it keeps firmware
blobs in a directory, records rich metadata (ECU, software/part numbers,
SHA-256, format, base address) in ``index.json`` and answers list/get/add/delete
queries. It is completely dependency free.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from ..exceptions import RepositoryError


@dataclass
class FirmwareMeta:
    """Metadata for one stored firmware image."""

    id: str
    filename: str
    size: int
    sha256: str
    ecu: str = "MED17.7.5"
    sw_version: str = ""
    part_number: str = ""
    hw_version: str = ""
    fmt: str = "bin"  # bin / hex / srec
    base_address: int = 0
    description: str = ""
    uploaded_at: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class FirmwareRepository:
    """A thread-safe directory-backed firmware store."""

    INDEX_NAME = "index.json"
    BLOB_DIR = "blobs"

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self.blob_dir = os.path.join(self.root, self.BLOB_DIR)
        self.index_path = os.path.join(self.root, self.INDEX_NAME)
        os.makedirs(self.blob_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._index: Dict[str, FirmwareMeta] = {}
        self._load_index()

    # ------------------------------------------------------------------ #
    # Index persistence
    # ------------------------------------------------------------------ #
    def _load_index(self) -> None:
        if not os.path.isfile(self.index_path):
            return
        try:
            with open(self.index_path, "r", encoding="utf-8") as fh:
                rows = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise RepositoryError(f"cannot read firmware index: {exc}") from exc
        for row in rows:
            meta = FirmwareMeta(**row)
            self._index[meta.id] = meta

    def _save_index(self) -> None:
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump([m.to_dict() for m in self._index.values()], fh, indent=2)
        os.replace(tmp, self.index_path)

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #
    def add(
        self,
        data: bytes,
        filename: str,
        *,
        ecu: str = "MED17.7.5",
        sw_version: str = "",
        part_number: str = "",
        hw_version: str = "",
        fmt: str = "bin",
        base_address: int = 0,
        description: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> FirmwareMeta:
        with self._lock:
            fw_id = uuid.uuid4().hex[:16]
            sha = hashlib.sha256(data).hexdigest()
            # Deduplicate on content: if a firmware with the same hash exists,
            # return the existing entry.
            for meta in self._index.values():
                if meta.sha256 == sha and meta.filename == filename:
                    return meta
            blob_path = os.path.join(self.blob_dir, fw_id)
            with open(blob_path, "wb") as fh:
                fh.write(data)
            meta = FirmwareMeta(
                id=fw_id,
                filename=os.path.basename(filename),
                size=len(data),
                sha256=sha,
                ecu=ecu,
                sw_version=sw_version,
                part_number=part_number,
                hw_version=hw_version,
                fmt=fmt,
                base_address=base_address,
                description=description,
                uploaded_at=time.time(),
                extra=extra or {},
            )
            self._index[fw_id] = meta
            self._save_index()
            return meta

    def add_file(self, path: str, **kwargs: Any) -> FirmwareMeta:
        with open(path, "rb") as fh:
            data = fh.read()
        kwargs.setdefault("filename", os.path.basename(path))
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if ext in ("hex", "ihex", "ihx"):
            kwargs.setdefault("fmt", "hex")
        elif ext in ("s19", "srec", "mot", "sre", "s28", "s37"):
            kwargs.setdefault("fmt", "srec")
        else:
            kwargs.setdefault("fmt", "bin")
        return self.add(data, **kwargs)

    def get_meta(self, fw_id: str) -> FirmwareMeta:
        try:
            return self._index[fw_id]
        except KeyError:
            raise RepositoryError(f"unknown firmware id {fw_id!r}") from None

    def get_data(self, fw_id: str) -> bytes:
        meta = self.get_meta(fw_id)
        blob_path = os.path.join(self.blob_dir, meta.id)
        try:
            with open(blob_path, "rb") as fh:
                return fh.read()
        except OSError as exc:
            raise RepositoryError(f"firmware blob missing for {fw_id!r}: {exc}") from exc

    def blob_path(self, fw_id: str) -> str:
        self.get_meta(fw_id)
        return os.path.join(self.blob_dir, fw_id)

    def delete(self, fw_id: str) -> None:
        with self._lock:
            meta = self._index.pop(fw_id, None)
            if meta is None:
                raise RepositoryError(f"unknown firmware id {fw_id!r}")
            try:
                os.remove(os.path.join(self.blob_dir, fw_id))
            except OSError:
                pass
            self._save_index()

    def list(self, *, ecu: Optional[str] = None, query: Optional[str] = None) -> List[FirmwareMeta]:
        with self._lock:
            items = list(self._index.values())
        if ecu:
            items = [m for m in items if m.ecu.lower() == ecu.lower()]
        if query:
            q = query.lower()
            items = [
                m
                for m in items
                if q in m.filename.lower()
                or q in m.sw_version.lower()
                or q in m.part_number.lower()
                or q in m.description.lower()
            ]
        return sorted(items, key=lambda m: m.uploaded_at, reverse=True)

    def verify(self, fw_id: str) -> bool:
        """Re-hash the stored blob and compare to the recorded SHA-256."""

        meta = self.get_meta(fw_id)
        data = self.get_data(fw_id)
        return hashlib.sha256(data).hexdigest() == meta.sha256
