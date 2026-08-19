# MEDC17 checksum correction

After a legitimate calibration change, a Bosch **MED17/EDC17** image no longer
matches its internal checksums and the ECU rejects it. `med17flasher checksum`
recomputes and corrects them.

```bash
med17flasher checksum verify  tuned.bin              # list regions + OK/BAD
med17flasher checksum correct tuned.bin -o fixed.bin # correct, write fixed.bin
```

## How it works

MED17/EDC17 flash carries **checksum descriptor structures**, each marked by the
magic pair `FADECAFE` (seed) / `CAFEAFFE` (expected). A structure names a region
(start/end address) and an algorithm:

| id | algorithm | target |
|----|-----------|--------|
| `0x00` | CRC-32 (zlib / IEEE-802.3) | `0x35015001` (= `~CAFEAFFE`) |
| `0x01` | ADD32 (sum of dwords) | `0xCAFEAFFE` |
| `0x10` | ADD16 (folded words) | `0xCAFEAFFE` |

TriCore aliases the same flash into cached (`0x8xxxxxxx`) and uncached
(`0xAxxxxxxx`) segments; addresses are canonicalised to `0x8xxxxxxx` before
mapping to a file offset.

Correction changes a single **compensation dword** per region:

* **ADD32/ADD16** — adjust the dword arithmetically so the sum hits the target.
* **CRC-32** — solved **directly** (no brute force): CRC is affine over GF(2),
  so we build the 32×32 matrix mapping each compensation-bit to its effect on
  the CRC and solve `A·x = target ⊕ CRC(0)` by Gaussian elimination. One linear
  solve, one 4-byte patch.

## Scope and ethics

This is a clean-room MIT implementation. It **corrects internal integrity
checksums** so a modified-but-honest image is accepted — a standard, necessary
step in ECU calibration/repair.

It deliberately does **not**:

* forge **RSA signatures** (defeating cryptographic authenticity), or
* **spoof the CVN** (Calibration Verification Number) to a *stock* value to hide
  a calibration from diagnostics/emissions checks.

The real CVN can be **computed and reported** for the actual content
(`medc17_checksum.compute_cvn`), never rewritten to a different value.

## Note on exact offsets

The descriptor layout and the compensation-slot position are modelled on the
public MED17/EDC17 format and validated with synthetic round-trips. For a
specific ECU, verify against a real dump — send one and the exact adjust-slot /
descriptor offsets can be pinned.
