"""ECU profile: everything variant-specific in one overridable place.

The profile describes *how* to talk to and reprogram a particular ECU: CAN
identifiers, ISO-TP/UDS timing, the Security Access level and seed/key
algorithm, the flash routine identifiers, and - most importantly - the memory
map that splits an image into program blocks.

A representative MED17.7.5 profile is built in (:func:`builtin_med17_7_5`).
Because every real car/variant differs, the same structure loads from YAML/JSON
so an integrator can pin exact addresses, the correct security level and the
seed/key constants for their ECU without touching code.

.. warning::
   The built-in addresses, routine ids and seed/key parameters are a *template*
   modelled on the public MED17 flash flow. Verify them against your ECU's
   ODX/flash description before writing to real hardware.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from ..exceptions import Med17FlasherError
from . import uds_const as C


@dataclass
class MemoryRegion:
    """One logical program block in the ECU's address space."""

    name: str
    start: int
    size: int
    erase: bool = True
    checksum: str = "crc32"
    #: If set, the flash sequence patches the computed checksum into the image
    #: at this absolute address before download (checksum correction).
    checksum_patch_address: Optional[int] = None
    checksum_patch_size: int = 4

    @property
    def end(self) -> int:
        return self.start + self.size


@dataclass
class CanConfig:
    tx_id: int = 0x7E0  # physical request id (tester -> ECU)
    rx_id: int = 0x7E8  # response id (ECU -> tester)
    functional_id: int = 0x7DF  # functional/broadcast request id
    is_extended_id: bool = False
    padding_byte: Optional[int] = 0x55
    bitrate: int = 500000


@dataclass
class TimingConfig:
    p2: float = 1.0
    p2_star: float = 5.0
    st_min: int = 0x00  # our advertised separation time (raw STmin)
    block_size: int = 0
    tester_present_period: float = 2.0
    # Extra time to wait for slow flash routines (erase/check) via 0x78 loop.
    routine_timeout: float = 20.0


@dataclass
class SecurityConfig:
    """Security Access (0x27) configuration."""

    request_seed_level: int = int(C.SecurityAccessType.REQUEST_SEED_PROGRAMMING)
    send_key_level: int = int(C.SecurityAccessType.SEND_KEY_PROGRAMMING)
    algorithm: str = "med17"  # name registered in med17flasher.seedkey
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RoutineConfig:
    erase_memory: int = int(C.Routine.ERASE_MEMORY)
    check_programming_dependencies: int = int(C.Routine.CHECK_PROGRAMMING_DEPENDENCIES)
    check_memory: int = int(C.Routine.CHECK_MEMORY)
    #: How the erase routine addresses a region:
    #: "address_size" -> pass 4-byte address + 4-byte size (ALFID-prefixed)
    #: "block_id"     -> pass a 1-byte logical block id (index into memory_map)
    #: "none"         -> RoutineControl start erase with no argument (whole flash)
    erase_argument: str = "address_size"


@dataclass
class FingerprintWrite:
    """A WriteDataByIdentifier fingerprint write done during flashing."""

    did: int
    value: str  # hex string
    #: "after_security" (before erase) or "after_download" (after RequestDownload)
    when: str = "after_security"


@dataclass
class GatewayConfig:
    """A gateway / diagnostic-firewall Security Access done before flashing.

    Mercedes routes diagnostics through a gateway (e.g. EZS) that must itself be
    unlocked before the target ECU can be reprogrammed.
    """

    tx_id: int
    rx_id: int
    security_level: int
    algorithm: str = "med17"
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EcuProfile:
    name: str = "MED17.7.5"
    description: str = "Bosch MED17.7.5 (Infineon TriCore) - template profile"
    can: CanConfig = field(default_factory=CanConfig)
    timing: TimingConfig = field(default_factory=TimingConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    routines: RoutineConfig = field(default_factory=RoutineConfig)
    memory_map: List[MemoryRegion] = field(default_factory=list)
    #: dataFormatIdentifier for RequestDownload (0x00 = uncompressed/unencrypted).
    transfer_data_format: int = 0x00
    #: Programming session used before flashing.
    programming_session: int = int(C.Session.PROGRAMMING)
    #: Whether to send a compression/encryption "flash driver" before erase
    #: (many MED17 flows upload a small RAM routine first; left off by default).
    upload_flash_driver: bool = False

    # -- optional real-world flow tweaks (all default to the generic behaviour) --
    #: Hard-reset the ECU and wait ``settle_delay`` s before starting.
    pre_hard_reset: bool = False
    settle_delay: float = 0.0
    #: Run a checkMemory routine after each block (set False when checksum:false).
    verify_after_write: bool = True
    #: Clear DTCs (service 0x14, group 0xFFFFFF) after the reset.
    clear_dtc_after: bool = False
    #: Session to leave the ECU in at the end (None = default session).
    final_session: Optional[int] = None
    #: Fingerprint writes performed during flashing.
    fingerprints: List[FingerprintWrite] = field(default_factory=list)
    #: Optional gateway / diagnostic-firewall unlock before flashing.
    gateway: Optional[GatewayConfig] = None

    # ------------------------------------------------------------------ #
    # (de)serialisation
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EcuProfile":
        def _int(value: Any) -> int:
            if isinstance(value, str):
                return int(value, 0)  # accepts "0x7E0"
            return int(value)

        can_data = dict(data.get("can", {}))
        for key in ("tx_id", "rx_id", "functional_id", "padding_byte", "bitrate"):
            if key in can_data and can_data[key] is not None:
                can_data[key] = _int(can_data[key])
        can = CanConfig(**can_data) if can_data else CanConfig()

        timing = TimingConfig(**data.get("timing", {})) if data.get("timing") else TimingConfig()

        sec_data = dict(data.get("security", {}))
        for key in ("request_seed_level", "send_key_level"):
            if key in sec_data and sec_data[key] is not None:
                sec_data[key] = _int(sec_data[key])
        security = SecurityConfig(**sec_data) if sec_data else SecurityConfig()

        rt_data = dict(data.get("routines", {}))
        for key in ("erase_memory", "check_programming_dependencies", "check_memory"):
            if key in rt_data and rt_data[key] is not None:
                rt_data[key] = _int(rt_data[key])
        routines = RoutineConfig(**rt_data) if rt_data else RoutineConfig()

        regions = []
        for r in data.get("memory_map", []):
            regions.append(
                MemoryRegion(
                    name=r["name"],
                    start=_int(r["start"]),
                    size=_int(r["size"]),
                    erase=bool(r.get("erase", True)),
                    checksum=r.get("checksum", "crc32"),
                    checksum_patch_address=(
                        _int(r["checksum_patch_address"])
                        if r.get("checksum_patch_address") is not None
                        else None
                    ),
                    checksum_patch_size=int(r.get("checksum_patch_size", 4)),
                )
            )

        fingerprints = [
            FingerprintWrite(did=_int(f["did"]), value=str(f["value"]),
                             when=f.get("when", "after_security"))
            for f in data.get("fingerprints", [])
        ]
        gateway = None
        if data.get("gateway"):
            g = data["gateway"]
            gateway = GatewayConfig(
                tx_id=_int(g["tx_id"]), rx_id=_int(g["rx_id"]),
                security_level=_int(g["security_level"]),
                algorithm=g.get("algorithm", "med17"), params=g.get("params", {}),
            )

        profile = cls(
            name=data.get("name", "MED17.7.5"),
            description=data.get("description", ""),
            can=can,
            timing=timing,
            security=security,
            routines=routines,
            memory_map=regions,
            transfer_data_format=_int(data.get("transfer_data_format", 0x00)),
            programming_session=_int(data.get("programming_session", C.Session.PROGRAMMING)),
            upload_flash_driver=bool(data.get("upload_flash_driver", False)),
            pre_hard_reset=bool(data.get("pre_hard_reset", False)),
            settle_delay=float(data.get("settle_delay", 0.0)),
            verify_after_write=bool(data.get("verify_after_write", True)),
            clear_dtc_after=bool(data.get("clear_dtc_after", False)),
            final_session=(_int(data["final_session"]) if data.get("final_session") is not None else None),
            fingerprints=fingerprints,
            gateway=gateway,
        )
        return profile


def load_profile(source: Any) -> EcuProfile:
    """Load a profile from a path (YAML/JSON), a mapping, or an EcuProfile."""

    if isinstance(source, EcuProfile):
        return source
    if isinstance(source, dict):
        return EcuProfile.from_dict(source)
    if isinstance(source, (str, os.PathLike)):
        path = os.fspath(source)
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        data = _parse_structured(text, path)
        return EcuProfile.from_dict(data)
    raise Med17FlasherError(f"cannot load ECU profile from {type(source).__name__}")


def _parse_structured(text: str, path: str) -> Dict[str, Any]:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover - PyYAML ships with the env
            raise Med17FlasherError(
                "PyYAML is required to read YAML profiles; use JSON or install pyyaml"
            ) from exc
        return yaml.safe_load(text)
    return json.loads(text)


# --------------------------------------------------------------------------- #
# Built-in template
# --------------------------------------------------------------------------- #
def builtin_med17_7_5() -> EcuProfile:
    """A representative MED17.7.5 profile (TC1797, 4 MB program flash).

    Addresses use the TriCore program-flash segment base 0x80000000. The low
    boot sector (0x80000000..0x8001FFFF) is left out on purpose - it must not be
    reprogrammed over UDS. Everything here is overridable via a YAML profile.
    """

    memory_map = [
        MemoryRegion("CBOOT", 0x80020000, 0x00020000, erase=True, checksum="crc32"),
        MemoryRegion("ASW1", 0x80040000, 0x001C0000, erase=True, checksum="crc32"),
        MemoryRegion("ASW2", 0x80200000, 0x00100000, erase=True, checksum="crc32"),
        MemoryRegion("CAL", 0x80300000, 0x00100000, erase=True, checksum="crc32"),
    ]
    return EcuProfile(
        name="MED17.7.5",
        description="Bosch MED17.7.5 (Infineon TriCore TC1797) - template profile",
        can=CanConfig(tx_id=0x7E0, rx_id=0x7E8, functional_id=0x7DF, padding_byte=0x55),
        timing=TimingConfig(p2=1.0, p2_star=5.0, routine_timeout=25.0),
        security=SecurityConfig(
            request_seed_level=int(C.SecurityAccessType.REQUEST_SEED_PROGRAMMING),
            send_key_level=int(C.SecurityAccessType.SEND_KEY_PROGRAMMING),
            algorithm="med17",
            params={"k": "0x1C5A36B7", "shift": 5},
        ),
        routines=RoutineConfig(erase_argument="address_size"),
        memory_map=memory_map,
        transfer_data_format=0x00,
    )


def default_profile() -> EcuProfile:
    """Return the built-in MED17.7.5 profile, or the packaged YAML if present."""

    packaged = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "..", "config", "med17_7_5.yaml"
    )
    packaged = os.path.normpath(packaged)
    if os.path.isfile(packaged):
        try:
            return load_profile(packaged)
        except Med17FlasherError:
            pass
    return builtin_med17_7_5()
