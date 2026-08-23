# The MED17.7.5 flash sequence

This is the exact UDS exchange `Flasher.flash()` performs. Service ids are shown
as `SID(sub-function)`. All requests go on the profile's physical request id
(`can.tx_id`, default `0x7E0`); responses arrive on `can.rx_id` (`0x7E8`).

A background **TesterPresent** (`0x3E 0x80`, suppressed response) is sent every
`timing.tester_present_period` seconds for the whole run so the session never
times out.

```
STEP                         REQUEST                       POSITIVE RESPONSE
──────────────────────────────────────────────────────────────────────────────
1  Extended session          10 03                         50 03 ...
2  Disable DTC storage       85 02                         C5 02
   Disable normal comms      28 03 03                      68 03
3  Programming session       10 02                         50 02 ...
4  Request seed              27 11                         67 11 <seed(4)>
   (compute key = f(seed))
   Send key                  27 12 <key(4)>                67 12
   ── on wrong key: 7F 27 35 (invalidKey)
5  For each program block:
   a. (optional) patch checksum into the image
   b. Erase memory           31 01 FF00 44 <addr(4)><size(4)>   71 01 FF00 00
   c. Request download       34 00 44 <addr(4)><size(4)>        74 20 <maxBlockLen(2)>
   d. Transfer data (loop)   36 <bsc> <data...>                 76 <bsc>
        bsc = 1,2,3,… wrapping 0xFF→0x01
        chunk size = maxBlockLen − 2
   e. Request transfer exit  37                                 77
   f. Check memory           31 01 0202 44 <addr><size><crc32>  71 01 0202 00
        status byte ≠ 0 ⇒ FlashError
6  Check dependencies        31 01 FF01                    71 01 FF01 00
7  ECU reset                 11 01                         51 01
   (on error, the sequence restores comms/DTCs and the default session)
```

## Notes and knobs

* **ALFID `0x44`** — the addressAndLengthFormatIdentifier: 4 address bytes + 4
  size bytes. Overridable per request.
* **`maxNumberOfBlockLength`** — returned by `RequestDownload`; it *includes* the
  `0x36` SID and the block-sequence-counter byte, so the transfer chunk size is
  `maxBlockLen − 2`.
* **Block sequence counter (BSC)** — starts at 1, increments per `TransferData`,
  wraps `0xFF → 0x01` (never 0 after the first wrap on MED17).
* **`responsePending` (0x78)** — long routines (erase, checkMemory) may answer
  `7F <sid> 78` repeatedly; the client keeps waiting (up to `p2_star` each time).
* **Erase argument mode** — `routines.erase_argument`:
  * `address_size` (default): `ALFID + addr + size`
  * `block_id`: a single byte = the memory-map index
* **Checksum patching** — set `checksum_patch_address` on a `MemoryRegion` to
  have the flasher compute the region checksum and insert it into the image
  before download (checksum correction).

## Program blocks

Blocks come from the profile's `memory_map`. The demo profile
(`config/med17_7_5_demo.yaml`) has two small regions (`ASW`, `CAL`) so a full
flash finishes in milliseconds against the simulator. The full profile
(`config/med17_7_5.yaml`) models `CBOOT`, `ASW1`, `ASW2`, `CAL` on the TriCore
`0x80000000` program-flash segment, leaving the boot sector out.

> The exact addresses, routine ids and the `checkMemory` argument layout differ
> between ECU variants/ODX versions. Always confirm them against your ECU's
> flash description; override them in a YAML profile rather than editing code.
