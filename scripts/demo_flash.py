#!/usr/bin/env python3
"""End-to-end demo: flash a generated firmware against the virtual ECU.

This wires the whole toolkit together in one process - a virtual MED17.7.5, a
UDS client over ISO-TP on a virtual CAN bus, and the flash sequence - so you
can see a full, verified reprogramming run without any hardware::

    python scripts/demo_flash.py
"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from med17flasher.core import (  # noqa: E402
    IsoTpConfig,
    IsoTpLayer,
    UdsClient,
    UdsTiming,
    VirtualCanNetwork,
    load_profile,
)
from med17flasher.core.firmware import FirmwareImage  # noqa: E402
from med17flasher.core.flash_sequence import Flasher, ProfileSeedKey  # noqa: E402
from med17flasher.logging_setup import configure_logging  # noqa: E402
from med17flasher.simulator import VirtualEcu, VirtualEcuConfig  # noqa: E402


def build_image(profile) -> FirmwareImage:
    img = FirmwareImage()
    for region in profile.memory_map:
        img.add_segment(
            region.start,
            bytes(((region.start >> 12) + i * 7) & 0xFF for i in range(region.size)),
        )
    return img.normalise()


def main() -> int:
    configure_logging(logging.INFO)
    profile = load_profile("config/med17_7_5_demo.yaml")

    net = VirtualCanNetwork()
    sim = VirtualEcu(
        net.new_endpoint("ecu"),
        profile,
        VirtualEcuConfig(
            security_algorithm=profile.security.algorithm,
            security_params=profile.security.params,
            max_block_length=0x0102,
            pending_rounds=1,
        ),
    )
    sim.start()

    tp = IsoTpLayer(
        net.new_endpoint("tester"),
        IsoTpConfig(tx_id=profile.can.tx_id, rx_id=profile.can.rx_id, padding_byte=0x55),
    )
    uds = UdsClient(tp, UdsTiming(p2=profile.timing.p2, p2_star=profile.timing.p2_star))

    image = build_image(profile)
    flasher = Flasher(uds, profile, ProfileSeedKey(profile))

    print("\n=== Identification ===")
    for did, value in flasher.identify():
        print(f"  DID 0x{did:04X}: {value!r}")

    print("\n=== Flashing ===")
    result = flasher.flash(image)
    print(f"\nResult: success={result.success} blocks={result.blocks} "
          f"in {result.duration:.2f}s")

    # Verify what the ECU actually stored.
    ok = all(
        sim.read_memory(r.start, r.size) == image.read(r.start, r.size)
        for r in profile.memory_map
    )
    print(f"Memory verification: {'PASS' if ok else 'FAIL'}")
    sim.stop()
    return 0 if (result.success and ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
