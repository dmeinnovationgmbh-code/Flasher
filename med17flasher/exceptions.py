"""Exception hierarchy shared across the whole package.

Every error raised deliberately by ``med17flasher`` derives from
:class:`Med17FlasherError`, so callers can wrap an entire flash run in a single
``except Med17FlasherError`` and still get precise sub-types when they need
them.
"""

from __future__ import annotations

from typing import Optional


class Med17FlasherError(Exception):
    """Base class for all errors raised by med17flasher."""


# --------------------------------------------------------------------------- #
# Transport / bus layer
# --------------------------------------------------------------------------- #
class TransportError(Med17FlasherError):
    """A CAN backend / low level I/O problem."""


class BackendNotAvailableError(TransportError):
    """A requested CAN backend cannot be used (missing dependency/hardware)."""


class IsoTpError(TransportError):
    """An ISO 15765-2 (ISO-TP) framing or flow-control error."""


class IsoTpTimeoutError(IsoTpError):
    """No (or incomplete) ISO-TP response arrived within the timeout."""


# --------------------------------------------------------------------------- #
# UDS layer
# --------------------------------------------------------------------------- #
class UdsError(Med17FlasherError):
    """Base class for UDS (ISO 14229) level problems."""


class UdsTimeoutError(UdsError):
    """The ECU did not answer a UDS request in time."""


class NegativeResponseError(UdsError):
    """The ECU returned a negative response (0x7F ...).

    Parameters
    ----------
    service:
        The request service id that triggered the negative response.
    nrc:
        The negative response code byte.
    message:
        Optional human readable description.
    """

    def __init__(self, service: int, nrc: int, message: Optional[str] = None):
        from .core.uds_const import nrc_name, service_name

        self.service = service
        self.nrc = nrc
        self.nrc_name = nrc_name(nrc)
        self.service_name = service_name(service)
        text = message or (
            f"Negative response to {self.service_name} "
            f"(0x{service:02X}): {self.nrc_name} (0x{nrc:02X})"
        )
        super().__init__(text)


class UnexpectedResponseError(UdsError):
    """The ECU answered, but not with the positive response we expected."""


# --------------------------------------------------------------------------- #
# Security access / seed-key
# --------------------------------------------------------------------------- #
class SecurityAccessError(Med17FlasherError):
    """Security Access (UDS 0x27) failed."""


class SeedKeyError(Med17FlasherError):
    """A seed -> key computation could not be performed."""


class AlgorithmNotFoundError(SeedKeyError):
    """No seed/key algorithm is registered under the requested name."""


# --------------------------------------------------------------------------- #
# Firmware / flashing
# --------------------------------------------------------------------------- #
class FirmwareError(Med17FlasherError):
    """A firmware container is malformed or does not fit the ECU profile."""


class ChecksumError(FirmwareError):
    """A checksum could not be computed or verified."""


class FlashError(Med17FlasherError):
    """The flashing sequence failed."""


class FlashAborted(FlashError):
    """The flashing sequence was aborted by the caller."""


# --------------------------------------------------------------------------- #
# Repository / file server
# --------------------------------------------------------------------------- #
class RepositoryError(Med17FlasherError):
    """A firmware repository / file-server operation failed."""
