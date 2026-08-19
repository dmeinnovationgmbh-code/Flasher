"""Small helper to give every entry point consistent logging.

Logging is intentionally centralised: the flash sequence emits a lot of
INFO/DEBUG detail that is invaluable when a real ECU misbehaves, and the GUI /
CLI want to route that same stream to a text widget or the console.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional, TextIO

_CONFIGURED = False

DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DEFAULT_DATEFMT = "%H:%M:%S"


def configure_logging(
    level: int = logging.INFO,
    *,
    stream: Optional[TextIO] = None,
    fmt: str = DEFAULT_FORMAT,
    datefmt: str = DEFAULT_DATEFMT,
    force: bool = False,
) -> None:
    """Configure the root ``med17flasher`` logger exactly once.

    Repeated calls are no-ops unless ``force`` is given, which keeps libraries
    that import us from fighting over the root logger.
    """

    global _CONFIGURED
    logger = logging.getLogger("med17flasher")
    if _CONFIGURED and not force:
        logger.setLevel(level)
        return

    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a child logger below the ``med17flasher`` namespace."""

    if not name.startswith("med17flasher"):
        name = f"med17flasher.{name}"
    return logging.getLogger(name)
