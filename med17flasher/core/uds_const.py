"""UDS (ISO 14229-1) constants used throughout the flasher.

Only the services, sub-functions and identifiers that actually matter for
diagnosing and reprogramming a MED17.7.5 are enumerated here, but the negative
response code table is complete so error reporting is always meaningful.
"""

from __future__ import annotations

from enum import IntEnum

# The fixed offset added to a request SID to form a positive response SID.
POSITIVE_RESPONSE_OFFSET = 0x40

# The service id that prefixes every negative response.
NEGATIVE_RESPONSE_SID = 0x7F


class Service(IntEnum):
    """Diagnostic service identifiers (request SIDs)."""

    DIAGNOSTIC_SESSION_CONTROL = 0x10
    ECU_RESET = 0x11
    CLEAR_DIAGNOSTIC_INFORMATION = 0x14
    READ_DTC_INFORMATION = 0x19
    READ_DATA_BY_IDENTIFIER = 0x22
    READ_MEMORY_BY_ADDRESS = 0x23
    SECURITY_ACCESS = 0x27
    COMMUNICATION_CONTROL = 0x28
    READ_DATA_BY_PERIODIC_ID = 0x2A
    DYNAMICALLY_DEFINE_DATA_ID = 0x2C
    WRITE_DATA_BY_IDENTIFIER = 0x2E
    INPUT_OUTPUT_CONTROL_BY_ID = 0x2F
    ROUTINE_CONTROL = 0x31
    REQUEST_DOWNLOAD = 0x34
    REQUEST_UPLOAD = 0x35
    TRANSFER_DATA = 0x36
    REQUEST_TRANSFER_EXIT = 0x37
    WRITE_MEMORY_BY_ADDRESS = 0x3D
    TESTER_PRESENT = 0x3E
    CONTROL_DTC_SETTING = 0x85


class Session(IntEnum):
    """diagnosticSessionType values for service 0x10."""

    DEFAULT = 0x01
    PROGRAMMING = 0x02
    EXTENDED_DIAGNOSTIC = 0x03
    SAFETY_SYSTEM_DIAGNOSTIC = 0x04


class ResetType(IntEnum):
    """resetType values for service 0x11."""

    HARD_RESET = 0x01
    KEY_OFF_ON_RESET = 0x02
    SOFT_RESET = 0x03
    ENABLE_RAPID_POWER_SHUTDOWN = 0x04
    DISABLE_RAPID_POWER_SHUTDOWN = 0x05


class RoutineControlType(IntEnum):
    """routineControlType (sub-function) values for service 0x31."""

    START_ROUTINE = 0x01
    STOP_ROUTINE = 0x02
    REQUEST_ROUTINE_RESULTS = 0x03


class CommunicationControlType(IntEnum):
    """controlType values for service 0x28."""

    ENABLE_RX_AND_TX = 0x00
    ENABLE_RX_DISABLE_TX = 0x01
    DISABLE_RX_ENABLE_TX = 0x02
    DISABLE_RX_AND_TX = 0x03


class CommunicationType(IntEnum):
    """communicationType bit field for service 0x28 (the second byte)."""

    NORMAL = 0x01
    NETWORK_MANAGEMENT = 0x02
    NORMAL_AND_NM = 0x03


class DtcSettingType(IntEnum):
    """DTCSettingType (sub-function) values for service 0x85."""

    ON = 0x01
    OFF = 0x02


class SecurityAccessType(IntEnum):
    """Common requestSeed / sendKey sub-function values for service 0x27.

    Odd values request a seed, the following even value sends the key. The
    concrete level required to program a MED17.7.5 is configured in the ECU
    profile; these are the widely used defaults.
    """

    REQUEST_SEED_1 = 0x01
    SEND_KEY_1 = 0x02
    REQUEST_SEED_3 = 0x03  # frequently the "extended diagnostics" level
    SEND_KEY_3 = 0x04
    REQUEST_SEED_PROGRAMMING = 0x11  # the level guarding flash programming
    SEND_KEY_PROGRAMMING = 0x12


# Data identifiers (service 0x22) that are commonly present on VAG ECUs.
class DataIdentifier(IntEnum):
    """A useful subset of standardised and VAG specific DIDs."""

    ACTIVE_DIAGNOSTIC_SESSION = 0xF186
    VIN = 0xF190
    ECU_MANUFACTURING_DATE = 0xF18B
    ECU_SERIAL_NUMBER = 0xF18C
    VEHICLE_MANUFACTURER_ECU_SW_NUMBER = 0xF188
    SYSTEM_SUPPLIER_ECU_HW_NUMBER = 0xF191
    SYSTEM_SUPPLIER_ECU_SW_NUMBER = 0xF194
    VW_SPARE_PART_NUMBER = 0xF187
    VW_ASAM_DATASET_NUMBER = 0xF19E
    VW_ECU_HARDWARE_NUMBER = 0xF1A3
    BOOT_SOFTWARE_IDENTIFICATION = 0xF180
    FINGERPRINT = 0xF15A


# Routine identifiers (service 0x31) used by the MED17 flash flow. The exact
# routine ids are part of the ECU profile; these are the conventional values.
class Routine(IntEnum):
    ERASE_MEMORY = 0xFF00
    CHECK_PROGRAMMING_DEPENDENCIES = 0xFF01
    ERASE_MIRROR_MEMORY_DTC = 0xFF01
    CHECK_MEMORY = 0x0202  # verify checksum of a freshly written block


# --------------------------------------------------------------------------- #
# Negative response codes (ISO 14229-1 table)
# --------------------------------------------------------------------------- #
class NRC(IntEnum):
    GENERAL_REJECT = 0x10
    SERVICE_NOT_SUPPORTED = 0x11
    SUB_FUNCTION_NOT_SUPPORTED = 0x12
    INCORRECT_MESSAGE_LENGTH_OR_INVALID_FORMAT = 0x13
    RESPONSE_TOO_LONG = 0x14
    BUSY_REPEAT_REQUEST = 0x21
    CONDITIONS_NOT_CORRECT = 0x22
    REQUEST_SEQUENCE_ERROR = 0x24
    NO_RESPONSE_FROM_SUBNET_COMPONENT = 0x25
    FAILURE_PREVENTS_EXECUTION_OF_REQUESTED_ACTION = 0x26
    REQUEST_OUT_OF_RANGE = 0x31
    SECURITY_ACCESS_DENIED = 0x33
    INVALID_KEY = 0x35
    EXCEEDED_NUMBER_OF_ATTEMPTS = 0x36
    REQUIRED_TIME_DELAY_NOT_EXPIRED = 0x37
    UPLOAD_DOWNLOAD_NOT_ACCEPTED = 0x70
    TRANSFER_DATA_SUSPENDED = 0x71
    GENERAL_PROGRAMMING_FAILURE = 0x72
    WRONG_BLOCK_SEQUENCE_COUNTER = 0x73
    REQUEST_CORRECTLY_RECEIVED_RESPONSE_PENDING = 0x78
    SUB_FUNCTION_NOT_SUPPORTED_IN_ACTIVE_SESSION = 0x7E
    SERVICE_NOT_SUPPORTED_IN_ACTIVE_SESSION = 0x7F
    RPM_TOO_HIGH = 0x81
    RPM_TOO_LOW = 0x82
    ENGINE_IS_RUNNING = 0x83
    ENGINE_IS_NOT_RUNNING = 0x84
    ENGINE_RUN_TIME_TOO_LOW = 0x85
    TEMPERATURE_TOO_HIGH = 0x86
    TEMPERATURE_TOO_LOW = 0x87
    VEHICLE_SPEED_TOO_HIGH = 0x88
    VEHICLE_SPEED_TOO_LOW = 0x89
    THROTTLE_PEDAL_TOO_HIGH = 0x8A
    THROTTLE_PEDAL_TOO_LOW = 0x8B
    TRANSMISSION_RANGE_NOT_IN_NEUTRAL = 0x8C
    TRANSMISSION_RANGE_NOT_IN_GEAR = 0x8D
    BRAKE_SWITCHES_NOT_CLOSED = 0x8F
    SHIFTER_LEVER_NOT_IN_PARK = 0x90
    TORQUE_CONVERTER_CLUTCH_LOCKED = 0x91
    VOLTAGE_TOO_HIGH = 0x92
    VOLTAGE_TOO_LOW = 0x93


# The NRC value that means "keep waiting, I am still working on it".
RESPONSE_PENDING = int(NRC.REQUEST_CORRECTLY_RECEIVED_RESPONSE_PENDING)


_NRC_DESCRIPTIONS = {
    0x10: "General reject",
    0x11: "Service not supported",
    0x12: "Sub-function not supported",
    0x13: "Incorrect message length or invalid format",
    0x14: "Response too long",
    0x21: "Busy - repeat request",
    0x22: "Conditions not correct",
    0x24: "Request sequence error",
    0x25: "No response from sub-net component",
    0x26: "Failure prevents execution of requested action",
    0x31: "Request out of range",
    0x33: "Security access denied",
    0x35: "Invalid key",
    0x36: "Exceeded number of attempts",
    0x37: "Required time delay not expired",
    0x70: "Upload/download not accepted",
    0x71: "Transfer data suspended",
    0x72: "General programming failure",
    0x73: "Wrong block sequence counter",
    0x78: "Request correctly received - response pending",
    0x7E: "Sub-function not supported in active session",
    0x7F: "Service not supported in active session",
    0x81: "RPM too high",
    0x82: "RPM too low",
    0x83: "Engine is running",
    0x84: "Engine is not running",
    0x85: "Engine run time too low",
    0x86: "Temperature too high",
    0x87: "Temperature too low",
    0x88: "Vehicle speed too high",
    0x89: "Vehicle speed too low",
    0x8A: "Throttle/pedal too high",
    0x8B: "Throttle/pedal too low",
    0x8C: "Transmission range not in neutral",
    0x8D: "Transmission range not in gear",
    0x8F: "Brake switches not closed",
    0x90: "Shifter lever not in park",
    0x91: "Torque converter clutch locked",
    0x92: "Voltage too high",
    0x93: "Voltage too low",
}


def nrc_name(code: int) -> str:
    """Return a human readable description for a negative response code."""

    if 0x38 <= code <= 0x4F:
        return f"Reserved by extended data link security document (0x{code:02X})"
    if 0x94 <= code <= 0xEF:
        return f"Reserved for specific conditions not correct (0x{code:02X})"
    if 0xF0 <= code <= 0xFE:
        return f"Vehicle manufacturer specific (0x{code:02X})"
    return _NRC_DESCRIPTIONS.get(code, f"Unknown NRC (0x{code:02X})")


def service_name(sid: int) -> str:
    """Return a readable name for a request service id."""

    # A response SID is request SID + 0x40; normalise before lookup.
    request_sid = sid
    if sid & POSITIVE_RESPONSE_OFFSET and (sid - POSITIVE_RESPONSE_OFFSET) in _SERVICE_NAMES:
        request_sid = sid - POSITIVE_RESPONSE_OFFSET
    return _SERVICE_NAMES.get(request_sid, f"Service 0x{sid:02X}")


_SERVICE_NAMES = {int(s): s.name for s in Service}
