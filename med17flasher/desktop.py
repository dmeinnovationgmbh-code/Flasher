"""Desktop-app entry point.

Starts the local web backend (which serves the bundled React UI) on a free port
and opens it in a **native window** via ``pywebview`` (a real desktop app, no
terminal, no browser tab). If pywebview is unavailable it falls back to the
system browser. This is the entry point PyInstaller bundles into a single
downloadable executable, so end users get a desktop app without installing
Python or Node.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import time

from .logging_setup import configure_logging, get_logger

log = get_logger("desktop")

_TITLE = "DME Innovation MED17 Flasher"


def _log_path() -> str:
    return os.path.join(tempfile.gettempdir(), "med17flasher.log")


def _open_native_window(url: str) -> bool:
    """Open the UI in a native window. Returns True if a window was shown."""

    try:
        import webview  # type: ignore
    except ImportError:
        log.info("pywebview not bundled; using the browser")
        return False
    try:
        webview.create_window(_TITLE, url, width=1280, height=900,
                              min_size=(960, 660))
        webview.start()  # blocks until the window is closed
        return True
    except Exception:  # noqa: BLE001 - any webview failure -> browser fallback
        log.exception("native window failed; falling back to the browser")
        return False


def _open_browser_and_wait(url: str, server) -> int:
    import webbrowser

    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    # These prints only show if a console exists (Linux build); harmless otherwise.
    print("\n" + "=" * 58)
    print(f"  {_TITLE} läuft.")
    print(f"  Im Browser öffnen:  {url}")
    print("  Fenster/Prozess offen lassen — Beenden stoppt die App.")
    print("=" * 58 + "\n")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def _selftest() -> int:
    """Exercise the real startup path headlessly and exit 0/1.

    This is what CI runs against the *frozen* binary. It caught nothing before
    because CI only built the exe and never launched it — which is exactly how
    the "bundled YAML profile but no PyYAML" startup crash shipped green. Here we
    actually construct the service, bind the server, and fetch the bundled UI +
    a couple of API endpoints through HTTP, so a broken build fails the job.
    """

    # Must never raise: on a windowed Windows build a raised exception would
    # reach main() and pop a blocking MessageBox, hanging CI. Catch everything
    # here and return a status code instead.
    import json
    import urllib.request

    server = None
    try:
        from .webserver import FlashService, WebServer

        server = WebServer(FlashService(), host="127.0.0.1", port=0)
        server.start(block=False)
        base = server.url

        def get(path: str) -> bytes:
            with urllib.request.urlopen(base + path, timeout=10) as r:
                if r.status != 200:
                    raise RuntimeError(f"{path} -> HTTP {r.status}")
                return r.read()

        vehicle = json.loads(get("/api/vehicle"))
        assert vehicle.get("ecu"), "vehicle payload missing ecu"
        profiles = json.loads(get("/api/profiles")).get("profiles", [])
        assert profiles, "no ECU profiles were bundled/loadable"
        index = get("/")  # the bundled React UI
        assert b'<div id="root"' in index or b"<title" in index, "web UI not served"

        root = server._httpd.static_root  # type: ignore[attr-defined]
        # A frozen desktop app must ship the built UI, not the dev fallback page.
        if root is None:
            sys.stderr.write("selftest FAILED: the web UI (webui/dist) is not bundled\n")
            return 1
        sys.stdout.write(
            f"selftest OK: UI bundled, {len(profiles)} profile(s), ecu={vehicle['ecu']}\n"
        )
        return 0
    except BaseException as exc:  # noqa: BLE001 - report and fail, never propagate
        sys.stderr.write(f"selftest FAILED: {type(exc).__name__}: {exc}\n")
        return 1
    finally:
        if server is not None:
            try:
                server.stop()
            except Exception:  # noqa: BLE001
                pass


def _run(argv=None) -> int:
    configure_logging(logging.INFO, logfile=_log_path())
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return _selftest()

    from .webserver import FlashService, WebServer

    server = WebServer(FlashService(), host="127.0.0.1", port=0)
    server.start(block=False)
    url = server.url
    root = server._httpd.static_root  # type: ignore[attr-defined]
    log.info("%s on %s", _TITLE, url)
    if not root:
        log.warning("web UI not bundled/built; the API is available under /api")

    if _open_native_window(url):
        server.stop()
        return 0
    return _open_browser_and_wait(url, server)


def _show_error(exc: BaseException) -> None:
    """Surface a startup failure without a console (native dialog + logfile)."""

    msg = f"{type(exc).__name__}: {exc}"
    try:
        with open(_log_path(), "a", encoding="utf-8") as fh:
            import traceback

            fh.write("\n--- startup failure ---\n")
            traceback.print_exc(file=fh)
    except Exception:  # noqa: BLE001
        pass
    # A real dialog on Windows (no terminal needed).
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                None,
                f"{_TITLE} konnte nicht starten:\n\n{msg}\n\nDetails: {_log_path()}",
                _TITLE, 0x10,
            )
            return
        except Exception:  # noqa: BLE001
            pass
    # Console builds (Linux) / fallback: print + wait so it stays readable.
    print("\n" + "=" * 58)
    print(f"  {_TITLE} konnte nicht starten:\n    {msg}")
    print(f"  Log: {_log_path()}")
    print("=" * 58)
    try:
        import traceback

        traceback.print_exc()
        input("\nMit Enter schließen … ")
    except BaseException:  # noqa: BLE001 - no stdin: just exit
        pass


def main(argv=None) -> int:
    """Entry point. Never vanish silently — show any startup failure."""

    try:
        return _run(argv)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        try:
            log.exception("desktop app failed to start")
        except Exception:  # noqa: BLE001
            pass
        _show_error(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
