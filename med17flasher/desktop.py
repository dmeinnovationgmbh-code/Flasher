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


def main(argv=None) -> int:
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
        # Preferred: a real native window.
        import webview  # type: ignore

        window = webview.create_window("MED17.7.5 Flash Tool", url,
                                       width=1200, height=920, min_size=(900, 640))
        webview.start()  # blocks until the window closes
        server.stop()
        return 0
    except ImportError:
        pass

    # Fallback: open the system browser and keep the server alive.
    import webbrowser

    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    print(f"\nMED17.7.5 Flash Tool is running at {url}")
    print("Your browser should open automatically. Press Ctrl+C to quit.\n")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nshutting down ...")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
