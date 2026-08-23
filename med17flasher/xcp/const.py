"""XCP protocol constants (ASAM MCD-1 XCP).

Only the subset needed for measurement/calibration over CAN is defined here:
the connection/identification commands, memory read (``SHORT_UPLOAD`` /
``SET_MTA`` + ``UPLOAD``), and the synchronous data acquisition (DAQ) command
set used to stream live values.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Command codes (master -> slave), the first byte of a CTO request.
# --------------------------------------------------------------------------- #
CONNECT = 0xFF
DISCONNECT = 0xFE
GET_STATUS = 0xFD
SYNCH = 0xFC
GET_COMM_MODE_INFO = 0xFB
GET_ID = 0xFA
SET_REQUEST = 0xF9
GET_SEED = 0xF8
UNLOCK = 0xF7
SET_MTA = 0xF6
UPLOAD = 0xF5
SHORT_UPLOAD = 0xF4
BUILD_CHECKSUM = 0xF3
DOWNLOAD = 0xF0
SHORT_DOWNLOAD = 0xED
# DAQ
GET_DAQ_PROCESSOR_INFO = 0xDA
GET_DAQ_RESOLUTION_INFO = 0xD9
GET_DAQ_LIST_INFO = 0xD8
GET_DAQ_EVENT_INFO = 0xD7
FREE_DAQ = 0xD6
ALLOC_DAQ = 0xD5
ALLOC_ODT = 0xD4
ALLOC_ODT_ENTRY = 0xD3
SET_DAQ_PTR = 0xE2
WRITE_DAQ = 0xE1
SET_DAQ_LIST_MODE = 0xE0
GET_DAQ_LIST_MODE = 0xDF
START_STOP_DAQ_LIST = 0xDE
START_STOP_SYNCH = 0xDD

COMMAND_NAMES = {
    CONNECT: "CONNECT", DISCONNECT: "DISCONNECT", GET_STATUS: "GET_STATUS",
    SYNCH: "SYNCH", GET_COMM_MODE_INFO: "GET_COMM_MODE_INFO", GET_ID: "GET_ID",
    SET_MTA: "SET_MTA", UPLOAD: "UPLOAD", SHORT_UPLOAD: "SHORT_UPLOAD",
    BUILD_CHECKSUM: "BUILD_CHECKSUM", DOWNLOAD: "DOWNLOAD",
    SHORT_DOWNLOAD: "SHORT_DOWNLOAD",
    GET_DAQ_PROCESSOR_INFO: "GET_DAQ_PROCESSOR_INFO",
    GET_DAQ_RESOLUTION_INFO: "GET_DAQ_RESOLUTION_INFO",
    FREE_DAQ: "FREE_DAQ", ALLOC_DAQ: "ALLOC_DAQ", ALLOC_ODT: "ALLOC_ODT",
    ALLOC_ODT_ENTRY: "ALLOC_ODT_ENTRY", SET_DAQ_PTR: "SET_DAQ_PTR",
    WRITE_DAQ: "WRITE_DAQ", SET_DAQ_LIST_MODE: "SET_DAQ_LIST_MODE",
    START_STOP_DAQ_LIST: "START_STOP_DAQ_LIST",
    START_STOP_SYNCH: "START_STOP_SYNCH",
}

# --------------------------------------------------------------------------- #
# Packet identifiers (slave -> master), the first byte of a response frame.
# --------------------------------------------------------------------------- #
PID_RES = 0xFF   # positive response
PID_ERR = 0xFE   # error
PID_EV = 0xFD    # event
PID_SERV = 0xFC  # service request
# DTO (DAQ data) frames use a PID in 0x00..0xFB (the ODT/PID number).

# --------------------------------------------------------------------------- #
# Error codes (second byte of a PID_ERR response).
# --------------------------------------------------------------------------- #
ERR_CMD_SYNCH = 0x00
ERR_CMD_BUSY = 0x10
ERR_DAQ_ACTIVE = 0x11
ERR_PGM_ACTIVE = 0x12
ERR_CMD_UNKNOWN = 0x20
ERR_CMD_SYNTAX = 0x21
ERR_OUT_OF_RANGE = 0x22
ERR_WRITE_PROTECTED = 0x23
ERR_ACCESS_DENIED = 0x24
ERR_ACCESS_LOCKED = 0x25
ERR_PAGE_NOT_VALID = 0x26
ERR_MODE_NOT_VALID = 0x27
ERR_SEGMENT_NOT_VALID = 0x28
ERR_SEQUENCE = 0x29
ERR_DAQ_CONFIG = 0x2A
ERR_MEMORY_OVERFLOW = 0x30
ERR_GENERIC = 0x31
ERR_VERIFY = 0x32

ERROR_NAMES = {
    ERR_CMD_SYNCH: "ERR_CMD_SYNCH", ERR_CMD_BUSY: "ERR_CMD_BUSY",
    ERR_DAQ_ACTIVE: "ERR_DAQ_ACTIVE", ERR_PGM_ACTIVE: "ERR_PGM_ACTIVE",
    ERR_CMD_UNKNOWN: "ERR_CMD_UNKNOWN", ERR_CMD_SYNTAX: "ERR_CMD_SYNTAX",
    ERR_OUT_OF_RANGE: "ERR_OUT_OF_RANGE", ERR_WRITE_PROTECTED: "ERR_WRITE_PROTECTED",
    ERR_ACCESS_DENIED: "ERR_ACCESS_DENIED", ERR_ACCESS_LOCKED: "ERR_ACCESS_LOCKED",
    ERR_PAGE_NOT_VALID: "ERR_PAGE_NOT_VALID", ERR_MODE_NOT_VALID: "ERR_MODE_NOT_VALID",
    ERR_SEGMENT_NOT_VALID: "ERR_SEGMENT_NOT_VALID", ERR_SEQUENCE: "ERR_SEQUENCE",
    ERR_DAQ_CONFIG: "ERR_DAQ_CONFIG", ERR_MEMORY_OVERFLOW: "ERR_MEMORY_OVERFLOW",
    ERR_GENERIC: "ERR_GENERIC", ERR_VERIFY: "ERR_VERIFY",
}

# COMM_MODE_BASIC bits (CONNECT response byte 2)
CMB_BYTE_ORDER_MOTOROLA = 0x01  # 1 = big-endian (Motorola), 0 = little (Intel)
CMB_ADDRESS_GRANULARITY_MASK = 0x06
CMB_SLAVE_BLOCK_MODE = 0x40
CMB_OPTIONAL = 0x80

# START_STOP_DAQ_LIST / START_STOP_SYNCH modes
DAQ_STOP = 0x00
DAQ_START = 0x01
DAQ_SELECT = 0x02
DAQ_STOP_ALL = 0x00
DAQ_START_SELECTED = 0x01
DAQ_STOP_SELECTED = 0x02

# SET_DAQ_LIST_MODE mode bits
DAQ_MODE_DIRECTION_STIM = 0x02
DAQ_MODE_TIMESTAMP = 0x10
DAQ_MODE_PID_OFF = 0x20
