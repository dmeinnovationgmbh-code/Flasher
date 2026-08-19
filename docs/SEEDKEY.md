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

## The seed/key server

Traditionally the secret routine lives behind a small network service so it sits
in one place. This toolkit ships one (HTTP + a line-based TCP protocol):

```bash
med17flasher seedkey-server --store my_seedkeys.json
```

* **HTTP**: `POST /seedkey` with `{"ecu": "...", "level": 17, "seed": "11223344"}`
  → `{"key": "..."}`. Also `GET /algorithms`, `GET /entries`, `GET /health`.
* **TCP**: send `MED17.7.5 0x11 11223344\n` → `OK <keyhex>\n`.

Point the flasher at it instead of a local store:

```bash
med17flasher flash --seedkey-server http://127.0.0.1:8377 firmware.bin
```

Internally the flasher calls `resolver.compute(ecu, level, seed)`; a
`SeedKeyStore`, a `seedkey.server.SeedKeyClient`, or your own object all satisfy
that interface.
