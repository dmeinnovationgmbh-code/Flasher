#!/usr/bin/env python3
"""Convenience launcher for the firmware file server + seed/key server together.

    python scripts/run_server.py --root ./firmware-repo
"""

from __future__ import annotations

import argparse
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from med17flasher.logging_setup import configure_logging  # noqa: E402
from med17flasher.seedkey import SeedKeyStore  # noqa: E402
from med17flasher.seedkey.server import (  # noqa: E402
    SeedKeyHttpServer,
    SeedKeyService,
    SeedKeyTcpServer,
)
from med17flasher.server import FileServer, FirmwareRepository  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="./firmware-repo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--file-port", type=int, default=8080)
    parser.add_argument("--seedkey-port", type=int, default=8377)
    parser.add_argument("--seedkey-tcp-port", type=int, default=8378)
    parser.add_argument("--token", default=None)
    parser.add_argument("--seedkey-store", default=None)
    args = parser.parse_args()

    import logging

    configure_logging(logging.INFO)

    repo = FirmwareRepository(args.root)
    file_server = FileServer(repo, host=args.host, port=args.file_port, token=args.token)

    store = SeedKeyStore.load(args.seedkey_store) if args.seedkey_store else SeedKeyStore.default()
    service = SeedKeyService(store)
    sk_http = SeedKeyHttpServer(service, host=args.host, port=args.seedkey_port)
    sk_tcp = SeedKeyTcpServer(service, host=args.host, port=args.seedkey_tcp_port)

    file_server.start()
    sk_http.start()
    sk_tcp.start()

    print("Servers running:")
    print(f"  firmware file server : {file_server.url}  (root={args.root})")
    print(f"  seed/key HTTP        : http://{args.host}:{args.seedkey_port}")
    print(f"  seed/key TCP         : {args.host}:{args.seedkey_tcp_port}")
    print("Press Ctrl+C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        file_server.stop()
        sk_http.stop()
        sk_tcp.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
