"""HART-IP message header codec (version 1, unencrypted)."""

from __future__ import annotations

import struct
from dataclasses import dataclass

PORT = 5094
VERSION = 1
HEADER_SIZE = 8

# message types
REQUEST = 0
RESPONSE = 1
ERROR = 3
NAK = 15

# message IDs
SESSION_INITIATE = 0
SESSION_CLOSE = 1
KEEP_ALIVE = 2
PDU = 3

_HEADER = struct.Struct(">BBBBHH")


class HartIPError(ValueError):
    """Raised when bytes are not a valid HART-IP message header."""


@dataclass(frozen=True)
class Header:
    version: int
    msg_type: int
    msg_id: int
    status: int
    sequence: int
    byte_count: int  # whole message including the 8 header bytes


def encode_message(msg_type: int, msg_id: int, sequence: int, body: bytes = b"",
                   status: int = 0) -> bytes:
    total = HEADER_SIZE + len(body)
    if total > 0xFFFF:
        raise ValueError("message too long")
    return _HEADER.pack(VERSION, msg_type, msg_id, status, sequence & 0xFFFF, total) + body


def decode_header(raw: bytes) -> Header:
    if len(raw) < HEADER_SIZE:
        raise HartIPError(f"header needs {HEADER_SIZE} bytes, got {len(raw)}")
    version, msg_type, msg_id, status, sequence, count = _HEADER.unpack(raw[:HEADER_SIZE])
    if version != VERSION:
        raise HartIPError(f"unsupported HART-IP version {version}")
    if count < HEADER_SIZE:
        raise HartIPError(f"byte count {count} is smaller than the header")
    return Header(version, msg_type, msg_id, status, sequence, count)
