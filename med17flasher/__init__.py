"""
med17flasher
============

A complete, hardware-agnostic desktop flashing toolkit for the Bosch
**MED17.7.5** engine control unit (Infineon TriCore, VAG platform).

The package is organised into loosely coupled layers so that every piece can
be used on its own or wired together into the full desktop application:

* :mod:`med17flasher.core`      - CAN backends, ISO-TP, UDS client, flash
                                  sequence state machine, checksum + firmware
                                  container handling and the ECU profile.
* :mod:`med17flasher.seedkey`   - pluggable Security Access (UDS 0x27)
                                  seed -> key algorithm framework + a seed/key
                                  network server.
* :mod:`med17flasher.server`    - a dependency-free HTTP file server + REST API
                                  that acts as the firmware repository and its
                                  matching client.
* :mod:`med17flasher.simulator` - a virtual MED17.7.5 ECU that speaks real UDS
                                  so the whole stack can be exercised without
                                  hardware.
* :mod:`med17flasher.gui`       - a Tkinter desktop front-end.
* :mod:`med17flasher.cli`       - a command line front-end.

Nothing in :mod:`med17flasher.core` requires third-party packages; optional
CAN adapters (``python-can``, ``pyserial`` for ELM327 ...) light up only when
they are installed.
"""

from .version import __version__

__all__ = ["__version__"]
