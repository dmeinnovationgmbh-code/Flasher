"""Drive a **32-bit J2534 PassThru driver** from a 64-bit process.

Tactrix ships ``op20pt32.dll``; Mongoose, VCX and most other interfaces are
32-bit too. The PyInstaller desktop build is 64-bit, and the Windows loader
flatly refuses to map a 32-bit image into it, so the DLL has to live in a
32-bit helper process. This module is the CAN-bus counterpart of
:mod:`med17flasher.seedkey.bridge`: the helper owns a real
:class:`~med17flasher.core.j2534.J2534Bus`, and :class:`J2534BridgeBus` forwards
``send`` / ``recv`` to it over the line-JSON protocol from
:mod:`med17flasher.core.procbridge`.

Throughput
----------
``recv`` returns the child's **whole read batch** in one reply and the parent
hands frames out one at a time, so a pipe round trip is amortised over up to
16 frames rather than paid per frame. That matters: a full MED17 flash moves
hundreds of thousands of CAN frames.

Run the worker by hand to debug a driver::

    py -3-32 -m med17flasher.core.j2534_bridge --library op20pt32.dll
    {"cmd": "info"}
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

from ..exceptions import TransportError
from ..logging_setup import get_logger
from .can_backends import CanBus, CanFrame
from .procbridge import LineJsonBridge, serve

log = get_logger("core.j2534.bridge")

#: Extra seconds allowed on top of a blocking ``recv`` before the parent
#: declares the child wedged. The child returns as soon as its own timeout
#: expires, so this only covers spawn/serialisation jitter.
_REPLY_MARGIN = 5.0


class _Bridge(LineJsonBridge):
    """The helper process wrapper: J2534 flavoured error messages."""

    label = "J2534 bridge"
    error_type = TransportError

    def __init__(self, library: str, *, baudrate: int, extended: bool,
                 loopback: bool, python32: Optional[str] = None,
                 timeout: float = 10.0) -> None:
        super().__init__(python32=python32, timeout=timeout)
        self.library = library
        self.baudrate = baudrate
        self.extended = extended
        self.loopback = loopback

    def _child_argv(self) -> List[str]:
        argv = [self.python32]
        argv += self._module_warning_filter("med17flasher.core.j2534_bridge")
        argv += ["-m", "med17flasher.core.j2534_bridge", "--library", self.library,
                 "--baudrate", str(self.baudrate)]
        if self.extended:
            argv.append("--extended")
        if self.loopback:
            argv.append("--loopback")
        return argv


class J2534BridgeBus(CanBus):
    """A :class:`CanBus` backed by a 32-bit J2534 driver in a helper process.

    The constructor signature mirrors :class:`~med17flasher.core.j2534.J2534Bus`
    so :func:`~med17flasher.core.j2534.open_j2534` can swap one for the other.
    """

    def __init__(
        self,
        library: str = "",
        *,
        device: str = "",
        baudrate: int = 500000,
        extended: bool = False,
        loopback: bool = False,
        python32: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        # Resolve the registry entry here, in the 64-bit parent: both registry
        # views are read, so the child can be handed a concrete DLL path and
        # never has to repeat the lookup.
        from .j2534 import find_device

        self.device = find_device(library or device)
        self.library = self.device.library
        self.baudrate = int(baudrate)
        self.extended = bool(extended)
        self.name = f"j2534-bridge:{self.device.name}"

        self._rx: List[CanFrame] = []
        self._closed = False
        self._bridge = _Bridge(self.library, baudrate=self.baudrate,
                               extended=self.extended, loopback=loopback,
                               python32=python32, timeout=timeout)
        # Fail during construction, not on the first frame of a flash: a driver
        # that cannot open must surface before anyone touches the ECU.
        info = self._bridge.request({"cmd": "info"})
        self.info: Dict[str, Any] = info.get("info") or {}
        log.info("J2534 via 32-bit bridge: %s", self.info)

    # ------------------------------------------------------------------ #
    def send(self, frame: CanFrame, timeout: float = 1.0) -> None:
        if self._closed:
            raise TransportError("send on a closed J2534 bus")
        self._bridge.request({
            "cmd": "send",
            "id": frame.arbitration_id,
            "data": frame.data.hex(),
            "ext": bool(frame.is_extended_id),
            "timeout": float(timeout),
        })

    def recv(self, timeout: float = 1.0) -> Optional[CanFrame]:
        if self._closed:
            return None
        if self._rx:
            return self._rx.pop(0)
        reply = self._bridge.request(
            {"cmd": "recv", "timeout": float(timeout)},
            timeout=float(timeout) + _REPLY_MARGIN,
        )
        frames = [
            CanFrame(int(item[0]), bytes.fromhex(item[1]), bool(item[2]),
                     timestamp=float(item[3]))
            for item in reply.get("frames") or []
        ]
        if not frames:
            return None
        self._rx = frames[1:]
        return frames[0]

    def flush_rx(self) -> None:
        self._rx.clear()
        if not self._closed:
            self._bridge.request({"cmd": "flush"})

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bridge.close()

    # ------------------------------------------------------------------ #
    def battery_voltage(self) -> Optional[float]:
        """Battery voltage reported by the interface, or ``None``."""

        value = self.info.get("battery")
        return float(value) if isinstance(value, (int, float)) else None


# --------------------------------------------------------------------------- #
# Child side (``py -3-32 -m med17flasher.core.j2534_bridge``)
# --------------------------------------------------------------------------- #
def _handle(bus, request: Dict[str, Any]) -> Dict[str, Any]:
    cmd = request.get("cmd")
    if cmd == "recv":
        timeout = float(request.get("timeout", 1.0))
        frames = []
        # Drain the batch the driver already buffered, so one reply can carry
        # several frames. Only the first read may block.
        frame = bus.recv(timeout=timeout)
        while frame is not None:
            frames.append([frame.arbitration_id, frame.data.hex(),
                           bool(frame.is_extended_id), frame.timestamp])
            frame = bus.recv(timeout=0.0)
        return {"ok": True, "frames": frames}
    if cmd == "send":
        bus.send(
            CanFrame(int(request["id"]), bytes.fromhex(str(request.get("data", ""))),
                     bool(request.get("ext", False))),
            timeout=float(request.get("timeout", 1.0)),
        )
        return {"ok": True}
    if cmd == "flush":
        bus.flush_rx()
        return {"ok": True}
    if cmd == "info":
        info: Dict[str, Any] = {
            "device": bus.device.name,
            "library": bus.library,
            "baudrate": bus.baudrate,
            "extended": bus.extended,
            "python": sys.executable,
            # 32 or 64 - proves which interpreter actually loaded the DLL.
            "bits": 8 * __import__("struct").calcsize("P"),
        }
        info.update(bus.read_version())
        battery = bus.battery_voltage()
        if battery is not None:
            info["battery"] = battery
        return {"ok": True, "info": info}
    if cmd == "ping":
        return {"ok": True}
    return {"ok": False, "error": f"unknown command: {cmd!r}"}


def _worker_main(argv: List[str]) -> int:
    """The helper process: open the J2534 bus, answer JSON requests on stdin."""

    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m med17flasher.core.j2534_bridge",
        description="32-bit J2534 PassThru worker (line-based JSON on stdin/stdout)",
    )
    parser.add_argument("--library", required=True, help="path to the PassThru DLL")
    parser.add_argument("--baudrate", type=int, default=500000)
    parser.add_argument("--extended", action="store_true", help="enable 29-bit ids")
    parser.add_argument("--loopback", action="store_true", help="keep the tx echo")
    args = parser.parse_args(argv)

    from .j2534 import J2534Bus  # reuse the one ctypes implementation

    try:
        # Fail fast and loudly: a wrong-bitness or missing DLL should surface as
        # the loader's own message on stderr (which the parent reports) rather
        # than as an error on every single frame.
        bus = J2534Bus(args.library, baudrate=args.baudrate,
                       extended=args.extended, loopback=args.loopback)
    except Exception as exc:  # noqa: BLE001 - anything here is fatal
        sys.stderr.write(f"{exc}\n")
        sys.stderr.flush()
        return 2

    return serve(lambda request: _handle(bus, request), on_quit=bus.close)


if __name__ == "__main__":
    sys.exit(_worker_main(sys.argv[1:]))
