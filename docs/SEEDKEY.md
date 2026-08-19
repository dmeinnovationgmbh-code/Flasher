# Security Access (seed/key)

MED17 guards flash programming with UDS Security Access (service `0x27`): the
tester requests a **seed**, transforms it into a **key** with an ECU-specific
routine, and sends the key back. Only after the ECU accepts the key does it
allow erase/download.

This toolkit provides the **framework** and several **reference algorithms**.
It does not ship any manufacturer's confidential production routine — you plug
that in for your ECU.

## Using the built-in algorithms

```python
from med17flasher.seedkey import compute_key
key = compute_key("med17", bytes.fromhex("11223344"),
                  level=0x11, params={"k": 0x1C5A36B7, "rounds": 5, "shift": 5})
```

CLI:

```bash
med17flasher seedkey 11223344 --algorithm med17 --level 0x11 \
    --param k=0x1C5A36B7 --param rounds=5 --param shift=5
```

Available reference algorithms (`med17flasher backends` lists them):

| name      | shape                                              |
|-----------|----------------------------------------------------|
| `xor`     | `key = seed XOR k`                                 |
| `add`     | `key = (seed + k) mod 2ⁿ`                          |
| `sum`     | byte-wise `(seed[i] + k) & 0xFF`                   |
| `med17`   | LFSR/Galois feedback rounds + rotate (`k, rounds, shift, xor_out`) |
| `vag_crc` | CRC-mix (`poly, init, app_key`)                    |
| `fixed`   | constant key (for "security disabled" / bench)     |

These are **deterministic and self-consistent** — the simulator validates keys
with the very same code, which is what makes the end-to-end tests possible — but
they are *not* a captured production routine.

## The seed/key store

A JSON catalogue maps `(ecu, level)` → algorithm + constants:

```json
[
  {"ecu": "MED17.7.5", "level": 17, "algorithm": "med17",
   "params": {"k": "0x1C5A36B7", "rounds": 5, "shift": 5},
   "note": "replace with your ECU's values"}
]
```

```python
from med17flasher.seedkey import SeedKeyStore
store = SeedKeyStore.load("my_seedkeys.json")
key = store.compute("MED17.7.5", 0x11, seed)
```

Pass it to the flasher with `--seedkey-store my_seedkeys.json`.

## Plugging in your ECU's routine

Write a small Python plug-in that registers a function or a `SeedKeyAlgorithm`:

```python
# my_algo.py
from med17flasher.seedkey import register_function

def my_key(seed, level, params):
    # your ECU's transform here
    v = int.from_bytes(seed, "big")
    v = ((v ^ 0xA5A5A5A5) + 0x1234) & 0xFFFFFFFF
    return v.to_bytes(4, "big")

register_function("my_ecu", my_key, "my ECU's seed/key")
```

Load it at flash time:

```bash
med17flasher flash --plugin my_algo.py --seedkey-store my_seedkeys.json firmware.bin
# (with an entry in the store whose "algorithm" is "my_ecu")
```

## Recovering the algorithm from captured pairs (`seedkey-solve`)

If you don't yet have the routine, the honest way to obtain it is to **derive it
from example pairs you capture from an ECU you own** (or from your licensed
tool): request a seed (`0x27` requestSeed), let the trusted tool produce the
key, and record the `(seed, key)` pair. A handful of pairs is usually enough.

```bash
# pairs.txt: one "seedhex keyhex" per line (or a .json list of {seed,key})
med17flasher seedkey-solve --pairs pairs.txt --level 0x11 --emit-store recovered.json
```

The solver:

* **directly solves** the simple families — `xor` (`k = seed ^ key`),
  `add` (`k = key - seed`), `sum` (per-byte offset) — from as little as one pair;
* **brute-forces** the structured families (`med17`, `vag_crc`) over a parameter
  grid; supply a `--wordlist` of likely 32-bit constants to make that tractable:

```bash
med17flasher seedkey-solve --pairs pairs.txt --wordlist constants.txt \
    --level 0x11 --emit-store recovered.json
```

On a full match it prints the algorithm + parameters and (with `--emit-store`)
writes a ready-to-use seed/key store you pass straight to `flash --seedkey-store`.

Programmatic use:

```python
from med17flasher.seedkey import SeedKeySolver, load_pairs
best = SeedKeySolver(load_pairs("pairs.txt")).best(level=0x11, wordlist=[0x1C5A36B7, ...])
print(best.algorithm, best.params)   # e.g. med17 {'k': ..., 'rounds': 5, 'shift': 5}
```

> The solver never invents an algorithm — it only reports one that provably
> reproduces **every** pair you supply. An unknown 32-bit-keyed proprietary
> routine generally cannot be brute-forced without a wordlist of candidate
> constants; when it can't be recovered, plug the routine in directly as a
> plug-in (above).

## Using a vendor seed/key DLL or EXE

Production ECU flashing usually keeps the secret routine in a **J2534 seed-key
DLL** (exports `GenerateKeyExOpt` / `GetSeedLength` / `GetKeyLength` / …) or a
small `*-seed-key.exe`. Both plug in directly:

```bash
# compute a key with a DLL (32-bit Windows / Wine - the DLL is 32-bit stdcall):
med17flasher seedkey <seedhex> --dll MED1775_12_42_00.dll --level 0x05

# flash using the DLL for Security Access:
med17flasher flash --seedkey-dll MED1775_12_42_00.dll ...

# or an external executable (stdout = key hex), like execa('cpcng-seed-key.exe'):
med17flasher flash --seedkey-exe cpcng-seed-key.exe ...
```

See [`MED1775.md`](MED1775.md) for the full flow.

## The 32-bit problem — and the automatic bridge

Vendor seed/key DLLs (`MED1775_12_42_00.dll` and friends) are **32-bit Windows
stdcall** libraries. Windows will not map a 32-bit image into a 64-bit process,
so loading one from 64-bit Python — which is what the packaged desktop build
is — fails no matter what:

```
OSError: [WinError 193] %1 is not a valid Win32 application
```

`--seedkey-bridge` solves this without any manual setup: the flasher spawns a
small **32-bit helper process** that loads the DLL and answers key requests over
a pipe, so a 64-bit app can use a 32-bit DLL transparently.

```bash
# flash from 64-bit Python using a 32-bit DLL:
med17flasher flash --seedkey-bridge MED1775_12_42_00.dll firmware.bin

# same for a secure read:
med17flasher read --secure --seedkey-bridge MED1775_12_42_00.dll -o dump.bin

# or front the 32-bit DLL from the seed/key server:
med17flasher seedkey-server --bridge MED1775_12_42_00.dll
```

The 32-bit interpreter is auto-discovered: first the `py` launcher (`py -3-32`),
then the usual install locations (`C:\Python3*-32\python.exe`,
`%LOCALAPPDATA%\Programs\Python\Python3*-32\python.exe`, …). Every candidate is
bitness-checked with `struct.calcsize('P') == 4`. Point at a specific one with
`--python32 "C:\Python311-32\python.exe"` when you have several installed. If
none is found you get a clear error instead of the cryptic WinError 193 —
install the 32-bit build from python.org (it coexists happily with the 64-bit
one) and make sure `med17flasher` is importable from it (the bridge puts this
checkout on the child's `PYTHONPATH`; otherwise `pip install med17flasher` into
the 32-bit interpreter too).

Programmatic use — it satisfies the same `compute(ecu, level, seed)` resolver
interface as everything else, and is a context manager:

```python
from med17flasher.seedkey import SeedKeyBridge

with SeedKeyBridge("MED1775_12_42_00.dll") as bridge:
    print(bridge.info())          # {'ecu_name': ..., 'seed_length': 4, 'bits': 32, ...}
    key = bridge.compute("MED17.7.5", 0x05, bytes.fromhex("183fd11c"))
```

Under the hood the parent and the helper exchange one JSON object per line over
stdin/stdout:

| direction | message |
|-----------|---------|
| → | `{"cmd": "key", "level": 5, "seed": "183fd11c"}` |
| ← | `{"ok": true, "key": "0a1b2c3d"}` |
| → | `{"cmd": "info"}` |
| ← | `{"ok": true, "info": {"ecu_name": "...", "seed_length": 4, "key_length": 4, "access_types": [5], "bits": 32}}` |
| → | `{"cmd": "quit"}` |
| ← | `{"ok": true}` |
| ← | `{"ok": false, "error": "..."}` on any failure |

The helper is `python -m med17flasher.seedkey.bridge --dll PATH [--options STR]`,
which you can also run by hand to debug a DLL. It is started lazily on the first
key request and reused for the whole session; replies are deadline-enforced
(`timeout=`, 10 s by default) so a wedged DLL cannot hang a flash, and if the
helper dies its stderr is included in the `SeedKeyError`. On Windows it is
launched with `CREATE_NO_WINDOW`, so no console window flashes up.

The older route still works and remains the right answer when the DLL lives on a
*different* machine: run a **seed/key server** on the Windows host (see below)
that fronts the DLL, and let the flasher run anywhere.

## The seed/key server

Traditionally the secret routine lives behind a small network service so it sits
in one place. This toolkit ships one (HTTP + a line-based TCP protocol):

```bash
med17flasher seedkey-server --store my_seedkeys.json
# or front a vendor DLL/EXE (run on the 32-bit Windows host):
med17flasher seedkey-server --dll MED1775_12_42_00.dll
med17flasher seedkey-server --exe cpcng-seed-key.exe
```

* **HTTP**: `POST /seedkey` with `{"ecu": "...", "level": 17, "seed": "11223344"}`
  → `{"key": "..."}`. Also `GET /key/<level>/<seedhex>` → `{"key": "..."}`
  (production-compatible), `GET /algorithms`, `GET /entries`, `GET /health`.
* **TCP**: send `MED17.7.5 0x11 11223344\n` → `OK <keyhex>\n`.

Point the flasher at it instead of a local store:

```bash
med17flasher flash --seedkey-server http://127.0.0.1:8377 firmware.bin
```

Internally the flasher calls `resolver.compute(ecu, level, seed)`; a
`SeedKeyStore`, a `seedkey.server.SeedKeyClient`, or your own object all satisfy
that interface.
