# XCP measurement / logging

`med17flasher` includes a small, dependency-free **XCP master** (ASAM MCD-1 XCP)
for reading **live values** from a MED17-class ECU over CAN — engine speed,
coolant temperature, boost, lambda, whatever your A2L exposes — and logging them
to CSV. It supports both simple **polling** (read each variable on a timer) and
real **DAQ** (the slave streams the values).

> ⚠️ Measurement is read-only, but you still need the ECU's XCP CAN ids and the
> memory addresses of the variables (from its A2L). Only work on ECUs you are
> authorised to access.

## Quick start (no hardware)

```bash
# A built-in virtual XCP slave with a wandering RPM, coolant and battery:
med17flasher xcp --simulator --samples 20
med17flasher xcp --simulator --daq --csv run.csv        # real DAQ streaming
```

## Measuring a real ECU

```bash
med17flasher xcp \
    --backend socketcan:can0 \       # or pcan:PCAN_USBBUS1, slcan:/dev/ttyUSB0, ...
    --cro 0x7E0 --dto 0x7E1 \        # XCP command / data CAN ids
    --signal rpm@0x80005000:u16:0.25 \
    --signal coolant@0xD0001234:s16:0.1:-40:degC \
    --signal boost@0x80005010:u16:0.001:0:bar \
    --rate 20 \                      # 20 Hz polling
    --csv drive.csv
# or stream via DAQ instead of polling:
med17flasher xcp --backend socketcan:can0 --daq --signals-file signals.txt --csv drive.csv
```

### From an A2L (auto-config)

If you have the ECU's A2L, let it fill in the CAN ids and the signals:

```bash
# CRO/DTO come from the A2L's IF_DATA XCP_ON_CAN; --find picks the measurements:
med17flasher xcp --backend socketcan:can0 --a2l ecu.a2l --find "n*ot" --csv drive.csv
# inspect what an A2L exposes (transport, DAQ events, measurements):
med17flasher a2l ecu.a2l --find rpm
```

29-bit (extended) CAN ids in the A2L are detected automatically. See
[`REFERENCES.md`](REFERENCES.md) for the `IF_DATA` layout.

### Signal spec

`NAME@ADDRESS:TYPE[:factor[:offset[:unit]]]` — physical value = `raw * factor + offset`.

* **TYPE**: `u8 s8 u16 s16 u32 s32 f32 f64`
* Addresses and integers accept `0x…` hex.
* `--signals-file` reads one spec per line (`#` comments allowed).

## Library API

```python
from med17flasher.core import create_bus
from med17flasher.xcp import XcpOnCan, XcpClient, Signal, PollingMeasurement

bus = create_bus("socketcan:can0")
client = XcpClient(XcpOnCan(bus, cro_id=0x7E0, dto_id=0x7E1))
client.connect()
sigs = [Signal("rpm", 0x80005000, "u16", factor=0.25)]
for sample in PollingMeasurement(client, sigs).run(rate_hz=20, duration=10):
    print(sample.t, sample.values)
client.disconnect()
```

Real DAQ:

```python
from med17flasher.xcp import configure_daq, DaqMeasurement
layout = configure_daq(client, sigs, event=0)
DaqMeasurement(client, layout).run(duration=10, csv_path="run.csv")
```

## What's implemented

* Transport: XCP-on-CAN (one packet per frame, CRO/DTO ids, EV/SERV skipping).
* Client: `CONNECT` / `DISCONNECT` / `GET_STATUS`, byte-order detection,
  `SET_MTA` + `UPLOAD`, `SHORT_UPLOAD`, and the DAQ set (`FREE_DAQ`, `ALLOC_*`,
  `SET_DAQ_PTR`, `WRITE_DAQ`, `SET_DAQ_LIST_MODE`, `START_STOP_DAQ_LIST`,
  `START_STOP_SYNCH`, `GET_DAQ_PROCESSOR_INFO`).
* Measurement: polling and DAQ, value decoding (int/float, factor/offset),
  CSV logging.
* A `VirtualXcpSlave` (in-process) so the whole path is testable without
  hardware — see `tests/test_xcp.py`.

Not implemented: STIM (stimulation), block-mode UPLOAD, resume mode, seed/key
unlock for XCP (`GET_SEED`/`UNLOCK` are defined but calibration write is out of
scope here).
