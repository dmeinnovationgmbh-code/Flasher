"""Firmware container handling.

A firmware image is modelled as an ordered list of :class:`Segment` objects
(an absolute start address plus a blob of bytes). The loaders understand:

* raw binary (``.bin``) - needs a base address
* Intel HEX (``.hex``/``.ihex``)
* Motorola S-Record (``.s19``/``.srec``/``.mot``)

Given an ECU memory map, :meth:`FirmwareImage.blocks_for` slices the image into
the logical program blocks the flash sequence downloads one by one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..exceptions import FirmwareError
from . import checksum as _cs


@dataclass
class Segment:
    """A contiguous run of bytes at an absolute address."""

    address: int
    data: bytes

    @property
    def end(self) -> int:
        """First address *after* this segment."""

        return self.address + len(self.data)

    def __len__(self) -> int:
        return len(self.data)

    def __repr__(self) -> str:
        return f"Segment(0x{self.address:08X}, len={len(self.data)})"


@dataclass
class FlashBlock:
    """One unit of work for the flash sequence (matches a memory map entry)."""

    name: str
    address: int
    data: bytes
    erase: bool = True

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def end(self) -> int:
        return self.address + len(self.data)


@dataclass
class FirmwareImage:
    """A collection of memory segments plus optional metadata."""

    segments: List[Segment] = field(default_factory=list)
    source: Optional[str] = None
    metadata: Dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # Construction / normalisation
    # ------------------------------------------------------------------ #
    def add_segment(self, address: int, data: bytes) -> None:
        if data:
            self.segments.append(Segment(address, bytes(data)))

    def normalise(self) -> "FirmwareImage":
        """Sort segments and merge adjacent/overlapping ones."""

        if not self.segments:
            return self
        ordered = sorted(self.segments, key=lambda s: s.address)
        merged: List[Segment] = [Segment(ordered[0].address, ordered[0].data)]
        for seg in ordered[1:]:
            last = merged[-1]
            if seg.address == last.end:
                merged[-1] = Segment(last.address, last.data + seg.data)
            elif seg.address < last.end:
                # Overlap: later data wins for the overlapping region.
                overlap = last.end - seg.address
                combined = last.data + seg.data[overlap:]
                merged[-1] = Segment(last.address, combined)
            else:
                merged.append(Segment(seg.address, seg.data))
        self.segments = merged
        return self

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    @property
    def total_size(self) -> int:
        return sum(len(s) for s in self.segments)

    @property
    def span(self) -> Tuple[int, int]:
        """(lowest address, highest end) across all segments."""

        if not self.segments:
            return (0, 0)
        return (
            min(s.address for s in self.segments),
            max(s.end for s in self.segments),
        )

    def read(self, address: int, size: int, fill: int = 0xFF) -> bytes:
        """Read ``size`` bytes starting at ``address``.

        Gaps not covered by any segment are filled with ``fill`` (0xFF is the
        erased state of NOR flash).
        """

        out = bytearray([fill]) * size
        end = address + size
        for seg in self.segments:
            if seg.end <= address or seg.address >= end:
                continue
            start = max(seg.address, address)
            stop = min(seg.end, end)
            out[start - address : stop - address] = seg.data[
                start - seg.address : stop - seg.address
            ]
        return bytes(out)

    def covers(self, address: int, size: int) -> bool:
        """True if every byte in [address, address+size) is present."""

        remaining = size
        cursor = address
        for seg in sorted(self.segments, key=lambda s: s.address):
            if cursor < seg.address:
                return False
            if seg.address <= cursor < seg.end:
                advance = min(seg.end - cursor, remaining)
                cursor += advance
                remaining -= advance
                if remaining <= 0:
                    return True
        return remaining <= 0

    def blocks_for(self, memory_map: Sequence["object"]) -> List[FlashBlock]:
        """Slice this image into flash blocks according to ``memory_map``.

        Each memory-map entry must expose ``name``, ``start``, ``size`` and
        ``erase`` attributes (see :class:`~med17flasher.core.ecu_profile.MemoryRegion`).
        A region with no data present in the image is skipped.
        """

        blocks: List[FlashBlock] = []
        for region in memory_map:
            if not self._has_any(region.start, region.size):
                continue
            data = self.read(region.start, region.size)
            blocks.append(
                FlashBlock(
                    name=region.name,
                    address=region.start,
                    data=data,
                    erase=getattr(region, "erase", True),
                )
            )
        return blocks

    def _has_any(self, address: int, size: int) -> bool:
        end = address + size
        return any(
            not (seg.end <= address or seg.address >= end) for seg in self.segments
        )

    def checksum(self, algorithm: str = "crc32") -> int:
        """Checksum over the concatenation of all segment data (in order)."""

        blob = b"".join(s.data for s in sorted(self.segments, key=lambda s: s.address))
        return _cs.compute(algorithm, blob)


# --------------------------------------------------------------------------- #
# Loaders
# --------------------------------------------------------------------------- #
def load_binary(path: str, base_address: int = 0) -> FirmwareImage:
    with open(path, "rb") as fh:
        data = fh.read()
    img = FirmwareImage(source=path)
    img.add_segment(base_address, data)
    return img.normalise()


def load_intel_hex(path: str) -> FirmwareImage:
    img = FirmwareImage(source=path)
    upper = 0  # extended linear/segment address
    segment_base = 0
    with open(path, "r", encoding="ascii", errors="strict") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            if not line.startswith(":"):
                raise FirmwareError(f"{path}:{lineno}: not an Intel HEX record")
            try:
                record = bytes.fromhex(line[1:])
            except ValueError as exc:
                raise FirmwareError(f"{path}:{lineno}: bad hex: {exc}") from exc
            if len(record) < 5:
                raise FirmwareError(f"{path}:{lineno}: truncated record")
            count = record[0]
            offset = (record[1] << 8) | record[2]
            rtype = record[3]
            payload = record[4 : 4 + count]
            if len(payload) != count:
                raise FirmwareError(f"{path}:{lineno}: length mismatch")
            if (sum(record) & 0xFF) != 0:
                raise FirmwareError(f"{path}:{lineno}: checksum error")

            if rtype == 0x00:  # data
                addr = upper + segment_base + offset
                img.add_segment(addr, payload)
            elif rtype == 0x01:  # EOF
                break
            elif rtype == 0x02:  # extended segment address
                segment_base = ((payload[0] << 8) | payload[1]) << 4
            elif rtype == 0x04:  # extended linear address
                upper = ((payload[0] << 8) | payload[1]) << 16
            elif rtype in (0x03, 0x05):  # start segment/linear address (ignored)
                continue
            else:
                raise FirmwareError(f"{path}:{lineno}: unsupported record type 0x{rtype:02X}")
    return img.normalise()


def load_srecord(path: str) -> FirmwareImage:
    img = FirmwareImage(source=path)
    addr_bytes = {"S1": 2, "S2": 3, "S3": 4}
    with open(path, "r", encoding="ascii", errors="strict") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            if not line.startswith("S"):
                raise FirmwareError(f"{path}:{lineno}: not an S-Record")
            stype = line[:2]
            try:
                body = bytes.fromhex(line[2:])
            except ValueError as exc:
                raise FirmwareError(f"{path}:{lineno}: bad hex: {exc}") from exc
            count = body[0]
            if len(body) - 1 != count:
                raise FirmwareError(f"{path}:{lineno}: length mismatch")
            if (sum(body) & 0xFF) != 0xFF:
                raise FirmwareError(f"{path}:{lineno}: checksum error")
            if stype in addr_bytes:
                nbytes = addr_bytes[stype]
                address = int.from_bytes(body[1 : 1 + nbytes], "big")
                data = body[1 + nbytes : -1]
                img.add_segment(address, data)
            # S0 header, S5/S6 counts, S7/S8/S9 start address: ignored.
    return img.normalise()


_EXT_LOADERS = {
    ".hex": load_intel_hex,
    ".ihex": load_intel_hex,
    ".ihx": load_intel_hex,
    ".s19": load_srecord,
    ".s28": load_srecord,
    ".s37": load_srecord,
    ".srec": load_srecord,
    ".mot": load_srecord,
    ".sre": load_srecord,
}


def load_firmware(path: str, base_address: int = 0) -> FirmwareImage:
    """Load a firmware file, dispatching on extension.

    Unknown extensions are treated as raw binary at ``base_address``.
    """

    ext = os.path.splitext(path)[1].lower()
    loader = _EXT_LOADERS.get(ext)
    if loader is not None:
        return loader(path)
    return load_binary(path, base_address)


def detect_regions(
    image: FirmwareImage,
    *,
    fill: int = 0xFF,
    min_gap: int = 0x1000,
    align: int = 0x100,
) -> List[Tuple[int, int]]:
    """Suggest program regions by finding runs of non-``fill`` data.

    Runs separated by a gap smaller than ``min_gap`` are merged; the result is
    a list of ``(start, size)`` tuples aligned to ``align``. Handy for turning
    a raw ECU dump into a memory-map skeleton for an ECU profile.
    """

    low, high = image.span
    if high <= low:
        return []
    blob = image.read(low, high - low, fill=fill)

    runs: List[List[int]] = []  # [start, end) absolute
    run_start: Optional[int] = None
    gap = 0
    for i, byte in enumerate(blob):
        if byte != fill:
            if run_start is None:
                run_start = i
            gap = 0
        else:
            if run_start is not None:
                gap += 1
                if gap >= min_gap:
                    runs.append([run_start, i - gap + 1])
                    run_start = None
                    gap = 0
    if run_start is not None:
        end = len(blob) - gap if gap else len(blob)
        runs.append([run_start, end])

    regions: List[Tuple[int, int]] = []
    for start, end in runs:
        abs_start = low + start
        abs_end = low + end
        # align outward
        abs_start -= abs_start % align
        if abs_end % align:
            abs_end += align - (abs_end % align)
        regions.append((abs_start, abs_end - abs_start))
    return regions


def validate_calibration(
    data: bytes,
    *,
    length: Optional[int] = None,
    start_byte: int = 0x60,
    end_byte: int = 0xDE,
) -> None:
    """Validate a calibration image against the med1775 signature markers.

    A valid calibration starts with ``start_byte`` (0x60) and ends with
    ``end_byte`` (0xDE), and - if ``length`` is given - must be exactly that
    long. Raises :class:`~med17flasher.exceptions.FirmwareError` otherwise.
    """

    if length is not None and len(data) != length:
        raise FirmwareError(
            f"calibration length 0x{len(data):X} != expected 0x{length:X}"
        )
    if not data:
        raise FirmwareError("empty calibration image")
    if data[0] != start_byte:
        raise FirmwareError(f"calibration must start with 0x{start_byte:02X}, got 0x{data[0]:02X}")
    if data[-1] != end_byte:
        raise FirmwareError(f"calibration must end with 0x{end_byte:02X}, got 0x{data[-1]:02X}")


def save_binary(image: FirmwareImage, path: str, fill: int = 0xFF) -> None:
    """Write the image out as a flat binary (gaps filled) from its span."""

    low, high = image.span
    with open(path, "wb") as fh:
        fh.write(image.read(low, high - low, fill=fill))
