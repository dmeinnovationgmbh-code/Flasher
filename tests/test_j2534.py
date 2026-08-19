"""J2534 PassThru backend tests, against a compiled mock driver.

A J2534 driver is a plain C shared library, so the whole ctypes layer - struct
layout, the 4-byte big-endian CAN id inside ``Data``, filters, batched reads,
teardown order - can be exercised for real on Linux by compiling a mock
``.so``. Only the Windows registry discovery and the 32-bit-ness itself cannot
be faked here.

The mock models an ECU: every frame written comes back twice - once as the
device's own transmit echo (which the backend must drop) and once as a reply
from ``id + 8``, the usual UDS response offset.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from med17flasher.core.can_backends import CanFrame, create_bus, list_j2534_devices
from med17flasher.core.j2534 import (
    CAN_29BIT_ID,
    PASSTHRU_MSG,
    J2534Bus,
    J2534Device,
    _is_bitness_error,
    available,
    find_device,
    list_devices,
)
from med17flasher.exceptions import BackendNotAvailableError, TransportError

# ``unsigned long`` is 32 bits in the Win32 API this mirrors, so the mock spells
# every field uint32_t - matching our ctypes struct on every platform.
MOCK_C = r"""
#include <stdint.h>
#include <string.h>

#define DATA_MAX 4128
#define RING 256
#define NOERROR_          0x00
#define ERR_FAILED        0x07
#define ERR_BUFFER_EMPTY  0x10
#define RX_TX_MSG_TYPE    0x01
#define CAN_29BIT         0x100

typedef struct {
  uint32_t ProtocolID, RxStatus, TxFlags, Timestamp, DataSize, ExtraDataIndex;
  unsigned char Data[DATA_MAX];
} PASSTHRU_MSG;

typedef struct { uint32_t Parameter, Value; } SCONFIG;
typedef struct { uint32_t NumOfParams; SCONFIG* ConfigPtr; } SCONFIG_LIST;

static PASSTHRU_MSG ring[RING];
static int ring_n = 0;
static int open_count = 0, close_count = 0, disconnect_count = 0;
static int filter_count = 0, stop_filter_count = 0;
static int connect_fails = 0;
static uint32_t last_protocol = 0, last_flags = 0, last_baudrate = 0;
static uint32_t last_config_param = 0, last_config_value = 0;

/* --- test hooks (not part of J2534) --- */
int32_t MockOpenCount(void){ return open_count; }
int32_t MockCloseCount(void){ return close_count; }
int32_t MockDisconnectCount(void){ return disconnect_count; }
int32_t MockFilterCount(void){ return filter_count; }
int32_t MockStopFilterCount(void){ return stop_filter_count; }
int32_t MockPending(void){ return ring_n; }
uint32_t MockLastProtocol(void){ return last_protocol; }
uint32_t MockLastFlags(void){ return last_flags; }
uint32_t MockLastBaudrate(void){ return last_baudrate; }
uint32_t MockLastConfigParam(void){ return last_config_param; }
uint32_t MockLastConfigValue(void){ return last_config_value; }
void MockSetConnectFails(int v){ connect_fails = v; }
void MockReset(void){ ring_n=0; open_count=0; close_count=0; disconnect_count=0;
                      filter_count=0; stop_filter_count=0; connect_fails=0; }

/* Pump mode: instead of echoing, queue transmitted frames for Python to relay
   onto a virtual CAN network, so the real UDS stack can talk to the simulator
   through this driver. */
static int pump_mode = 0;
static PASSTHRU_MSG txq[RING];
static int tx_n = 0;
void MockSetPumpMode(int v){ pump_mode = v; tx_n = 0; }

static void push(uint32_t rxstatus, const unsigned char* data, uint32_t size);

/* Python -> driver receive queue. */
void MockPushRx(uint32_t id, const unsigned char* data, uint32_t len, uint32_t ext){
  unsigned char buf[DATA_MAX];
  buf[0]=(id>>24)&0xFF; buf[1]=(id>>16)&0xFF; buf[2]=(id>>8)&0xFF; buf[3]=id&0xFF;
  for (uint32_t i=0;i<len && i<8;i++) buf[4+i]=data[i];
  push(ext ? CAN_29BIT : 0, buf, 4+len);
}

/* driver -> Python transmit queue; returns payload length or -1 when empty. */
int32_t MockPopTx(uint32_t* id, unsigned char* out){
  if (tx_n <= 0) return -1;
  PASSTHRU_MSG* m = &txq[0];
  *id = ((uint32_t)m->Data[0]<<24)|((uint32_t)m->Data[1]<<16)
      | ((uint32_t)m->Data[2]<<8)|(uint32_t)m->Data[3];
  uint32_t n = m->DataSize > 4 ? m->DataSize - 4 : 0;
  if (n > 8) n = 8;
  for (uint32_t i=0;i<n;i++) out[i] = m->Data[4+i];
  for (int i=1;i<tx_n;i++) txq[i-1] = txq[i];
  tx_n--;
  return (int32_t)n;
}

static void push(uint32_t rxstatus, const unsigned char* data, uint32_t size){
  if (ring_n >= RING) return;
  PASSTHRU_MSG* m = &ring[ring_n++];
  memset(m, 0, sizeof(*m));
  m->ProtocolID = 5; m->RxStatus = rxstatus; m->DataSize = size;
  m->Timestamp = 1000000;                     /* 1.0 s, in microseconds */
  memcpy(m->Data, data, size < DATA_MAX ? size : DATA_MAX);
}

int32_t PassThruOpen(void* name, uint32_t* pDeviceID){
  (void)name; open_count++; *pDeviceID = 42; return NOERROR_;
}
int32_t PassThruClose(uint32_t dev){ (void)dev; close_count++; return NOERROR_; }

int32_t PassThruConnect(uint32_t dev, uint32_t protocol, uint32_t flags,
                        uint32_t baud, uint32_t* pChannelID){
  (void)dev;
  last_protocol = protocol; last_flags = flags; last_baudrate = baud;
  if (connect_fails) return ERR_FAILED;
  *pChannelID = 7; return NOERROR_;
}
int32_t PassThruDisconnect(uint32_t ch){ (void)ch; disconnect_count++; return NOERROR_; }

int32_t PassThruWriteMsgs(uint32_t ch, PASSTHRU_MSG* msgs, uint32_t* pNum, uint32_t t){
  (void)ch; (void)t;
  for (uint32_t i = 0; i < *pNum; i++){
    PASSTHRU_MSG* m = &msgs[i];
    if (pump_mode){ if (tx_n < RING) txq[tx_n++] = *m; continue; }
    /* 1) the device's own transmit echo - the backend must drop this */
    push(RX_TX_MSG_TYPE | (m->TxFlags & CAN_29BIT), m->Data, m->DataSize);
    /* 2) an "ECU reply" from id + 8, carrying the payload reversed */
    unsigned char reply[DATA_MAX];
    uint32_t id = ((uint32_t)m->Data[0]<<24)|((uint32_t)m->Data[1]<<16)
                | ((uint32_t)m->Data[2]<<8)|(uint32_t)m->Data[3];
    id += 8;
    reply[0]=(id>>24)&0xFF; reply[1]=(id>>16)&0xFF;
    reply[2]=(id>>8)&0xFF;  reply[3]=id&0xFF;
    uint32_t n = m->DataSize > 4 ? m->DataSize - 4 : 0;
    for (uint32_t k = 0; k < n; k++) reply[4+k] = m->Data[4 + (n-1-k)];
    push(m->TxFlags & CAN_29BIT, reply, 4 + n);
  }
  return NOERROR_;
}

int32_t PassThruReadMsgs(uint32_t ch, PASSTHRU_MSG* msgs, uint32_t* pNum, uint32_t t){
  (void)ch; (void)t;
  uint32_t want = *pNum, got = 0;
  while (got < want && got < (uint32_t)ring_n) { msgs[got] = ring[got]; got++; }
  for (int i = (int)got; i < ring_n; i++) ring[i - got] = ring[i];
  ring_n -= (int)got;
  *pNum = got;
  return got ? NOERROR_ : ERR_BUFFER_EMPTY;
}

int32_t PassThruStartMsgFilter(uint32_t ch, uint32_t type, PASSTHRU_MSG* mask,
                               PASSTHRU_MSG* pattern, PASSTHRU_MSG* flow,
                               uint32_t* pFilterID){
  (void)ch; (void)type; (void)mask; (void)pattern; (void)flow;
  *pFilterID = (uint32_t)(++filter_count); return NOERROR_;
}
int32_t PassThruStopMsgFilter(uint32_t ch, uint32_t id){
  (void)ch; (void)id; stop_filter_count++; return NOERROR_;
}

int32_t PassThruIoctl(uint32_t ch, uint32_t id, void* in, void* out){
  (void)ch;
  if (id == 0x08 || id == 0x07) { ring_n = 0; return NOERROR_; }   /* CLEAR_*_BUFFER */
  if (id == 0x03) { *(uint32_t*)out = 12400; return NOERROR_; }    /* READ_VBATT */
  if (id == 0x02 && in) {                                          /* SET_CONFIG */
    SCONFIG_LIST* l = (SCONFIG_LIST*)in;
    if (l->NumOfParams > 0) {
      last_config_param = l->ConfigPtr[0].Parameter;
      last_config_value = l->ConfigPtr[0].Value;
    }
    return NOERROR_;
  }
  return NOERROR_;
}

int32_t PassThruReadVersion(uint32_t dev, char* fw, char* dll, char* api){
  (void)dev; strcpy(fw,"1.2.3"); strcpy(dll,"MOCK-DLL"); strcpy(api,"04.04");
  return NOERROR_;
}
int32_t PassThruGetLastError(char* msg){ strcpy(msg,"mock failure"); return NOERROR_; }
"""

HAS_CC = bool(shutil.which("gcc") or shutil.which("cc"))
needs_cc = pytest.mark.skipif(not HAS_CC, reason="no C compiler for the mock J2534 driver")


@pytest.fixture(scope="module")
def mock_driver():
    cc = shutil.which("gcc") or shutil.which("cc")
    if not cc:
        pytest.skip("no C compiler to build the mock J2534 driver")
    d = tempfile.mkdtemp()
    src = os.path.join(d, "mock_j2534.c")
    so = os.path.join(d, "mock_j2534.so")
    with open(src, "w") as fh:
        fh.write(MOCK_C)
    subprocess.run([cc, "-shared", "-fPIC", "-o", so, src], check=True)
    yield so
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def bus(mock_driver):
    ctypes.CDLL(mock_driver).MockReset()
    b = J2534Bus(mock_driver)
    yield b
    b.close()


def _hooks(bus_or_path):
    path = bus_or_path if isinstance(bus_or_path, str) else bus_or_path.library
    return ctypes.CDLL(path)


# --------------------------------------------------------------------------- #
# Struct layout - a single wrong field shifts every byte the driver sees
# --------------------------------------------------------------------------- #
def test_passthru_msg_layout_matches_the_standard():
    assert ctypes.sizeof(PASSTHRU_MSG) == 6 * 4 + 4128
    assert PASSTHRU_MSG.ProtocolID.offset == 0
    assert PASSTHRU_MSG.DataSize.offset == 16
    assert PASSTHRU_MSG.Data.offset == 24
    # Win32 ``unsigned long`` is 32 bits; c_ulong would be 64 on Linux and
    # silently shift everything after the first field.
    assert PASSTHRU_MSG.RxStatus.size == 4


# --------------------------------------------------------------------------- #
# Open / connect
# --------------------------------------------------------------------------- #
@needs_cc
def test_open_connects_as_raw_can_and_installs_a_pass_filter(bus):
    hooks = _hooks(bus)
    assert hooks.MockLastProtocol() == 5          # PROTOCOL_CAN, not ISO15765
    assert hooks.MockLastBaudrate() == 500000
    assert hooks.MockLastFlags() == 0             # 11-bit by default
    # Without a pass filter a J2534 channel receives absolutely nothing.
    assert hooks.MockFilterCount() == 1
    assert hooks.MockOpenCount() == 1


@needs_cc
def test_extended_mode_sets_the_29bit_flag_and_two_filters(mock_driver):
    _hooks(mock_driver).MockReset()
    b = J2534Bus(mock_driver, extended=True)
    try:
        assert _hooks(mock_driver).MockLastFlags() == CAN_29BIT_ID
        # One filter for 11-bit, one for 29-bit, so both are received.
        assert _hooks(mock_driver).MockFilterCount() == 2
    finally:
        b.close()


@needs_cc
def test_loopback_is_disabled_by_default(mock_driver):
    _hooks(mock_driver).MockReset()
    b = J2534Bus(mock_driver)
    try:
        hooks = _hooks(mock_driver)
        assert hooks.MockLastConfigParam() == 0x03   # CFG_LOOPBACK
        assert hooks.MockLastConfigValue() == 0
    finally:
        b.close()


@needs_cc
def test_failed_connect_still_closes_the_device(mock_driver):
    """A leaked handle locks the interface until the driver is unloaded."""

    hooks = _hooks(mock_driver)
    hooks.MockReset()
    hooks.MockSetConnectFails(1)
    try:
        with pytest.raises(TransportError) as exc:
            J2534Bus(mock_driver)
        assert "ERR_FAILED" in str(exc.value)
        assert "mock failure" in str(exc.value)      # GetLastError text is surfaced
        assert hooks.MockOpenCount() == 1
        assert hooks.MockCloseCount() == 1           # ... and released again
    finally:
        hooks.MockSetConnectFails(0)


# --------------------------------------------------------------------------- #
# Frame encoding - the classic J2534 mistake is the id inside Data
# --------------------------------------------------------------------------- #
@needs_cc
def test_send_encodes_the_id_as_four_big_endian_bytes(bus):
    bus.send(CanFrame(0x7E0, bytes([0x02, 0x10, 0x03])))
    frame = bus.recv(timeout=0.1)
    assert frame is not None
    # The transmit echo must have been dropped; this is the "ECU" reply.
    assert frame.arbitration_id == 0x7E8
    assert frame.data == bytes([0x03, 0x10, 0x02])   # mock reverses the payload
    assert bus.recv(timeout=0.05) is None


@needs_cc
def test_recv_batches_frames_into_one_driver_call(bus):
    for i in range(5):
        bus.send(CanFrame(0x700 + i, bytes([i])))
    got = []
    for _ in range(5):
        frame = bus.recv(timeout=0.1)
        assert frame is not None
        got.append(frame.arbitration_id)
    assert got == [0x708, 0x709, 0x70A, 0x70B, 0x70C]
    assert bus.recv(timeout=0.05) is None


@needs_cc
def test_timestamp_is_converted_from_microseconds(bus):
    bus.send(CanFrame(0x7E0, b"\x01"))
    frame = bus.recv(timeout=0.1)
    assert frame is not None
    assert frame.timestamp == pytest.approx(1.0)


@needs_cc
def test_extended_frames_round_trip(mock_driver):
    _hooks(mock_driver).MockReset()
    b = J2534Bus(mock_driver, extended=True)
    try:
        b.send(CanFrame(0x18DAF110, bytes([0xAA, 0xBB]), is_extended_id=True))
        frame = b.recv(timeout=0.1)
        assert frame is not None
        assert frame.is_extended_id
        assert frame.arbitration_id == 0x18DAF118
        assert frame.data == bytes([0xBB, 0xAA])
    finally:
        b.close()


@needs_cc
def test_sending_29bit_on_an_11bit_channel_is_refused(bus):
    with pytest.raises(TransportError) as exc:
        bus.send(CanFrame(0x18DAF110, b"\x01", is_extended_id=True))
    assert "extended=True" in str(exc.value)


@needs_cc
def test_flush_rx_drops_buffered_and_driver_side_frames(bus):
    for i in range(3):
        bus.send(CanFrame(0x700 + i, bytes([i])))
    bus.recv(timeout=0.1)              # pulls a batch into our own buffer
    bus.flush_rx()
    assert _hooks(bus).MockPending() == 0
    assert bus.recv(timeout=0.05) is None


# --------------------------------------------------------------------------- #
# Introspection
# --------------------------------------------------------------------------- #
@needs_cc
def test_read_version_and_battery(bus):
    assert bus.read_version() == {"firmware": "1.2.3", "dll": "MOCK-DLL",
                                  "api": "04.04"}
    assert bus.battery_voltage() == pytest.approx(12.4)


@needs_cc
def test_close_releases_filter_channel_and_device(mock_driver):
    hooks = _hooks(mock_driver)
    hooks.MockReset()
    b = J2534Bus(mock_driver)
    b.close()
    assert hooks.MockStopFilterCount() == 1
    assert hooks.MockDisconnectCount() == 1
    assert hooks.MockCloseCount() == 1
    b.close()                          # idempotent: no double release
    assert hooks.MockCloseCount() == 1


@needs_cc
def test_create_bus_routes_j2534_specs(mock_driver):
    _hooks(mock_driver).MockReset()
    b = create_bus(f"j2534:{mock_driver}")
    try:
        assert isinstance(b, J2534Bus)
        b.send(CanFrame(0x7E0, b"\x01"))
        assert b.recv(timeout=0.1).arbitration_id == 0x7E8
    finally:
        b.close()


# --------------------------------------------------------------------------- #
# Driver validation
# --------------------------------------------------------------------------- #
def test_a_non_passthru_library_is_rejected(tmp_path):
    cc = shutil.which("gcc") or shutil.which("cc")
    if not cc:
        pytest.skip("no C compiler")
    src = tmp_path / "not_j2534.c"
    src.write_text("int unrelated(void){return 1;}\n")
    so = tmp_path / "not_j2534.so"
    subprocess.run([cc, "-shared", "-fPIC", "-o", str(so), str(src)], check=True)
    with pytest.raises(BackendNotAvailableError) as exc:
        J2534Bus(str(so))
    assert "not a J2534 PassThru driver" in str(exc.value)
    assert "PassThruOpen" in str(exc.value)


def test_missing_driver_is_reported_clearly():
    with pytest.raises(BackendNotAvailableError) as exc:
        J2534Bus("/nonexistent/op20pt32.dll")
    assert "not found" in str(exc.value)


def test_bitness_error_detection():
    """A 64-bit process loading op20pt32.dll must route to the bridge."""

    assert _is_bitness_error(OSError("[WinError 193] %1 is not a valid Win32 application"))
    assert _is_bitness_error(OSError("wrong ELF class: ELFCLASS32"))
    assert not _is_bitness_error(OSError("file not found"))


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def test_discovery_is_empty_and_harmless_off_windows():
    if sys.platform == "win32":
        pytest.skip("this asserts the non-Windows behaviour")
    assert list_devices() == []
    assert list_j2534_devices() == []
    assert available() is False


def test_find_device_explains_how_to_get_a_driver():
    if sys.platform == "win32":
        pytest.skip("depends on what is installed")
    with pytest.raises(BackendNotAvailableError) as exc:
        find_device("tactrix")
    assert "no J2534 PassThru device is installed" in str(exc.value)


def test_find_device_accepts_an_explicit_path(mock_driver):
    device = find_device(mock_driver)
    assert device.library == mock_driver
    assert device.exists


def test_device_as_dict_reports_installed_state():
    device = J2534Device(name="Openport 2.0", vendor="Tactrix",
                         library="/nope/op20pt32.dll", protocols=["CAN", "ISO15765"])
    assert device.supports_can
    assert device.as_dict()["installed"] is False


# --------------------------------------------------------------------------- #
# The 32-bit bridge (exercised end to end with this interpreter as "python32")
# --------------------------------------------------------------------------- #
@needs_cc
def test_bridge_bus_round_trips_frames(mock_driver):
    """The whole out-of-process path: spawn, open the driver, send, receive."""

    from med17flasher.core.j2534_bridge import J2534BridgeBus

    _hooks(mock_driver).MockReset()
    bus = J2534BridgeBus(mock_driver, python32=sys.executable, timeout=30.0)
    try:
        assert bus.info["library"] == mock_driver
        assert bus.info["firmware"] == "1.2.3"
        assert bus.info["bits"] in (32, 64)      # proves which python loaded it
        assert bus.battery_voltage() == pytest.approx(12.4)

        bus.send(CanFrame(0x7E0, bytes([0x02, 0x10, 0x03])))
        frame = bus.recv(timeout=1.0)
        assert frame is not None
        assert frame.arbitration_id == 0x7E8
        assert frame.data == bytes([0x03, 0x10, 0x02])
        assert frame.timestamp == pytest.approx(1.0)
    finally:
        bus.close()


@needs_cc
def test_bridge_bus_batches_a_burst_into_one_reply(mock_driver):
    from med17flasher.core.j2534_bridge import J2534BridgeBus

    _hooks(mock_driver).MockReset()
    bus = J2534BridgeBus(mock_driver, python32=sys.executable, timeout=30.0)
    try:
        for i in range(5):
            bus.send(CanFrame(0x700 + i, bytes([i])))
        got = [bus.recv(timeout=1.0) for _ in range(5)]
        assert [f.arbitration_id for f in got] == [0x708, 0x709, 0x70A, 0x70B, 0x70C]
        # A whole burst arrives in one round trip and is handed out from the
        # parent's own buffer.
        assert bus.recv(timeout=0.2) is None
    finally:
        bus.close()


@needs_cc
def test_bridge_bus_reports_a_driver_that_cannot_open(tmp_path):
    from med17flasher.core.j2534_bridge import J2534BridgeBus

    cc = shutil.which("gcc") or shutil.which("cc")
    src = tmp_path / "bad.c"
    src.write_text("int unrelated(void){return 1;}\n")
    so = tmp_path / "bad.so"
    subprocess.run([cc, "-shared", "-fPIC", "-o", str(so), str(src)], check=True)
    with pytest.raises(TransportError) as exc:
        J2534BridgeBus(str(so), python32=sys.executable, timeout=30.0)
    # The child's own diagnosis has to reach the user, not just "it died".
    assert "not a J2534 PassThru driver" in str(exc.value)


def test_bridge_worker_is_importable_by_a_bare_interpreter():
    """The helper runs under a *separate* Python, so its imports must resolve.

    A frozen build ships a plain-source copy of the package for exactly this;
    here the checkout serves the same role.
    """

    from med17flasher.core.procbridge import bridge_source_root

    root = bridge_source_root()
    assert root and os.path.isfile(os.path.join(root, "med17flasher", "__init__.py"))
    env = dict(os.environ, PYTHONPATH=root)
    proc = subprocess.run(
        [sys.executable, "-c",
         "import med17flasher.core.j2534_bridge as m; print(m._worker_main)"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------------------- #
# The real thing: a full UDS flash carried over the J2534 backend
# --------------------------------------------------------------------------- #
class _Pump(threading.Thread):
    """Relay frames between the mock driver and a virtual CAN network.

    The driver's transmit queue is drained onto the network and everything the
    network carries is pushed into the driver's receive queue, so the simulator
    on the other side sees a perfectly ordinary CAN bus. The bus lock is held
    around each driver call because the C mock has no locking of its own.
    """

    def __init__(self, bus, hooks, endpoint):
        super().__init__(daemon=True, name="j2534-pump")
        self.bus, self.hooks, self.endpoint = bus, hooks, endpoint
        # NB: not ``_stop`` - threading.Thread already uses that name.
        self._stopping = threading.Event()
        self._id = ctypes.c_uint32(0)
        self._buf = (ctypes.c_ubyte * 8)()

    def run(self):
        while not self._stopping.is_set():
            moved = False
            with self.bus._lock:
                while True:
                    n = self.hooks.MockPopTx(ctypes.byref(self._id), self._buf)
                    if n < 0:
                        break
                    self.endpoint.send(CanFrame(self._id.value,
                                                bytes(self._buf[:n])))
                    moved = True
            frame = self.endpoint.recv(timeout=0.002)
            if frame is not None:
                data = (ctypes.c_ubyte * len(frame.data))(*frame.data)
                with self.bus._lock:
                    self.hooks.MockPushRx(frame.arbitration_id, data,
                                          len(frame.data), 0)
                moved = True
            if not moved:
                time.sleep(0.001)

    def stop(self):
        self._stopping.set()
        self.join(timeout=5.0)


@needs_cc
def test_full_uds_flash_over_the_j2534_backend(mock_driver, network, simulator,
                                               demo_profile):
    """Proof the backend satisfies the contract the flasher actually needs.

    Everything above the driver is the production code path: ISO-TP
    segmentation with flow control, UDS with 0x78 handling, security access,
    erase, block transfer and CRC verification - all of it riding on
    :class:`J2534Bus`.
    """

    from med17flasher.core import IsoTpConfig, IsoTpLayer, UdsClient, UdsTiming
    from med17flasher.core.firmware import FirmwareImage
    from med17flasher.core.flash_sequence import Flasher, ProfileSeedKey, Stage

    hooks = _hooks(mock_driver)
    hooks.MockReset()
    hooks.MockSetPumpMode(1)
    bus = J2534Bus(mock_driver)
    pump = _Pump(bus, hooks, network.new_endpoint("pump"))
    pump.start()
    try:
        tp = IsoTpLayer(bus, IsoTpConfig(
            tx_id=demo_profile.can.tx_id, rx_id=demo_profile.can.rx_id,
            padding_byte=demo_profile.can.padding_byte))
        uds = UdsClient(tp, UdsTiming(p2=2.0, p2_star=4.0))

        img = FirmwareImage()
        img.add_segment(0x80040000, bytes((i * 7) & 0xFF for i in range(0x2000)))
        img.add_segment(0x80042000, bytes((0xC0 + (i & 0x1F)) & 0xFF
                                          for i in range(0x1000)))
        img = img.normalise()

        stages = []
        flasher = Flasher(uds, demo_profile, ProfileSeedKey(demo_profile),
                          progress=lambda p: stages.append(p.stage))
        result = flasher.flash(img)

        assert result.success
        assert result.blocks == ["ASW", "CAL"]
        # The bytes really landed in the ECU, through the PassThru encoding.
        assert simulator.read_memory(0x80040000, 0x2000) == img.read(0x80040000, 0x2000)
        assert simulator.read_memory(0x80042000, 0x1000) == img.read(0x80042000, 0x1000)
        assert len(simulator.erased_regions) == 2
        assert len(simulator.verified_regions) == 2
        for stage in (Stage.SECURITY_ACCESS, Stage.ERASE, Stage.TRANSFER,
                      Stage.VERIFY, Stage.DONE):
            assert stage in stages
    finally:
        pump.stop()
        bus.close()
        hooks.MockSetPumpMode(0)
