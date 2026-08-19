"""Command line interface for the MED17.7.5 flasher.

Run ``med17flasher --help`` (or ``python -m med17flasher --help``) for the full
list of sub-commands:

* ``flash``          - reprogram an ECU from a firmware file
* ``identify``       - read the ECU's identification DIDs
* ``convert``        - convert bin/Intel-HEX/S-Record to a flat .bin
* ``inflate``        - inflate DEFLATE/zlib/gzip data (e.g. a compressed section)
* ``extract-calibration`` - slice a flashable calibration out of a full ECU read
* ``scan``           - read-only ECU reconnaissance (sessions / DIDs / seeds)
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
            print("Dry run: not writing (planned blocks below)")
            for block in image.blocks_for(profile.memory_map):
                print(f"  - {block.name}: 0x{block.address:08X} ({block.size} bytes)")
            return 0

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
    bus, sim = _open_bus(args, profile)
    try:
        uds = _build_uds(bus, profile)
        if not args.simulator:
            uds.enter_extended_session()
        data = uds.read_memory_by_address(args.address, args.size)
        with open(args.output, "wb") as fh:
            fh.write(data)
        print(f"read {len(data)} bytes from 0x{args.address:08X} -> {args.output}")
        return 0
    except Med17FlasherError as exc:
        print(f"read failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if sim:
            sim.stop()
        bus.close()


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
    import json as _json

    from .core.trace import analyze, read_trace

    frames = read_trace(args.trace)
    if not frames:
        print(f"no CAN frames parsed from {args.trace!r}", file=sys.stderr)
        return 1
    report = analyze(frames, tx_id=args.tx, rx_id=args.rx)
    print(f"Trace {args.trace}: {len(frames)} frames, "
          f"{report.request_count} requests / {report.response_count} responses")
    print(f"  sessions       : {[hex(s) for s in report.sessions]}")
    print(f"  security levels: {[hex(l) for l in report.security_levels]}")
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
    if args.dll or args.exe:
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
    print("CAN backends:")
    for name, ok in available_backends().items():
        print(f"  {'[x]' if ok else '[ ]'} {name}")
    print("\nSeed/key algorithms:")
    for algo in list_algorithms():
        print(f"  - {algo}")
    return 0


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
    print(f"MED17 Flash Tool web UI on {server.url}")
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
    p.add_argument("--seedkey-exe", help="external seed-key executable")
    p.add_argument("--seedkey-options", default="", help="option string for the seed-key DLL")
    p.add_argument("--plugin", help="seed/key algorithm plug-in .py to load")
    p.add_argument("--no-identify", action="store_true", help="skip reading identification DIDs")
    p.add_argument("--dry-run", action="store_true", help="plan the flash but do not write")
    p.set_defaults(func=cmd_flash)

    # identify
    p = sub.add_parser("identify", help="read identification DIDs")
    add_bus_args(p)
    p.set_defaults(func=cmd_identify)

    # read
    p = sub.add_parser("read", help="read a memory range to a file")
    add_bus_args(p)
    p.add_argument("--address", type=lambda x: int(x, 0), required=True)
    p.add_argument("--size", type=lambda x: int(x, 0), required=True)
    p.add_argument("-o", "--output", required=True)
    p.set_defaults(func=cmd_read)

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

    # capture
    p = sub.add_parser("capture", help="passively record CAN frames to a candump log")
    add_bus_args(p)
    p.add_argument("-o", "--output", default="capture.log")
    p.add_argument("--seconds", type=float, default=None, help="capture duration (default: until Ctrl+C)")
    p.add_argument("--analyze", action="store_true", help="quick-analyze the capture when done")
    p.set_defaults(func=cmd_capture)

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

    # profile
    p = sub.add_parser("profile", help="print an ECU profile as JSON")
    p.add_argument("--profile")
    p.set_defaults(func=cmd_profile)

    # gui
    p = sub.add_parser("gui", help="launch the desktop GUI")
    p.set_defaults(func=cmd_gui)

    # webserver
    p = sub.add_parser("webserver", help="serve the MED17 Flash Tool web UI + API")
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
