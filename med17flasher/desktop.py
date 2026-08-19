"""Desktop-app entry point.

Starts the local web backend (which serves the bundled React UI) on a free port
and opens it - in a native window via ``pywebview`` if installed, otherwise in
the default browser. This is the entry point PyInstaller bundles into a single
downloadable executable, so end users get a desktop app without installing
Python or Node.
"""

from __future__ import annotations

import logging
import sys
import threading
import time

from .logging_setup import configure_logging, get_logger

log = get_logger("desktop")


def _run(argv=None) -> int:
    configure_logging(logging.INFO)
    from .webserver import FlashService, WebServer

    server = WebServer(FlashService(), host="127.0.0.1", port=0)
    server.start(block=False)
    url = server.url
    root = server._httpd.static_root  # type: ignore[attr-defined]
    log.info("MED17.7.5 Flash Tool desktop app on %s", url)
    if not root:
        log.warning("web UI not bundled/built; the API is available under /api")

    try:
        # Preferred: a real native window (only if pywebview is bundled).
        import webview  # type: ignore

        webview.create_window("MED17.7.5 Flash Tool", url,
                              width=1200, height=920, min_size=(900, 640))
        webview.start()  # blocks until the window closes
        server.stop()
        return 0
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 - a webview failure must fall back, not crash
        log.exception("native window failed; falling back to the browser")

    # Fallback: open the system browser and keep the server alive.
    import webbrowser

    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    print("\n" + "=" * 58)
    print("  MED17.7.5 Flash Tool läuft.")
    print(f"  Im Browser öffnen:  {url}")
    print("  Der Browser sollte sich automatisch öffnen.")
    print("  Dieses Fenster offen lassen — Schließen beendet die App.")
    print("=" * 58 + "\n")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nshutting down ...")
    finally:
        server.stop()
    return 0


def main(argv=None) -> int:
    """Entry point. Never let an exception close the console without a trace."""

    try:
        return _run(argv)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - keep the window open on any crash
        # A frozen console app that raises here just vanishes ("black window
        # flashes and closes"). Show the error and wait so the user can read it.
        try:
            log.exception("desktop app failed to start")
        except Exception:  # noqa: BLE001
            pass
        import traceback

        print("\n" + "=" * 58)
        print("  MED17.7.5 Flash Tool konnte nicht starten:")
        print(f"    {type(exc).__name__}: {exc}")
        print("=" * 58)
        traceback.print_exc()
        try:
            input("\nMit Enter schließen … ")
        except BaseException:  # noqa: BLE001 - no stdin (service/pytest): just exit
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
