"""Command line interface for the MED17.7.5 flasher.

Run ``med17flasher --help`` (or ``python -m med17flasher --help``) for the full
list of sub-commands:

* ``flash``          - reprogram an ECU from a firmware file
* ``identify``       - read the ECU's identification DIDs
* ``checksum``       - verify/correct MEDC17 internal flash checksums
* ``convert``        - convert bin/Intel-HEX/S-Record to a flat .bin
* ``inflate``        - inflate DEFLATE/zlib/gzip data (e.g. a compressed section)
* ``extract-calibration`` - slice a flashable calibration out of a full ECU read
* ``scan``           - read-only ECU reconnaissance (sessions / DIDs / seeds)
* ``a2l``            - read an ASAP2/A2L file and emit XCP signal specs
* ``capture``        - passively record CAN frames to a candump log
* ``read``           - read a memory range to a file
* ``seedkey``        - compute a key from a seed on the command line
* ``seedkey-solve``  - recover a seed/key algorithm from captured seed/key pairs
* ``analyze-trace``  - derive an ECU profile + seed/key pairs from a CAN trace
* ``analyze-firmware`` - detect program regions in a firmware dump
* ``ingest``         - scan a folder (default ``_input/``) and auto-process files
* ``seedkey-server`` - run the seed/key network server
* ``fileserver``     - run the firmware file server
* ``simulator``      - run a stand-alone virtual MED17.7.5
* ``backends``       - list usable CAN backends
* ``profile``        - print an ECU profile
* ``gui``            - launch the desktop GUI
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from typing import Optional

from . import __version__
from .core import (
    IsoTpConfig,
    IsoTpLayer,
    UdsClient,
    UdsTiming,
    available_backends,
    create_bus,
    default_profile,
    load_firmware,
    load_profile,
)
from .core.ecu_profile import EcuProfile
from .core.flash_sequence import Flasher, FlashProgress, ProfileSeedKey, Stage
from .exceptions import Med17FlasherError
from .logging_setup import configure_logging, get_logger
from .seedkey import SeedKeyStore, compute_key, list_algorithms, load_plugin

log = get_logger("cli")


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _load_profile_arg(path: Optional[str]) -> EcuProfile:
    if path:
        return load_profile(path)
    return default_profile()


def _build_uds(bus, profile: EcuProfile) -> UdsClient:
    tp = IsoTpLayer(
        bus,
        IsoTpConfig(
            tx_id=profile.can.tx_id,
            rx_id=profile.can.rx_id,
            is_extended_id=profile.can.is_extended_id,
            padding_byte=profile.can.padding_byte,
            block_size=profile.timing.block_size,
            st_min=profile.timing.st_min,
        ),
    )
    timing = UdsTiming(p2=profile.timing.p2, p2_star=profile.timing.p2_star)
    return UdsClient(tp, timing)


def _seedkey_resolver(args, profile: EcuProfile):
    """Pick a seed/key resolver from the CLI arguments."""

    if getattr(args, "seedkey_bridge", None):
        # A 32-bit vendor DLL cannot be loaded by a 64-bit interpreter at all,
        # so it is served by a helper process instead (see seedkey/bridge.py).
        from .seedkey import SeedKeyBridge

        return SeedKeyBridge(
            args.seedkey_bridge,
            python32=getattr(args, "python32", None),
            options=getattr(args, "seedkey_options", ""),
        )
    if getattr(args, "seedkey_dll", None) or getattr(args, "seedkey_exe", None):
        from .seedkey import AlgorithmResolver, make_backend

        spec = (f"dll:{args.seedkey_dll}" if args.seedkey_dll
                else f"exe:{args.seedkey_exe}")
        kwargs = {"options": getattr(args, "seedkey_options", "")} if args.seedkey_dll else {}
        return AlgorithmResolver(make_backend(spec, **kwargs))
    if getattr(args, "seedkey_server", None):
        from .seedkey.server import SeedKeyClient

        return SeedKeyClient(args.seedkey_server)
    if getattr(args, "seedkey_store", None):
        store = SeedKeyStore.load(args.seedkey_store)
        return store
    return ProfileSeedKey(profile)


def _open_bus(args, profile: EcuProfile):
    """Open the CAN bus, optionally attaching an in-process simulator.

    Returns ``(bus, simulator_or_None)``.
    """

    if getattr(args, "simulator", False):
        from .core import VirtualCanNetwork
        from .simulator import VirtualEcu, VirtualEcuConfig

        # A fresh, isolated network per invocation so simulators never share a
        # broadcast medium (which would cause duplicate responses).
        net = VirtualCanNetwork()
        ecu_bus = net.new_endpoint("ecu")
        tester_bus = net.new_endpoint("tester")
        sim = VirtualEcu(
            ecu_bus,
            profile,
            VirtualEcuConfig(
                security_algorithm=profile.security.algorithm,
                security_params=profile.security.params,
            ),
        )
        sim.start()
        return tester_bus, sim
    return create_bus(args.backend), None


def _progress_printer():
    last = {"pct": -1}

    def printer(p: FlashProgress) -> None:
        if p.stage in (Stage.TRANSFER,):
            pct = int(p.percent)
            if pct != last["pct"]:
                last["pct"] = pct
                bar = "#" * (pct // 4) + "-" * (25 - pct // 4)
                sys.stdout.write(f"\r  [{bar}] {pct:3d}%  {p.block_name}   ")
                sys.stdout.flush()
        else:
            if last["pct"] >= 0:
                sys.stdout.write("\n")
                last["pct"] = -1
            print(f"  * {p.stage.value}: {p.message}")

    return printer


# --------------------------------------------------------------------------- #
# Sub-commands
# --------------------------------------------------------------------------- #
def cmd_flash(args) -> int:
    profile = _load_profile_arg(args.profile)
    if args.tx is not None:
        profile.can.tx_id = args.tx
    if args.rx is not None:
        profile.can.rx_id = args.rx
    if args.plugin:
        load_plugin(args.plugin)

    image = load_firmware(args.firmware, base_address=args.base)
    low, high = image.span
    print(
        f"Firmware: {args.firmware}\n"
        f"  size={image.total_size} bytes, span=0x{low:08X}..0x{high:08X}, "
        f"segments={len(image.segments)}"
    )

    bus, sim = _open_bus(args, profile)
    try:
        uds = _build_uds(bus, profile)
        resolver = _seedkey_resolver(args, profile)
        flasher = Flasher(uds, profile, resolver, progress=_progress_printer())

        if not args.no_identify:
            try:
                ident = flasher.identify()
                for did, value in ident:
                    print(f"  DID 0x{did:04X}: {value!r}")
            except Med17FlasherError as exc:
                print(f"  (identify skipped: {exc})")

        if args.dry_run:
            # A real rehearsal: check the file against the profile, open the
            # session and (unless --no-unlock) complete Security Access - then
            # stop, having written nothing. This is what proves the profile and
            # the seed/key are right *before* the erase.
            report = flasher.preflight(image, unlock=not args.no_unlock)
            print(f"\nPreflight: {report.summary()} ({report.duration:.1f}s)")
            for c in report.checks:
                mark = "[x]" if c.ok else ("[!]" if not c.fatal else "[X]")
                print(f"  {mark} {c.name}" + (f" - {c.detail}" if c.detail else ""))
            if not report.ok:
                print("\nDO NOT FLASH: a blocking check failed.", file=sys.stderr)
            return 0 if report.ok else 1

        result = flasher.flash(image)
        print(f"\nSUCCESS: flashed {', '.join(result.blocks)} in {result.duration:.1f}s")
        return 0
    except Med17FlasherError as exc:
        print(f"\nFLASH FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        if sim:
            sim.stop()
        bus.close()


def cmd_identify(args) -> int:
    profile = _load_profile_arg(args.profile)
    bus, sim = _open_bus(args, profile)
    try:
        uds = _build_uds(bus, profile)
        flasher = Flasher(uds, profile, ProfileSeedKey(profile))
        for did, value in flasher.identify():
            printable = value.decode("latin-1", "replace").strip("\x00 ")
            print(f"DID 0x{did:04X}: {printable!r}  ({value.hex()})")
        return 0
    except Med17FlasherError as exc:
        print(f"identify failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if sim:
            sim.stop()
        bus.close()


def cmd_read(args) -> int:
    profile = _load_profile_arg(args.profile)
    if args.tx is not None:
        profile.can.tx_id = args.tx
    if args.rx is not None:
        profile.can.rx_id = args.rx

    # Resolve the region: explicit --address/--size, or a named memory-map region.
    address, size = args.address, args.size
    if args.region:
        region = next((r for r in profile.memory_map if r.name.lower() == args.region.lower()), None)
        if region is None:
            print(f"no region named {args.region!r} in the profile "
                  f"(have: {', '.join(r.name for r in profile.memory_map)})", file=sys.stderr)
            return 2
        address, size = region.start, region.size
    if address is None or size is None:
        print("specify --address and --size, or --region NAME", file=sys.stderr)
        return 2

    bus, sim = _open_bus(args, profile)
    uds = None
    try:
        uds = _build_uds(bus, profile)
        uds.start_tester_present(profile.timing.tester_present_period)
        try:
            uds.enter_extended_session()
        except Med17FlasherError:
            pass
        if args.secure:
            resolver = _seedkey_resolver(args, profile)
            print(f"security access (level 0x{profile.security.request_seed_level:02X}) ...")
            seed = uds.request_seed(profile.security.request_seed_level)
            if any(seed):
                key = resolver.compute(profile.name, profile.security.request_seed_level, seed)
                uds.send_key(profile.security.send_key_level, key)

        chunk = args.chunk
        data = bytearray()
        print(f"reading 0x{size:X} bytes from 0x{address:08X} in 0x{chunk:X}-byte chunks ...")
        while len(data) < size:
            n = min(chunk, size - len(data))
            block = uds.read_memory_by_address(address + len(data), n)
            if not block:
                print(f"\nECU returned no data at 0x{address + len(data):08X}", file=sys.stderr)
                return 1
            data.extend(block)
            pct = int(100 * len(data) / size)
            sys.stdout.write(f"\r  {len(data):#x}/{size:#x} ({pct:3d}%)")
            sys.stdout.flush()
        sys.stdout.write("\n")
        with open(args.output, "wb") as fh:
            fh.write(bytes(data[:size]))
        print(f"read {len(data)} bytes from 0x{address:08X} -> {args.output}")
        return 0
    except Med17FlasherError as exc:
        print(f"\nread failed: {exc}", file=sys.stderr)
        print("  note: MED17.7.5 flash reads usually need a programming/extended session "
              "and Security Access (try --secure), or bench/boot mode.", file=sys.stderr)
        return 1
    finally:
        uds.stop_tester_present() if 'uds' in dir() else None
        if sim:
            sim.stop()
        bus.close()


def cmd_backup(args) -> int:
    """Read the ENTIRE ECU (every profile region) to one file before writing.

    A full read is the first thing a careful tuner does: if a later write goes
    wrong, this image is the way back. Reads every region in the profile's
    memory map, concatenates them into one ``.bin`` and writes a manifest that
    records each region's address, size, file offset and CRC32.
    """

    import zlib

    profile = _load_profile_arg(args.profile)
    if args.tx is not None:
        profile.can.tx_id = args.tx
    if args.rx is not None:
        profile.can.rx_id = args.rx

    regions = list(profile.memory_map)
    if not regions:
        print("profile has no memory_map regions to back up", file=sys.stderr)
        return 2

    bus, sim = _open_bus(args, profile)
    uds = None
    try:
        uds = _build_uds(bus, profile)
        uds.start_tester_present(profile.timing.tester_present_period)
        try:
            uds.enter_extended_session()
        except Med17FlasherError:
            pass
        if not args.no_secure:
            try:
                resolver = _seedkey_resolver(args, profile)
                seed = uds.request_seed(profile.security.request_seed_level)
                if any(seed):
                    key = resolver.compute(profile.name, profile.security.request_seed_level, seed)
                    uds.send_key(profile.security.send_key_level, key)
                    print(f"security access granted "
                          f"(level 0x{profile.security.request_seed_level:02X})")
            except Med17FlasherError as exc:
                print(f"security access failed ({exc}); reading anyway ...", file=sys.stderr)

        chunk = args.chunk
        combined = bytearray()
        manifest = []
        for r in regions:
            print(f"reading {r.name}: 0x{r.size:X} bytes @ 0x{r.start:08X} ...")
            data = bytearray()
            while len(data) < r.size:
                n = min(chunk, r.size - len(data))
                block = uds.read_memory_by_address(r.start + len(data), n)
                if not block:
                    print(f"\nECU returned no data at 0x{r.start + len(data):08X}",
                          file=sys.stderr)
                    return 1
                data.extend(block)
                pct = int(100 * len(data) / r.size)
                sys.stdout.write(f"\r  {r.name}: {pct:3d}%")
                sys.stdout.flush()
            sys.stdout.write("\n")
            crc = zlib.crc32(bytes(data[:r.size])) & 0xFFFFFFFF
            manifest.append((r.name, r.start, r.size, len(combined), crc))
            combined.extend(data[:r.size])

        with open(args.output, "wb") as fh:
            fh.write(bytes(combined))
        total_crc = zlib.crc32(bytes(combined)) & 0xFFFFFFFF
        man_path = os.path.splitext(args.output)[0] + ".manifest.txt"
        with open(man_path, "w", encoding="utf-8") as fh:
            fh.write(f"# full backup of {profile.name}\n")
            fh.write(f"# {os.path.basename(args.output)}  {len(combined)} bytes  "
                     f"crc32=0x{total_crc:08X}\n")
            fh.write("# region\taddress\tsize\tfile_offset\tcrc32\n")
            for name, start, size, off, crc in manifest:
                fh.write(f"{name}\t0x{start:08X}\t{size}\t@{off}\tcrc32=0x{crc:08X}\n")

        print(f"backup complete: {len(combined)} bytes ({len(manifest)} region(s)) "
              f"-> {args.output}")
        print(f"  total CRC32 0x{total_crc:08X}   manifest -> {man_path}")
        return 0
    except Med17FlasherError as exc:
        print(f"\nbackup failed: {exc}", file=sys.stderr)
        print("  note: a full read usually needs a programming/extended session and "
              "Security Access (on by default; --no-secure to skip), or bench/boot mode.",
              file=sys.stderr)
        return 1
    finally:
        if uds is not None:
            try:
                uds.stop_tester_present()
            except Exception:  # noqa: BLE001
                pass
        if sim:
            sim.stop()
        bus.close()


def cmd_checksum(args) -> int:
    """Verify or correct MEDC17/EDC17 internal flash checksums."""

    from .core import medc17_checksum as mc

    with open(args.input, "rb") as fh:
        data = fh.read()

    if args.action == "verify":
        results = mc.verify(data)
        if not results:
            print("no MEDC17 checksum blocks found "
                  "(is this a raw MED17/EDC17 flash dump?)")
            return 1
        bad = 0
        print(f"Found {len(results)} checksum region(s):")
        for res in results:
            r = res.region
            status = "OK " if res.ok else "BAD"
            if not res.ok:
                bad += 1
            print(f"  [{status}] {r.algo_name:5s} 0x{r.start_mem:08X}..0x{r.end_mem:08X} "
                  f"computed=0x{res.computed:08X} target=0x{res.target:08X}")
        print(f"{len(results) - bad} OK, {bad} need correction")
        return 0 if bad == 0 else 2

    # correct
    if not args.output:
        print("checksum correct needs -o/--output", file=sys.stderr)
        return 2
    fixed, before = mc.correct(data)
    bad_before = [r for r in before if not r.ok]
    after = mc.verify(fixed)
    bad_after = [r for r in after if not r.ok]
    with open(args.output, "wb") as fh:
        fh.write(fixed)
    changed = sum(1 for i in range(min(len(data), len(fixed))) if data[i] != fixed[i])
    print(f"{args.input} -> {args.output}")
    print(f"  regions: {len(before)}  corrected: {len(bad_before)}  "
          f"still bad: {len(bad_after)}  bytes changed: {changed}")
    for res in after:
        r = res.region
        print(f"  [{'OK ' if res.ok else 'BAD'}] {r.algo_name:5s} "
              f"0x{r.start_mem:08X}..0x{r.end_mem:08X}")
    return 0 if not bad_after else 1


def cmd_convert(args) -> int:
    """Convert any firmware container (bin/Intel-HEX/S-Record) to a flat .bin."""

    if args.inflate:
        from .core.compression import inflate

        with open(args.input, "rb") as fh:
            raw = fh.read()
        data, mode = inflate(raw, offset=args.offset, mode=args.mode)
        with open(args.output, "wb") as fh:
            fh.write(data)
        print(f"{args.input} -> {args.output} (inflated {mode}, offset 0x{args.offset:X})")
        print(f"  {len(raw)} -> {len(data)} bytes")
        return 0

    from .core import load_firmware
    from .core.firmware import save_binary

    image = load_firmware(args.input, base_address=args.base)
    low, high = image.span
    save_binary(image, args.output, fill=args.fill)
    size = os.path.getsize(args.output)
    print(f"{args.input} -> {args.output}")
    print(f"  span 0x{low:08X}..0x{high:08X}, {size} bytes ({len(image.segments)} segment(s))")
    return 0


def cmd_inflate(args) -> int:
    """Inflate DEFLATE/zlib/gzip data (e.g. a Mercedes flash section) to raw bytes."""

    from .core.compression import inflate, inflate_sections

    with open(args.input, "rb") as fh:
        raw = fh.read()
    if args.sections:
        parts = inflate_sections(raw, offset=args.offset)
        if not parts:
            print("no inflatable sections found", file=sys.stderr)
            return 1
        blob = b"".join(parts)
        with open(args.output, "wb") as fh:
            fh.write(blob)
        print(f"{args.input} -> {args.output}: {len(parts)} section(s), {len(blob)} bytes total")
        for i, part in enumerate(parts):
            print(f"  section {i}: {len(part)} bytes")
        return 0
    data, mode = inflate(raw, offset=args.offset, mode=args.mode)
    with open(args.output, "wb") as fh:
        fh.write(data)
    print(f"{args.input} -> {args.output} (inflated {mode}, offset 0x{args.offset:X}): "
          f"{len(raw)} -> {len(data)} bytes")
    return 0


# Known med1775 calibration variants: transferAddress -> (offset in a full read, length)
_CAL_VARIANTS = {
    0x84002000: (0x00402000, 0x000FE000),
    0x80140000: (0x00140000, 0x000C0000),
}


def cmd_extract_calibration(args) -> int:
    """Slice a flashable calibration out of a full ECU read and validate it.

    Mirrors the production logic: for a full read, take
    ``data[offset : offset + length]`` for the chosen transferAddress; the result
    must start with 0x60 and end with 0xDE.
    """

    from .core.firmware import validate_calibration

    with open(args.input, "rb") as fh:
        data = fh.read()

    if args.address in _CAL_VARIANTS and (args.offset is None or args.length is None):
        offset, length = _CAL_VARIANTS[args.address]
    else:
        if args.offset is None or args.length is None:
            print("unknown --address; pass --offset and --length explicitly",
                  file=sys.stderr)
            return 2
        offset, length = args.offset, args.length

    if args.full_read:
        cal = data[offset : offset + length]
    else:
        cal = data[:length]

    print(f"transferAddress 0x{args.address:08X}: offset 0x{offset:X}, length 0x{length:X}")
    if len(cal) != length:
        print(f"ERROR: sliced {len(cal)} bytes, expected 0x{length:X}", file=sys.stderr)
        return 1
    try:
        validate_calibration(cal, length=length)
    except Med17FlasherError as exc:
        print(f"WARNING: calibration signature check failed: {exc}", file=sys.stderr)
        if not args.force:
            print("  refusing to write (use --force to override)", file=sys.stderr)
            return 1
    with open(args.output, "wb") as fh:
        fh.write(cal)
    print(f"wrote {len(cal)} bytes -> {args.output} "
          f"(starts 0x{cal[0]:02X}, ends 0x{cal[-1]:02X})")
    return 0


def cmd_scan(args) -> int:
    from .recon import EcuScanner

    profile = _load_profile_arg(args.profile)
    if args.tx is not None:
        profile.can.tx_id = args.tx
    if args.rx is not None:
        profile.can.rx_id = args.rx
    bus, sim = _open_bus(args, profile)
    try:
        uds = _build_uds(bus, profile)
        scanner = EcuScanner(uds, profile.can.tx_id, profile.can.rx_id)
        print(f"Scanning ECU on {args.backend if not args.simulator else 'simulator'} "
              f"(tx=0x{profile.can.tx_id:03X} rx=0x{profile.can.rx_id:03X}) ...")
        report = scanner.scan(
            probe_programming_session=args.deep,
            read_memory_at=(args.read_memory if args.read_memory is not None else None),
        )
        print(report.text_summary())
        if args.emit_profile:
            _dump_profile(report.to_profile(), args.emit_profile)
            print(f"\nwrote profile skeleton -> {args.emit_profile}")
        if args.emit_seeds and report.seeds:
            with open(args.emit_seeds, "w", encoding="utf-8") as fh:
                for level, info in report.seeds.items():
                    if info.startswith("seed "):
                        fh.write(f"# level 0x{level:02X}\n{info.split()[1]}\n")
            print(f"wrote seeds -> {args.emit_seeds}")
            print("Note: seeds alone cannot recover the algorithm - you also need the "
                  "matching keys. Capture a real flash (med17flasher capture) to get pairs.")
        return 0 if report.online else 1
    except Med17FlasherError as exc:
        print(f"scan failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if sim:
            sim.stop()
        bus.close()


def cmd_measure(args) -> int:
    """XCP measurement / logging (poll or DAQ) of live ECU values."""

    import math
    import time as _t

    from .core import VirtualCanNetwork
    from .xcp import (
        DaqMeasurement,
        PollingMeasurement,
        XcpClient,
        XcpOnCan,
        VirtualXcpSlave,
        configure_daq,
        parse_signal,
    )

    specs = list(args.signal or [])
    if args.signals_file:
        with open(args.signals_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    specs.append(line)

    # --a2l: pull the XCP CAN ids (and, with --find, the signals) from an A2L,
    # so the operator doesn't type --cro/--dto or hand-write signal specs.
    a2l_signals = []
    if getattr(args, "a2l", None):
        from .a2l import load_a2l

        try:
            a2l = load_a2l(args.a2l)
        except Med17FlasherError as exc:
            print(f"a2l failed: {exc}", file=sys.stderr)
            return 1
        if a2l.xcp and a2l.xcp.transport == "can" and a2l.xcp.can_id_master is not None:
            # Only override ids the user left at the defaults.
            if args.cro == 0x7E0:
                args.cro = a2l.xcp.can_id_master
            if args.dto == 0x7E1:
                args.dto = a2l.xcp.can_id_slave
            if a2l.xcp.is_extended:
                args.extended = True
            print(f"XCP-on-CAN from A2L: CRO 0x{args.cro:X} / DTO 0x{args.dto:X}"
                  + ("  (29-bit)" if a2l.xcp.is_extended else ""))
        if args.find:
            names = [m.name for m in a2l.find(args.find)]
            a2l_signals = a2l.to_signals(names)
            print(f"{len(a2l_signals)} signal(s) selected from {args.a2l}"
                  f" matching {args.find!r}")

    sim = None
    if args.simulator:
        net = VirtualCanNetwork()
        slave = VirtualXcpSlave(net.new_endpoint("ecu"), args.cro, args.dto,
                                byte_order=args.byte_order, daq_period=0.02)
        t0 = _t.monotonic()

        def provider(addr, size):
            if addr == 0x2000:  # a wandering engine speed
                rpm = int(3200 + 2600 * math.sin((_t.monotonic() - t0) * 1.3))
                return max(0, rpm).to_bytes(2, args.byte_order)
            return None

        slave.provider = provider
        slave.set_bytes(0x2002, (900).to_bytes(2, args.byte_order))    # coolant raw
        slave.set_bytes(0x2004, (13800).to_bytes(2, args.byte_order))  # battery mV
        slave.start()
        bus = net.new_endpoint("tester")
        sim = slave
        if not specs:
            specs = ["rpm@0x2000:u16", "coolant@0x2002:s16:0.1:-40:degC",
                     "battery@0x2004:u16:0.001:0:V"]
    else:
        bus = create_bus(args.backend)

    if not specs and not a2l_signals:
        print("no signals given; use --signal name@addr:type (repeatable), "
              "--signals-file FILE, or --a2l FILE --find PATTERN", file=sys.stderr)
        return 2

    try:
        signals = a2l_signals + [parse_signal(s) for s in specs]
    except ValueError as exc:
        print(f"bad signal spec: {exc}", file=sys.stderr)
        return 2

    tp = XcpOnCan(bus, args.cro, args.dto, is_extended_id=args.extended,
                  timeout=args.timeout, pad_to=(8 if args.pad else None))
    client = XcpClient(tp)
    names = [s.name for s in signals]
    units = {s.name: s.unit for s in signals}

    def on_sample(sample) -> None:
        cells = "  ".join(f"{n}={sample.values[n]:.2f}{units[n]}" for n in names)
        sys.stdout.write("\r  t=%7.2fs  %s        " % (sample.t, cells))
        sys.stdout.flush()

    try:
        info = client.connect()
        where = "simulator" if args.simulator else args.backend
        print(f"XCP connected on {where} "
              f"(MAX_CTO={info['maxCto']}, byte order {info['byteOrder']}, "
              f"{len(signals)} signal(s), {'DAQ' if args.daq else 'polling'})")
        if args.daq:
            layout = configure_daq(client, signals, event=args.event,
                                   prescaler=args.prescaler)
            meas = DaqMeasurement(client, layout)
            rows = meas.run(duration=args.duration, max_samples=args.samples,
                            callback=on_sample, csv_path=args.csv)
        else:
            meas = PollingMeasurement(client, signals)
            rows = meas.run(rate_hz=args.rate, duration=args.duration,
                            max_samples=args.samples, callback=on_sample,
                            csv_path=args.csv)
        sys.stdout.write("\n")
        print(f"captured {len(rows)} sample(s)"
              + (f" -> {args.csv}" if args.csv else ""))
        return 0
    except KeyboardInterrupt:
        sys.stdout.write("\nstopped.\n")
        return 0
    except Med17FlasherError as exc:
        print(f"\nmeasure failed: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            if client.connected:
                client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        if sim:
            sim.stop()
        bus.close()


def cmd_a2l(args) -> int:
    """Inspect an ASAP2/A2L description and turn it into XCP signal specs."""

    from .a2l import load_a2l

    try:
        a2l = load_a2l(args.file)
    except Med17FlasherError as exc:
        print(f"a2l failed: {exc}", file=sys.stderr)
        return 1

    print(f"{args.file}: project {a2l.project or '-'} / module {a2l.module or '-'}")
    print(f"  {len(a2l.measurements)} measurement(s), "
          f"{len(a2l.characteristics)} characteristic(s), "
          f"{len(a2l.compu_methods)} conversion(s)")
    for warning in a2l.warnings[:5]:
        print(f"  warning: {warning}")
    if len(a2l.warnings) > 5:
        print(f"  ... {len(a2l.warnings) - 5} more warning(s)")

    if a2l.xcp:
        x = a2l.xcp
        if x.transport == "can" and x.can_id_master is not None:
            print(f"  XCP-on-CAN: CRO 0x{x.can_id_master:X} / DTO "
                  f"0x{x.can_id_slave:X}" + (f" @ {x.baudrate}" if x.baudrate else "")
                  + "  (use: med17flasher xcp --a2l ...)")
        elif x.transport == "udp":
            print("  XCP-on-UDP (Ethernet) - CAN transport args not applicable")
        if x.events:
            rasters = ", ".join(
                f"#{e.number} {e.name}" + (f" ({e.period_s * 1000:g}ms)"
                                           if e.period_s else "")
                for e in x.events[:6])
            print(f"  DAQ events: {rasters}"
                  + (" ..." if len(x.events) > 6 else ""))

    rows = a2l.find(args.find if args.find else "*")
    shown = rows if args.limit <= 0 else rows[: args.limit]
    if shown:
        print()
        print(f"{'name':<32} {'address':<10} {'type':<4} {'factor':>12} "
              f"{'offset':>12}  unit")
        for meas in shown:
            flag = "  (non-linear)" if meas.nonlinear else ""
            print(f"{meas.name:<32} 0x{meas.address:08X} {meas.dtype:<4} "
                  f"{meas.factor:>12g} {meas.offset:>12g}  {meas.unit}{flag}")
        if len(rows) > len(shown):
            print(f"... {len(rows) - len(shown)} more (use --limit 0 to list all)")
    else:
        print("\nno measurement matches" + (f" {args.find!r}" if args.find else ""))

    if args.emit_signals:
        try:
            written, skipped = _write_signal_specs(args.emit_signals, rows, args.file)
        except OSError as exc:
            print(f"cannot write {args.emit_signals}: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {written} signal spec(s) -> {args.emit_signals}")
        print(f"  feed it back with: med17flasher xcp --signals-file "
              f"{args.emit_signals}")
        if skipped:
            print(f"  {skipped} name(s) skipped: ':' and '@' are spec separators")
    return 0


def _write_signal_specs(path: str, measurements, source: str):
    """Write ``name@0xADDR:dtype:factor:offset:unit`` lines for ``xcp --signals-file``."""

    written = skipped = 0
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"# XCP signal specs generated by med17flasher a2l from {source}\n")
        for meas in measurements:
            if ":" in meas.name or "@" in meas.name:
                skipped += 1  # the spec grammar would be ambiguous
                continue
            if meas.nonlinear:
                # The conversion could not be folded into factor/offset, so the
                # emitted spec reports raw counts - say so in the file.
                fh.write(f"# {meas.name}: non-linear conversion, raw counts below\n")
            unit = meas.unit.replace(":", "").strip()
            fh.write(f"{meas.name}@0x{meas.address:08X}:{meas.dtype}:"
                     f"{meas.factor:g}:{meas.offset:g}:{unit}\n")
            written += 1
    return written, skipped


def cmd_capture(args) -> int:
    import threading as _threading

    from .core import capture_frames, write_candump
    from .core.trace import analyze

    profile = _load_profile_arg(args.profile)
    bus, sim = _open_bus(args, profile)
    stop = _threading.Event()
    count = {"n": 0}

    def on_frame(_tf):
        count["n"] += 1
        if count["n"] % 200 == 0:
            sys.stdout.write(f"\r  captured {count['n']} frames ...")
            sys.stdout.flush()

    try:
        where = "simulator" if args.simulator else args.backend
        print(f"Capturing CAN frames on {where} "
              f"for {args.seconds or '∞'}s (Ctrl+C to stop) ...")
        try:
            frames = capture_frames(bus, seconds=args.seconds, stop_event=stop, on_frame=on_frame)
        except KeyboardInterrupt:
            stop.set()
            frames = []
        sys.stdout.write("\n")
        write_candump(frames, args.output)
        print(f"wrote {len(frames)} frames -> {args.output}")
        if args.analyze and frames:
            report = analyze(frames, tx_id=profile.can.tx_id, rx_id=profile.can.rx_id)
            print(f"  quick analysis: {report.request_count} requests, "
                  f"{len(report.seed_key_pairs)} seed/key pair(s), "
                  f"{len(report.download_blocks)} download block(s)")
            print(f"  run: med17flasher analyze-trace {args.output} --emit-profile ecu.yaml")
        return 0
    finally:
        if sim:
            sim.stop()
        bus.close()


def cmd_sniff(args) -> int:
    """Passively sniff another flasher's read/write session and reverse it.

    The intended rig: split the OBD2 line so the ECU sees both the *other*
    tool (e.g. an Autotuner) and our Tactrix at once. The other tool does the
    real read/write; we only listen. We inject no frames and never act as a
    tester, so we cannot disturb that session - critical, since two masters
    fighting on one bus mid-write would brick the ECU. (A CAN controller still
    ACKs received frames electrically; use a listen-only interface for true
    silence.) Live UDS decode gives you
    confidence it is really capturing; when it ends we reassemble the whole
    recording and derive the ECU profile (memory map, routines) and the
    seed/key pairs, exactly like `analyze-trace`.
    """

    import threading as _threading

    from .core import capture_frames, write_candump
    from .core.trace import LiveUdsTracker, analyze

    profile = _load_profile_arg(args.profile)
    tx_id = args.tx if args.tx is not None else profile.can.tx_id
    rx_id = args.rx if args.rx is not None else profile.can.rx_id

    try:
        bus, sim = _open_sniff_bus(args, profile)
    except Exception as exc:  # noqa: BLE001
        print(f"cannot open the sniff interface: {exc}", file=sys.stderr)
        print("  tip: `med17flasher backends` lists installed J2534 interfaces; "
              "`med17flasher j2534 --listen 5` proves the wiring first.",
              file=sys.stderr)
        return 1

    tracker = LiveUdsTracker()
    stop = _threading.Event()
    seen_ids: dict = {}
    counter = {"n": 0}

    def on_frame(tf):
        counter["n"] += 1
        seen_ids[tf.arbitration_id] = seen_ids.get(tf.arbitration_id, 0) + 1
        for ev in tracker.feed(tf):
            # Redraw over the running frame counter, then reprint it.
            sys.stdout.write("\r" + " " * 48 + "\r")
            print(f"  [{tf.timestamp:9.3f}] {ev.text}")
        if counter["n"] % 25 == 0:
            sys.stdout.write(f"\r  ... {counter['n']} Frames, "
                             f"{len(seen_ids)} CAN-IDs mitgeschnitten")
            sys.stdout.flush()

    where = "simulator" if getattr(args, "simulator", False) else args.backend
    print(f"Sniffe passiv auf {where} "
          f"(nur lesen, injiziert keine Frames) - {args.seconds or '∞'}s, Ctrl+C stoppt.")
    print("  Starte jetzt am anderen Tool (Autotuner) den Lese-/Schreibvorgang.\n")
    try:
        try:
            frames = capture_frames(bus, seconds=args.seconds, stop_event=stop,
                                    on_frame=on_frame)
        except KeyboardInterrupt:
            stop.set()
            frames = capture_frames(bus, seconds=0.0, stop_event=stop)  # drain
    finally:
        if sim:
            sim.stop()
        bus.close()

    sys.stdout.write("\r" + " " * 48 + "\r")
    print(f"\nMitschnitt beendet: {len(frames)} Frames, {len(seen_ids)} CAN-IDs.")
    if seen_ids:
        top = sorted(seen_ids.items(), key=lambda kv: -kv[1])[:8]
        print("  aktivste IDs: "
              + ", ".join(f"0x{i:03X}(x{c})" for i, c in top))

    write_candump(frames, args.output)
    print(f"  Rohmitschnitt -> {args.output}")

    if args.no_analyze or not frames:
        if not frames:
            print("  keine Frames - Zuendung an? OBD-Kabel/Splitter? richtige Baudrate "
                  f"(--baudrate, aktuell {args.baudrate})? richtiger --backend?")
        return 0

    # Auto-detect the busiest request/response id pair if the profile's guess
    # produced nothing - the sniffed tool may use non-standard addressing.
    report = analyze(frames, tx_id=tx_id, rx_id=rx_id)
    if report.request_count == 0 and len(seen_ids) >= 2:
        guess_tx, guess_rx = _guess_uds_ids(seen_ids)
        if guess_tx is not None and (guess_tx, guess_rx) != (tx_id, rx_id):
            print(f"  keine UDS-Requests auf 0x{tx_id:03X} - versuche erkannte "
                  f"IDs 0x{guess_tx:03X}/0x{guess_rx:03X}")
            tx_id, rx_id = guess_tx, guess_rx
            report = analyze(frames, tx_id=tx_id, rx_id=rx_id)

    print(f"\nAnalyse (Request 0x{tx_id:03X} / Response 0x{rx_id:03X}):")
    print(f"  {report.request_count} Requests / {report.response_count} Responses")
    print(f"  Sitzungen        : {[hex(s) for s in report.sessions]}")
    print(f"  Security-Level    : {[hex(lvl) for lvl in report.security_levels]}")
    print(f"  Seed/Key-Paare    : {len(report.seed_key_pairs)}")
    for level, seed, key in report.seed_key_pairs:
        print(f"      L0x{level:02X}: seed={seed.hex()} key={key.hex()}")
    print(f"  erase-Routine     : "
          f"{hex(report.erase_routine) if report.erase_routine else '-'}")
    print(f"  checkMemory       : "
          f"{hex(report.check_memory_routine) if report.check_memory_routine else '-'}")
    print(f"  Download-Bloecke  : {len(report.download_blocks)}")
    for b in report.download_blocks:
        print(f"      0x{b.address:08X}  {b.size} Bytes  ({b.transfers} Transfers)")
    if report.dids:
        print(f"  gelesene DIDs     : {[hex(d) for d in report.dids]}")

    emit_profile = args.emit_profile or (os.path.splitext(args.output)[0] + ".profile.yaml")
    profile_out = report.to_profile()
    _dump_profile(profile_out, emit_profile)
    print(f"\n  abgeleitetes Profil -> {emit_profile}")

    if report.seed_key_pairs:
        emit_pairs = args.emit_pairs or (os.path.splitext(args.output)[0] + ".pairs.txt")
        with open(emit_pairs, "w", encoding="utf-8") as fh:
            for level, seed, key in report.seed_key_pairs:
                fh.write(f"{seed.hex()} {key.hex()}\n")
        print(f"  Seed/Key-Paare   -> {emit_pairs}")
        try:
            from .seedkey import SeedKeySolver

            solver_pairs = report.seed_key_pairs_for_solver()
            distinct = {p.seed for p in solver_pairs}
            levels = {lvl for lvl, _s, _k in report.seed_key_pairs}
            level = next(iter(levels)) if len(levels) == 1 else 0
            full = [r for r in SeedKeySolver(solver_pairs).solve(level=level)
                    if r.is_full_match]
            if len(distinct) >= 2 and len(full) == 1:
                print(f"  Seed/Key-Algorithmus erkannt: {full[0].algorithm} "
                      f"{full[0].params}")
            else:
                print(f"  {len(solver_pairs)} Paar(e) extrahiert; fuer die "
                      f"Algorithmus-Rekonstruktion ein paar mehr (andere Seeds) "
                      f"sniffen, dann: med17flasher seedkey-solve --pairs {emit_pairs}")
        except Exception:  # noqa: BLE001
            pass

    print("\n  Naechster Schritt: Profil pruefen und mit "
          f"`med17flasher flash --dry-run --profile {emit_profile} ...` einen "
          "risikofreien Probelauf fahren.")
    return 0


def _guess_uds_ids(seen_ids: dict):
    """Guess the (request, response) diagnostic id pair from observed traffic.

    UDS on a powertrain bus is almost always an id pair 8 apart (0x7E0/0x7E8,
    0x7E1/0x7E9, ...). Pick the busiest such pair; fall back to the two most
    active ids.
    """

    ids = set(seen_ids)
    best = None
    for tx in sorted(ids):
        rx = tx + 8
        if rx in ids:
            score = seen_ids[tx] + seen_ids[rx]
            if best is None or score > best[0]:
                best = (score, tx, rx)
    if best is not None:
        return best[1], best[2]
    ranked = sorted(seen_ids.items(), key=lambda kv: -kv[1])
    if len(ranked) >= 2:
        a, b = ranked[0][0], ranked[1][0]
        return (min(a, b), max(a, b))
    return None, None


def _open_sniff_bus(args, profile):
    """Open a passive listening bus for `sniff`, defaulting to J2534/Tactrix."""

    if getattr(args, "simulator", False):
        return _open_bus(args, profile)

    backend = (args.backend or "j2534").strip()
    head = backend.partition(":")[0].lower()
    if head in ("j2534", "passthru", "tactrix", "openport"):
        # Route through create_bus so the 32-bit bridge fallback applies, and
        # carry the device/baudrate/extended options the user gave us.
        target = backend.partition(":")[2] or getattr(args, "device", "") or ""
        head = "j2534" if head in ("j2534", "passthru") else head
        spec = f"{head}:{target}" if target else head
        return create_bus(spec, baudrate=args.baudrate, extended=args.extended), None
    return create_bus(backend), None


def cmd_seedkey(args) -> int:
    seed = bytes.fromhex(args.seed.replace(" ", ""))
    params = {}
    for item in args.param or []:
        key, _, value = item.partition("=")
        params[key] = value
    if args.dll or args.exe:
        from .seedkey import make_backend

        spec = f"dll:{args.dll}" if args.dll else f"exe:{args.exe}"
        kwargs = {"options": args.options} if args.dll else {}
        key = make_backend(spec, **kwargs).compute(seed, level=args.level, params=params)
    elif args.store:
        store = SeedKeyStore.load(args.store)
        key = store.compute(args.ecu, args.level, seed)
    else:
        key = compute_key(args.algorithm, seed, level=args.level, params=params)
    print(key.hex())
    return 0


def cmd_seedkey_solve(args) -> int:
    from .seedkey import SeedKeySolver, load_pairs, load_wordlist
    from .seedkey.solver import SeedKeyPair

    pairs = []
    if args.pairs:
        pairs.extend(load_pairs(args.pairs))
    for item in args.pair or []:
        seed_hex, _, key_hex = item.partition(":")
        if not key_hex:
            print(f"bad --pair {item!r}, expected seedhex:keyhex", file=sys.stderr)
            return 2
        pairs.append(SeedKeyPair.from_hex(seed_hex, key_hex))
    if not pairs:
        print("no pairs given; use --pairs FILE or --pair seed:key", file=sys.stderr)
        return 2

    wordlist = load_wordlist(args.wordlist) if args.wordlist else None
    solver = SeedKeySolver(pairs)
    results = solver.solve(level=args.level, wordlist=wordlist)

    if not results:
        print(f"No candidate reproduced the {len(pairs)} pair(s).")
        print("Tips: supply more/again-captured pairs, a --wordlist of likely "
              "32-bit constants, or plug the routine in directly (--plugin).")
        return 1

    print(f"Analysed {len(pairs)} pair(s):")
    full = [r for r in results if r.is_full_match]
    for r in results[:10]:
        print(f"  {r}")
    # Ambiguity guard: a single pair (or too few) is trivially matched by the
    # linear families (xor/add/sum), which masks the true algorithm.
    if len(pairs) < 2:
        print("\nWARNING: only one pair - xor/add/sum match ANY single pair "
              "trivially. Capture more pairs with DIFFERENT seeds to disambiguate.")
    elif len(full) > 1:
        print(f"\nWARNING: {len(full)} algorithms reproduce all pairs "
              f"({', '.join(r.algorithm for r in full)}). Capture more pairs "
              f"with different seeds to disambiguate.")
    if full:
        best = full[0]
        print("\nRECOVERED (best candidate):" if len(full) > 1 else "\nRECOVERED:")
        print(f"  algorithm = {best.algorithm}")
        print(f"  params    = {best.params}")
        print(f"  level     = 0x{best.level:X}")
        if args.emit_store:
            import json as _json

            entry = [{
                "ecu": args.ecu, "level": best.level,
                "algorithm": best.algorithm, "params": best.params,
                "note": "recovered by seedkey-solve",
            }]
            with open(args.emit_store, "w", encoding="utf-8") as fh:
                _json.dump(entry, fh, indent=2)
            print(f"  wrote seed/key store -> {args.emit_store}")
        return 0
    print("\nNo full match - best candidate above is partial.")
    return 1


def cmd_analyze_trace(args) -> int:

    from .core.trace import analyze, read_trace

    frames = read_trace(args.trace)
    if not frames:
        print(f"no CAN frames parsed from {args.trace!r}", file=sys.stderr)
        return 1
    report = analyze(frames, tx_id=args.tx, rx_id=args.rx)
    print(f"Trace {args.trace}: {len(frames)} frames, "
          f"{report.request_count} requests / {report.response_count} responses")
    print(f"  sessions       : {[hex(s) for s in report.sessions]}")
    print(f"  security levels: {[hex(lvl) for lvl in report.security_levels]}")
    print(f"  seed/key pairs : {len(report.seed_key_pairs)}")
    for level, seed, key in report.seed_key_pairs:
        print(f"      level 0x{level:02X}: seed={seed.hex()} key={key.hex()}")
    print(f"  erase routine  : {hex(report.erase_routine) if report.erase_routine else '-'}")
    print(f"  checkMemory    : {hex(report.check_memory_routine) if report.check_memory_routine else '-'}")
    print(f"  download blocks: {len(report.download_blocks)}")
    for b in report.download_blocks:
        print(f"      0x{b.address:08X}  {b.size} bytes  ({b.transfers} transfers)")
    if report.dids:
        print(f"  DIDs read      : {[hex(d) for d in report.dids]}")

    if args.emit_profile:
        profile = report.to_profile()
        _dump_profile(profile, args.emit_profile)
        print(f"  wrote profile -> {args.emit_profile}")
    if args.emit_pairs and report.seed_key_pairs:
        with open(args.emit_pairs, "w", encoding="utf-8") as fh:
            for level, seed, key in report.seed_key_pairs:
                fh.write(f"{seed.hex()} {key.hex()}\n")
        print(f"  wrote seed/key pairs -> {args.emit_pairs}")
        # opportunistically try to recover the algorithm, but only claim it
        # when it is unambiguous (>=2 pairs with distinct seeds, one full match).
        try:
            from .seedkey import SeedKeySolver

            solver_pairs = report.seed_key_pairs_for_solver()
            distinct_seeds = {p.seed for p in solver_pairs}
            levels = {lvl for lvl, _s, _k in report.seed_key_pairs}
            level = next(iter(levels)) if len(levels) == 1 else 0
            results = SeedKeySolver(solver_pairs).solve(level=level)
            full = [r for r in results if r.is_full_match]
            if len(distinct_seeds) >= 2 and len(full) == 1:
                print(f"  seed/key recovered: {full[0].algorithm} {full[0].params}")
            elif solver_pairs:
                print(f"  {len(solver_pairs)} seed/key pair(s) extracted; capture a "
                      f"few more (different seeds) then run: med17flasher seedkey-solve "
                      f"--pairs {args.emit_pairs}")
        except Exception:  # noqa: BLE001
            pass
    return 0


def cmd_analyze_firmware(args) -> int:
    from .core import detect_regions, load_firmware

    image = load_firmware(args.firmware, base_address=args.base)
    low, high = image.span
    print(f"Firmware {args.firmware}: {image.total_size} bytes, "
          f"span 0x{low:08X}..0x{high:08X}, {len(image.segments)} segment(s)")
    regions = detect_regions(image, min_gap=args.min_gap, align=args.align)
    print(f"Detected {len(regions)} region(s):")
    for i, (start, size) in enumerate(regions, 1):
        print(f"  BLOCK{i}: 0x{start:08X}  {size} bytes (0x{size:X})")
    print(f"Whole-image CRC32: 0x{image.checksum('crc32'):08X}")

    if args.emit_profile:
        from .core.ecu_profile import MemoryRegion, builtin_med17_7_5

        profile = builtin_med17_7_5()
        profile.memory_map = [
            MemoryRegion(f"BLOCK{i}", start, size, checksum="crc32")
            for i, (start, size) in enumerate(regions, 1)
        ]
        profile.description = f"Derived from firmware dump {os.path.basename(args.firmware)}"
        _dump_profile(profile, args.emit_profile)
        print(f"wrote profile -> {args.emit_profile}")
    return 0


def cmd_ingest(args) -> int:
    """Scan a folder and auto-process firmware / traces / seed-key pair files."""

    root = args.dir
    if not os.path.isdir(root):
        print(f"no such directory: {root}", file=sys.stderr)
        return 2
    entries = sorted(os.listdir(root))
    if not entries:
        print(f"{root} is empty - drop firmware (.bin/.hex/.s19), CAN traces "
              f"(.log/.asc/.csv) or seed/key pair files (.txt/.json) there.")
        return 0

    fw_ext = {".bin", ".hex", ".ihex", ".s19", ".srec", ".mot", ".frf", ".odx"}
    trace_ext = {".log", ".asc", ".trc", ".candump"}
    handled = 0
    for name in entries:
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(name)[1].lower()
        stem = os.path.splitext(name)[0]
        print(f"\n=== {name} ===")
        try:
            if ext in trace_ext or (ext == ".csv"):
                ns = argparse.Namespace(
                    trace=path, tx=args.tx, rx=args.rx,
                    emit_profile=os.path.join(root, f"{stem}.profile.yaml"),
                    emit_pairs=os.path.join(root, f"{stem}.pairs.txt"),
                )
                cmd_analyze_trace(ns)
            elif ext in fw_ext:
                ns = argparse.Namespace(
                    firmware=path, base=args.base, min_gap=0x1000, align=0x100,
                    emit_profile=os.path.join(root, f"{stem}.profile.yaml"),
                )
                cmd_analyze_firmware(ns)
                handled += 1
            elif ext in (".txt", ".json"):
                from .seedkey import SeedKeySolver, load_pairs

                pairs = load_pairs(path)
                if pairs:
                    best = SeedKeySolver(pairs).best(level=args.level)
                    if best and best.is_full_match:
                        print(f"  recovered seed/key: {best.algorithm} {best.params}")
                    else:
                        print(f"  {len(pairs)} pair(s) loaded; no full match "
                              f"(try --wordlist with seedkey-solve)")
            else:
                print("  (skipped: unrecognised file type)")
                continue
            handled += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  error: {exc}")
    print(f"\nIngest complete: {handled} file(s) processed. "
          f"Review the generated *.profile.yaml before flashing real hardware.")
    return 0


def _dump_profile(profile, path: str) -> None:
    data = profile.to_dict()
    try:
        import yaml  # type: ignore

        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False)
    except ImportError:
        import json as _json

        with open(path, "w", encoding="utf-8") as fh:
            _json.dump(data, fh, indent=2, default=str)


def cmd_seedkey_server(args) -> int:
    from .seedkey.server import SeedKeyHttpServer, SeedKeyService, SeedKeyTcpServer

    algorithm = None
    bridge = None
    if getattr(args, "bridge", None):
        # Front a *32-bit* DLL from this (possibly 64-bit) process: the DLL is
        # loaded by a 32-bit helper interpreter, not by us.
        from .seedkey import SeedKeyBridge

        bridge = SeedKeyBridge(args.bridge, python32=getattr(args, "python32", None),
                               options=args.options)
        algorithm = bridge.as_algorithm()
        print(f"seed/key backend: bridge:{args.bridge} (via {bridge.python32})")
    elif args.dll or args.exe:
        from .seedkey import make_backend

        spec = f"dll:{args.dll}" if args.dll else f"exe:{args.exe}"
        kwargs = {"options": args.options} if args.dll else {}
        algorithm = make_backend(spec, **kwargs)
        print(f"seed/key backend: {spec}")
    store = SeedKeyStore.load(args.store) if args.store else SeedKeyStore.default()
    service = SeedKeyService(store, algorithm=algorithm)
    http = SeedKeyHttpServer(service, host=args.host, port=args.port)
    tcp = SeedKeyTcpServer(service, host=args.host, port=args.tcp_port)
    http.start(block=False)
    tcp.start(block=False)
    print(
        f"seed/key HTTP on http://{args.host}:{args.port}  "
        f"TCP on {args.host}:{args.tcp_port}\nPress Ctrl+C to stop."
    )
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        http.stop()
        tcp.stop()
        if bridge is not None:
            bridge.close()
    return 0


def cmd_fileserver(args) -> int:
    from .server import FileServer, FirmwareRepository

    repo = FirmwareRepository(args.root)
    server = FileServer(repo, host=args.host, port=args.port, token=args.token)
    print(f"firmware file server on {server.url} (root={args.root})")
    print("Press Ctrl+C to stop.")
    try:
        server.start(block=True)
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        server.stop()
    return 0


def cmd_simulator(args) -> int:
    from .simulator import VirtualEcu, VirtualEcuConfig

    profile = _load_profile_arg(args.profile)
    bus = create_bus(args.backend)
    sim = VirtualEcu(
        bus,
        profile,
        VirtualEcuConfig(
            security_algorithm=profile.security.algorithm,
            security_params=profile.security.params,
        ),
    )
    sim.start()
    print(
        f"virtual MED17.7.5 running on backend {args.backend!r} "
        f"(listening 0x{profile.can.tx_id:03X}, answering 0x{profile.can.rx_id:03X})"
    )
    print("Press Ctrl+C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        sim.stop()
        bus.close()
    return 0


def cmd_backends(args) -> int:
    from .core.can_backends import list_j2534_devices

    print("CAN backends:")
    for name, ok in available_backends().items():
        print(f"  {'[x]' if ok else '[ ]'} {name}")

    devices = list_j2534_devices()
    print("\nJ2534 PassThru interfaces:")
    if devices:
        for device in devices:
            mark = "[x]" if device.get("installed") else "[!]"
            print(f"  {mark} {device.get('name')}"
                  + (f" ({device['vendor']})" if device.get("vendor") else ""))
            print(f"      {device.get('library')}")
            if device.get("protocols"):
                print(f"      protocols: {', '.join(device['protocols'])}")
            if not device.get("installed"):
                print("      driver DLL missing - reinstall the vendor package")
    else:
        print("  (none registered)"
              + ("" if sys.platform == "win32" else " - J2534 drivers are Windows-only"))

    print("\nSeed/key algorithms:")
    for algo in list_algorithms():
        print(f"  - {algo}")
    return 0


def cmd_j2534(args) -> int:
    """Open the interface and report what it says - the pre-flight check."""

    from .core.j2534 import open_j2534

    try:
        bus = open_j2534(args.device or "", baudrate=args.baudrate,
                         extended=args.extended)
    except Exception as exc:  # noqa: BLE001
        print(f"cannot open J2534 interface: {exc}", file=sys.stderr)
        return 1

    try:
        print(f"interface : {bus.name}")
        info = getattr(bus, "info", None) or {}
        if not info and hasattr(bus, "read_version"):
            info = bus.read_version()
        for key in ("firmware", "dll", "api", "python", "bits"):
            if info.get(key):
                print(f"{key:<10}: {info[key]}")
        battery = bus.battery_voltage() if hasattr(bus, "battery_voltage") else None
        if battery is not None:
            print(f"battery   : {battery:.2f} V"
                  + ("  ** too low to flash safely **" if battery < 12.0 else ""))
        if args.listen:
            print(f"\nlistening {args.listen:g}s for CAN traffic ...")
            deadline = time.monotonic() + args.listen
            seen: dict = {}
            while time.monotonic() < deadline:
                frame = bus.recv(timeout=0.2)
                if frame is not None:
                    seen[frame.arbitration_id] = seen.get(frame.arbitration_id, 0) + 1
            if seen:
                print(f"{sum(seen.values())} frames, {len(seen)} ids:")
                for arb, count in sorted(seen.items()):
                    print(f"  0x{arb:03X}  x{count}")
            else:
                print("no traffic - check ignition, the OBD cable and the bit rate")
        return 0
    finally:
        bus.close()


def cmd_profile(args) -> int:
    import json

    profile = _load_profile_arg(args.profile)
    print(json.dumps(profile.to_dict(), indent=2, default=str))
    return 0


def cmd_gui(args) -> int:
    try:
        from .gui.app import main as gui_main
    except Exception as exc:  # noqa: BLE001
        print(f"cannot start GUI: {exc}", file=sys.stderr)
        return 1
    return gui_main(args)


def cmd_desktop(args) -> int:
    from .desktop import main as desktop_main

    return desktop_main(args)


def cmd_webserver(args) -> int:
    from .webserver import FlashService, WebServer

    profile = _load_profile_arg(args.profile) if args.profile else None
    service = FlashService(profile, throttle_kbs=args.throttle, backend=args.backend,
                           firmware_path=args.firmware, allow_write=args.allow_write)
    if args.backend != "simulator":
        print(f"  backend: {args.backend} (real hardware)")
        if not service.allow_write:
            print("  real writing is DISABLED (need --firmware + --allow-write and a "
                  "verified profile); identify works, flashing is gated")
    server = WebServer(service, host=args.host, port=args.port)
    root = server._httpd.static_root  # type: ignore[attr-defined]
    print(f"DME Innovation MED17 Flasher web UI on {server.url}")
    if root:
        print(f"  serving built UI from {root}")
    else:
        print("  UI not built yet - run: cd webui && npm install && npm run build")
        print("  (the JSON/SSE API is already live under /api/)")
    if args.open:
        import webbrowser

        threading.Timer(0.6, lambda: webbrowser.open(server.url)).start()
    print("Press Ctrl+C to stop.")
    try:
        server.start(block=True)
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        server.stop()
    return 0


# --------------------------------------------------------------------------- #
# Argument parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="med17flasher",
        description="Complete MED17.7.5 desktop flasher (UDS/ISO-TP over CAN).",
    )
    parser.add_argument("--version", action="version", version=f"med17flasher {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="-v INFO, -vv DEBUG")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_bus_args(p, with_sim=True):
        p.add_argument("--backend", default="virtual",
                       help="CAN backend spec, e.g. socketcan:can0, pcan:PCAN_USBBUS1, virtual")
        p.add_argument("--profile", help="ECU profile YAML/JSON (defaults to built-in MED17.7.5)")
        if with_sim:
            p.add_argument("--simulator", action="store_true",
                           help="spin up an in-process virtual ECU instead of real hardware")

    # flash
    p = sub.add_parser("flash", help="reprogram an ECU from a firmware file")
    add_bus_args(p)
    p.add_argument("firmware", help="firmware file (.bin/.hex/.s19)")
    p.add_argument("--base", type=lambda x: int(x, 0), default=0,
                   help="base address for raw .bin images")
    p.add_argument("--tx", type=lambda x: int(x, 0), help="override request CAN id")
    p.add_argument("--rx", type=lambda x: int(x, 0), help="override response CAN id")
    p.add_argument("--seedkey-store", help="seed/key store JSON")
    p.add_argument("--seedkey-server", help="seed/key HTTP server URL")
    p.add_argument("--seedkey-dll", help="J2534 seed-key DLL (32-bit Windows)")
    p.add_argument("--seedkey-bridge", metavar="PATH",
                   help="32-bit seed-key DLL served by a helper process "
                        "(use this from 64-bit Python/the desktop build)")
    p.add_argument("--python32", metavar="PATH",
                   help="32-bit python.exe for --seedkey-bridge (auto-detected)")
    p.add_argument("--seedkey-exe", help="external seed-key executable")
    p.add_argument("--seedkey-options", default="", help="option string for the seed-key DLL")
    p.add_argument("--plugin", help="seed/key algorithm plug-in .py to load")
    p.add_argument("--no-identify", action="store_true", help="skip reading identification DIDs")
    p.add_argument("--dry-run", action="store_true",
                   help="rehearse the flash (check file, open session, unlock) but write nothing")
    p.add_argument("--no-unlock", action="store_true",
                   help="with --dry-run, skip Security Access (no seed/key needed)")
    p.set_defaults(func=cmd_flash)

    # identify
    p = sub.add_parser("identify", help="read identification DIDs")
    add_bus_args(p)
    p.set_defaults(func=cmd_identify)

    # read
    p = sub.add_parser("read", help="read (download) a memory range / calibration to a file")
    add_bus_args(p)
    p.add_argument("--address", type=lambda x: int(x, 0), help="start address")
    p.add_argument("--size", type=lambda x: int(x, 0), help="number of bytes")
    p.add_argument("--region", help="read a named memory-map region instead of --address/--size")
    p.add_argument("--chunk", type=lambda x: int(x, 0), default=0x400,
                   help="bytes per ReadMemoryByAddress request (default 0x400)")
    p.add_argument("--secure", action="store_true", help="do Security Access before reading")
    p.add_argument("--tx", type=lambda x: int(x, 0), help="override request CAN id")
    p.add_argument("--rx", type=lambda x: int(x, 0), help="override response CAN id")
    p.add_argument("--seedkey-store", help="seed/key store JSON (with --secure)")
    p.add_argument("--seedkey-server", help="seed/key HTTP server URL (with --secure)")
    p.add_argument("--seedkey-dll", help="J2534 seed-key DLL (with --secure)")
    p.add_argument("--seedkey-bridge", metavar="PATH",
                   help="32-bit seed-key DLL served by a helper process (with --secure)")
    p.add_argument("--python32", metavar="PATH",
                   help="32-bit python.exe for --seedkey-bridge (auto-detected)")
    p.add_argument("--seedkey-exe", help="external seed-key executable (with --secure)")
    p.add_argument("--seedkey-options", default="", help="option string for the seed-key DLL")
    p.add_argument("-o", "--output", required=True)
    p.set_defaults(func=cmd_read)

    # backup — read the whole ECU before writing
    p = sub.add_parser("backup",
                       help="read the ENTIRE ECU (all profile regions) to one .bin "
                            "before flashing — your way back from a bad write")
    add_bus_args(p)
    p.add_argument("--chunk", type=lambda x: int(x, 0), default=0x400,
                   help="bytes per ReadMemoryByAddress request (default 0x400)")
    p.add_argument("--no-secure", action="store_true",
                   help="skip Security Access (some ECUs read without it)")
    p.add_argument("--tx", type=lambda x: int(x, 0), help="override request CAN id")
    p.add_argument("--rx", type=lambda x: int(x, 0), help="override response CAN id")
    p.add_argument("--seedkey-store", help="seed/key store JSON")
    p.add_argument("--seedkey-server", help="seed/key HTTP server URL")
    p.add_argument("--seedkey-dll", help="J2534 seed-key DLL")
    p.add_argument("--seedkey-bridge", metavar="PATH",
                   help="32-bit seed-key DLL served by a helper process")
    p.add_argument("--python32", metavar="PATH",
                   help="32-bit python.exe for --seedkey-bridge (auto-detected)")
    p.add_argument("--seedkey-exe", help="external seed-key executable")
    p.add_argument("--seedkey-options", default="", help="option string for the seed-key DLL")
    p.add_argument("-o", "--output", default="ecu-backup.bin",
                   help="combined backup .bin (default ecu-backup.bin)")
    p.set_defaults(func=cmd_backup)

    # checksum
    p = sub.add_parser("checksum", help="verify/correct MEDC17 internal flash checksums")
    p.add_argument("action", choices=["verify", "correct"])
    p.add_argument("input", help="MED17/EDC17 flash .bin")
    p.add_argument("-o", "--output", help="output .bin (for 'correct')")
    p.set_defaults(func=cmd_checksum)

    # convert
    p = sub.add_parser("convert", help="convert bin/Intel-HEX/S-Record to a flat .bin")
    p.add_argument("input", help="firmware file (.bin/.hex/.s19/.srec)")
    p.add_argument("-o", "--output", required=True, help="output .bin")
    p.add_argument("--base", type=lambda x: int(x, 0), default=0,
                   help="base address for raw .bin inputs")
    p.add_argument("--fill", type=lambda x: int(x, 0), default=0xFF,
                   help="fill byte for gaps (default 0xFF)")
    p.add_argument("--inflate", action="store_true",
                   help="the input is DEFLATE/zlib/gzip compressed - inflate it")
    p.add_argument("--offset", type=lambda x: int(x, 0), default=0,
                   help="start offset of the compressed stream (with --inflate)")
    p.add_argument("--mode", choices=["auto", "raw", "zlib", "gzip"], default="auto",
                   help="compression mode for --inflate (default auto)")
    p.set_defaults(func=cmd_convert)

    # inflate
    p = sub.add_parser("inflate", help="inflate DEFLATE/zlib/gzip data to raw bytes")
    p.add_argument("input", help="compressed input file (e.g. a .cff / flash section)")
    p.add_argument("-o", "--output", required=True, help="output .bin")
    p.add_argument("--offset", type=lambda x: int(x, 0), default=0,
                   help="start offset of the compressed stream (e.g. 0x30)")
    p.add_argument("--mode", choices=["auto", "raw", "zlib", "gzip"], default="auto")
    p.add_argument("--sections", action="store_true",
                   help="inflate consecutive raw-DEFLATE sections and concatenate")
    p.set_defaults(func=cmd_inflate)

    # extract-calibration
    p = sub.add_parser("extract-calibration",
                       help="slice a flashable calibration out of a full ECU read")
    p.add_argument("input", help="full ECU read (.bin)")
    p.add_argument("-o", "--output", required=True, help="output calibration .bin")
    p.add_argument("--address", type=lambda x: int(x, 0), default=0x84002000,
                   help="transferAddress (0x84002000 or 0x80140000)")
    p.add_argument("--offset", type=lambda x: int(x, 0), default=None,
                   help="override slice offset in the full read")
    p.add_argument("--length", type=lambda x: int(x, 0), default=None,
                   help="override calibration length")
    p.add_argument("--full-read", action="store_true", default=True,
                   help="input is a full read (slice at offset); default on")
    p.add_argument("--no-full-read", dest="full_read", action="store_false",
                   help="input already starts at the calibration")
    p.add_argument("--force", action="store_true",
                   help="write even if the 0x60..0xDE signature check fails")
    p.set_defaults(func=cmd_extract_calibration)

    # scan
    p = sub.add_parser("scan", help="read-only ECU reconnaissance (sessions/DIDs/seeds)")
    add_bus_args(p)
    p.add_argument("--tx", type=lambda x: int(x, 0), help="override request CAN id")
    p.add_argument("--rx", type=lambda x: int(x, 0), help="override response CAN id")
    p.add_argument("--deep", action="store_true",
                   help="also probe the programming session (0x10 02)")
    p.add_argument("--read-memory", type=lambda x: int(x, 0), default=None,
                   help="try a 16-byte readMemoryByAddress at this address")
    p.add_argument("--emit-profile", help="write a profile skeleton from the scan")
    p.add_argument("--emit-seeds", help="write the collected seeds to a file")
    p.set_defaults(func=cmd_scan)

    # xcp / measure
    p = sub.add_parser("xcp", aliases=["measure"],
                       help="measure/log live ECU values over XCP (poll or DAQ)")
    add_bus_args(p)
    p.add_argument("--signal", action="append", metavar="NAME@ADDR:TYPE",
                   help="a signal to measure, e.g. rpm@0x80005000:u16:0.25 "
                        "(TYPE u8/s8/u16/s16/u32/s32/f32/f64; optional :factor:offset:unit). "
                        "Repeatable.")
    p.add_argument("--signals-file", help="file with one signal spec per line (# comments)")
    p.add_argument("--a2l", help="ASAP2/A2L file: auto-derive the XCP CAN ids "
                   "(CRO/DTO) and, with --find, the signals to measure")
    p.add_argument("--find", help="with --a2l: measure the A2L measurements whose "
                   "name matches this pattern (substring or glob)")
    p.add_argument("--cro", type=lambda x: int(x, 0), default=0x7E0,
                   help="XCP command (CRO) CAN id (default 0x7E0)")
    p.add_argument("--dto", type=lambda x: int(x, 0), default=0x7E1,
                   help="XCP response/data (DTO) CAN id (default 0x7E1)")
    p.add_argument("--extended", action="store_true", help="use 29-bit CAN ids")
    p.add_argument("--byte-order", choices=["little", "big"], default="little",
                   help="slave byte order for the simulator (default little)")
    p.add_argument("--daq", action="store_true",
                   help="use real XCP DAQ streaming instead of polling")
    p.add_argument("--rate", type=float, default=10.0,
                   help="polling rate in Hz (default 10)")
    p.add_argument("--event", type=int, default=0, help="DAQ event channel (--daq)")
    p.add_argument("--prescaler", type=int, default=1, help="DAQ prescaler (--daq)")
    p.add_argument("--duration", type=float, default=None,
                   help="stop after N seconds (default: until Ctrl+C)")
    p.add_argument("--samples", type=int, default=None, help="stop after N samples")
    p.add_argument("--csv", help="log samples to this CSV file")
    p.add_argument("--pad", action="store_true", help="pad CTO frames to 8 bytes")
    p.add_argument("--timeout", type=float, default=1.0, help="response timeout (s)")
    p.set_defaults(func=cmd_measure)

    # a2l
    p = sub.add_parser("a2l",
                       help="read an ASAP2/A2L file and emit XCP signal specs")
    p.add_argument("file", help="the ECU description file (.a2l)")
    p.add_argument("--find", metavar="PATTERN",
                   help="only list measurements matching this substring or glob "
                        "(case-insensitive), e.g. 'nmot' or 'n*_w'")
    p.add_argument("--limit", type=int, default=50,
                   help="max rows to print, 0 for all (default 50); does not "
                        "limit --emit-signals")
    p.add_argument("--emit-signals", metavar="OUT",
                   help="write the matching signals as "
                        "name@0xADDR:dtype:factor:offset:unit, ready for "
                        "'med17flasher xcp --signals-file OUT'")
    p.set_defaults(func=cmd_a2l)

    # capture
    p = sub.add_parser("capture", help="passively record CAN frames to a candump log")
    add_bus_args(p)
    p.add_argument("-o", "--output", default="capture.log")
    p.add_argument("--seconds", type=float, default=None, help="capture duration (default: until Ctrl+C)")
    p.add_argument("--analyze", action="store_true", help="quick-analyze the capture when done")
    p.set_defaults(func=cmd_capture)

    # sniff - passively reverse-engineer another flasher's session
    p = sub.add_parser(
        "sniff",
        help="passively sniff another tool's read/write (Tactrix on a split OBD2 bus) "
             "and derive the profile + seed/key",
    )
    p.add_argument("--backend", default="j2534",
                   help="interface to listen on (default: j2534/Tactrix; also "
                        "socketcan:can0, tactrix, ...)")
    p.add_argument("--device", default="",
                   help="J2534 device name substring ('tactrix') or PassThru DLL path")
    p.add_argument("--baudrate", type=int, default=500000,
                   help="CAN bit rate of the bus you are sniffing (default 500000)")
    p.add_argument("--extended", action="store_true",
                   help="the sniffed bus uses 29-bit CAN ids")
    p.add_argument("--simulator", action="store_true",
                   help="sniff an in-process simulated flash instead (for testing)")
    p.add_argument("--profile",
                   help="ECU profile for the request/response id hints "
                        "(defaults to built-in MED17.7.5)")
    p.add_argument("--tx", type=lambda x: int(x, 0), default=None,
                   help="request CAN id for the final analysis (default from profile)")
    p.add_argument("--rx", type=lambda x: int(x, 0), default=None,
                   help="response CAN id for the final analysis (default from profile)")
    p.add_argument("-o", "--output", default="sniff.log",
                   help="write the raw capture as a candump log (default sniff.log)")
    p.add_argument("--seconds", type=float, default=None,
                   help="stop after N seconds (default: until Ctrl+C)")
    p.add_argument("--emit-profile",
                   help="where to write the derived profile "
                        "(default: <output>.profile.yaml)")
    p.add_argument("--emit-pairs",
                   help="where to write extracted seed/key pairs "
                        "(default: <output>.pairs.txt)")
    p.add_argument("--no-analyze", action="store_true",
                   help="just record the log; skip the automatic analysis")
    p.set_defaults(func=cmd_sniff)

    # seedkey
    p = sub.add_parser("seedkey", help="compute a key from a seed")
    p.add_argument("seed", help="seed bytes as hex, e.g. 11223344")
    p.add_argument("--algorithm", default="med17")
    p.add_argument("--ecu", default="MED17.7.5")
    p.add_argument("--level", type=lambda x: int(x, 0), default=0x11)
    p.add_argument("--param", action="append", help="algorithm parameter key=value (repeatable)")
    p.add_argument("--store", help="use a seed/key store JSON instead of --algorithm")
    p.add_argument("--dll", help="compute via a J2534 seed-key DLL (32-bit Windows)")
    p.add_argument("--exe", help="compute via an external seed-key executable")
    p.add_argument("--options", default="", help="option/config string passed to the DLL")
    p.set_defaults(func=cmd_seedkey)

    # seedkey-solve
    p = sub.add_parser("seedkey-solve", help="recover a seed/key algorithm from captured pairs")
    p.add_argument("--pairs", help="file of seed/key pairs (.json or text 'seed key' lines)")
    p.add_argument("--pair", action="append", help="a single seedhex:keyhex pair (repeatable)")
    p.add_argument("--wordlist", help="file of candidate 32-bit constants for brute force")
    p.add_argument("--level", type=lambda x: int(x, 0), default=0, help="security access level")
    p.add_argument("--ecu", default="MED17.7.5")
    p.add_argument("--emit-store", help="write a seed/key store JSON for a full match")
    p.set_defaults(func=cmd_seedkey_solve)

    # analyze-trace
    p = sub.add_parser("analyze-trace", help="derive a profile + seed/key pairs from a CAN trace")
    p.add_argument("trace", help="CAN log (candump/.log, Vector .asc, or .csv)")
    p.add_argument("--tx", type=lambda x: int(x, 0), default=0x7E0, help="request CAN id")
    p.add_argument("--rx", type=lambda x: int(x, 0), default=0x7E8, help="response CAN id")
    p.add_argument("--emit-profile", help="write a derived ECU profile YAML/JSON")
    p.add_argument("--emit-pairs", help="write extracted seed/key pairs to a file")
    p.set_defaults(func=cmd_analyze_trace)

    # analyze-firmware
    p = sub.add_parser("analyze-firmware", help="detect program regions in a firmware dump")
    p.add_argument("firmware", help="firmware file (.bin/.hex/.s19)")
    p.add_argument("--base", type=lambda x: int(x, 0), default=0x80000000,
                   help="base address for raw .bin dumps")
    p.add_argument("--min-gap", type=lambda x: int(x, 0), default=0x1000,
                   help="minimum 0xFF gap that separates two regions")
    p.add_argument("--align", type=lambda x: int(x, 0), default=0x100)
    p.add_argument("--emit-profile", help="write a derived ECU profile YAML/JSON")
    p.set_defaults(func=cmd_analyze_firmware)

    # ingest
    p = sub.add_parser("ingest", help="scan a folder and auto-process files")
    p.add_argument("dir", nargs="?", default="_input", help="folder to scan (default: _input)")
    p.add_argument("--tx", type=lambda x: int(x, 0), default=0x7E0)
    p.add_argument("--rx", type=lambda x: int(x, 0), default=0x7E8)
    p.add_argument("--base", type=lambda x: int(x, 0), default=0x80000000)
    p.add_argument("--level", type=lambda x: int(x, 0), default=0x11)
    p.set_defaults(func=cmd_ingest)

    # seedkey-server
    p = sub.add_parser("seedkey-server", help="run the seed/key network server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8377)
    p.add_argument("--tcp-port", type=int, default=8378)
    p.add_argument("--store", help="seed/key store JSON")
    p.add_argument("--dll", help="serve keys via a J2534 seed-key DLL (32-bit Windows)")
    p.add_argument("--bridge", metavar="PATH",
                   help="serve keys via a 32-bit seed-key DLL run in a helper "
                        "process (works from 64-bit Python)")
    p.add_argument("--python32", metavar="PATH",
                   help="32-bit python.exe for --bridge (auto-detected)")
    p.add_argument("--exe", help="serve keys via an external seed-key executable")
    p.add_argument("--options", default="", help="option/config string passed to the DLL")
    p.set_defaults(func=cmd_seedkey_server)

    # fileserver
    p = sub.add_parser("fileserver", help="run the firmware file server")
    p.add_argument("--root", default="./firmware-repo")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--token", help="bearer token protecting write operations")
    p.set_defaults(func=cmd_fileserver)

    # simulator
    p = sub.add_parser("simulator", help="run a stand-alone virtual MED17.7.5")
    p.add_argument("--backend", default="virtual:shared")
    p.add_argument("--profile")
    p.set_defaults(func=cmd_simulator)

    # backends
    p = sub.add_parser("backends", help="list CAN backends and algorithms")
    p.set_defaults(func=cmd_backends)

    # j2534
    p = sub.add_parser("j2534",
                       help="check a J2534 interface (Tactrix Openport, ...)")
    p.add_argument("--device", default="",
                   help="name substring ('tactrix') or a path to the PassThru DLL")
    p.add_argument("--baudrate", type=int, default=500000)
    p.add_argument("--extended", action="store_true", help="enable 29-bit ids")
    p.add_argument("--listen", type=float, default=0.0, metavar="SECONDS",
                   help="passively count CAN traffic to prove the wiring works")
    p.set_defaults(func=cmd_j2534)

    # profile
    p = sub.add_parser("profile", help="print an ECU profile as JSON")
    p.add_argument("--profile")
    p.set_defaults(func=cmd_profile)

    # gui
    p = sub.add_parser("gui", help="launch the Tkinter desktop GUI")
    p.set_defaults(func=cmd_gui)

    # desktop
    p = sub.add_parser("desktop", help="launch the desktop app (web UI in a window/browser)")
    p.set_defaults(func=cmd_desktop)

    # webserver
    p = sub.add_parser("webserver", help="serve the DME Innovation MED17 Flasher web UI + API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--profile", help="ECU profile (defaults to the C63 demo profile)")
    p.add_argument("--throttle", type=float, default=180.0,
                   help="live flash rate cap in KB/s (0 = unthrottled)")
    p.add_argument("--backend", default="simulator",
                   help="CAN backend for real hardware (e.g. socketcan:can0); default: simulator")
    p.add_argument("--firmware", help="real firmware image to flash (required for real writing)")
    p.add_argument("--allow-write", action="store_true",
                   help="permit writing to REAL hardware (needs --backend + --firmware + profile)")
    p.add_argument("--open", action="store_true", help="open the UI in a browser")
    p.set_defaults(func=cmd_webserver)

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    configure_logging(level)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Med17FlasherError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
