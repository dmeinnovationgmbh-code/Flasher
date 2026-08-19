# Changelog

All notable changes to the **DME Innovation MED17 Flasher** are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/); this project
uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

_Nothing yet._

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
