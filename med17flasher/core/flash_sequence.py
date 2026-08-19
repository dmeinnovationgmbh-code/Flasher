"""The MED17.7.5 flashing state machine.

:class:`Flasher` drives the full reprogramming sequence over a
:class:`~med17flasher.core.uds.UdsClient`:

1. enter extended session, read identification
2. disable DTC storage + normal communication
3. enter programming session
4. Security Access (request seed -> compute key -> send key)
5. for each program block: erase -> requestDownload -> transferData* ->
   requestTransferExit -> checkMemory
6. checkProgrammingDependencies
7. ECU reset, back to the default session

Progress is reported through a callback, and a :class:`threading.Event` can
abort the run cleanly between UDS requests. A TesterPresent keep-alive runs in
the background for the whole flash so the ECU never times the session out.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional, Protocol

from ..exceptions import FlashAborted, FlashError, Med17FlasherError
from ..logging_setup import get_logger
from . import checksum as _cs
from . import uds_const as C
from .ecu_profile import EcuProfile, MemoryRegion, default_profile
from .firmware import FirmwareImage, FlashBlock
from .isotp import IsoTpConfig, IsoTpLayer
from .uds import UdsClient

log = get_logger("core.flash")


class Stage(str, Enum):
    IDLE = "idle"
    CONNECT = "connect"
    IDENTIFY = "identify"
    PRECONDITION = "precondition"
    PROGRAMMING_SESSION = "programming_session"
    SECURITY_ACCESS = "security_access"
    ERASE = "erase"
    DOWNLOAD = "download"
    TRANSFER = "transfer"
    TRANSFER_EXIT = "transfer_exit"
    VERIFY = "verify"
    DEPENDENCIES = "dependencies"
    RESET = "reset"
    DONE = "done"
    FAILED = "failed"


@dataclass
class FlashProgress:
    stage: Stage
    message: str = ""
    block_name: str = ""
    block_index: int = 0
    block_count: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    overall_done: int = 0
    overall_total: int = 0

    @property
    def percent(self) -> float:
        if not self.overall_total:
            return 0.0
        return 100.0 * self.overall_done / self.overall_total


ProgressCallback = Callable[[FlashProgress], None]


class SeedKeyResolver(Protocol):
    """Anything that can turn a seed into a key for a given ECU/level."""

    def compute(self, ecu: str, level: int, seed: bytes) -> bytes:  # pragma: no cover
        ...


class ProfileSeedKey:
    """A resolver that uses the algorithm/params baked into the ECU profile."""

    def __init__(self, profile: EcuProfile) -> None:
        self._algorithm = profile.security.algorithm
        self._params = dict(profile.security.params)

    def compute(self, ecu: str, level: int, seed: bytes) -> bytes:
        from ..seedkey import compute_key

        return compute_key(self._algorithm, seed, level=level, params=self._params)


@dataclass
class FlashResult:
    success: bool
    blocks: List[str] = field(default_factory=list)
    duration: float = 0.0
    message: str = ""


class Flasher:
    """Reprogram a MED17.7.5 following its ECU profile."""

    def __init__(
        self,
        uds: UdsClient,
        profile: Optional[EcuProfile] = None,
        seedkey: Optional[SeedKeyResolver] = None,
        *,
        progress: Optional[ProgressCallback] = None,
        abort_event: Optional[threading.Event] = None,
    ) -> None:
        self.uds = uds
        self.profile = profile or default_profile()
        self.seedkey = seedkey or ProfileSeedKey(self.profile)
        self._progress_cb = progress
        self._abort = abort_event or threading.Event()

    # ------------------------------------------------------------------ #
    # Control
    # ------------------------------------------------------------------ #
    def abort(self) -> None:
        self._abort.set()

    def _check_abort(self) -> None:
        if self._abort.is_set():
            raise FlashAborted("flash aborted by caller")

    def _report(self, progress: FlashProgress) -> None:
        log.info("[%s] %s", progress.stage.value, progress.message or progress.block_name)
        if self._progress_cb:
            try:
                self._progress_cb(progress)
            except Exception:  # a broken UI callback must not kill the flash
                log.exception("progress callback raised")

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def flash(self, image: FirmwareImage) -> FlashResult:
        """Run the complete flashing sequence for ``image``."""

        start = time.monotonic()
        blocks = image.blocks_for(self.profile.memory_map)
        if not blocks:
            raise FlashError(
                "the firmware image does not overlap any region of the ECU "
                "memory map; check the base address / profile"
            )
        overall_total = sum(b.size for b in blocks)
        overall_done = 0
        done_names: List[str] = []

        self.uds.start_tester_present(self.profile.timing.tester_present_period)
        try:
            self._pre_reset()
            self._gateway_unlock()
            self._connect()
            self._preconditions()
            self._enter_programming_session()
            self._security_access()
            self._write_fingerprints("after_security")

            for index, block in enumerate(blocks, 1):
                self._check_abort()
                block = self._patch_checksum(block)
                self._erase_block(block, index, len(blocks))
                max_block_len = self._request_download(block, index, len(blocks))
                if index == 1:
                    self._write_fingerprints("after_download")
                overall_done = self._transfer_block(
                    block, index, len(blocks), max_block_len, overall_done, overall_total
                )
                self._request_transfer_exit(block)
                if self.profile.verify_after_write:
                    self._verify_block(block, index, len(blocks))
                done_names.append(block.name)

            self._check_dependencies()
            self._reset()
            self._post_reset()
        except FlashAborted:
            self._report(FlashProgress(Stage.FAILED, message="aborted"))
            self._safe_return_to_default()
            raise
        except Med17FlasherError as exc:
            self._report(FlashProgress(Stage.FAILED, message=str(exc)))
            self._safe_return_to_default()
            raise
        finally:
            self.uds.stop_tester_present()

        duration = time.monotonic() - start
        self._report(
            FlashProgress(
                Stage.DONE,
                message=f"flashed {len(done_names)} block(s) in {duration:.1f}s",
                overall_done=overall_total,
                overall_total=overall_total,
            )
        )
        return FlashResult(True, done_names, duration, "ok")

    def identify(self):
        """Read identification DIDs (safe, read-only)."""

        self._report(FlashProgress(Stage.IDENTIFY, message="reading identification"))
        return self.uds.identify()

    # ------------------------------------------------------------------ #
    # Steps
    # ------------------------------------------------------------------ #
    def _pre_reset(self) -> None:
        if not self.profile.pre_hard_reset:
            return
        self._report(FlashProgress(Stage.CONNECT, message="pre-flash hard reset"))
        try:
            self.uds.ecu_reset(C.ResetType.HARD_RESET)
        except Med17FlasherError as exc:
            log.debug("pre-flash reset returned: %s", exc)
        if self.profile.settle_delay:
            self._report(FlashProgress(Stage.CONNECT,
                                       message=f"waiting {self.profile.settle_delay:.0f}s for ECU"))
            time.sleep(self.profile.settle_delay)

    def _gateway_unlock(self) -> None:
        gw = self.profile.gateway
        if gw is None:
            return
        self._check_abort()
        self._report(FlashProgress(Stage.SECURITY_ACCESS,
                                   message=f"gateway unlock (id 0x{gw.tx_id:03X}, level 0x{gw.security_level:02X})"))
        gw_tp = IsoTpLayer(self.uds.tp.bus, IsoTpConfig(
            tx_id=gw.tx_id, rx_id=gw.rx_id,
            is_extended_id=self.profile.can.is_extended_id,
            padding_byte=self.profile.can.padding_byte))
        gw_uds = UdsClient(gw_tp)
        try:
            gw_uds.enter_extended_session()
        except Med17FlasherError:
            pass
        seed = gw_uds.request_seed(gw.security_level)
        if any(seed):
            from ..seedkey import compute_key

            key = compute_key(gw.algorithm, seed, level=gw.security_level, params=gw.params)
            gw_uds.send_key(gw.security_level + 1, key)
        self._report(FlashProgress(Stage.SECURITY_ACCESS, message="gateway unlocked"))

    def _write_fingerprints(self, when: str) -> None:
        for fp in self.profile.fingerprints:
            if fp.when != when:
                continue
            self._check_abort()
            self._report(FlashProgress(Stage.SECURITY_ACCESS,
                                       message=f"fingerprint 0x{fp.did:04X}={fp.value}"))
            self.uds.write_data_by_identifier(fp.did, bytes.fromhex(fp.value))

    def _post_reset(self) -> None:
        if self.profile.clear_dtc_after:
            try:
                self.uds.request(bytes([C.Service.CLEAR_DIAGNOSTIC_INFORMATION, 0xFF, 0xFF, 0xFF]))
                self._report(FlashProgress(Stage.DONE, message="cleared DTCs"))
            except Med17FlasherError as exc:
                log.debug("clear DTC returned: %s", exc)
        if self.profile.final_session is not None:
            try:
                self.uds.diagnostic_session_control(self.profile.final_session)
            except Med17FlasherError as exc:
                log.debug("final session change returned: %s", exc)

    def _connect(self) -> None:
        self._report(FlashProgress(Stage.CONNECT, message="entering extended session"))
        try:
            self.uds.enter_extended_session()
        except Med17FlasherError as exc:
            # Some flows go straight to the programming session.
            log.debug("extended session skipped: %s", exc)

    def _preconditions(self) -> None:
        self._report(
            FlashProgress(Stage.PRECONDITION, message="disabling DTCs and normal comms")
        )
        # Best-effort: some ECUs reject these outside programming session.
        for action in (self.uds.disable_dtc_setting, self.uds.disable_normal_communication):
            try:
                action()
            except Med17FlasherError as exc:
                log.debug("precondition step skipped: %s", exc)

    def _enter_programming_session(self) -> None:
        self._report(
            FlashProgress(Stage.PROGRAMMING_SESSION, message="entering programming session")
        )
        self.uds.diagnostic_session_control(self.profile.programming_session)

    def _security_access(self) -> None:
        sec = self.profile.security
        self._report(
            FlashProgress(
                Stage.SECURITY_ACCESS,
                message=f"requesting seed (level 0x{sec.request_seed_level:02X})",
            )
        )
        seed = self.uds.request_seed(sec.request_seed_level)
        if not any(seed):
            # An all-zero seed means security is already unlocked.
            self._report(
                FlashProgress(Stage.SECURITY_ACCESS, message="ECU already unlocked")
            )
            return
        key = self.seedkey.compute(self.profile.name, sec.request_seed_level, seed)
        self._report(
            FlashProgress(
                Stage.SECURITY_ACCESS,
                message=f"seed={seed.hex()} -> key={key.hex()}",
            )
        )
        self.uds.send_key(sec.send_key_level, key)
        self._report(FlashProgress(Stage.SECURITY_ACCESS, message="security access granted"))

    def _patch_checksum(self, block: FlashBlock) -> FlashBlock:
        """Insert a freshly computed checksum into the block if configured."""

        region = self._region_for(block)
        if region is None or region.checksum_patch_address is None:
            return block
        addr = region.checksum_patch_address
        size = region.checksum_patch_size
        if not (block.address <= addr and addr + size <= block.end):
            log.warning(
                "checksum patch address 0x%08X outside block %s; skipping",
                addr,
                block.name,
            )
            return block
        data = bytearray(block.data)
        offset = addr - block.address
        # Compute the checksum over the block excluding the checksum field
        # itself, matching the common convention.
        payload = bytes(data[:offset]) + bytes(data[offset + size :])
        value = _cs.compute(region.checksum, payload)
        data[offset : offset + size] = value.to_bytes(size, "little")
        self._report(
            FlashProgress(
                Stage.VERIFY,
                block_name=block.name,
                message=f"patched {region.checksum} checksum 0x{value:X} @0x{addr:08X}",
            )
        )
        return FlashBlock(block.name, block.address, bytes(data), block.erase)

    def _erase_block(self, block: FlashBlock, index: int, count: int) -> None:
        if not block.erase:
            return
        self._check_abort()
        self._report(
            FlashProgress(
                Stage.ERASE,
                block_name=block.name,
                block_index=index,
                block_count=count,
                message=f"erasing {block.name} (0x{block.address:08X}, {block.size} bytes)",
            )
        )
        arg = self._erase_argument(block, index)
        self.uds.start_routine(
            self.profile.routines.erase_memory,
            arg,
        )

    def _erase_argument(self, block: FlashBlock, index: int) -> bytes:
        mode = self.profile.routines.erase_argument
        if mode == "none":
            return b""  # RoutineControl start erase with no argument (whole flash)
        if mode == "block_id":
            return bytes([index - 1])
        # default: 4-byte address + 4-byte size, prefixed with an ALFID byte
        # (0x44) as used by many MED17 eraseMemory routines.
        return bytes([0x44]) + block.address.to_bytes(4, "big") + block.size.to_bytes(4, "big")

    def _request_download(self, block: FlashBlock, index: int, count: int) -> int:
        self._check_abort()
        self._report(
            FlashProgress(
                Stage.DOWNLOAD,
                block_name=block.name,
                block_index=index,
                block_count=count,
                message=f"requestDownload {block.name}",
            )
        )
        max_block = self.uds.request_download(
            block.address, block.size, data_format=self.profile.transfer_data_format
        )
        if max_block < 3:
            # ECU did not report a usable block length; fall back to 256+2.
            max_block = 0x102
        return max_block

    def _transfer_block(
        self,
        block: FlashBlock,
        index: int,
        count: int,
        max_block_len: int,
        overall_done: int,
        overall_total: int,
    ) -> int:
        # maxNumberOfBlockLength includes the 0x36 SID and the BSC byte.
        chunk_size = max(1, max_block_len - 2)
        data = block.data
        bsc = 1
        sent = 0
        while sent < len(data):
            self._check_abort()
            chunk = data[sent : sent + chunk_size]
            self.uds.transfer_data(bsc, chunk)
            sent += len(chunk)
            overall_done += len(chunk)
            bsc = (bsc + 1) & 0xFF
            if bsc == 0:
                bsc = 1  # BSC never uses 0 after the first wrap on MED17
            self._report(
                FlashProgress(
                    Stage.TRANSFER,
                    block_name=block.name,
                    block_index=index,
                    block_count=count,
                    bytes_done=sent,
                    bytes_total=len(data),
                    overall_done=overall_done,
                    overall_total=overall_total,
                    message=f"transferring {block.name}: {sent}/{len(data)} bytes",
                )
            )
        return overall_done

    def _request_transfer_exit(self, block: FlashBlock) -> None:
        self._check_abort()
        self._report(
            FlashProgress(
                Stage.TRANSFER_EXIT,
                block_name=block.name,
                message=f"requestTransferExit {block.name}",
            )
        )
        self.uds.request_transfer_exit()

    def _verify_block(self, block: FlashBlock, index: int, count: int) -> None:
        self._check_abort()
        region = self._region_for(block)
        algo = region.checksum if region else "crc32"
        checksum = _cs.compute(algo, block.data)
        # checkMemory routine argument: ALFID + address + size + expected checksum
        arg = (
            bytes([0x44])
            + block.address.to_bytes(4, "big")
            + block.size.to_bytes(4, "big")
            + checksum.to_bytes(4, "big")
        )
        self._report(
            FlashProgress(
                Stage.VERIFY,
                block_name=block.name,
                block_index=index,
                block_count=count,
                message=f"checkMemory {block.name} ({algo}=0x{checksum:08X})",
            )
        )
        result = self.uds.start_routine(self.profile.routines.check_memory, arg)
        # A non-empty routine status record whose first byte is non-zero means
        # the ECU rejected the checksum.
        if result and result[0] not in (0x00,):
            raise FlashError(
                f"checkMemory failed for {block.name}: status=0x{result[0]:02X}"
            )

    def _check_dependencies(self) -> None:
        self._check_abort()
        self._report(
            FlashProgress(Stage.DEPENDENCIES, message="checkProgrammingDependencies")
        )
        try:
            self.uds.start_routine(self.profile.routines.check_programming_dependencies)
        except Med17FlasherError as exc:
            # Not every ECU exposes this routine; log but don't fail the flash.
            log.debug("checkProgrammingDependencies skipped: %s", exc)

    def _reset(self) -> None:
        self._report(FlashProgress(Stage.RESET, message="ECU reset"))
        try:
            self.uds.ecu_reset(C.ResetType.HARD_RESET)
        except Med17FlasherError as exc:
            log.debug("ecu reset returned: %s", exc)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _safe_return_to_default(self) -> None:
        try:
            self.uds.enable_normal_communication()
        except Med17FlasherError:
            pass
        try:
            self.uds.enable_dtc_setting()
        except Med17FlasherError:
            pass
        try:
            self.uds.enter_default_session()
        except Med17FlasherError:
            pass

    def _region_for(self, block: FlashBlock) -> Optional[MemoryRegion]:
        for region in self.profile.memory_map:
            if region.name == block.name:
                return region
        return None
