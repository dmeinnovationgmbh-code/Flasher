"""XCP (ASAM MCD-1) measurement / calibration over CAN.

A dependency-free XCP master for reading live values from a MED17-class ECU:

* :class:`XcpOnCan` - the XCP-on-CAN transport
* :class:`XcpClient` - CONNECT / memory read / DAQ command set
* :class:`Signal`, :class:`PollingMeasurement`, :func:`configure_daq`,
  :class:`DaqMeasurement` - measurement + CSV logging
* :class:`VirtualXcpSlave` - an in-process slave for tests/demos
"""

from .client import XcpClient
from .measure import (
    DaqLayout,
    DaqMeasurement,
    PollingMeasurement,
    Sample,
    Signal,
    configure_daq,
    parse_signal,
)
from .slave import VirtualXcpSlave
from .transport import XcpOnCan

__all__ = [
    "XcpOnCan",
    "XcpClient",
    "Signal",
    "Sample",
    "PollingMeasurement",
    "DaqMeasurement",
    "DaqLayout",
    "configure_daq",
    "parse_signal",
    "VirtualXcpSlave",
]
