"""Shared pytest fixtures.

The fixtures wire a virtual CAN network with a :class:`VirtualEcu` on one end
and a UDS client on the other, using a small, fast MED17.7.5 profile so the
whole flash flow runs in milliseconds.
"""

from __future__ import annotations

import logging

import pytest

from med17flasher.core import (
    CanConfig,
    EcuProfile,
    IsoTpConfig,
    IsoTpLayer,
    MemoryRegion,
    RoutineConfig,
    SecurityConfig,
    TimingConfig,
    UdsClient,
    UdsTiming,
    VirtualCanNetwork,
)
from med17flasher.logging_setup import configure_logging
from med17flasher.simulator import VirtualEcu, VirtualEcuConfig

configure_logging(logging.WARNING)

SECURITY_PARAMS = {"k": "0x1C5A36B7", "rounds": 5, "shift": 5}


@pytest.fixture
def demo_profile() -> EcuProfile:
    return EcuProfile(
        name="MED17.7.5",
        can=CanConfig(tx_id=0x7E0, rx_id=0x7E8, padding_byte=0x55),
        timing=TimingConfig(p2=1.0, p2_star=2.0, tester_present_period=0.3),
        security=SecurityConfig(
            request_seed_level=0x11,
            send_key_level=0x12,
            algorithm="med17",
            params=SECURITY_PARAMS,
        ),
        routines=RoutineConfig(erase_argument="address_size"),
        memory_map=[
            MemoryRegion("ASW", 0x80040000, 0x2000, checksum="crc32"),
            MemoryRegion("CAL", 0x80042000, 0x1000, checksum="crc32"),
        ],
    )


@pytest.fixture
def network() -> VirtualCanNetwork:
    return VirtualCanNetwork()


@pytest.fixture
def simulator(network, demo_profile):
    ecu_bus = network.new_endpoint("ecu")
    sim = VirtualEcu(
        ecu_bus,
        demo_profile,
        VirtualEcuConfig(
            security_algorithm="med17",
            security_params=SECURITY_PARAMS,
            max_block_length=0x0102,  # 256-byte payloads -> multiframe transfers
        ),
    )
    sim.start()
    yield sim
    sim.stop()


@pytest.fixture
def uds(network, demo_profile, simulator) -> UdsClient:
    bus = network.new_endpoint("tester")
    tp = IsoTpLayer(
        bus,
        IsoTpConfig(
            tx_id=demo_profile.can.tx_id,
            rx_id=demo_profile.can.rx_id,
            padding_byte=demo_profile.can.padding_byte,
        ),
    )
    return UdsClient(tp, UdsTiming(p2=1.0, p2_star=2.0))
