# Web UI

`webui/` is the **DME Innovation MED17 Flasher** design, implemented as a React (Vite)
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

**Expert / real flash** (the "Experte · Echt-Flash" panel):

| Method & path              | Purpose                                              |
|----------------------------|------------------------------------------------------|
| `GET  /api/profiles`       | bundled ECU profiles (`config/*.yaml`, incl. med1775)|
| `GET  /api/backends`       | usable CAN transports + seed/key algorithm names     |
| `GET  /api/expert`         | current expert-flash configuration                   |
| `POST /api/firmware?name=` | upload a firmware file (raw body: .bin/.hex/.s19)     |
| `POST /api/expert/config`  | set `{profileId, backend, seedkey, allowWrite}`      |
| `POST /api/expert/flash`   | start the configured real flash → `{started: bool}`  |

The expert flash reuses the same `/api/flash/stream` SSE and progress display.
`seedkey` selects the key source: `{source:"profile"}` (algorithm from the
profile), `{source:"server", url}` (a seed/key server), `{source:"dll"|"exe",
path, options}` (a vendor J2534 DLL / seed-key exe), or `{source:"store",
path}` (a JSON catalogue). Writing to a **real** (non-simulator) backend is
refused unless `allowWrite:true` is set — the simulator always runs.

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

## Flashing a real ECU from the UI

The top **Schreibvorgang** card is the C63 demo (simulated ECU, generated
image). To flash a **real** file, use the **Experte · Echt-Flash** panel:

1. pick a **Profil** (e.g. `med17_7_5_med1775` for the real production flow);
2. pick a **Verbindung** — `Simulator` (safe) or a real adapter
   (`socketcan:can0`, `pcan:…`, `slcan:…`, …);
3. upload your **Firmware-Datei** (`.bin` / `.hex` / `.s19`);
4. choose the **Seed/Key** source (profile algorithm, server URL, vendor DLL/exe
   path, or a JSON catalogue);
5. for a real adapter, tick **Schreiben freigeben** and press **Echt flashen**.

Progress streams into the same Schreibvorgang card. Writing to real hardware is
gated: without the write opt-in the backend refuses before it even opens the
bus. This is the same engine as `med17flasher flash …` on the CLI — see
[`MED1775.md`](MED1775.md) and [`SEEDKEY.md`](SEEDKEY.md).

> The purchase flow (`buy_map`) is a stub: it unlocks a map for the VIN after a
> short delay. Wire it to a real Stripe checkout + entitlement store before
> using it for anything real. No real payment or tuning files are included.

## Design provenance

The UI is a faithful build of the Claude Design handoff `MED17 Flash Tool v5`
(dark "Pro-Werkzeug" project). Tokens: Titillium Web + JetBrains Mono, papaya
accent `#FF7A00`, card radius 14 px, on the `#F5F5F7` canvas.
