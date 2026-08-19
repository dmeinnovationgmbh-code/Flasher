"""CAN bus abstraction and concrete backends.

Everything above this module talks to :class:`CanBus`; the concrete transport
(a real adapter, SocketCAN, or the in-process virtual bus used by the ECU
simulator and the tests) is chosen at the edge via :func:`create_bus`.

Only :class:`VirtualCanBus` and :class:`SocketCanBus` (Linux) are dependency
free. :class:`PythonCanBus` lights up when ``python-can`` is installed and
covers the huge range of adapters that library supports (PCAN, Vector,
Kvaser, SocketCAN, slcan, ...).
"""

from __future__ import annotations

import queue
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..exceptions import BackendNotAvailableError, TransportError
from ..logging_setup import get_logger

log = get_logger("core.can")


@dataclass
class CanFrame:
    """A single classic CAN 2.0 frame.

    ``data`` is at most 8 bytes. CAN-FD is intentionally not modelled: the
    MED17.7.5 diagnostic channel is classic 11-bit CAN.
    """

    arbitration_id: int
    data: bytes
    is_extended_id: bool = False
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.data, (bytes, bytearray)):
            raise TypeError("CanFrame.data must be bytes")
        self.data = bytes(self.data)
        if len(self.data) > 8:
            raise ValueError("classic CAN frame data length must be <= 8")
        max_id = 0x1FFFFFFF if self.is_extended_id else 0x7FF
        if not (0 <= self.arbitration_id <= max_id):
            raise ValueError(
                f"arbitration id 0x{self.arbitration_id:X} out of range for "
                f"{'extended' if self.is_extended_id else 'standard'} frame"
            )

    def __str__(self) -> str:
        kind = "x" if self.is_extended_id else "s"
        return f"{self.arbitration_id:03X}{kind} [{len(self.data)}] {self.data.hex(' ')}"


class CanBus(ABC):
    """Abstract full-duplex CAN endpoint."""

    #: Human friendly identifier used in logs and the GUI.
    name: str = "can"

    @abstractmethod
    def send(self, frame: CanFrame, timeout: float = 1.0) -> None:
        """Transmit a single frame."""

    @abstractmethod
    def recv(self, timeout: float = 1.0) -> Optional[CanFrame]:
        """Return the next received frame or ``None`` on timeout."""

    def flush_rx(self) -> None:
        """Drop any buffered received frames.

        Called before a new UDS request so stale frames from a previous,
        possibly aborted, exchange never leak into the next one.
        """

        while self.recv(timeout=0.0) is not None:
            pass

    @abstractmethod
    def close(self) -> None:
        """Release any underlying resources."""

    # Context-manager sugar so ``with create_bus(...) as bus:`` works.
    def __enter__(self) -> "CanBus":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# Virtual, in-process bus
# --------------------------------------------------------------------------- #
class VirtualCanNetwork:
    """A shared broadcast medium connecting several :class:`VirtualCanBus`.

    A frame transmitted by one endpoint is delivered to *every other* endpoint,
    exactly like nodes sharing a physical CAN segment. This is what lets the
    tester side and the :mod:`med17flasher.simulator` ECU talk to each other in
    the same process, and it is the backbone of the integration tests.
    """

    def __init__(self, *, latency: float = 0.0) -> None:
        self._endpoints: List["VirtualCanBus"] = []
        self._lock = threading.RLock()
        self.latency = latency

    def attach(self, endpoint: "VirtualCanBus") -> None:
        with self._lock:
            self._endpoints.append(endpoint)

    def detach(self, endpoint: "VirtualCanBus") -> None:
        with self._lock:
            if endpoint in self._endpoints:
                self._endpoints.remove(endpoint)

    def broadcast(self, sender: "VirtualCanBus", frame: CanFrame) -> None:
        if self.latency:
            time.sleep(self.latency)
        with self._lock:
            targets = [e for e in self._endpoints if e is not sender]
        for endpoint in targets:
            endpoint._deliver(frame)

    def new_endpoint(self, name: str = "virtual") -> "VirtualCanBus":
        bus = VirtualCanBus(self, name=name)
        return bus


class VirtualCanBus(CanBus):
    """One node on a :class:`VirtualCanNetwork`."""

    def __init__(self, network: VirtualCanNetwork, name: str = "virtual") -> None:
        self.name = name
        self._network = network
        self._rx: "queue.Queue[CanFrame]" = queue.Queue()
        self._closed = False
        network.attach(self)

    def _deliver(self, frame: CanFrame) -> None:
        if not self._closed:
            self._rx.put(frame)

    def send(self, frame: CanFrame, timeout: float = 1.0) -> None:
        if self._closed:
            raise TransportError("send on a closed VirtualCanBus")
        stamped = CanFrame(
            frame.arbitration_id,
            frame.data,
            frame.is_extended_id,
            timestamp=time.time(),
        )
        self._network.broadcast(self, stamped)

    def recv(self, timeout: float = 1.0) -> Optional[CanFrame]:
        if self._closed:
            return None
        try:
            if timeout <= 0:
                return self._rx.get_nowait()
            return self._rx.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._network.detach(self)


# --------------------------------------------------------------------------- #
# Native SocketCAN backend (Linux, no third-party dependency)
# --------------------------------------------------------------------------- #
class SocketCanBus(CanBus):
    """A native SocketCAN endpoint implemented directly on top of ``socket``.

    Works on Linux with ``can0``/``vcan0`` interfaces without pulling in
    ``python-can``. Useful on the bench, and against a virtual CAN interface
    (``vcan``) for realistic host-level testing.
    """

    _CAN_RAW = 1
    _AF_CAN = 29
    _PF_CAN = 29
    _CAN_EFF_FLAG = 0x80000000
    _CAN_EFF_MASK = 0x1FFFFFFF
    _CAN_SFF_MASK = 0x000007FF
    _FRAME_FMT = "=IB3x8s"  # can_id, can_dlc, pad(3), data(8)
    _FRAME_SIZE = 16

    def __init__(self, channel: str = "can0") -> None:
        import socket
        import struct

        self._socket_mod = socket
        self._struct = struct
        self.name = f"socketcan:{channel}"
        try:
            self._sock = socket.socket(
                self._PF_CAN, socket.SOCK_RAW, self._CAN_RAW
            )
            self._sock.bind((channel,))
        except (OSError, AttributeError) as exc:  # pragma: no cover - hw dependent
            raise BackendNotAvailableError(
                f"cannot open SocketCAN channel {channel!r}: {exc}"
            ) from exc

    def send(self, frame: CanFrame, timeout: float = 1.0) -> None:
        can_id = frame.arbitration_id
        if frame.is_extended_id:
            can_id = (can_id & self._CAN_EFF_MASK) | self._CAN_EFF_FLAG
        payload = frame.data.ljust(8, b"\x00")
        packet = self._struct.pack(
            self._FRAME_FMT, can_id, len(frame.data), payload
        )
        try:
            self._sock.settimeout(timeout if timeout > 0 else None)
            self._sock.send(packet)
        except OSError as exc:  # pragma: no cover - hw dependent
            raise TransportError(f"SocketCAN send failed: {exc}") from exc

    def recv(self, timeout: float = 1.0) -> Optional[CanFrame]:
        try:
            self._sock.settimeout(timeout if timeout > 0 else 0.0)
            packet = self._sock.recv(self._FRAME_SIZE)
        except (self._socket_mod.timeout, BlockingIOError):
            return None
        except OSError as exc:  # pragma: no cover - hw dependent
            raise TransportError(f"SocketCAN recv failed: {exc}") from exc
        can_id, dlc, payload = self._struct.unpack(self._FRAME_FMT, packet)
        is_ext = bool(can_id & self._CAN_EFF_FLAG)
        arb = can_id & (self._CAN_EFF_MASK if is_ext else self._CAN_SFF_MASK)
        return CanFrame(arb, payload[:dlc], is_ext, timestamp=time.time())

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:  # pragma: no cover
            pass


# --------------------------------------------------------------------------- #
# python-can backend (optional, huge adapter coverage)
# --------------------------------------------------------------------------- #
class PythonCanBus(CanBus):
    """Adapter around :mod:`can` (python-can), when it is installed."""

    def __init__(self, **kwargs: object) -> None:
        try:
            import can  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dep
            raise BackendNotAvailableError(
                "python-can is not installed; run 'pip install python-can'"
            ) from exc

        self._can = can
        interface = kwargs.pop("interface", None) or kwargs.pop("bustype", None)
        channel = kwargs.get("channel")
        self.name = f"python-can:{interface or 'default'}:{channel or ''}"
        try:
            self._bus = can.Bus(interface=interface, **kwargs)  # type: ignore[arg-type]
        except Exception as exc:  # pragma: no cover - optional dep
            raise BackendNotAvailableError(
                f"python-can could not open the bus: {exc}"
            ) from exc

    def send(self, frame: CanFrame, timeout: float = 1.0) -> None:
        msg = self._can.Message(
            arbitration_id=frame.arbitration_id,
            data=frame.data,
            is_extended_id=frame.is_extended_id,
        )
        try:
            self._bus.send(msg, timeout=timeout)
        except Exception as exc:  # pragma: no cover - optional dep
            raise TransportError(f"python-can send failed: {exc}") from exc

    def recv(self, timeout: float = 1.0) -> Optional[CanFrame]:
        try:
            msg = self._bus.recv(timeout=timeout)
        except Exception as exc:  # pragma: no cover - optional dep
            raise TransportError(f"python-can recv failed: {exc}") from exc
        if msg is None:
            return None
        return CanFrame(
            msg.arbitration_id,
            bytes(msg.data),
            bool(msg.is_extended_id),
            timestamp=msg.timestamp or time.time(),
        )

    def close(self) -> None:
        try:
            self._bus.shutdown()
        except Exception:  # pragma: no cover - optional dep
            pass


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
#: Registry of shared virtual networks keyed by name, so several endpoints can
#: be created via ``create_bus("virtual:mynet")`` and end up on the same wire.
_VIRTUAL_NETWORKS: Dict[str, VirtualCanNetwork] = {}
_VNET_LOCK = threading.Lock()


def get_virtual_network(name: str = "default") -> VirtualCanNetwork:
    """Return (creating on first use) a named shared virtual CAN network."""

    with _VNET_LOCK:
        net = _VIRTUAL_NETWORKS.get(name)
        if net is None:
            net = VirtualCanNetwork()
            _VIRTUAL_NETWORKS[name] = net
        return net


def create_bus(spec: str, **kwargs: object) -> CanBus:
    """Create a :class:`CanBus` from a compact ``backend:target`` string.

    Examples
    --------
    ``"virtual"`` / ``"virtual:testnet"``
        In-process broadcast bus (optionally on a named network).
    ``"socketcan:can0"``
        Native Linux SocketCAN on ``can0``.
    ``"pcan:PCAN_USBBUS1"``, ``"vector:0"``, ``"slcan:/dev/ttyUSB0"`` ...
        Delegated to python-can (``interface`` inferred from the prefix).
    """

    backend, _, target = spec.partition(":")
    backend = backend.strip().lower()

    if backend in ("virtual", "vcan-mem", "mem"):
        net = get_virtual_network(target or "default")
        return net.new_endpoint(name=spec)

    if backend == "socketcan" and target:
        # Prefer python-can's socketcan if present (better feature coverage),
        # otherwise fall back to the native implementation.
        try:
            return PythonCanBus(interface="socketcan", channel=target, **kwargs)
        except BackendNotAvailableError:
            return SocketCanBus(channel=target)

    if backend in ("socketcan-native", "rawcan"):
        return SocketCanBus(channel=target or "can0")

    # Everything else is delegated to python-can, mapping the prefix to its
    # interface name.
    interface_map = {
        "pcan": "pcan",
        "vector": "vector",
        "kvaser": "kvaser",
        "ixxat": "ixxat",
        "slcan": "slcan",
        "seeedstudio": "seeedstudio",
        "usb2can": "usb2can",
        "neovi": "neovi",
        "systec": "systec",
        "canalystii": "canalystii",
        "gs_usb": "gs_usb",
    }
    interface = interface_map.get(backend, backend)
    if target:
        kwargs.setdefault("channel", target)
    return PythonCanBus(interface=interface, **kwargs)


def available_backends() -> Dict[str, bool]:
    """Report which backends are usable in the current environment."""

    result = {"virtual": True}
    try:  # SocketCAN is Linux only
        import socket

        result["socketcan"] = hasattr(socket, "AF_CAN") or True
    except Exception:  # pragma: no cover
        result["socketcan"] = False
    try:
        import can  # noqa: F401

        result["python-can"] = True
    except ImportError:
        result["python-can"] = False
    return result
