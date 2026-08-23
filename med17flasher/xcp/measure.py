"""Measurement / logging on top of the XCP client.

A :class:`Signal` names a memory location and how to decode it. Two acquisition
strategies are provided:

* :class:`PollingMeasurement` - read each signal with ``SHORT_UPLOAD`` at a
  fixed rate (simple, works against any XCP slave).
* :func:`configure_daq` + :class:`DaqMeasurement` - set up a real XCP **DAQ**
  list so the slave streams the values, and decode the incoming DTO frames.

Both can log to CSV.
"""

from __future__ import annotations

import csv
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..logging_setup import get_logger
from . import const as X
from .client import XcpClient

log = get_logger("xcp.measure")

_SIZES = {"u8": 1, "s8": 1, "u16": 2, "s16": 2, "u32": 4, "s32": 4,
          "f32": 4, "f64": 8}
_FLOAT_FMT = {"f32": "f", "f64": "d"}


@dataclass
class Signal:
    """A measurable quantity: where it lives and how to turn bytes into a value."""

    name: str
    address: int
    dtype: str = "u16"
    ext: int = 0
    factor: float = 1.0     # physical = raw * factor + offset
    offset: float = 0.0
    unit: str = ""

    def __post_init__(self) -> None:
        if self.dtype not in _SIZES:
            raise ValueError(f"unknown signal type {self.dtype!r}; "
                             f"expected one of {sorted(_SIZES)}")

    @property
    def size(self) -> int:
        return _SIZES[self.dtype]

    def decode(self, raw: bytes, byte_order: str = "little") -> float:
        if len(raw) < self.size:
            raise ValueError(f"{self.name}: got {len(raw)} bytes, need {self.size}")
        raw = raw[: self.size]
        if self.dtype in _FLOAT_FMT:
            endian = "<" if byte_order == "little" else ">"
            value = struct.unpack(endian + _FLOAT_FMT[self.dtype], raw)[0]
        else:
            signed = self.dtype.startswith("s")
            value = int.from_bytes(raw, byte_order, signed=signed)
        return value * self.factor + self.offset


def parse_signal(spec: str) -> Signal:
    """Parse ``name@0xADDR:dtype[:factor[:offset[:unit]]]`` into a :class:`Signal`.

    Examples
    --------
    ``rpm@0x80005000:u16:0.25``  ``coolant@0xD0001234:s16:0.1:-40:degC``
    """

    if "@" not in spec:
        raise ValueError(f"signal spec must be name@addr:type, got {spec!r}")
    name, rest = spec.split("@", 1)
    parts = rest.split(":")
    address = int(parts[0], 0)
    dtype = parts[1] if len(parts) > 1 and parts[1] else "u16"
    factor = float(parts[2]) if len(parts) > 2 and parts[2] else 1.0
    offset = float(parts[3]) if len(parts) > 3 and parts[3] else 0.0
    unit = parts[4] if len(parts) > 4 else ""
    return Signal(name=name.strip(), address=address, dtype=dtype,
                  factor=factor, offset=offset, unit=unit)


@dataclass
class Sample:
    t: float                    # seconds since measurement start
    values: Dict[str, float]


class _CsvSink:
    def __init__(self, path: str, names: List[str]) -> None:
        self._fh = open(path, "w", newline="", encoding="utf-8")
        self._w = csv.writer(self._fh)
        self._w.writerow(["time_s", *names])

    def write(self, sample: Sample, names: List[str]) -> None:
        self._w.writerow([f"{sample.t:.4f}",
                          *[sample.values.get(n, "") for n in names]])

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass


class PollingMeasurement:
    """Sample signals by reading each one with SHORT_UPLOAD at a fixed rate."""

    def __init__(self, client: XcpClient, signals: List[Signal]) -> None:
        self.client = client
        self.signals = signals
        self.names = [s.name for s in signals]

    def sample(self) -> Sample:
        values: Dict[str, float] = {}
        for sig in self.signals:
            raw = self.client.read(sig.address, sig.size, sig.ext)
            values[sig.name] = sig.decode(raw, self.client.byte_order)
        return Sample(t=0.0, values=values)

    def run(
        self,
        *,
        rate_hz: float = 10.0,
        duration: Optional[float] = None,
        max_samples: Optional[int] = None,
        callback: Optional[Callable[[Sample], None]] = None,
        csv_path: Optional[str] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> List[Sample]:
        period = 1.0 / rate_hz if rate_hz > 0 else 0.0
        stop_event = stop_event or threading.Event()
        sink = _CsvSink(csv_path, self.names) if csv_path else None
        out: List[Sample] = []
        start = time.monotonic()
        n = 0
        try:
            while not stop_event.is_set():
                tick = time.monotonic()
                sample = self.sample()
                sample.t = tick - start
                out.append(sample)
                if callback:
                    callback(sample)
                if sink:
                    sink.write(sample, self.names)
                n += 1
                if max_samples is not None and n >= max_samples:
                    break
                if duration is not None and sample.t >= duration:
                    break
                if period:
                    rest = period - (time.monotonic() - tick)
                    if rest > 0:
                        stop_event.wait(rest)
        finally:
            if sink:
                sink.close()
        return out


# --------------------------------------------------------------------------- #
# Real XCP DAQ: configure one DAQ list with all signals in a single ODT.
# --------------------------------------------------------------------------- #
@dataclass
class DaqLayout:
    daq: int
    first_pid: int
    signals: List[Signal]
    offsets: List[int] = field(default_factory=list)  # byte offset of each signal


def configure_daq(client: XcpClient, signals: List[Signal], *, daq: int = 0,
                  event: int = 0, prescaler: int = 1,
                  timestamp: bool = False) -> DaqLayout:
    """Allocate one DAQ list holding all signals in a single ODT and arm it."""

    # A timestamp is carried only in the first ODT of a DAQ list; our single-ODT
    # decoder does not skip those bytes, so refuse rather than mis-decode. (Use
    # the sample's own arrival time instead.)
    if timestamp:
        raise NotImplementedError(
            "DAQ timestamps are not decoded; call configure_daq(timestamp=False)")

    total = sum(s.size for s in signals)
    budget = max(1, client.max_dto - 1)  # one PID byte precedes the data
    if total > budget:
        raise ValueError(
            f"the {len(signals)} signals need {total} bytes but one DAQ frame "
            f"holds MAX_DTO-1 = {budget}; use polling or fewer/smaller signals"
        )
    client.free_daq()
    client.alloc_daq(1)
    client.alloc_odt(daq, 1)
    client.alloc_odt_entry(daq, 0, len(signals))
    offsets: List[int] = []
    cursor = 0
    for i, sig in enumerate(signals):
        client.set_daq_ptr(daq, 0, i)
        client.write_daq(0, sig.size, sig.ext, sig.address)
        offsets.append(cursor)
        cursor += sig.size
    mode = X.DAQ_MODE_TIMESTAMP if timestamp else 0
    client.set_daq_list_mode(mode, daq, event, prescaler, 0)
    first_pid = client.start_stop_daq_list(X.DAQ_SELECT, daq)
    client.start_stop_synch(X.DAQ_START_SELECTED)
    return DaqLayout(daq=daq, first_pid=first_pid, signals=signals, offsets=offsets)


class DaqMeasurement:
    """Collect and decode DTO frames produced by a configured DAQ list."""

    def __init__(self, client: XcpClient, layout: DaqLayout) -> None:
        self.client = client
        self.layout = layout
        self.names = [s.name for s in layout.signals]

    def _decode_frame(self, payload: bytes) -> Optional[Sample]:
        if not payload:
            return None
        # payload[0] is the PID (ODT number, offset by first_pid); data follows.
        data = payload[1:]
        values: Dict[str, float] = {}
        for sig, off in zip(self.layout.signals, self.layout.offsets):
            chunk = data[off: off + sig.size]
            if len(chunk) < sig.size:
                return None
            values[sig.name] = sig.decode(chunk, self.client.byte_order)
        return Sample(t=0.0, values=values)

    def run(
        self,
        *,
        duration: Optional[float] = None,
        max_samples: Optional[int] = None,
        callback: Optional[Callable[[Sample], None]] = None,
        csv_path: Optional[str] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> List[Sample]:
        stop_event = stop_event or threading.Event()
        sink = _CsvSink(csv_path, self.names) if csv_path else None
        out: List[Sample] = []
        start = time.monotonic()
        n = 0
        try:
            while not stop_event.is_set():
                payload = self.client.tp.collect_daq(timeout=1.0)
                if payload is None:
                    if duration is not None and (time.monotonic() - start) >= duration:
                        break
                    continue
                sample = self._decode_frame(payload)
                if sample is None:
                    continue
                sample.t = time.monotonic() - start
                out.append(sample)
                if callback:
                    callback(sample)
                if sink:
                    sink.write(sample, self.names)
                n += 1
                if max_samples is not None and n >= max_samples:
                    break
                if duration is not None and sample.t >= duration:
                    break
        finally:
            try:
                self.client.start_stop_synch(X.DAQ_STOP_ALL)
            except Exception:  # noqa: BLE001
                pass
            if sink:
                sink.close()
        return out
