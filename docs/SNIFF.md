# Sniffing another flasher (reverse-engineering a read/write)

`med17flasher sniff` **passively records** a complete read or write that another
tool (e.g. an Autotuner, a bench flasher, a dealer tester) performs on the ECU,
then reconstructs the flow and **derives the ECU profile and the seed/key
pairs** from it. It is the €0 alternative to buying a MED17.7.5 protocol package:
you already own the car, the other tool and a Tactrix — this turns one observed
session into a working profile for *this* flasher.

> It only ever **listens**. It never transmits a single CAN frame. That is not
> a nicety — while the other tool is mid-write, a second master injecting frames
> on the same bus could corrupt the transfer and brick the ECU. Sniffing is
> safe precisely because it is silent.

## The wiring: a split OBD2 line

A J2534 device is exclusive to one application, so the tool doing the flash and
the tool doing the sniff **cannot share one interface** — you need two, both on
the same CAN pair:

```
                 ┌─────────────────┐
   Autotuner ────┤                 │
   (or other     │  OBD2 splitter  ├──── ECU (in car / on bench)
    flasher)     │  CAN-H / CAN-L  │
   Tactrix   ────┤  in parallel    │
   (this tool)   └─────────────────┘
```

Both interfaces sit on the same CAN-H/CAN-L. The Autotuner is the master and
does the real work; the Tactrix is a silent extra node that hears everything.
An OBD2 Y-splitter (or a bench harness with CAN-H/CAN-L broken out) is all the
hardware you need beyond the second interface.

## Run it

```bash
# 1) Prove the wiring first (counts live traffic, still passive):
med17flasher j2534 --listen 5

# 2) Start the sniff, THEN start the read/write on the other tool:
med17flasher sniff --device tactrix --baudrate 500000 -o session.log
#   ... run the Autotuner read or write now ...
#   Ctrl+C when it finishes (or use --seconds N)
```

While it captures, it decodes the UDS flow live so you can see it is really
working:

```
  [   12.874] Sitzung -> Programmierung (0x02)
  [   12.913] Security Access L0x11: Seed = 1a2b3c4d
  [   12.951] Security Access L0x11: Key  = 8369ee49
  [   12.951]   -> Seed/Key-Paar L0x11: 1a2b3c4d / 8369ee49
  [   13.002] RoutineControl eraseMemory (0xFF00) 0x80040000  1835008 Bytes
  [   13.040] RequestDownload -> 0x80040000  1835008 Bytes (0x1C0000)
  [   13.520]   TransferData: 64 Bloecke ...
  ...
  [   58.113] RequestTransferExit (7168 Bloecke)
  [   58.140] RoutineControl checkMemory (0x0202)
  [   58.160] ECUReset (Hard-Reset)
```

When it ends it reassembles the **whole** recording (not just the heads it
showed live) and prints the derived profile plus every seed/key pair, and
writes them out:

```
  abgeleitetes Profil -> session.profile.yaml
  Seed/Key-Paare       -> session.pairs.txt
```

## What you get, and the next step

* **`session.log`** — the raw candump; re-analyse any time with
  `med17flasher analyze-trace session.log`.
* **`session.profile.yaml`** — CAN ids, security level, erase/checkMemory
  routine ids and the memory map (every RequestDownload address + size).
* **`session.pairs.txt`** — the `seed key` pairs exchanged during Security
  Access.

One session already lets you **replay** the same write with this flasher
(the ECU accepts the captured key). To make the flasher compute keys itself for
*fresh* seeds, recover the algorithm from **several** sniffed sessions (each has
a different seed):

```bash
med17flasher sniff -o run2.log      # capture a second/third session
med17flasher seedkey-solve --pairs session.pairs.txt --pair <seed>:<key> ...
```

Two pairs with different seeds are the minimum to disambiguate the linear
families; more is better. See [`docs/SEEDKEY.md`](SEEDKEY.md).

Then rehearse against your derived profile **without writing anything**:

```bash
med17flasher flash --dry-run --profile session.profile.yaml \
    --seedkey-store recovered.json image.bin
```

## Options

| flag | meaning |
|------|---------|
| `--backend` | interface to listen on — default `j2534` (Tactrix); also `tactrix`, `socketcan:can0`, … |
| `--device` | J2534 device name substring (`tactrix`) or a path to the PassThru DLL |
| `--baudrate` | CAN bit rate of the bus you are sniffing (default 500000) |
| `--extended` | the bus uses 29-bit CAN ids |
| `--seconds N` | stop after N seconds (default: until Ctrl+C) |
| `--tx` / `--rx` | request/response ids for the final analysis (default from the profile; auto-detected from traffic if the profile guess sees nothing) |
| `-o` | raw candump output (default `sniff.log`) |
| `--emit-profile` / `--emit-pairs` | override the derived-file paths |
| `--no-analyze` | just record; skip the automatic analysis |
| `--simulator` | sniff an in-process simulated flash (for trying the tool without hardware) |

## Legality / scope

This is for reverse-engineering **your own** setup: your car, your other tool,
your bus. It reads data that already crosses a wire you own. It does not crack,
patch or redistribute anyone's licensed software — it derives a profile for
*this* flasher from what you legitimately observe.
