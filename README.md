# DME Innovation MED17 Flasher

<img src="webui/public/assets/dme-logo.svg" alt="DME Innovation" height="34">

A **complete, hardware-agnostic desktop flashing toolkit** for the Bosch
**MED17.7.5** engine control unit (Infineon TriCore, VAG platform). It speaks
real UDS (ISO 14229) over ISO-TP (ISO 15765-2) on CAN, implements the full
reprogramming sequence with Security Access (seed/key), and ships with a
firmware **file server**, a **seed/key server**, a **desktop GUI**, a **CLI**,
and a **virtual ECU simulator** so the whole stack runs and is testable without
any hardware.

> ⚠️ **Read [`docs/SAFETY.md`](docs/SAFETY.md) before writing to a real ECU.**
> The built-in addresses, routine ids and seed/key constants are a *template*.
> Flashing an ECU with the wrong data can render it inoperable. Only work on
> vehicles/ECUs you are authorised to modify.

---

## Highlights

| Layer | What it does |
|-------|--------------|
| **CAN backends** | **J2534 PassThru** (Tactrix Openport 2.0, Mongoose, VCX — incl. a 32-bit-DLL bridge), virtual (in-process), native SocketCAN, and `python-can` (PCAN, Vector, Kvaser, slcan, …) behind one interface |
| **ISO-TP** | Full ISO 15765-2: SF/FF/CF/FC, block size & STmin, padding, 32-bit escape frames, extended addressing |
| **UDS client** | Sessions, ECU reset, Security Access, Routine Control, Request/Transfer Download, Read/Write DID & memory, TesterPresent keep-alive, `responsePending` (0x78) handling |
| **Flash sequence** | Session → preconditions → programming session → seed/key → per-block erase/download/transfer/verify → dependencies → reset, with progress + abort |
| **Seed/Key** | Pluggable algorithm framework + reference algorithms + a **J2534 seed-key DLL** / seed-key EXE backend + a JSON catalogue + a **solver** that recovers the algorithm from captured seed→key pairs + an HTTP/TCP **seed/key server** (with a production-style `GET /key/:level/:seed` route) |
| **File server** | Dependency-free HTTP REST firmware repository (upload/list/download/delete + metadata + bearer auth) with a client |
| **Simulator** | A virtual MED17.7.5 that answers real UDS, including a genuine seed/key challenge and CRC-checked programming |
| **Front-ends** | A **React (Vite) web UI** (the DME "DME Innovation MED17 Flasher" design, wired to the real flash engine via a JSON/SSE API), a Tkinter desktop GUI, and a full-featured CLI |

The **core has no third-party dependencies** — it runs on a stock Python 3.8+.
Optional adapters (`python-can`, `pyserial`) and YAML profiles (`PyYAML`) light
up when installed.

---

## Install

```bash
# from the repository root
pip install -e ".[dev,all]"     # editable, with optional extras + test deps
# or minimal (core only, no third-party deps):
pip install -e .
```

The GUI needs Tkinter (usually `apt install python3-tk` on Debian/Ubuntu).

## Download the desktop app

The **DME Innovation MED17 Flasher** ships as a single standalone executable (no Python
or Node needed) that opens the web UI in a window/browser and runs the real
backend locally. **See [`docs/INSTALL.md`](docs/INSTALL.md) for step-by-step
per-OS install instructions.**

* **Download a prebuilt binary** from the repository's **Releases** page
  (`med17flasher-desktop-windows.exe`, `-macos`, `-linux`) or, on Windows, the
  classic setup installer `med17flasher-setup-windows.exe` (Start-menu + desktop
  shortcut). Releases are built automatically from a `v*` tag by
  `.github/workflows/release.yml`; every dev-branch push also uploads the same
  binaries as **Actions artifacts**. Download, run, done.
* **Windows:** run the `.exe` (SmartScreen → *More info → Run anyway*) or the setup installer.
* **macOS:** `chmod +x` then right-click → **Open** (Gatekeeper, first launch only).
* **Linux:** `chmod +x ./med17flasher-desktop-linux && ./med17flasher-desktop-linux`,
  or `sh packaging/install_linux.sh ./med17flasher-desktop-linux` to add it to your app menu.
* **Run from source:** `pip install -e . && med17flasher desktop`
* **Build the binary yourself:** `make desktop`  (→ `dist/med17flasher-desktop`)

Optional: `pip install pywebview` for a native window instead of the browser.

## 60-second tour (no hardware needed)

```bash
# 1. Run a full, verified flash against the built-in virtual ECU:
python scripts/demo_flash.py

# 2. Same thing via the CLI:
python scripts/make_demo_firmware.py -o demo.bin
med17flasher flash --simulator --profile config/med17_7_5_demo.yaml \
    --base 0x80040000 demo.bin

# 3. Compute a key from a seed:
med17flasher seedkey 11223344 --algorithm med17 --level 0x11 --param k=0x1C5A36B7

# 4. Launch the desktop GUI (defaults to the simulator):
med17flasher gui
```

## Flashing a real ECU

```bash
med17flasher flash \
    --backend j2534 \                 # or socketcan:can0, pcan:PCAN_USBBUS1, slcan:/dev/ttyUSB0, ...
    --profile my_ecu.yaml \           # your verified addresses/routines/security
    --seedkey-store my_seedkeys.json \# your ECU's seed/key algorithm + constants
    firmware.bin
```

See [`docs/FLASH_SEQUENCE.md`](docs/FLASH_SEQUENCE.md) for the exact UDS exchange
and [`docs/SEEDKEY.md`](docs/SEEDKEY.md) for plugging in your ECU's algorithm.

## CLI commands

```
med17flasher flash          reprogram an ECU from a firmware file (.bin/.hex/.s19)
med17flasher identify       read identification DIDs
med17flasher read           read a memory range to a file
med17flasher seedkey        compute a key from a seed
med17flasher seedkey-solve  recover a seed/key algorithm from captured pairs
med17flasher seedkey-server run the seed/key network server (HTTP + TCP)
med17flasher scan           read-only ECU reconnaissance (sessions/DIDs/seeds)
med17flasher xcp            measure/log live ECU values over XCP (poll or DAQ)
med17flasher a2l            read an ASAP2/A2L file and emit XCP signal specs
med17flasher capture        passively record CAN frames to a candump log
med17flasher sniff          passively sniff another tool's read/write (Tactrix on a split bus) and derive the profile + seed/key
med17flasher analyze-trace  derive a profile + seed/key pairs from a CAN trace
med17flasher analyze-firmware detect program regions in a firmware dump
med17flasher checksum       verify/correct MEDC17 internal flash checksums
med17flasher convert        convert bin/Intel-HEX/S-Record to a flat .bin
med17flasher inflate        inflate DEFLATE/zlib/gzip data (e.g. a flash section)
med17flasher extract-calibration  slice a flashable calibration from a full read
med17flasher ingest         scan a folder (_input/) and auto-process files
med17flasher fileserver     run the firmware file server
med17flasher simulator      run a stand-alone virtual MED17.7.5
med17flasher backends       list usable CAN backends, J2534 interfaces and seed/key algorithms
med17flasher j2534          check a J2534 interface (versions, battery, --listen for live traffic)
med17flasher profile        print an ECU profile as JSON
med17flasher gui            launch the Tkinter desktop GUI
med17flasher webserver      serve the React web UI + JSON/SSE API (--open)
```

Run `med17flasher <command> --help` for options. Add `-v`/`-vv` for INFO/DEBUG logs.

## The servers

```bash
# Firmware repository (REST):
med17flasher fileserver --root ./firmware-repo --token secret

# Seed/key server (HTTP on 8377, TCP on 8378):
med17flasher seedkey-server --store my_seedkeys.json

# Both at once:
python scripts/run_server.py --root ./firmware-repo
```

The flasher can offload key computation to the seed/key server
(`--seedkey-server http://host:8377`) and the GUI's *File Server* tab browses,
downloads and uploads firmware from the repository.

## Web UI (React)

The `webui/` folder is the DME **DME Innovation MED17 Flasher** design (Mercedes-AMG C63 S
demo) built as a React (Vite) app. It is served by the Python backend and its
flash view is driven by the **real** flash engine over a JSON/SSE API — the
progress %, PFLASH sector map, address, KB/s and log lines all come from an
actual UDS flash of the virtual ECU (throttled to a realistic CAN rate). The
OTS-Maps purchase flow is **simulated** exactly as designed (no real payment,
no tuning files).

```bash
# a prebuilt UI ships in webui/dist, so this just works:
med17flasher webserver --open          # http://127.0.0.1:8090

# rebuild the UI after changing it:
cd webui && npm install && npm run build

# or run the Vite dev server (proxies /api to the Python backend on :8090):
cd webui && npm run dev                 # http://127.0.0.1:5173
```

See [`docs/WEBUI.md`](docs/WEBUI.md) for the API and how to point the flash view
at real hardware.

## Desktop GUI

`med17flasher gui` opens a five-tab window:

* **Connection** – pick a backend or the simulator, choose a profile, connect, read identification
* **Flash** – load firmware (local or from the file server), watch a live progress bar, abort
* **Seed/Key** – compute keys and run the seed/key server
* **File Server** – browse / download / upload firmware
* **Log** – the full application log

Flashing runs on a worker thread; progress is marshalled back to the UI safely.

## Project layout

```
med17flasher/
  core/        CAN backends, ISO-TP, UDS, checksum, firmware, ECU profile, flash sequence
  seedkey/     algorithm framework + reference algorithms + store + seed/key server
  server/      firmware repository + HTTP file server + client
  simulator/   virtual MED17.7.5 ECU
  gui/         Tkinter desktop app
  cli.py       command line interface
config/        MED17.7.5 profiles (full + demo)
scripts/       demo_flash, make_demo_firmware, run_server
tests/         pytest suite (unit + end-to-end against the simulator)
docs/          ARCHITECTURE, FLASH_SEQUENCE, SEEDKEY, SAFETY
```

See [`docs/J2534.md`](docs/J2534.md) for connecting a Tactrix Openport (or any
other PassThru interface), [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for how
the layers fit together, and [`docs/MED1775.md`](docs/MED1775.md) for a real MED17.7.5
calibration-flash flow (security level 0x05/0x06, whole-flash erase, fingerprint
writes, gateway unlock) driven by a vendor **seed/key DLL**.

## Testing

```bash
pytest            # 67 tests: ISO-TP, UDS, seed/key, checksum, firmware, file server,
                  # CLI, and end-to-end flashes against the virtual ECU
```

## License

MIT — see [`LICENSE`](LICENSE).
