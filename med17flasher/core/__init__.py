"""Core flashing primitives: CAN backends, ISO-TP, UDS, firmware, flashing."""

from .can_backends import (
    CanBus,
    CanFrame,
    VirtualCanBus,
    VirtualCanNetwork,
    available_backends,
    create_bus,
    get_virtual_network,
)
from .ecu_profile import (
    CanConfig,
    EcuProfile,
    MemoryRegion,
    RoutineConfig,
    SecurityConfig,
    TimingConfig,
    builtin_med17_7_5,
    default_profile,
    load_profile,
)
from .firmware import (
    FirmwareImage,
    FlashBlock,
    Segment,
    detect_regions,
    load_firmware,
)
from .trace import BusRecorder, TraceReport, analyze as analyze_trace, read_trace
from .flash_sequence import (
    FlashProgress,
    FlashResult,
    Flasher,
    ProfileSeedKey,
    Stage,
)
from .isotp import IsoTpConfig, IsoTpLayer
from .uds import UdsClient, UdsTiming

__all__ = [
    "CanBus",
    "CanFrame",
    "VirtualCanBus",
    "VirtualCanNetwork",
    "available_backends",
    "create_bus",
    "get_virtual_network",
    "IsoTpConfig",
    "IsoTpLayer",
    "UdsClient",
    "UdsTiming",
    "EcuProfile",
    "CanConfig",
    "TimingConfig",
    "SecurityConfig",
    "RoutineConfig",
    "MemoryRegion",
    "builtin_med17_7_5",
    "default_profile",
    "load_profile",
    "FirmwareImage",
    "FlashBlock",
    "Segment",
    "load_firmware",
    "detect_regions",
    "BusRecorder",
    "TraceReport",
    "analyze_trace",
    "read_trace",
    "Flasher",
    "FlashProgress",
    "FlashResult",
    "ProfileSeedKey",
    "Stage",
]
