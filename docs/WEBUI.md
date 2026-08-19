# Web UI

`webui/` is the DME **MED17 Flash Tool** design, implemented as a React (Vite)
app and served by the Python backend (`med17flasher webserver`). The flash view
is wired to the **real** flashing engine; the OTS-Maps purchase flow is
simulated exactly as in the design.

## Running

```bash
med17flasher webserver --open              # serves webui/dist on :8090
# dev with hot reload (Vite proxies /api -> :8090):
cd webui && npm run dev
```

The Python server serves the prebuilt `webui/dist`. After editing the UI, run
`cd webui && npm run build` to regenerate it (CI also builds it on every push).

## Backend API

| Method & path            | Purpose                                            |
|--------------------------|----------------------------------------------------|
| `GET  /api/vehicle`      | vehicle + ECU + PFLASH sector metadata             |
| `GET  /api/maps`         | OTS maps (with per-VIN unlocked state)             |
| `GET  /api/telemetry`    | live board voltage / speed                         |
| `GET  /api/identify`     | identification DIDs (live read from the ECU)       |
| `POST /api/flash`        | start a flash → `{started: bool}`                  |
| `POST /api/flash/abort`  | abort the running flash                            |
| `GET  /api/flash/stream` | **SSE**: `progress` / `log` / `done` / `error`     |
| `POST /api/maps/<id>/buy`| simulate a purchase → `{unlocked: true}`           |

### SSE `progress` event

```jsonc
{
  "type": "progress",
  "pct": 42.7,
  "stage": "transfer",
  "block": "ASW",
  "sectors": [{"name": "SBOOT · 32K", "flex": 1, "fill": 100, "writing": false, "done": true}, ...],
  "address": 2148155264,      // current absolute flash address
  "bytesDone": 895000, "bytesTotal": 2097152,
  "speed": 182,               // KB/s
  "eta": 44,                  // seconds
  "volt": 13.8,
  "running": true
}
```

`log` events are `{type, cls, msg}` (`cls` ∈ `"" | ok | accent | err`), and the
stream ends with a `done` (or `error`) event.

## Pointing the flash view at real hardware

By default the backend flashes the in-process simulator (the design's C63
profile, `config/med17_7_5_c63.yaml`). To flash a real ECU instead:

* pass your verified profile: `med17flasher webserver --profile my_ecu.yaml`;
* in `med17flasher/webserver/service.py`, `FlashService` builds a
  `VirtualCanNetwork` + `VirtualEcu`. Swap `_make_uds` to open a real bus
  (`create_bus("socketcan:can0")`) and drive a real firmware image instead of
  the generated demo pattern. Everything above that (SSE, sector mapping,
  throttling, the whole React UI) stays the same.

> The purchase flow (`buy_map`) is a stub: it unlocks a map for the VIN after a
> short delay. Wire it to a real Stripe checkout + entitlement store before
> using it for anything real. No real payment or tuning files are included.

## Design provenance

The UI is a faithful build of the Claude Design handoff `MED17 Flash Tool v5`
(dark "Pro-Werkzeug" project). Tokens: Titillium Web + JetBrains Mono, papaya
accent `#FF7A00`, card radius 14 px, on the `#F5F5F7` canvas.
