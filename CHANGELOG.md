# Changelog

All notable changes to the **DME Innovation MED17 Flasher** are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/); this project
uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **J2534 PassThru CAN backend** — support for the interfaces professional
  flashing actually uses (**Tactrix Openport 2.0**, Mongoose, VCX, …). Until now
  the only real transports were SocketCAN and whatever `python-can` covers, and
  `python-can` does not speak J2534, so a Tactrix could not reach the bus at
  all. Includes Windows registry discovery (both 32- and 64-bit views), raw-CAN
  channel setup with the mandatory pass-all filter, transmit-echo suppression,
  batched reads, and interface/battery-voltage readout. See
  [`docs/J2534.md`](docs/J2534.md).
- **32-bit J2534 bridge** — vendor PassThru DLLs are 32-bit (`op20pt32.dll`) and
  cannot be loaded by the 64-bit desktop build. `open_j2534()` now detects that
  loader error and transparently runs the driver in a 32-bit helper process,
  relaying frames over a line-JSON pipe.
- `med17flasher j2534` — pre-flight check: opens the interface, prints
  firmware/DLL/API versions and **battery voltage** (a brown-out mid-erase
  bricks a MED17), and `--listen N` counts live bus traffic to tell a wiring
  problem from an ECU problem. `med17flasher backends` now lists installed
  PassThru interfaces by name.
- The app's transport dropdown lists installed J2534 interfaces by their real
  name; when none is installed the entry stays visible but disabled.

### Changed
- The 32-bit helper-process machinery (interpreter discovery, spawning, the
  line-JSON exchange) moved into `core/procbridge.py` and is now shared by the
  seed/key and J2534 bridges instead of being duplicated. `SeedKeyBridge._request`
  is now the public `request()`.
- The frozen build ships a plain-source copy of the package (`bridge_src/`) so
  the 32-bit helper can import it — previously the bridges only worked from a
  source checkout, never from an installed desktop build.

### Testing
- `tests/test_j2534.py` compiles a **mock PassThru driver in C** and exercises
  the real ctypes layer against it (struct layout, big-endian id encoding, echo
  suppression, filters, batching, teardown, the bridge), finishing with a
  **complete UDS flash** — security access, erase, transfer, CRC verify —
  carried through the J2534 backend into the ECU simulator.

## [1.0.0] - 2026-08-19

First complete release. **Everything is verified against the built-in simulator;
nothing has been validated on a real ECU yet** — read
[`docs/SAFETY.md`](docs/SAFETY.md) before writing to hardware.

### Desktop app
- Single standalone executable for **Windows, macOS and Linux** — no Python or
  Node needed. Opens as a **native window** (pywebview) on Windows/macOS; falls
  back to the browser on Linux.
- Windows **Inno Setup installer** with Start-menu and desktop shortcuts.
- Branded **DME Innovation MED17 Flasher**, with the DME wordmark as the app icon.
- Robust startup: a missing/broken bundled profile falls back to a built-in one,
  a startup failure shows a native error dialog + writes `med17flasher.log`, and
  CI runs the frozen binary with `--selftest` so a build that crashes can't ship.

### Web UI (four tabs, all driven by the real engine)
- **Flashen** — expert real-flash: choose an ECU profile (incl. the real
  `med1775` production flow), a transport (simulator or SocketCAN/PCAN/SLCAN/
  Vector/Kvaser), upload your own firmware (`.bin`/`.hex`/`.s19`), pick the
  seed/key source, and watch live progress + log. Writing to real hardware is
  gated behind an explicit opt-in.
- **Messen** — live XCP values with per-signal tiles and charts, polling or DAQ
  streaming, CSV export.
- **Diagnose** — read-only ECU scan (sessions, identification DIDs, seed levels),
  memory read with hex dump, MEDC17 checksum verify/correct. Identification read
  from the ECU replaces the placeholder vehicle data.
- **OTS-Maps** — the map-purchase showcase (simulated; no real payment/files).

### Engine and tooling
- **UDS** (ISO 14229) client over **ISO-TP** (ISO 15765-2) with `responsePending`
  (0x78) handling; full flash sequence with progress + abort.
- **Seed/Key** — algorithm framework + reference algorithms, JSON catalogue, a
  solver that recovers the algorithm from captured `(seed, key)` pairs, an
  HTTP/TCP server, J2534 **DLL/EXE** backends, and a **32-bit bridge** so a
  64-bit build can use a 32-bit vendor DLL.
- **XCP** (ASAM MCD-1) master over CAN — memory read, full DAQ command set,
  polling + DAQ measurement, CSV logging, and a virtual slave for tests.
- **A2L** (ASAP2) parser → XCP signal specs.
- **MEDC17/EDC17 checksums** — verify + correct (CRC32 via a GF(2) solver,
  ADD32/ADD16), read-only CVN.
- Firmware **file server**, `convert`/`inflate`/`extract-calibration`, CAN trace
  and firmware analysis, and a **virtual MED17.7.5 simulator**.
- CLI (`med17flasher …`) covering all of the above.

### Quality
- **222 tests**, `ruff` lint in CI, builds on Python 3.9–3.12, and desktop builds
  for all three OSes with a post-build binary selftest.

### Known limitations
- No real-hardware validation; bundled profiles are a documented template.
- The `med1775` seed/key is a placeholder — supply the real key via the vendor
  DLL, the bridge, or a seed/key server.
- The native window is unverified on Windows/macOS (built in CI, run only on
  Linux which uses the browser fallback).
- Builds are **not code-signed** — Windows SmartScreen warns on first run.
- No `.cff` container parser yet, and MEDC17 checksum offsets are not pinned to a
  specific ECU (both need a real sample to finish).

[Unreleased]: https://github.com/dmeinnovationgmbh-code/Flasher/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/dmeinnovationgmbh-code/Flasher/releases/tag/v1.0.0
