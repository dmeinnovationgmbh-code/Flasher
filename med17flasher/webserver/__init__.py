"""Web UI backend: a JSON/SSE API that drives the real flasher, plus static
serving for the built React front-end (``webui/dist``)."""

from .app import WebServer
from .service import FlashService

__all__ = ["WebServer", "FlashService"]
