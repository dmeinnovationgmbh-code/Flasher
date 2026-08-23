# Safety & responsible use

Reprogramming an engine control unit is powerful and, done wrong, can leave the
ECU inoperable ("bricked"), damage the engine, or make a vehicle unsafe or
non-compliant. Read this before connecting to real hardware.

## Authorisation

Only work on ECUs and vehicles you own or are explicitly authorised to service.
ECU reprogramming may be subject to local regulations (emissions, roadworthiness,
type approval). You are responsible for staying within them.

## The built-in data is a TEMPLATE

`config/med17_7_5.yaml`, the routine ids, the `checkMemory` argument layout and
the seed/key constants are **modelled on the public MED17 flash flow**, not
captured from a specific ECU. They exist so the tool runs end-to-end against the
simulator. Before writing to hardware, verify against your ECU's ODX / flash
description and put the correct values in your own YAML profile and seed/key
store. Do **not** assume the defaults match your ECU.

## Before you flash a real ECU

* **Back up first.** Read out and archive the current flash (and, where
  possible, the calibration) so you can recover.
* **Stable power.** Use a bench power supply / battery maintainer. A voltage dip
  mid-erase is a classic way to brick an ECU. The UDS layer surfaces
  `voltageTooLow`/`voltageTooHigh` NRCs — heed them.
* **Correct addresses.** Confirm every `MemoryRegion` start/size. Never write
  over the boot sector unless you know exactly what you are doing.
* **Right image for the right ECU.** Match hardware/software part numbers
  (read them with `med17flasher identify`).
* **Dry run (rehearsal).** `med17flasher flash --dry-run` — or the app's
  **Probelauf** button — opens the bus, checks the file against the profile,
  enters the programming session and completes **Security Access**, then stops
  without erasing anything. A seed/key that would fail *after* the erase (and
  brick the ECU) fails here instead, on an untouched ECU. Add `--no-unlock` to
  rehearse without a seed/key. In the app a real write is blocked until a
  rehearsal for that exact configuration has passed.
* **Recovery plan.** Know how to enter boot/BSL mode and reflash if a UDS flash
  is interrupted.

## What the tool does to protect you

* Typed errors: every deliberate failure is a `Med17FlasherError` subclass.
* On any mid-flash failure the sequence attempts to re-enable normal
  communication + DTC storage and return to the default session.
* `checkMemory` CRC verification after each block; a mismatch aborts.
* Abort support: the GUI *Abort* button / an abort `Event` stops cleanly between
  requests.

None of this can substitute for a correct profile, a good image and stable power.

## Test in the sandbox first

Everything in this repository can be exercised against the built-in **virtual
ECU** (`--simulator`, `scripts/demo_flash.py`, the test suite). Validate your
profile, image slicing and seed/key setup there before touching a real ECU.
