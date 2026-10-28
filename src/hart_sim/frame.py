"""HART frame codec: delimiter | address | command | byte count | [status] | data | checksum."""

from __future__ import annotations

import struct
from dataclasses import dataclass

NAN_BYTES = b"\x7f\xa0\x00\x00"

_STX_SHORT = 0x02
_STX_LONG = 0x82
_ACK_SHORT = 0x06
_ACK_LONG = 0x86


class FrameError(ValueError):
    """Raised when raw bytes are not a valid HART frame."""


def checksum(data: bytes) -> int:
    """Longitudinal parity: XOR of all bytes."""
    result = 0
    for b in data:
        result ^= b
    return result


def pack_ascii(text: str, length: int) -> bytes:
    """Pack text into 6-bit packed ASCII, padded with spaces to `length` characters."""
    if length % 4:
        raise ValueError("length must be a multiple of 4")
    text = text.upper()
    if len(text) > length:
        raise ValueError(f"text longer than {length} characters")
    text = text.ljust(length)
    values = []
    for ch in text:
        if not 0x20 <= ord(ch) <= 0x5F:
            raise ValueError(f"character {ch!r} not representable in packed ASCII")
        values.append(ord(ch) & 0x3F)
    out = bytearray()
    for i in range(0, length, 4):
        a, b, c, d = values[i : i + 4]
        out += bytes([(a << 2) | (b >> 4), ((b & 0x0F) << 4) | (c >> 2), ((c & 0x03) << 6) | d])
    return bytes(out)


def unpack_ascii(data: bytes) -> str:
    """Unpack 6-bit packed ASCII; trailing spaces are stripped."""
    if len(data) % 3:
        raise ValueError("packed ASCII length must be a multiple of 3")
    chars = []
    for i in range(0, len(data), 3):
        x, y, z = data[i : i + 3]
        for v in (x >> 2, ((x & 0x03) << 4) | (y >> 4), ((y & 0x0F) << 2) | (z >> 6), z & 0x3F):
            chars.append(chr(v | 0x40 if v < 0x20 else v))
    return "".join(chars).rstrip(" ")


def pack_float(x: float) -> bytes:
    """IEEE 754 single, big-endian. NaN becomes the HART not-available value."""
    if x != x:
        return NAN_BYTES
    try:
        return struct.pack(">f", x)
    except OverflowError as exc:
        raise ValueError("float out of single-precision range") from exc


def unpack_float(b: bytes) -> float:
    return struct.unpack(">f", b)[0]


@dataclass(frozen=True)
class Address:
    polling: int | None
    unique_id: tuple[int, int] | None  # (expanded device type, device ID)
    primary: bool
    burst: bool = False

    @classmethod
    def short(cls, polling: int, primary: bool = True) -> "Address":
        if not 0 <= polling <= 63:
            raise ValueError("polling address must be 0-63")
        return cls(polling, None, primary)

    @classmethod
    def long(cls, device_type: int, device_id: int, primary: bool = True) -> "Address":
        if not 0 <= device_type <= 0xFFFF:
            raise ValueError("expanded device type must fit in 16 bits")
        if not 0 <= device_id <= 0xFFFFFF:
            raise ValueError("device ID must fit in 24 bits")
        # The address carries only the low 14 bits of the expanded type; decode()
        # restores the top two bits as 11 for non-zero values (0xC000-0xFFFF range,
        # which covers the generic 0xFA01/0xFA02 profiles). Other types do not
        # round-trip.
        return cls(None, (device_type, device_id), primary)

    @classmethod
    def broadcast(cls, primary: bool = True) -> "Address":
        return cls.long(0, 0, primary)

    @property
    def is_long(self) -> bool:
        return self.unique_id is not None

    @property
    def is_broadcast(self) -> bool:
        return self.unique_id is not None and self.unique_id[0] & 0x3FFF == 0 and self.unique_id[1] == 0

    def encode(self) -> bytes:
        flags = (0x80 if self.primary else 0) | (0x40 if self.burst else 0)
        if self.unique_id is None:
            return bytes([flags | (self.polling or 0)])
        dev_type, dev_id = self.unique_id
        return bytes([flags | (dev_type >> 8) & 0x3F, dev_type & 0xFF]) + dev_id.to_bytes(3, "big")


@dataclass(frozen=True)
class Frame:
    address: Address
    command: int
    data: bytes = b""
    is_response: bool = False
    response_code: int = 0
    device_status: int = 0

    def encode(self) -> bytes:
        """Encode without preambles."""
        long = self.address.is_long
        if self.is_response:
            delimiter = _ACK_LONG if long else _ACK_SHORT
            body = bytes([self.response_code, self.device_status]) + self.data
        else:
            delimiter = _STX_LONG if long else _STX_SHORT
            body = self.data
        if len(body) > 255:
            raise ValueError("frame body too long")
        raw = bytes([delimiter]) + self.address.encode() + bytes([self.command, len(body)]) + body
        return raw + bytes([checksum(raw)])

    @classmethod
    def decode(cls, raw: bytes) -> "Frame":
        if len(raw) < 1:
            raise FrameError("empty frame")
        delimiter = raw[0]
        if delimiter not in (_STX_SHORT, _STX_LONG, _ACK_SHORT, _ACK_LONG):
            raise FrameError(f"bad delimiter 0x{delimiter:02x}")
        long = bool(delimiter & 0x80)
        is_response = bool(delimiter & 0x04)
        addr_len = 5 if long else 1
        header_len = 1 + addr_len + 2
        if len(raw) < header_len + 1:
            raise FrameError("frame too short")
        byte_count = raw[header_len - 1]
        if len(raw) != header_len + byte_count + 1:
            raise FrameError("byte count does not match frame length")
        if checksum(raw[:-1]) != raw[-1]:
            raise FrameError("bad checksum")
        a = raw[1 : 1 + addr_len]
        primary = bool(a[0] & 0x80)
        burst = bool(a[0] & 0x40)
        if long:
            dev_type = ((a[0] & 0x3F) << 8) | a[1]
            if dev_type:
                dev_type |= 0xC000
            address = Address(None, (dev_type, int.from_bytes(a[2:5], "big")), primary, burst)
        else:
            address = Address(a[0] & 0x3F, None, primary, burst)
        command = raw[header_len - 2]
        body = raw[header_len : header_len + byte_count]
        if is_response:
            if byte_count < 2:
                raise FrameError("response too short for status bytes")
            return cls(address, command, bytes(body[2:]), True, body[0], body[1])
        return cls(address, command, bytes(body))
