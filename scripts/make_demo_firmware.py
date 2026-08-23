#!/usr/bin/env python3
"""Generate a demo firmware image matching the demo ECU profile.

Creates a raw ``.bin`` that fully covers the ASW and CAL regions of
``config/med17_7_5_demo.yaml`` so it can be flashed against the simulator.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from med17flasher.core import load_profile  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", default="demo_firmware.bin")
    parser.add_argument("--profile", default="config/med17_7_5_demo.yaml")
    args = parser.parse_args()

    profile = load_profile(args.profile)
    regions = sorted(profile.memory_map, key=lambda r: r.start)
    base = regions[0].start
    end = regions[-1].end
    blob = bytearray(end - base)
    for region in regions:
        off = region.start - base
        for i in range(region.size):
            # A recognisable, region-dependent pattern.
            blob[off + i] = (region.start // 0x1000 + i * 7) & 0xFF

    with open(args.output, "wb") as fh:
        fh.write(blob)
    print(f"wrote {len(blob)} bytes to {args.output} (base 0x{base:08X})")
    print(f"flash it with:\n  med17flasher flash --simulator "
          f"--profile {args.profile} --base 0x{base:08X} {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
