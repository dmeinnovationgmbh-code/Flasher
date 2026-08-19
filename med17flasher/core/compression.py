"""Decompression helpers for flash containers.

Mercedes/Bosch flash containers (as shipped to dealer/production tools) often
store their sections as **raw DEFLATE** (RFC 1951) - no zlib/gzip header, no
magic - sometimes several blocks/sections in a row. This module auto-detects
zlib / gzip / raw-DEFLATE and can inflate a single stream from a given offset.

A fully sectioned container (per-section length headers) still needs the exact
layout - point :func:`inflate` at the right ``offset`` per section, or supply a
container spec once the format is known.
"""

from __future__ import annotations

import gzip
import zlib
from typing import List, Tuple

from ..exceptions import FirmwareError


def inflate_raw(data: bytes, offset: int = 0) -> bytes:
    """Inflate a raw DEFLATE stream (RFC 1951) starting at ``offset``.

    Tolerates trailing data after the stream end (returns just the inflated
    bytes). Raises :class:`FirmwareError` if it is not a valid raw stream.
    """

    dobj = zlib.decompressobj(-15)  # negative wbits -> raw, no header
    try:
        out = dobj.decompress(bytes(data[offset:]))
        out += dobj.flush()
    except zlib.error as exc:
        raise FirmwareError(f"raw DEFLATE inflate failed at offset 0x{offset:X}: {exc}") from exc
    if not out:
        raise FirmwareError(f"raw DEFLATE produced no output at offset 0x{offset:X}")
    return out


def inflate(data: bytes, offset: int = 0, mode: str = "auto") -> Tuple[bytes, str]:
    """Inflate ``data`` from ``offset``. Returns ``(bytes, detected_mode)``.

    ``mode`` is ``"auto"`` (try zlib, gzip, then raw), or one of
    ``"zlib"`` / ``"gzip"`` / ``"raw"``.
    """

    blob = bytes(data[offset:])
    if mode == "zlib":
        return zlib.decompress(blob), "zlib"
    if mode == "gzip":
        return gzip.decompress(blob), "gzip"
    if mode == "raw":
        return inflate_raw(data, offset), "raw"

    errors: List[str] = []
    for name, fn in (
        ("zlib", lambda b: zlib.decompress(b)),
        ("gzip", lambda b: gzip.decompress(b)),
        ("raw", lambda b: inflate_raw(b, 0)),
    ):
        try:
            out = fn(blob)
            if out:
                return out, name
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
    raise FirmwareError(
        f"could not inflate at offset 0x{offset:X} (tried {', '.join(errors)})"
    )


def inflate_sections(data: bytes, offset: int = 0) -> List[bytes]:
    """Best-effort: inflate consecutive raw-DEFLATE blocks from ``offset``.

    After each stream, continue at the byte where the previous one ended
    (``decompressobj.unused_data``). Stops when the remainder no longer inflates.
    Useful for multi-section containers, but verify against the real layout.
    """

    sections: List[bytes] = []
    blob = bytes(data[offset:])
    while blob:
        dobj = zlib.decompressobj(-15)
        try:
            out = dobj.decompress(blob)
            out += dobj.flush()
        except zlib.error:
            break
        if not out:
            break
        sections.append(out)
        if not dobj.unused_data or dobj.unused_data == blob:
            break
        blob = dobj.unused_data
    return sections
