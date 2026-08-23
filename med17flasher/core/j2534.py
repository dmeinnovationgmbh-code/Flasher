"""SAE **J2534 PassThru** CAN backend (Tactrix Openport, Mongoose, VCX, ...).

Why this module exists
----------------------
Most professional flashing interfaces are **J2534 PassThru** devices: the
vendor ships a Windows DLL exporting ``PassThruOpen`` / ``PassThruConnect`` /
``PassThruReadMsgs`` / ``PassThruWriteMsgs``, and *that DLL is the only way to
reach the bus*. ``python-can`` does not speak J2534, so without this module a
Tactrix Openport 2.0 - the single most common tool in this corner of the world -
cannot be used at all.

Design
------
We connect the channel in **raw CAN mode** (``ProtocolID = CAN``), not
``ISO15765``. That is deliberate: this toolkit has its own, tested ISO-TP layer
(:mod:`med17flasher.core.isotp`) with explicit flow-control and STmin handling,
and the flash sequence depends on that exact behaviour. Letting the device
firmware do segmentation instead would silently change timing and error
semantics per vendor. Raw CAN keeps one code path for every backend.

Two J2534 details bite everyone who writes this the first time:

* **A freshly connected channel receives nothing.** You must install at least
  one ``PASS_FILTER``; the API defaults to blocking everything. We install a
  pass-all filter (mask 0 matches every id).
* **A CAN message's id lives in the first 4 bytes of** ``Data``, big-endian,
  followed by the payload - it is *not* a separate struct field. ``DataSize``
  therefore counts ``4 + len(payload)``.

Bitness
-------
Vendor PassThru DLLs are usually **32-bit** (Tactrix's is ``op20pt32.dll``), and
``ctypes`` can only load a DLL matching the running interpreter. A 64-bit build
must therefore reach it out-of-process - see :mod:`med17flasher.core.j2534_bridge`,
which :func:`open_j2534` falls back to automatically.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..exceptions import BackendNotAvailableError, TransportError
from ..logging_setup import get_logger
from .can_backends import CanBus, CanFrame

log = get_logger("core.j2534")

# --------------------------------------------------------------------------- #
# J2534-1 (v04.04) constants
# --------------------------------------------------------------------------- #
#: Protocol ids.
PROTOCOL_CAN = 5
PROTOCOL_ISO15765 = 6

#: Connect / TxFlags bits.
CAN_29BIT_ID = 0x00000100
ISO15765_FRAME_PAD = 0x00000040

#: RxStatus bits.
RX_TX_MSG_TYPE = 0x00000001      # this is the loopback echo of our own frame
RX_START_OF_MESSAGE = 0x00000002
RX_BREAK = 0x00000004
RX_TX_DONE = 0x00000008

#: Filter types.
PASS_FILTER = 1
BLOCK_FILTER = 2
FLOW_CONTROL_FILTER = 3

#: Ioctl ids.
GET_CONFIG = 0x01
SET_CONFIG = 0x02
READ_VBATT = 0x03
CLEAR_TX_BUFFER = 0x07
CLEAR_RX_BUFFER = 0x08
CLEAR_PERIODIC_MSGS = 0x09
CLEAR_MSG_FILTERS = 0x0A

#: SET_CONFIG parameter ids.
CFG_DATA_RATE = 0x01
CFG_LOOPBACK = 0x03
CFG_BIT_SAMPLE_POINT = 0x09
CFG_SYNC_JUMP_WIDTH = 0x0A

#: Return codes.
STATUS_NOERROR = 0x00
ERR_NOT_SUPPORTED = 0x01
ERR_INVALID_CHANNEL_ID = 0x02
ERR_INVALID_PROTOCOL_ID = 0x03
ERR_NULL_PARAMETER = 0x04
ERR_INVALID_IOCTL_VALUE = 0x05
ERR_INVALID_FLAGS = 0x06
ERR_FAILED = 0x07
ERR_DEVICE_NOT_CONNECTED = 0x08
ERR_TIMEOUT = 0x09
ERR_INVALID_MSG = 0x0A
ERR_INVALID_TIME_INTERVAL = 0x0B
ERR_EXCEEDED_LIMIT = 0x0C
ERR_INVALID_MSG_ID = 0x0D
ERR_DEVICE_IN_USE = 0x0E
ERR_INVALID_IOCTL_ID = 0x0F
ERR_BUFFER_EMPTY = 0x10
ERR_BUFFER_FULL = 0x11
ERR_BUFFER_OVERFLOW = 0x12
ERR_PIN_INVALID = 0x13
ERR_CHANNEL_IN_USE = 0x14
ERR_MSG_PROTOCOL_ID = 0x15
ERR_INVALID_FILTER_ID = 0x16
ERR_NO_FLOW_CONTROL = 0x17
ERR_NOT_UNIQUE = 0x18
ERR_INVALID_BAUDRATE = 0x19
ERR_INVALID_DEVICE_ID = 0x1A

_ERROR_NAMES = {
    ERR_NOT_SUPPORTED: "ERR_NOT_SUPPORTED",
    ERR_INVALID_CHANNEL_ID: "ERR_INVALID_CHANNEL_ID",
    ERR_INVALID_PROTOCOL_ID: "ERR_INVALID_PROTOCOL_ID",
    ERR_NULL_PARAMETER: "ERR_NULL_PARAMETER",
    ERR_INVALID_IOCTL_VALUE: "ERR_INVALID_IOCTL_VALUE",
    ERR_INVALID_FLAGS: "ERR_INVALID_FLAGS",
    ERR_FAILED: "ERR_FAILED",
    ERR_DEVICE_NOT_CONNECTED: "ERR_DEVICE_NOT_CONNECTED",
    ERR_TIMEOUT: "ERR_TIMEOUT",
    ERR_INVALID_MSG: "ERR_INVALID_MSG",
    ERR_INVALID_TIME_INTERVAL: "ERR_INVALID_TIME_INTERVAL",
    ERR_EXCEEDED_LIMIT: "ERR_EXCEEDED_LIMIT",
    ERR_INVALID_MSG_ID: "ERR_INVALID_MSG_ID",
    ERR_DEVICE_IN_USE: "ERR_DEVICE_IN_USE",
    ERR_INVALID_IOCTL_ID: "ERR_INVALID_IOCTL_ID",
    ERR_BUFFER_EMPTY: "ERR_BUFFER_EMPTY",
    ERR_BUFFER_FULL: "ERR_BUFFER_FULL",
    ERR_BUFFER_OVERFLOW: "ERR_BUFFER_OVERFLOW",
    ERR_PIN_INVALID: "ERR_PIN_INVALID",
    ERR_CHANNEL_IN_USE: "ERR_CHANNEL_IN_USE",
    ERR_MSG_PROTOCOL_ID: "ERR_MSG_PROTOCOL_ID",
    ERR_INVALID_FILTER_ID: "ERR_INVALID_FILTER_ID",
    ERR_NO_FLOW_CONTROL: "ERR_NO_FLOW_CONTROL",
    ERR_NOT_UNIQUE: "ERR_NOT_UNIQUE",
    ERR_INVALID_BAUDRATE: "ERR_INVALID_BAUDRATE",
    ERR_INVALID_DEVICE_ID: "ERR_INVALID_DEVICE_ID",
}

#: ``PASSTHRU_MSG.Data`` is a fixed 4128-byte array in the standard.
_MSG_DATA_SIZE = 4128

# J2534 is a Win32 API: its ``unsigned long`` is 32 bits even in a 64-bit
# process (LLP64). Spelling it ``c_uint32`` rather than ``c_ulong`` keeps the
# layout right on Linux/macOS too, where ``c_ulong`` would silently be 64 bits
# and shift every field after the first.
_U32 = ctypes.c_uint32


class PASSTHRU_MSG(ctypes.Structure):
    """The J2534 message struct (identical layout on every PassThru DLL)."""

    _fields_ = [
        ("ProtocolID", _U32),
        ("RxStatus", _U32),
        ("TxFlags", _U32),
        ("Timestamp", _U32),          # microseconds, device clock
        ("DataSize", _U32),
        ("ExtraDataIndex", _U32),
        ("Data", ctypes.c_ubyte * _MSG_DATA_SIZE),
    ]


class SCONFIG(ctypes.Structure):
    _fields_ = [("Parameter", _U32), ("Value", _U32)]


class SCONFIG_LIST(ctypes.Structure):
    _fields_ = [("NumOfParams", _U32), ("ConfigPtr", ctypes.POINTER(SCONFIG))]


# --------------------------------------------------------------------------- #
# Device discovery (Windows registry)
# --------------------------------------------------------------------------- #
@dataclass
class J2534Device:
    """One installed PassThru device as advertised in the registry."""

    name: str
    vendor: str = ""
    library: str = ""
    protocols: List[str] = field(default_factory=list)

    @property
    def supports_can(self) -> bool:
        return "CAN" in self.protocols

    @property
    def exists(self) -> bool:
        return bool(self.library) and os.path.isfile(self.library)

    def as_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "vendor": self.vendor,
            "library": self.library,
            "protocols": self.protocols,
            "installed": self.exists,
        }


#: Registry roots holding PassThru registrations, newest API revision first.
_REG_ROOTS = ("SOFTWARE\\PassThruSupport.04.04", "SOFTWARE\\PassThruSupport")

#: Protocol capability flags advertised as REG_DWORD values under each device.
_REG_PROTOCOLS = ("CAN", "ISO15765", "ISO9141", "ISO14230", "J1850PWM",
                  "J1850VPW", "SCI_A_ENGINE", "SCI_A_TRANS", "CAN_PS")


def list_devices() -> List[J2534Device]:
    """Enumerate installed J2534 devices (Windows only; [] elsewhere).

    Both registry views are read explicitly: a 64-bit process would otherwise
    miss every 32-bit vendor driver, which is most of them (Tactrix included).
    """

    if sys.platform != "win32":
        return []
    try:
        import winreg  # noqa: PLC0415 - Windows only
    except ImportError:  # pragma: no cover - non-Windows
        return []

    devices: List[J2534Device] = []
    seen: set = set()
    views = [getattr(winreg, "KEY_WOW64_32KEY", 0), getattr(winreg, "KEY_WOW64_64KEY", 0)]
    for root in _REG_ROOTS:
        for view in views:
            try:
                base = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root, 0,
                                      winreg.KEY_READ | view)
            except OSError:
                continue
            with base:
                for index in range(1024):
                    try:
                        sub = winreg.EnumKey(base, index)
                    except OSError:
                        break
                    device = _read_device(winreg, base, sub)
                    if device is None:
                        continue
                    key = (device.name.lower(), device.library.lower())
                    if key not in seen:
                        seen.add(key)
                        devices.append(device)
    return devices


def _read_device(winreg, base, sub: str) -> Optional[J2534Device]:
    """Read one device subkey; ``None`` when it carries no function library."""

    def value(key, name: str):
        try:
            return winreg.QueryValueEx(key, name)[0]
        except OSError:
            return None

    try:
        key = winreg.OpenKey(base, sub)
    except OSError:
        return None
    with key:
        library = value(key, "FunctionLibrary")
        if not library:
            return None
        protocols = [p for p in _REG_PROTOCOLS if value(key, p)]
        return J2534Device(
            name=str(value(key, "Name") or sub),
            vendor=str(value(key, "Vendor") or ""),
            library=str(library),
            protocols=protocols,
        )


def find_device(hint: str = "") -> J2534Device:
    """Resolve ``hint`` to a device: a DLL path, a name substring, or "" = first.

    ``hint`` is matched case-insensitively against the device name and the
    vendor, so ``"tactrix"`` or ``"openport"`` both find an Openport 2.0.
    """

    if hint and (hint.lower().endswith(".dll") or os.path.isfile(hint)):
        return J2534Device(name=os.path.basename(hint), library=hint,
                           protocols=["CAN"])

    devices = list_devices()
    if not devices:
        raise BackendNotAvailableError(
            "no J2534 PassThru device is installed"
            + (" (J2534 drivers are Windows-only; pass an explicit .dll path "
               "to use one through Wine)" if sys.platform != "win32" else
               ". Install the vendor driver - for a Tactrix Openport 2.0 that "
               "is the 'OpenPort 2.0 drivers' package from tactrix.com.")
        )

    if hint:
        needle = hint.lower()
        for device in devices:
            if needle in device.name.lower() or needle in device.vendor.lower():
                return device
        names = ", ".join(repr(d.name) for d in devices)
        raise BackendNotAvailableError(
            f"no installed J2534 device matches {hint!r}; found: {names}"
        )

    can = [d for d in devices if d.supports_can] or devices
    if len(can) > 1:
        log.info("several J2534 devices installed, using %r (others: %s)",
                 can[0].name, ", ".join(repr(d.name) for d in can[1:]))
    return can[0]


# --------------------------------------------------------------------------- #
# The bus
# --------------------------------------------------------------------------- #
class J2534Bus(CanBus):
    """A :class:`~med17flasher.core.can_backends.CanBus` on a PassThru device.

    Parameters
    ----------
    library:
        Path to the vendor DLL. Resolved from the registry when omitted.
    device:
        Name hint used for registry lookup (``"tactrix"``, ``"Mongoose"``, ...).
    baudrate:
        CAN bit rate; MED17 diagnostics run at 500 kbit/s.
    extended:
        Enable 29-bit addressing. MED17.7.5 diagnostics are 11-bit, so this
        defaults off; when on, both 11- and 29-bit frames are received.
    loopback:
        Leave the device's transmit echo on. Off by default - our ISO-TP layer
        would otherwise see its own frames as ECU responses. Echoes are dropped
        in :meth:`recv` regardless, so this is belt and braces.
    """

    def __init__(
        self,
        library: str = "",
        *,
        device: str = "",
        baudrate: int = 500000,
        extended: bool = False,
        loopback: bool = False,
        loader: Optional[object] = None,
    ) -> None:
        self.device = find_device(library or device)
        self.library = self.device.library
        self.baudrate = int(baudrate)
        self.extended = bool(extended)
        self.name = f"j2534:{self.device.name}"

        self._lock = threading.RLock()
        self._rx: List[CanFrame] = []      # batched reads, drained by recv()
        self._device_id = _U32(0)
        self._channel_id = _U32(0)
        self._filters: List[int] = []
        self._connected = False
        self._closed = False

        self._lib = self._load(loader)
        self._bind()
        self._open(loopback)

    # ------------------------------------------------------------------ #
    # DLL plumbing
    # ------------------------------------------------------------------ #
    def _load(self, loader: Optional[object]):
        if not self.library:
            raise BackendNotAvailableError("J2534 device has no FunctionLibrary")
        if not os.path.isfile(self.library):
            raise BackendNotAvailableError(
                f"J2534 driver not found: {self.library!r}. The device is "
                "registered but its DLL is missing - reinstall the vendor driver."
            )
        load = loader or (ctypes.WinDLL if sys.platform == "win32" else ctypes.CDLL)
        try:
            return load(self.library)  # type: ignore[operator]
        except OSError as exc:
            bits = 8 * ctypes.sizeof(ctypes.c_void_p)
            raise BackendNotAvailableError(
                f"cannot load the J2534 driver {self.library!r}: {exc}. "
                f"This process is {bits}-bit; most PassThru DLLs (including "
                "Tactrix's op20pt32.dll) are 32-bit. Use open_j2534(), which "
                "falls back to the out-of-process bridge automatically."
            ) from exc

    #: ``name -> argtypes`` for every entry point we call. Optional ones are
    #: tolerated missing so a partial/OEM driver still opens a CAN channel.
    _SIGNATURES: Dict[str, Tuple[type, ...]] = {
        "PassThruOpen": (ctypes.c_void_p, ctypes.POINTER(_U32)),
        "PassThruClose": (_U32,),
        "PassThruConnect": (_U32, _U32, _U32, _U32, ctypes.POINTER(_U32)),
        "PassThruDisconnect": (_U32,),
        "PassThruReadMsgs": (_U32, ctypes.POINTER(PASSTHRU_MSG),
                             ctypes.POINTER(_U32), _U32),
        "PassThruWriteMsgs": (_U32, ctypes.POINTER(PASSTHRU_MSG),
                              ctypes.POINTER(_U32), _U32),
        "PassThruStartMsgFilter": (_U32, _U32, ctypes.POINTER(PASSTHRU_MSG),
                                   ctypes.POINTER(PASSTHRU_MSG),
                                   ctypes.POINTER(PASSTHRU_MSG),
                                   ctypes.POINTER(_U32)),
        "PassThruStopMsgFilter": (_U32, _U32),
        "PassThruIoctl": (_U32, _U32, ctypes.c_void_p, ctypes.c_void_p),
        "PassThruReadVersion": (_U32, ctypes.c_char_p, ctypes.c_char_p,
                                ctypes.c_char_p),
        "PassThruGetLastError": (ctypes.c_char_p,),
    }

    #: Without these a CAN channel cannot be opened at all.
    _REQUIRED = ("PassThruOpen", "PassThruClose", "PassThruConnect",
                 "PassThruDisconnect", "PassThruReadMsgs", "PassThruWriteMsgs",
                 "PassThruStartMsgFilter")

    def _bind(self) -> None:
        self._fn: Dict[str, object] = {}
        missing: List[str] = []
        for name, argtypes in self._SIGNATURES.items():
            fn = getattr(self._lib, name, None)
            if fn is None:
                missing.append(name)
                continue
            fn.restype = ctypes.c_int32
            fn.argtypes = list(argtypes)
            self._fn[name] = fn
        required = [n for n in self._REQUIRED if n not in self._fn]
        if required:
            raise BackendNotAvailableError(
                f"{self.library!r} is not a J2534 PassThru driver "
                f"(missing {', '.join(required)})"
            )
        if missing:
            log.debug("J2534 driver lacks optional entry points: %s",
                      ", ".join(missing))

    def _last_error(self) -> str:
        fn = self._fn.get("PassThruGetLastError")
        if fn is None:
            return ""
        buf = ctypes.create_string_buffer(80)
        try:
            fn(buf)  # type: ignore[operator]
        except Exception:  # noqa: BLE001 - diagnostics must never mask the real error
            return ""
        return buf.value.decode("latin-1", "replace").strip()

    def _check(self, rc: int, what: str) -> int:
        if rc == STATUS_NOERROR:
            return rc
        name = _ERROR_NAMES.get(rc, f"0x{rc:02X}")
        detail = self._last_error()
        raise TransportError(
            f"J2534 {what} failed: {name}" + (f" - {detail}" if detail else "")
        )

    def _call(self, name: str, *args) -> int:
        fn = self._fn.get(name)
        if fn is None:
            raise TransportError(f"J2534 driver does not export {name}")
        return int(fn(*args))  # type: ignore[operator]

    # ------------------------------------------------------------------ #
    # Open / close
    # ------------------------------------------------------------------ #
    def _open(self, loopback: bool) -> None:
        # PassThruOpen(NULL, &id) selects the single attached device, which is
        # what every consumer-grade interface expects.
        self._check(self._call("PassThruOpen", None, ctypes.byref(self._device_id)),
                    "Open")
        opened = True
        try:
            flags = CAN_29BIT_ID if self.extended else 0
            self._check(
                self._call("PassThruConnect", self._device_id, _U32(PROTOCOL_CAN),
                           _U32(flags), _U32(self.baudrate),
                           ctypes.byref(self._channel_id)),
                "Connect",
            )
            self._connected = True
            self._ioctl(CLEAR_MSG_FILTERS)
            self._set_config(CFG_LOOPBACK, 1 if loopback else 0, optional=True)
            self._install_pass_filters()
            self._ioctl(CLEAR_RX_BUFFER)
            self._ioctl(CLEAR_TX_BUFFER)
        except Exception:
            # Never leak the device handle: a J2534 device left open stays
            # locked until the driver is unloaded, and the next run then fails
            # with ERR_DEVICE_IN_USE for no visible reason.
            self._teardown()
            opened = False
            raise
        finally:
            if not opened and not self._closed:
                self._closed = True
        log.info("J2534 open: %s (%s) @ %d bit/s%s", self.device.name,
                 os.path.basename(self.library), self.baudrate,
                 " ext" if self.extended else "")

    def _install_pass_filters(self) -> None:
        """Install pass-all filters - without one the channel receives nothing."""

        modes = [0] + ([CAN_29BIT_ID] if self.extended else [])
        for tx_flags in modes:
            mask = self._msg(b"\x00\x00\x00\x00", tx_flags)
            pattern = self._msg(b"\x00\x00\x00\x00", tx_flags)
            filter_id = _U32(0)
            self._check(
                self._call("PassThruStartMsgFilter", self._channel_id,
                           _U32(PASS_FILTER), ctypes.byref(mask),
                           ctypes.byref(pattern), None, ctypes.byref(filter_id)),
                "StartMsgFilter",
            )
            self._filters.append(filter_id.value)

    def _msg(self, data: bytes, tx_flags: int = 0) -> PASSTHRU_MSG:
        msg = PASSTHRU_MSG()
        msg.ProtocolID = PROTOCOL_CAN
        msg.TxFlags = tx_flags
        msg.DataSize = len(data)
        ctypes.memmove(msg.Data, data, len(data))
        return msg

    def _ioctl(self, ioctl_id: int, optional: bool = True) -> None:
        if "PassThruIoctl" not in self._fn:
            return
        rc = self._call("PassThruIoctl", self._channel_id, _U32(ioctl_id), None, None)
        if rc != STATUS_NOERROR and not optional:
            self._check(rc, f"Ioctl(0x{ioctl_id:02X})")
        elif rc != STATUS_NOERROR:
            log.debug("J2534 Ioctl 0x%02X returned 0x%02X (ignored)", ioctl_id, rc)

    def _set_config(self, parameter: int, value: int, optional: bool = False) -> None:
        if "PassThruIoctl" not in self._fn:
            return
        item = SCONFIG(Parameter=parameter, Value=value)
        cfg = SCONFIG_LIST(NumOfParams=1, ConfigPtr=ctypes.pointer(item))
        rc = self._call("PassThruIoctl", self._channel_id, _U32(SET_CONFIG),
                        ctypes.byref(cfg), None)
        if rc != STATUS_NOERROR:
            if optional:
                log.debug("J2534 SET_CONFIG(%d=%d) returned 0x%02X (ignored)",
                          parameter, value, rc)
            else:
                self._check(rc, f"SET_CONFIG({parameter}={value})")

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def read_version(self) -> Dict[str, str]:
        """Firmware / DLL / API version strings, for the log and the UI."""

        if "PassThruReadVersion" not in self._fn:
            return {}
        firmware = ctypes.create_string_buffer(80)
        dll = ctypes.create_string_buffer(80)
        api = ctypes.create_string_buffer(80)
        rc = self._call("PassThruReadVersion", self._device_id, firmware, dll, api)
        if rc != STATUS_NOERROR:
            return {}
        decode = lambda b: b.value.decode("latin-1", "replace").strip()  # noqa: E731
        return {"firmware": decode(firmware), "dll": decode(dll), "api": decode(api)}

    def battery_voltage(self) -> Optional[float]:
        """Battery voltage at pin 16 in volts, or ``None`` if unsupported.

        Worth showing before a flash: MED17 reprogramming that browns out
        mid-erase leaves a brick, and most workshops catch that with a
        "battery below 12 V" check rather than a charger.
        """

        if "PassThruIoctl" not in self._fn:
            return None
        millivolts = _U32(0)
        rc = self._call("PassThruIoctl", self._device_id, _U32(READ_VBATT), None,
                        ctypes.byref(millivolts))
        if rc != STATUS_NOERROR:
            return None
        return millivolts.value / 1000.0

    # ------------------------------------------------------------------ #
    # CanBus interface
    # ------------------------------------------------------------------ #
    def send(self, frame: CanFrame, timeout: float = 1.0) -> None:
        if self._closed:
            raise TransportError("send on a closed J2534 bus")
        if frame.is_extended_id and not self.extended:
            raise TransportError(
                f"cannot send 29-bit id 0x{frame.arbitration_id:X}: the J2534 "
                "channel was opened for 11-bit ids (pass extended=True)"
            )
        # A CAN message on J2534 is <4-byte big-endian id><payload>.
        data = frame.arbitration_id.to_bytes(4, "big") + frame.data
        msg = self._msg(data, CAN_29BIT_ID if frame.is_extended_id else 0)
        count = _U32(1)
        with self._lock:
            rc = self._call("PassThruWriteMsgs", self._channel_id,
                            ctypes.byref(msg), ctypes.byref(count),
                            _U32(max(0, int(timeout * 1000))))
        self._check(rc, "WriteMsgs")
        if count.value != 1:
            raise TransportError("J2534 WriteMsgs accepted no frame (bus off?)")

    #: Frames pulled per ReadMsgs call. During a flash the ECU answers in a
    #: burst, and one DLL round trip per frame dominates the transfer time.
    _BATCH = 16

    def recv(self, timeout: float = 1.0) -> Optional[CanFrame]:
        if self._closed:
            return None
        with self._lock:
            if self._rx:
                return self._rx.pop(0)
            frames = self._read_batch(timeout)
            if not frames:
                return None
            self._rx = frames[1:]
            return frames[0]

    def _read_batch(self, timeout: float) -> List[CanFrame]:
        buf = (PASSTHRU_MSG * self._BATCH)()
        for msg in buf:
            msg.ProtocolID = PROTOCOL_CAN
        count = _U32(self._BATCH)
        rc = self._call("PassThruReadMsgs", self._channel_id,
                        ctypes.cast(buf, ctypes.POINTER(PASSTHRU_MSG)),
                        ctypes.byref(count), _U32(max(0, int(timeout * 1000))))
        # An empty buffer / expired timeout is the normal "nothing arrived"
        # answer, not a failure; drivers disagree on which of the two they use.
        if rc in (ERR_BUFFER_EMPTY, ERR_TIMEOUT):
            return [f for f in self._decode(buf, count.value)]
        self._check(rc, "ReadMsgs")
        return [f for f in self._decode(buf, count.value)]

    def _decode(self, buf, count: int) -> List[CanFrame]:
        out: List[CanFrame] = []
        for i in range(min(count, self._BATCH)):
            msg = buf[i]
            # Skip our own transmit echo and protocol events (RX_BREAK etc.),
            # which carry no CAN payload.
            if msg.RxStatus & (RX_TX_MSG_TYPE | RX_BREAK | RX_TX_DONE):
                continue
            size = int(msg.DataSize)
            if size < 4:
                continue
            raw = bytes(bytearray(msg.Data[:size]))
            is_ext = bool(msg.RxStatus & CAN_29BIT_ID)
            arb = int.from_bytes(raw[:4], "big") & (0x1FFFFFFF if is_ext else 0x7FF)
            payload = raw[4:12]  # classic CAN: never more than 8 data bytes
            out.append(CanFrame(arb, payload, is_ext,
                                timestamp=msg.Timestamp / 1e6))
        return out

    def flush_rx(self) -> None:
        """Drop buffered frames, in the driver as well as in our own batch."""

        with self._lock:
            self._rx.clear()
            if not self._closed and self._connected:
                self._ioctl(CLEAR_RX_BUFFER)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._teardown()

    def _teardown(self) -> None:
        """Release filters, channel and device, tolerating a half-open state."""

        for filter_id in self._filters:
            try:
                self._call("PassThruStopMsgFilter", self._channel_id, _U32(filter_id))
            except Exception as exc:  # noqa: BLE001
                log.debug("StopMsgFilter(%d) failed: %s", filter_id, exc)
        self._filters.clear()
        if self._connected:
            try:
                self._call("PassThruDisconnect", self._channel_id)
            except Exception as exc:  # noqa: BLE001
                log.debug("PassThruDisconnect failed: %s", exc)
            self._connected = False
        try:
            self._call("PassThruClose", self._device_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("PassThruClose failed: %s", exc)


# --------------------------------------------------------------------------- #
# Entry point with automatic 32-bit fallback
# --------------------------------------------------------------------------- #
def open_j2534(hint: str = "", **kwargs) -> CanBus:
    """Open a J2534 bus, bridging to a 32-bit helper if the DLL needs one.

    This is what :func:`~med17flasher.core.can_backends.create_bus` calls. It
    tries in-process first (fast path: bitness already matches) and falls back
    to :class:`~med17flasher.core.j2534_bridge.J2534BridgeBus` when the loader
    refuses the image, which is what a 64-bit build hits with Tactrix's
    ``op20pt32.dll``.
    """

    device = find_device(hint)
    try:
        return J2534Bus(device.library, **kwargs)
    except BackendNotAvailableError as exc:
        if not _is_bitness_error(exc):
            raise
        log.info("J2534 driver %r cannot be loaded in-process (%s); "
                 "falling back to the 32-bit bridge", device.library, exc)

    from .j2534_bridge import J2534BridgeBus  # local: only needed on this path

    return J2534BridgeBus(device.library, **kwargs)


def _is_bitness_error(exc: Exception) -> bool:
    """Is this the loader refusing a wrong-architecture image?

    WinError 193 (``%1 is not a valid Win32 application``) is the Windows
    signal; the ELF loader says "wrong ELF class" instead.
    """

    text = str(exc).lower()
    return ("193" in text or "not a valid win32 application" in text
            or "wrong elf class" in text or "invalid win32" in text)


def available() -> bool:
    """True when at least one J2534 device with CAN support is installed."""

    try:
        return any(d.supports_can for d in list_devices())
    except Exception:  # noqa: BLE001 - discovery must never break backend listing
        return False
