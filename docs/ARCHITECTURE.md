# Architecture

The toolkit is built as a stack of loosely coupled layers. Each layer depends
only on the one below it and can be used on its own.

```
                    ┌───────────────────────────────────────────────┐
   front-ends       │      CLI (cli.py)        GUI (gui/app.py)      │
                    └───────────────┬───────────────────┬───────────┘
                                    │                   │
                    ┌───────────────▼───────────────────▼───────────┐
   orchestration    │           Flasher (core/flash_sequence)       │
                    │   session → seed/key → erase → download →      │
                    │   transfer → verify → dependencies → reset     │
                    └───┬───────────────┬───────────────┬───────────┘
                        │               │               │
              ┌─────────▼───┐   ┌───────▼──────┐  ┌─────▼───────────┐
   services   │  UdsClient  │   │   seedkey    │  │  firmware /     │
              │ (core/uds)  │   │  (algorithms,│  │  checksum /     │
              │             │   │  store,      │  │  ecu_profile)   │
              │             │   │  server)     │  │                 │
              └──────┬──────┘   └──────────────┘  └─────────────────┘
                     │
              ┌──────▼───────┐
   transport  │  IsoTpLayer  │   ISO 15765-2 segmentation + flow control
              │ (core/isotp) │
              └──────┬───────┘
                     │
              ┌──────▼─────────────────────────────────────────────┐
   link       │  CanBus:  VirtualCanBus | SocketCanBus | PythonCan │
              │  (core/can_backends)                                │
              └─────────────────────────────────────────────────────┘

   test harness:  simulator/virtual_ecu.py attaches a VirtualEcu to a
                  VirtualCanNetwork so the tester and a full UDS ECU talk
                  in-process — this is what powers the end-to-end tests.

   side services: server/ (firmware repository REST API + client)
                  seedkey/server.py (seed/key HTTP + TCP server)
```

## Layer responsibilities

### `core/can_backends.py`
Defines `CanFrame` and the abstract `CanBus`. Concrete backends:
* `VirtualCanBus` on a `VirtualCanNetwork` — a broadcast medium so several nodes
  (tester + simulator) share one in-process "wire".
* `SocketCanBus` — native Linux SocketCAN using only `socket`/`struct`.
* `PythonCanBus` — wraps `python-can` for real adapters.

`create_bus("backend:target")` is the single factory used at the edges.

### `core/isotp.py`
A symmetric ISO-TP implementation (used by both tester and ECU). Handles
Single/First/Consecutive/Flow-Control frames, block size and STmin (sending and
receiving), padding, the 32-bit escape First Frame, and extended addressing.

### `core/uds.py`
`UdsClient` — one method per UDS service the flash flow needs. It decodes
negative responses into `NegativeResponseError`, transparently waits out
`responsePending` (NRC 0x78), and can run a background TesterPresent keep-alive.

### `core/ecu_profile.py`
Everything variant-specific: CAN ids, timing, Security Access level + algorithm,
routine ids, and the **memory map** that slices an image into program blocks.
A built-in MED17.7.5 template loads from `config/med17_7_5.yaml`, or from any
YAML/JSON you provide.

### `core/firmware.py` + `core/checksum.py`
`FirmwareImage` is a set of address/data segments with loaders for raw binary,
Intel HEX and Motorola S-Record. `blocks_for(memory_map)` turns an image into
the ordered `FlashBlock`s the sequence downloads. `checksum.py` provides the
CRC/sum algorithms used for verification.

### `core/flash_sequence.py`
`Flasher` — the state machine. It reports `FlashProgress` through a callback and
can be aborted between requests via a `threading.Event`. `ProfileSeedKey` is the
default seed/key resolver (uses the profile's algorithm); a `SeedKeyStore` or a
remote `SeedKeyClient` can be substituted.

### `seedkey/`
`base.py` is the algorithm framework (interface + registry + plug-in loader),
`algorithms.py` the reference implementations, `store.py` a JSON catalogue
mapping (ECU, level) → algorithm + constants, and `server.py` a network server
(HTTP + TCP) plus an HTTP client.

### `server/`
`FirmwareRepository` stores firmware blobs + a JSON metadata index; `FileServer`
exposes it over a dependency-free HTTP REST API; `FileServerClient` consumes it.

### `simulator/virtual_ecu.py`
A threaded `VirtualEcu` that answers real UDS — sessions, a genuine seed/key
challenge (same algorithm the flasher uses), erase, download/transfer, and a
`checkMemory` routine that CRC-checks the bytes actually received. It makes the
entire tool-chain runnable and testable with no hardware.

## Data flow of one flash

1. A front-end builds a `CanBus`, wraps it in `IsoTpLayer`, then `UdsClient`.
2. It loads a `FirmwareImage` and an `EcuProfile`, and constructs a `Flasher`.
3. `Flasher.flash(image)`:
   - opens the extended then programming session, disables DTCs/comms;
   - runs Security Access — asking the resolver (profile / store / server) to
     turn the ECU's seed into a key;
   - for each block from `image.blocks_for(profile.memory_map)`: optional
     checksum patch → erase routine → `RequestDownload` → `TransferData` loop →
     `RequestTransferExit` → `checkMemory` routine;
   - runs `checkProgrammingDependencies`, then resets the ECU.
4. Progress events drive the CLI progress bar / GUI bar; errors surface as typed
   exceptions under `Med17FlasherError`.
