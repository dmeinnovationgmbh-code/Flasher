# Protocol references and our design choices

Standards-level facts this toolkit relies on, with sources, and where our code
sits relative to them. Compiled from public engineering references (ASAM/ISO
conformant open-source implementations and vendor/standard documentation).

> Nothing here is a manufacturer secret. Seed/key algorithms and vehicle-specific
> constants are deliberately **not** listed — those come from your own vendor DLL
> or from solving captured `(seed, key)` pairs (see [`SEEDKEY.md`](SEEDKEY.md)).

## UDS reprogramming (ISO 14229)

Standard programming order — matches `core/flash_sequence.py`:

```
DiagnosticSessionControl 0x10 → SecurityAccess 0x27 → RoutineControl 0x31 eraseMemory
→ RequestDownload 0x34 → TransferData 0x36 (×N) → RequestTransferExit 0x37
→ RoutineControl 0x31 checkMemory → 0x31 checkProgrammingDependencies → ECUReset 0x11
```

- **`RequestDownload` 0x34** = `dataFormatIdentifier | ALFID | address | size`.
  - `dataFormatIdentifier`: high nibble = compression, low nibble = encryption;
    `0x00` = raw. Both are **OEM-defined** — our profiles keep it configurable
    (`transfer_data_format`), default `0x00`. We do **not** hard-code a VAG
    compression code; capture it from a trace of your own ECU if non-zero.
  - **ALFID** = `(sizeLen << 4) | addrLen`. TriCore 32-bit → **0x44**. Verified
    in `core/uds.py::_encode_addr_size` (size length is the high nibble — the
    common bug is swapping them).
- **`RequestDownload` response 0x74**: `maxNumberOfBlockLength` **includes** the
  0x36 SID + block-sequence-counter, so the payload chunk is `maxBlock − 2`.
  Verified in `flash_sequence.py::_transfer_block`.
- **`TransferData` 0x36** block-sequence-counter starts at `0x01`, increments,
  wraps `0xFF → 0x00`. Matches `_transfer_block`.
- **RoutineControl RIDs**: `eraseMemory` **0xFF00** and
  `checkProgrammingDependencies` **0xFF01** are ISO-reserved; **`checkMemory` is
  OEM-specific** (VAG commonly **0x0202**). Our `med17_7_5_med1775.yaml` uses
  exactly `0xFF00 / 0xFF01 / 0x0202`. Treat `checkMemory` as per-profile config.
- **NRC 0x78** (responsePending): reload the **P2\*** timer and keep waiting;
  never resend, never treat as failure. Verified in `core/uds.py` (`p2_star`,
  capped at `max_pending`). Typical timing: P2 ≈ 50 ms, P2\* ≈ 5 s (widened
  during erase/checkMemory).

Sources: ISO 14229-1/-2:2013; pylessard/python-udsoncan (ISO-conformant
open-source); py-uds docs. The `0xFF00`/`0xFF01` reservations are ISO Annex
facts; the `0x0202` checkMemory RID is a VAG convention (secondary sources).

## J2534 PassThru (SAE J2534-1, API 04.04)

The vendor-DLL interface every professional tool uses — see
[`J2534.md`](J2534.md) for the practical side; this is what the code relies on.

- **`PASSTHRU_MSG`** = `ProtocolID | RxStatus | TxFlags | Timestamp | DataSize |
  ExtraDataIndex | Data[4128]`. All six header fields are Win32 `unsigned long`
  = **32 bits**, in a 64-bit process too (LLP64). We spell them `c_uint32` in
  `core/j2534.py`; `c_ulong` would be 64-bit on Linux and shift every field.
- **A CAN message carries its id in the first 4 bytes of `Data`, big-endian**,
  payload after it, so `DataSize = 4 + len(payload)`. It is *not* a struct
  field — this is the most common J2534 mistake.
- **A connected channel receives nothing until a filter exists.** At least one
  `PASS_FILTER` is required; matching is `(id & mask) == (pattern & mask)`, so
  mask `0` passes everything. Filter width follows the mask/pattern message's
  `CAN_29BIT_ID` flag, hence one filter per id width.
- **`RxStatus` bit0 `TX_MSG_TYPE`** marks the device's own transmit echo; bit8
  `CAN_29BIT_ID` marks an extended id. We drop echoes so ISO-TP never reads its
  own frames as responses.
- `PassThruReadMsgs` answers **`ERR_BUFFER_EMPTY` (0x10)** — some drivers
  `ERR_TIMEOUT` (0x09) — when nothing arrived; both are normal, not failures,
  and `pNumMsgs` still reports what was read.
- `PassThruIoctl(READ_VBATT)` returns **millivolts** at pin 16.
- **Registration**: `HKLM\SOFTWARE\PassThruSupport.04.04\<device>` with `Name`,
  `Vendor`, `FunctionLibrary` and per-protocol DWORDs. Most vendor drivers are
  32-bit and register only in the 32-bit registry view, so a 64-bit process must
  read **both** views explicitly.

We connect with `ProtocolID = CAN` (raw), not `ISO15765`, so our own tested
ISO-TP layer keeps ownership of segmentation and timing across every backend.

Sources: SAE J2534-1 (API 04.04) interface definition; the header layout and
error codes as published in open J2534 wrappers (e.g. jazdw/rp1210a-j2534,
python `j2534` bindings). Device-specific behaviour is **unverified here** — no
PassThru hardware was available; the backend is tested against a compiled mock
driver, see `tests/test_j2534.py`.

## XCP (ASAM MCD-1 XCP) on CAN

`CONNECT` response: `0xFF | RESOURCE | COMM_MODE_BASIC | MAX_CTO | MAX_DTO(2) |
protoVer | transportVer`.

- **COMM_MODE_BASIC**: bit0 `BYTE_ORDER` (0 = Intel/little, 1 = Motorola/big),
  bits1-2 `ADDRESS_GRANULARITY` (0=BYTE,1=WORD,2=DWORD), bit6 `SLAVE_BLOCK_MODE`,
  bit7 `OPTIONAL`. We parse byte order **and** address granularity in
  `xcp/client.py::connect` and warn if granularity ≠ BYTE (our pointer math is
  byte-based). **MED17/TriCore slaves are often big-endian** — we read it from
  CONNECT rather than assuming.
- **MAX_CTO / MAX_DTO** ≤ 8 on classic CAN.
- **DAQ setup order** (strict): one `FREE_DAQ` → `ALLOC_DAQ` → all `ALLOC_ODT` →
  all `ALLOC_ODT_ENTRY` → `SET_DAQ_PTR`/`WRITE_DAQ` → `SET_DAQ_LIST_MODE` →
  `START_STOP_DAQ_LIST` → `START_STOP_SYNCH`. Our `configure_daq` uses a single
  DAQ list + single ODT, so the "all ALLOC_ODT before any ALLOC_ODT_ENTRY" rule
  is trivially satisfied.
- **DAQ-DTO frame**: `PID (= absolute ODT number) | [timestamp in first ODT
  only] | data…`. Entry bytes appear in `WRITE_DAQ` order (no addresses in the
  frame — position is everything), which our decoder preserves. We decode the
  no-timestamp single-ODT case and **refuse** `timestamp=True` rather than
  mis-decode.

Sources: christoph2/pyxcp (`types.py`, ASAM-conformant); robotjatek/XCP (alloc
ordering note). Command codes and error codes in `xcp/const.py` match pyxcp.

## A2L / ASAP2 `IF_DATA XCP`

We auto-derive transport + rasters from the A2L (`a2l.py`, `xcp --a2l`):

- **`XCP_ON_CAN`**: `CAN_ID_MASTER` (CRO, master→slave), `CAN_ID_SLAVE` (DTO,
  slave→master, incl. all DAQ), `CAN_ID_BROADCAST`, `BAUDRATE`. A 29-bit id is
  flagged with **bit 31**; the real id is the low 29 bits — we detect and strip
  that, setting extended addressing.
- **`EVENT`**: `"<long>" "<short>" <channel#> <dir> <maxDaqList> <cycle> <unit>
  <priority>` → period = `cycle × 10^unit`.
- **`MEASUREMENT`** names a `COMPU_METHOD`; decode chain is `ECU_ADDRESS
  (+extension) → read datatype bytes with CONNECT byte order → apply
  COMPU_METHOD`.

Sources: christoph2/pyA2L and shreaker/OpenXCP demo A2Ls.

## Infineon TriCore memory map (TC1767 / TC1797)

- PFLASH is mirrored at **0x8000_0000 (cached)** and **0xA000_0000 (non-cached)**
  — same physical flash, two views. Our profiles use segment-8 addresses.
- **DFLASH/EEPROM at 0xAF00_0000**; boot ROM present. TC1767/TC1797 carry ~2 MB
  PFLASH and 64 KB DFLASH; the minimum erase sector is **16 KB** (small sectors
  at the bottom, larger — up to 256 KB — toward the top).

> ⚠️ **Unverified here:** the primary Infineon user-manual sector tables were not
> reachable during research. The base addresses and DFLASH location are
> corroborated, but **verify the exact per-sector layout against the TC1767 /
> TC1797 User's Manual** before trusting sector boundaries in a profile.

Sources: Infineon TC1767/TC1797 datasheets/user manuals (secondary/snippet).

## MEDC17 / EDC17 internal checksums

Descriptor/type-marker model (`core/medc17_checksum.py`): block type bytes
`0xC0` constants, `0x30` customer, `0x40` ASW, `0x60` dataset/calibration,
`0x10` startup. Algorithms: **CRC32** (IEEE-802.3, reversed poly `0xEDB88320`),
**ADD32**, **ADD16**; correction adjusts additive regions arithmetically and
solves CRC32 patch bytes via **GF(2)** algebra. `0xFADECAFE`/`0xCAFEAFFE` act as
Bosch-world seed/expected sentinels (used as CRC/ADD init and self-test values),
**not** guaranteed magic markers at a fixed offset — locate real regions via the
descriptor scan. **CVN** (Calibration Verification Number) is a CRC32 over
calibration regions reported over OBD (Mode 09).

> Exact per-variant checksum offsets are discovered from the descriptor blocks in
> a binary you own — not memorised. Pinning them needs a real MEDC17 dump.

Source: ConnorHowell/medc17-checksum-tool (open-source, structural — no secrets).
