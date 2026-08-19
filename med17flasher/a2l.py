"""ASAP2 / A2L parsing: turn an ECU description file into measurable signals.

An A2L file is the ECU's own description of what lives where: every
``MEASUREMENT`` carries an ``ECU_ADDRESS`` plus the ``COMPU_METHOD`` that turns
raw bytes into a physical value. Parsing it means the XCP code can be pointed at
*names* ("nmot", "tmot") instead of hand-typed addresses that go stale with the
next software version.

The parser is deliberately partial. Real A2Ls are tens of megabytes describing
axis descriptions, verbal tables, A2ML grammars and vendor ``IF_DATA`` blobs;
only ``MEASUREMENT``, ``CHARACTERISTIC`` and ``COMPU_METHOD`` matter here, so
everything else is walked past without being interpreted. Parsing is a single
tokenising pass over the text with one-token lookahead and no regex
backtracking, which keeps a multi-megabyte file to a couple of seconds and
constant-ish memory (blocks are buffered one at a time, never the whole file's
token stream).

Public API:

* :class:`A2lMeasurement`, :class:`A2lCharacteristic`, :class:`A2lCompuMethod`
* :class:`A2lFile` with :meth:`A2lFile.find` and :meth:`A2lFile.to_signals`
* :func:`parse_a2l` / :func:`load_a2l`

Anything unparsable raises :class:`~med17flasher.exceptions.FirmwareError`;
survivable oddities (unknown datatype, missing address, non-linear conversion)
are collected in :attr:`A2lFile.warnings` instead of aborting the parse, because
one broken block must not cost you the other 40 000.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
)

from .exceptions import FirmwareError
from .logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover - import for type checkers only
    from .xcp import Signal

log = get_logger("a2l")

__all__ = [
    "A2lMeasurement",
    "A2lCharacteristic",
    "A2lCompuMethod",
    "A2lFile",
    "parse_a2l",
    "load_a2l",
    "DATATYPES",
]

# A2L datatype -> the dtype strings understood by med17flasher.xcp.Signal.
# 64-bit integers have no Signal equivalent, so they are clamped to 32 bit and
# a warning is recorded (reading one gives you the low/high word only).
DATATYPES = {
    "UBYTE": "u8",
    "SBYTE": "s8",
    "UWORD": "u16",
    "SWORD": "s16",
    "ULONG": "u32",
    "SLONG": "s32",
    "A_UINT64": "u32",
    "A_INT64": "s32",
    "FLOAT32_IEEE": "f32",
    "FLOAT64_IEEE": "f64",
}
_CLAMPED_DATATYPES = {"A_UINT64", "A_INT64"}
_DEFAULT_DTYPE = "u16"

# Blocks whose contents we actually read; everything else is only walked.
_INTERESTING = ("MEASUREMENT", "CHARACTERISTIC", "COMPU_METHOD")

_MAX_WARNINGS = 200


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class A2lMeasurement:
    """One ``/begin MEASUREMENT`` entry, with its conversion already applied.

    ``factor``/``offset`` follow the same convention as
    :class:`med17flasher.xcp.Signal`: ``physical = raw * factor + offset``.
    """

    name: str
    long_identifier: str = ""
    dtype: str = _DEFAULT_DTYPE
    address: int = 0
    factor: float = 1.0
    offset: float = 0.0
    unit: str = ""
    nonlinear: bool = False     # conversion could not be reduced to factor/offset


@dataclass
class A2lCharacteristic:
    """One ``/begin CHARACTERISTIC`` entry (a calibratable map/curve/value)."""

    name: str
    long_identifier: str = ""
    address: int = 0
    kind: str = ""              # VALUE / CURVE / MAP / VAL_BLK / ASCII / ...


@dataclass
class A2lCompuMethod:
    """One ``/begin COMPU_METHOD``, reduced to a linear factor/offset if possible."""

    name: str
    long_identifier: str = ""
    conversion_type: str = ""   # IDENTICAL / LINEAR / RAT_FUNC / TAB_* / FORM
    fmt: str = ""
    unit: str = ""
    factor: float = 1.0
    offset: float = 0.0
    nonlinear: bool = False
    coeffs: Tuple[float, ...] = ()


@dataclass
class A2lEvent:
    """An XCP DAQ event channel (a selectable sampling raster)."""

    number: int
    name: str
    period_s: Optional[float] = None      # cycle x 10^unit, if given


@dataclass
class A2lXcp:
    """XCP transport parameters read from the module's ``IF_DATA XCP`` block.

    Lets the measurement tool auto-configure itself: the CAN ids come straight
    from ``XCP_ON_CAN`` and the DAQ rasters from the ``EVENT`` blocks, so the
    user does not have to type ``--cro/--dto`` or guess event numbers.
    """

    transport: str = "can"                # "can" or "udp"
    can_id_master: Optional[int] = None   # CRO: master -> slave (id only, no flag)
    can_id_slave: Optional[int] = None    # DTO: slave  -> master
    can_id_broadcast: Optional[int] = None
    is_extended: bool = False             # 29-bit ids (ASAM flags them with bit 31)
    baudrate: Optional[int] = None
    events: List[A2lEvent] = field(default_factory=list)


@dataclass
class A2lFile:
    """The subset of an A2L we care about, plus whatever went wrong reading it."""

    project: str = ""
    module: str = ""
    measurements: Dict[str, A2lMeasurement] = field(default_factory=dict)
    characteristics: Dict[str, A2lCharacteristic] = field(default_factory=dict)
    compu_methods: Dict[str, A2lCompuMethod] = field(default_factory=dict)
    xcp: Optional[A2lXcp] = None
    warnings: List[str] = field(default_factory=list)

    # -- lookup ----------------------------------------------------------- #
    def find(self, pattern: str) -> List[A2lMeasurement]:
        """Return measurements whose name matches ``pattern``, name-sorted.

        Matching is case-insensitive and accepts both a plain substring
        ("rpm") and a glob ("n*ot", "*_raw"), because nobody remembers whether
        the ECU supplier spelled it ``nmot`` or ``Nmot_w``.
        """

        pat = (pattern or "").strip().lower()
        if not pat or pat == "*":
            return sorted(self.measurements.values(), key=lambda m: m.name)
        out = [
            meas
            for name, meas in self.measurements.items()
            if pat in name.lower() or fnmatch.fnmatchcase(name.lower(), pat)
        ]
        return sorted(out, key=lambda m: m.name)

    # -- export ----------------------------------------------------------- #
    def to_signals(self, names: Optional[Iterable[str]] = None) -> List["Signal"]:
        """Build :class:`med17flasher.xcp.Signal` objects for ``names``.

        ``names`` defaults to every measurement. The import is done here rather
        than at module scope so this module stays usable (and cheap) without
        pulling in the XCP stack.
        """

        from .xcp import Signal  # lazy: avoids a hard import cycle with xcp

        if names is None:
            chosen = [self.measurements[key] for key in sorted(self.measurements)]
        else:
            if isinstance(names, str):      # a bare name is a common slip
                names = [names]
            chosen = []
            for name in names:
                meas = self.measurements.get(name)
                if meas is None:            # second chance: case-insensitive
                    lowered = name.lower()
                    meas = next(
                        (m for k, m in self.measurements.items() if k.lower() == lowered),
                        None,
                    )
                if meas is None:
                    raise FirmwareError(f"A2L: no MEASUREMENT named {name!r}")
                chosen.append(meas)

        signals = []
        for meas in chosen:
            try:
                signals.append(
                    Signal(
                        name=meas.name,
                        address=meas.address,
                        dtype=meas.dtype,
                        factor=meas.factor,
                        offset=meas.offset,
                        unit=meas.unit,
                    )
                )
            except ValueError as exc:       # never let a bare ValueError escape
                raise FirmwareError(f"A2L: {meas.name}: {exc}") from exc
        return signals


# --------------------------------------------------------------------------- #
# Tokeniser
#
# One anchored master pattern, matched at a known position. Every branch is
# unambiguous (the string branch's alternatives are disjoint, the word branch
# consumes one character per step), so matching is linear - a 50 MB A2L must not
# be able to trigger catastrophic backtracking.
# --------------------------------------------------------------------------- #
_WS_RE = re.compile(r"\s+")
_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
_TOKEN_RE = re.compile(
    r'"(?:[^"\\]|\\.)*"'            # quoted string
    r"|//[^\n]*"                    # line comment
    r"|/\*"                         # block comment opener
    r"|(?:[^\s\"/]|/(?![/*]))+"     # bare word; keeps "/begin" and "/end" whole
)
_ESCAPE_RE = re.compile(r"\\(.)")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0"}

Token = Tuple[bool, str]            # (is_quoted_string, value)


class _WarnLog:
    """Bounded warning collector.

    A single systematic defect in a large A2L would otherwise produce hundreds
    of thousands of identical strings, so the tail is counted, not stored.
    """

    def __init__(self) -> None:
        self.items: List[str] = []
        self.suppressed = 0

    def add(self, message: str) -> None:
        if len(self.items) < _MAX_WARNINGS:
            self.items.append(message)
        else:
            self.suppressed += 1
        log.debug("a2l: %s", message)

    def finish(self) -> List[str]:
        if self.suppressed:
            self.items.append(f"... {self.suppressed} further warning(s) suppressed")
        return self.items


def _unescape(text: str) -> str:
    return _ESCAPE_RE.sub(lambda m: _ESCAPES.get(m.group(1), m.group(1)), text)


def _tokenize(text: str, warns: _WarnLog) -> Iterator[Token]:
    """Yield ``(is_string, value)`` tokens, dropping comments and whitespace."""

    pos, end = 0, len(text)
    while pos < end:
        ws = _WS_RE.match(text, pos)
        if ws is not None:
            pos = ws.end()
            continue
        match = _TOKEN_RE.match(text, pos)
        if match is None:
            # The word branch matches every character except whitespace and a
            # quote, so the only way to get here is an unterminated string.
            raise FirmwareError(
                f"A2L: unterminated string near offset {pos} "
                f"({text[pos:pos + 40]!r})"
            )
        tok = match.group()
        pos = match.end()

        if tok == "/*":
            close = text.find("*/", pos)
            if close < 0:
                warns.add("unterminated /* block comment; ignored the rest of the file")
                return
            pos = close + 2
            continue
        if tok.startswith("//"):
            continue
        if tok.startswith('"'):
            value = tok[1:-1]
            # ASAP2 also allows "" inside a string as an escaped quote; the
            # regex stopped at the first one, so stitch the fragments together.
            while pos < end and text[pos] == '"':
                more = _STRING_RE.match(text, pos)
                if more is None:
                    raise FirmwareError(f"A2L: unterminated string near offset {pos}")
                value += '"' + more.group()[1:-1]
                pos = more.end()
            yield True, _unescape(value)
        else:
            yield False, tok


class _Stream:
    """Token source with one-token pushback - all the lookahead we ever need."""

    def __init__(self, text: str, warns: _WarnLog) -> None:
        self._tokens = _tokenize(text, warns)
        self._held: Optional[Token] = None

    def next(self) -> Optional[Token]:
        if self._held is not None:
            tok, self._held = self._held, None
            return tok
        return next(self._tokens, None)

    def push(self, tok: Token) -> None:
        self._held = tok

    def word(self) -> Optional[str]:
        """Next token's text, or ``None`` at end of file."""

        tok = self.next()
        return None if tok is None else tok[1]


# --------------------------------------------------------------------------- #
# Number helpers
# --------------------------------------------------------------------------- #
_HEXISH_RE = re.compile(r"\A[0-9A-Fa-f]+\Z")


def _to_int(tok: str) -> Optional[int]:
    """Parse an A2L integer: ``0x8000ABCD``, ``12345``, or bare hex digits."""

    try:
        return int(tok, 0)
    except ValueError:
        pass
    # Some tools emit addresses without the 0x prefix (or with a leading zero,
    # which base-0 rejects); those are hex by convention.
    if _HEXISH_RE.match(tok):
        try:
            return int(tok, 16)
        except ValueError:
            return None
    return None


def _to_float(tok: str) -> Optional[float]:
    try:
        return float(tok)
    except ValueError:
        as_int = _to_int(tok)
        return None if as_int is None else float(as_int)


# --------------------------------------------------------------------------- #
# Block parsing
# --------------------------------------------------------------------------- #
def _collect_block(stream: _Stream, btype: str) -> List[Token]:
    """Consume up to the matching ``/end btype`` and return its top-level tokens.

    Nested blocks (``IF_DATA``, ``ANNOTATION``, ``VIRTUAL``, ...) are skipped
    whole: their keywords must not be mistaken for the outer block's, and we
    never need their contents.
    """

    body: List[Token] = []
    nested: List[str] = []
    while True:
        tok = stream.next()
        if tok is None:
            raise FirmwareError(f"A2L: /begin {btype} is never closed")
        is_string, value = tok
        if not is_string and value == "/begin":
            opened = stream.word()
            if opened is None:
                raise FirmwareError("A2L: file ends right after /begin")
            nested.append(opened.upper())
            continue
        if not is_string and value == "/end":
            closed = stream.word()
            if closed is None:
                raise FirmwareError("A2L: file ends right after /end")
            if nested:
                if closed.upper() != nested[-1]:
                    raise FirmwareError(
                        f"A2L: /begin {nested[-1]} closed by /end {closed}"
                    )
                nested.pop()
                continue
            if closed.upper() != btype:
                raise FirmwareError(f"A2L: /begin {btype} closed by /end {closed}")
            return body
        if not nested:
            body.append(tok)


def _keyword_value(body: Sequence[Token], keyword: str, start: int = 0) -> Optional[str]:
    """Return the token following an exact keyword match, if any."""

    for i in range(start, len(body) - 1):
        is_string, value = body[i]
        if not is_string and value.upper() == keyword:
            return body[i + 1][1]
    return None


def _map_datatype(raw: str, owner: str, warns: _WarnLog) -> str:
    key = raw.upper()
    dtype = DATATYPES.get(key)
    if dtype is None:
        warns.add(f"{owner}: unknown datatype {raw!r}; assuming {_DEFAULT_DTYPE}")
        return _DEFAULT_DTYPE
    if key in _CLAMPED_DATATYPES:
        warns.add(
            f"{owner}: {key} clamped to {dtype} - 64-bit signals are not supported, "
            f"only the first 4 bytes are read"
        )
    return dtype


def _parse_measurement(body: List[Token], warns: _WarnLog
                       ) -> Optional[Tuple[A2lMeasurement, str]]:
    """Return ``(measurement, conversion_name)`` or ``None`` if unusable.

    Layout: ``Name "LongIdentifier" Datatype Conversion Resolution Accuracy
    LowerLimit UpperLimit`` followed by optional keywords, of which we want
    ``ECU_ADDRESS``.
    """

    if len(body) < 4:
        warns.add(f"MEASUREMENT with only {len(body)} field(s) skipped")
        return None
    name = body[0][1]
    long_id = body[1][1]
    dtype = _map_datatype(body[2][1], f"MEASUREMENT {name}", warns)
    conversion = body[3][1]

    address_tok = _keyword_value(body, "ECU_ADDRESS", start=4)
    if address_tok is None:
        # Plenty of real measurements are computed, not addressable; they simply
        # cannot be read over XCP, so they are dropped rather than given a fake 0.
        warns.add(f"MEASUREMENT {name}: no ECU_ADDRESS; skipped")
        return None
    address = _to_int(address_tok)
    if address is None:
        warns.add(f"MEASUREMENT {name}: unreadable ECU_ADDRESS {address_tok!r}; skipped")
        return None

    return A2lMeasurement(name=name, long_identifier=long_id, dtype=dtype,
                          address=address), conversion


def _parse_characteristic(body: List[Token], warns: _WarnLog
                          ) -> Optional[A2lCharacteristic]:
    """Layout: ``Name "LongIdentifier" Type Address Deposit MaxDiff Conversion ...``."""

    if len(body) < 3:
        warns.add(f"CHARACTERISTIC with only {len(body)} field(s) skipped")
        return None
    name = body[0][1]
    long_id = body[1][1]
    kind = body[2][1].upper()

    address = _to_int(body[3][1]) if len(body) > 3 else None
    if address is None:
        # Not every writer puts the address in the positional slot.
        keyword = _keyword_value(body, "ECU_ADDRESS", start=3)
        address = _to_int(keyword) if keyword else None
    if address is None:
        warns.add(f"CHARACTERISTIC {name}: no usable address; skipped")
        return None
    return A2lCharacteristic(name=name, long_identifier=long_id,
                             address=address, kind=kind)


def _linear_from_coeffs(name: str, coeffs: Sequence[float], warns: _WarnLog
                        ) -> Tuple[float, float, bool]:
    """Reduce RAT_FUNC ``COEFFS a b c d e f`` to ``(factor, offset, nonlinear)``.

    ``physical = (b*raw + c) / (e*raw + f)``; with ``a == d == e == 0`` that is a
    plain straight line, which is the only shape a :class:`Signal` can express.
    """

    a, b, c, d, e, f = coeffs
    if a or d or e:
        warns.add(f"COMPU_METHOD {name}: non-linear RAT_FUNC "
                  f"(a={a:g} d={d:g} e={e:g}); factor/offset left at 1/0")
        return 1.0, 0.0, True
    if not f:
        warns.add(f"COMPU_METHOD {name}: RAT_FUNC divides by zero (f=0); "
                  f"factor/offset left at 1/0")
        return 1.0, 0.0, True
    return b / f, c / f, False


def _parse_compu_method(body: List[Token], warns: _WarnLog) -> Optional[A2lCompuMethod]:
    """Layout: ``Name "LongIdentifier" ConversionType "Format" "Unit"`` + keywords."""

    if len(body) < 3:
        warns.add(f"COMPU_METHOD with only {len(body)} field(s) skipped")
        return None
    name = body[0][1]
    long_id = body[1][1]
    ctype = body[2][1].upper()
    fmt = body[3][1] if len(body) > 3 else ""
    unit = body[4][1] if len(body) > 4 else ""

    coeffs: Tuple[float, ...] = ()
    factor, offset, nonlinear = 1.0, 0.0, False

    linear = _collect_numbers(body, "COEFFS_LINEAR", 2)
    ratfunc = _collect_numbers(body, "COEFFS", 6)
    if linear is not None:
        coeffs = tuple(linear)
        factor, offset = linear[0], linear[1]
    elif ratfunc is not None:
        coeffs = tuple(ratfunc)
        factor, offset, nonlinear = _linear_from_coeffs(name, ratfunc, warns)
    elif ctype in ("IDENTICAL", "NO_COMPU_METHOD"):
        pass                                    # 1.0 / 0.0 already
    elif ctype in ("LINEAR", "RAT_FUNC"):
        warns.add(f"COMPU_METHOD {name}: {ctype} without usable coefficients; "
                  f"factor/offset left at 1/0")
    else:
        # TAB_INTP / TAB_NOINTP / TAB_VERB / FORM: a table or a formula, which
        # cannot be folded into factor/offset.
        nonlinear = True
        warns.add(f"COMPU_METHOD {name}: conversion type {ctype} is not linear; "
                  f"factor/offset left at 1/0")

    return A2lCompuMethod(name=name, long_identifier=long_id, conversion_type=ctype,
                          fmt=fmt, unit=unit, factor=factor, offset=offset,
                          nonlinear=nonlinear, coeffs=coeffs)


def _parse_xcp_on_can(body: List[Token]) -> Dict[str, Optional[int]]:
    """Extract CAN ids + baudrate from an ``XCP_ON_CAN`` block body.

    Layout (ASAM): ``XCP_ON_CAN <version> CAN_ID_BROADCAST <id> CAN_ID_MASTER
    <id> CAN_ID_SLAVE <id> BAUDRATE <baud> ...``. Master transmits on
    CAN_ID_MASTER; the slave replies (incl. all DAQ) on CAN_ID_SLAVE.
    """

    out: Dict[str, Optional[int]] = {}
    for key in ("CAN_ID_MASTER", "CAN_ID_SLAVE", "CAN_ID_BROADCAST", "BAUDRATE"):
        raw = _keyword_value(body, key)
        out[key.lower()] = _to_int(raw) if raw is not None else None
    # ASAM flags a 29-bit (extended) identifier by setting bit 31; the real id
    # is the low 29 bits. Detect it and strip the flag off the ids.
    extended = False
    for key in ("can_id_master", "can_id_slave", "can_id_broadcast"):
        v = out.get(key)
        if v is not None and v & 0x80000000:
            extended = True
            out[key] = v & 0x1FFFFFFF
    out["is_extended"] = extended  # type: ignore[assignment]
    return out


def _parse_event(body: List[Token]) -> Optional[A2lEvent]:
    """Extract (number, name, period) from an ``EVENT`` block body.

    Layout: ``EVENT "<long>" "<short>" <channel#> <direction> <maxDaqList>
    <cycle> <timeUnit> <priority>``. The period is ``cycle x 10^unit``; the
    time-unit codes are ASAM exponents (1ms=6? varies), so we only compute a
    period when both a cycle and a plausible unit are present, and never fail
    the parse over a missing/odd raster.
    """

    strings = [v for is_s, v in body if is_s]
    numbers = [n for n in (_to_int(v) for is_s, v in body if not is_s) if n is not None]
    if not numbers:
        return None
    name = strings[0] if strings else f"event{numbers[0]}"
    number = numbers[0]                 # EVENT_CHANNEL_NUMBER is the first number
    period: Optional[float] = None
    # numbers after the channel #: [maxDaqList, cycle, timeUnit, priority]
    if len(numbers) >= 4:
        cycle, unit = numbers[2], numbers[3]
        # ASAM time-unit codes: 1ns..1s as exponents; map the common ones.
        _UNIT = {0: 1e-9, 1: 1e-8, 2: 1e-7, 3: 1e-6, 4: 1e-5, 5: 1e-4,
                 6: 1e-3, 7: 1e-2, 8: 1e-1, 9: 1.0}
        if cycle > 0 and unit in _UNIT:
            period = cycle * _UNIT[unit]
    return A2lEvent(number=number, name=name.strip(), period_s=period)


def _collect_numbers(body: Sequence[Token], keyword: str, count: int
                     ) -> Optional[List[float]]:
    """Return the ``count`` numbers following ``keyword``, or ``None``."""

    for i, (is_string, value) in enumerate(body):
        if is_string or value.upper() != keyword:
            continue
        raw = body[i + 1: i + 1 + count]
        if len(raw) < count:
            return None
        numbers = [_to_float(tok[1]) for tok in raw]
        if any(n is None for n in numbers):
            return None
        return [float(n) for n in numbers]      # type: ignore[arg-type]
    return None


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def parse_a2l(text: str) -> A2lFile:
    """Parse A2L source text into an :class:`A2lFile`.

    Raises :class:`~med17flasher.exceptions.FirmwareError` if the block
    structure is broken (unbalanced ``/begin`` ... ``/end``, unterminated
    string) or if the text contains no ASAP2 blocks at all.
    """

    if not isinstance(text, str):
        raise FirmwareError("A2L: expected text, got "
                            f"{type(text).__name__}")

    warns = _WarnLog()
    stream = _Stream(text, warns)
    out = A2lFile()
    conversions: Dict[str, str] = {}    # measurement name -> COMPU_METHOD name
    open_blocks: List[str] = []
    seen_block = False

    while True:
        tok = stream.next()
        if tok is None:
            break
        is_string, value = tok
        if is_string:
            continue                    # a stray string outside any block
        upper = value.upper()

        if upper == "/BEGIN":
            btype = stream.word()
            if btype is None:
                raise FirmwareError("A2L: file ends right after /begin")
            btype = btype.upper()
            seen_block = True

            if btype in ("XCP_ON_CAN", "XCP_ON_UDP_IP", "EVENT"):
                body = _collect_block(stream, btype)
                if out.xcp is None:
                    out.xcp = A2lXcp()
                if btype == "XCP_ON_CAN":
                    ids = _parse_xcp_on_can(body)
                    out.xcp.transport = "can"
                    out.xcp.can_id_master = ids["can_id_master"]
                    out.xcp.can_id_slave = ids["can_id_slave"]
                    out.xcp.can_id_broadcast = ids["can_id_broadcast"]
                    out.xcp.is_extended = bool(ids.get("is_extended"))
                    out.xcp.baudrate = ids["baudrate"]
                elif btype == "XCP_ON_UDP_IP":
                    out.xcp.transport = "udp"
                else:  # EVENT
                    ev = _parse_event(body)
                    if ev is not None:
                        out.xcp.events.append(ev)
                continue

            if btype in _INTERESTING:
                body = _collect_block(stream, btype)
                if btype == "MEASUREMENT":
                    parsed = _parse_measurement(body, warns)
                    if parsed is not None:
                        meas, conversion = parsed
                        if meas.name in out.measurements:
                            warns.add(f"MEASUREMENT {meas.name} defined twice; "
                                      f"keeping the last one")
                        out.measurements[meas.name] = meas
                        conversions[meas.name] = conversion
                elif btype == "CHARACTERISTIC":
                    chara = _parse_characteristic(body, warns)
                    if chara is not None:
                        out.characteristics[chara.name] = chara
                else:
                    method = _parse_compu_method(body, warns)
                    if method is not None:
                        out.compu_methods[method.name] = method
                continue

            # A container (PROJECT / MODULE / MOD_COMMON / IF_DATA / A2ML / ...):
            # keep streaming, only noting the identifier of the two we name.
            open_blocks.append(btype)
            if btype in ("PROJECT", "MODULE"):
                name = _peek_identifier(stream)
                if btype == "PROJECT" and not out.project:
                    out.project = name
                elif btype == "MODULE" and not out.module:
                    out.module = name
            continue

        if upper == "/END":
            closed = stream.word()
            if closed is None:
                raise FirmwareError("A2L: file ends right after /end")
            if not open_blocks:
                raise FirmwareError(f"A2L: /end {closed} without a matching /begin")
            opened = open_blocks.pop()
            if closed.upper() != opened:
                raise FirmwareError(f"A2L: /begin {opened} closed by /end {closed}")
            continue

        # Anything else at container level (VERSION, ASAP2_VERSION, keywords of
        # MOD_COMMON, A2ML grammar text, ...) is not interesting here.

    if open_blocks:
        raise FirmwareError(f"A2L: /begin {open_blocks[-1]} is never closed")
    if not seen_block:
        raise FirmwareError("A2L: no /begin block found - this is not an ASAP2 file")

    _apply_conversions(out, conversions, warns)
    out.warnings = warns.finish()
    log.debug("a2l: %d measurement(s), %d characteristic(s), %d conversion(s), "
              "%d warning(s)", len(out.measurements), len(out.characteristics),
              len(out.compu_methods), len(out.warnings))
    return out


def _peek_identifier(stream: _Stream) -> str:
    """Read the identifier that follows ``/begin PROJECT`` / ``/begin MODULE``."""

    tok = stream.next()
    if tok is None:
        return ""
    is_string, value = tok
    if not is_string and value.upper() in ("/BEGIN", "/END"):
        stream.push(tok)                # nameless block: give the token back
        return ""
    return value


def _apply_conversions(out: A2lFile, conversions: Dict[str, str],
                       warns: _WarnLog) -> None:
    """Fold each measurement's COMPU_METHOD into its factor/offset/unit.

    Done as a second pass because A2L does not require a COMPU_METHOD to be
    defined before the MEASUREMENT that references it.
    """

    for name, conversion in conversions.items():
        meas = out.measurements.get(name)
        if meas is None:
            continue
        if not conversion or conversion.upper() == "NO_COMPU_METHOD":
            continue
        method = out.compu_methods.get(conversion)
        if method is None:
            warns.add(f"MEASUREMENT {name}: COMPU_METHOD {conversion!r} is not "
                      f"defined; raw values will be reported")
            continue
        meas.factor = method.factor
        meas.offset = method.offset
        meas.unit = method.unit
        meas.nonlinear = method.nonlinear


def load_a2l(path: str) -> A2lFile:
    """Read and parse an A2L file from disk.

    A2Ls come in UTF-8 (often with a BOM) and in Latin-1 from older tools, so
    both are tried before giving up.
    """

    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise FirmwareError(f"A2L: cannot read {path}: {exc}") from exc

    for encoding in ("utf-8-sig", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - latin-1 decodes every byte sequence
        raise FirmwareError(f"A2L: cannot decode {path}")

    log.info("parsing A2L %s (%d bytes)", path, len(raw))
    return parse_a2l(text)
