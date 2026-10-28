"""HART-IP client (master). Uses only the frame, hartip and units modules so it also works
as an independent tool against real gateways."""
from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass
from typing import Iterable

from . import hartip
from .frame import Address, Frame, FrameError, pack_ascii, pack_float, unpack_ascii, unpack_float

_DEFAULT_INACTIVITY_MS = 60_000
_POLL_TIMEOUT = 0.2

_STATUS_BITS = [
    (0x80, "device malfunction"),
    (0x40, "configuration changed"),
    (0x20, "cold start"),
    (0x10, "more status available"),
    (0x08, "loop current fixed"),
    (0x04, "loop current saturated"),
    (0x02, "non-primary variable out of limits"),
    (0x01, "PV out of limits"),
]


class HartError(Exception):
    """Communication error response, session failure or protocol failure."""


class HartTimeout(HartError, TimeoutError):
    """No answer within the timeout."""


class HartConnectionError(HartError):
    """The connection could not be opened or was lost."""


@dataclass(frozen=True)
class DeviceInfo:
    polling_address: int
    device_type: int
    device_id: int
    manufacturer_id: int
    hart_revision: int
    tag: str


@dataclass(frozen=True)
class Variables:
    current_ma: float
    pv: float
    pv_unit: int
    sv: float | None
    sv_unit: int | None
    status: int


class HartIPClient:
    def __init__(self, host: str = "127.0.0.1", port: int = hartip.PORT,
                 transport: str = "tcp", timeout: float = 2.0, primary: bool = True) -> None:
        if transport not in ("tcp", "udp"):
            raise ValueError("transport must be 'tcp' or 'udp'")
        self.host, self.port, self.transport = host, port, transport
        self.timeout, self.primary = timeout, primary
        self._sock: socket.socket | None = None
        self._seq = 0

    # --- session ---

    def open(self) -> None:
        if self._sock is not None:
            return
        if self.transport == "tcp":
            try:
                self._sock = socket.create_connection((self.host, self.port), self.timeout)
            except OSError as exc:
                raise HartConnectionError(f"cannot connect to {self.host}:{self.port}: {exc}") from exc
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.connect((self.host, self.port))
            except OSError as exc:  # e.g. socket.gaierror for an unknown host name
                sock.close()
                raise HartConnectionError(f"cannot reach {self.host}:{self.port}: {exc}") from exc
            self._sock = sock
        try:
            body = bytes([1 if self.primary else 0]) + struct.pack(">I", _DEFAULT_INACTIVITY_MS)
            header, _ = self._exchange(hartip.SESSION_INITIATE, body, self.timeout)
            if header.status != 0 or header.msg_type != hartip.RESPONSE:
                raise HartError(f"session initiate failed, HART-IP status {header.status}")
        except BaseException:
            self._close_socket()
            raise

    def close(self) -> None:
        if self._sock is None:
            return
        try:
            self._exchange(hartip.SESSION_CLOSE, b"", min(self.timeout, 0.5))
        except Exception:
            pass
        self._close_socket()

    def _close_socket(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self) -> "HartIPClient":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- transport ---

    def _recv_exact(self, count: int, deadline: float) -> bytes:
        sock = self._sock
        buf = b""
        while len(buf) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HartTimeout("no answer")
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(count - len(buf))
            except socket.timeout:
                raise HartTimeout("no answer") from None
            except OSError as exc:
                self._close_socket()
                raise HartConnectionError(f"connection lost: {exc}") from exc
            if not chunk:
                self._close_socket()
                raise HartConnectionError("connection closed by the server")
            buf += chunk
        return buf

    def _exchange(self, msg_id: int, body: bytes, timeout: float) -> tuple[hartip.Header, bytes]:
        if self._sock is None:
            raise HartConnectionError("client is not connected")
        self._seq = (self._seq + 1) & 0xFFFF
        seq = self._seq
        message = hartip.encode_message(hartip.REQUEST, msg_id, seq, body)
        deadline = time.monotonic() + timeout
        try:
            self._sock.sendall(message)
        except OSError as exc:
            self._close_socket()
            raise HartConnectionError(f"connection lost: {exc}") from exc
        while True:
            if self.transport == "tcp":
                raw = self._recv_exact(hartip.HEADER_SIZE, deadline)
                try:
                    header = hartip.decode_header(raw)
                except hartip.HartIPError as exc:
                    self._close_socket()
                    raise HartError(str(exc)) from exc
                payload = self._recv_exact(header.byte_count - hartip.HEADER_SIZE, deadline)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HartTimeout("no answer")
                self._sock.settimeout(remaining)
                try:
                    data = self._sock.recv(65535)
                except socket.timeout:
                    raise HartTimeout("no answer") from None
                except OSError:
                    continue  # e.g. ICMP port unreachable from an earlier datagram
                try:
                    header = hartip.decode_header(data)
                except hartip.HartIPError:
                    continue
                payload = data[hartip.HEADER_SIZE:header.byte_count]
            if header.sequence != seq:
                continue  # stale or foreign reply
            return header, payload

    # --- commands ---

    def command(self, address: Address, number: int, data: bytes = b"",
                timeout: float | None = None) -> Frame:
        """Send one HART command; return the response frame (response code may be nonzero)."""
        address = Address(address.polling, address.unique_id, self.primary, address.burst)
        request = Frame(address, number, data).encode()
        header, payload = self._exchange(hartip.PDU, request,
                                         self.timeout if timeout is None else timeout)
        if header.msg_type != hartip.RESPONSE:
            raise HartError(f"HART-IP error response, status {header.status}")
        if header.status != 0:
            raise HartError(f"HART-IP status {header.status}")
        try:
            frame = Frame.decode(payload)
        except FrameError as exc:
            raise HartError(f"invalid HART response: {exc}") from exc
        if not frame.is_response:
            raise HartError("reply is not a HART response frame")
        if frame.response_code & 0x80:
            raise HartError(f"communication error 0x{frame.response_code:02x}")
        return frame

    def poll(self, addresses: Iterable[int] | None = None,
             max_address: int = 63) -> list[DeviceInfo]:
        """Look for devices at polling addresses 0..max_address (or exactly `addresses`)."""
        found = []
        if addresses is None:
            addresses = range(max_address + 1)
        for polling in addresses:
            addr = Address.short(polling)
            try:
                f0 = self.command(addr, 0, timeout=_POLL_TIMEOUT)
                if f0.response_code != 0 or len(f0.data) < 21:
                    continue
                # Devices answer short-frame requests only for command 0: use the long address.
                f13 = self.command(_long_address(f0.data), 13, timeout=max(_POLL_TIMEOUT, 0.5))
            except HartTimeout:
                continue
            info = _device_info(polling, f0.data, f13)
            if info is not None:
                found.append(info)
        return found

    def find(self, tag: str) -> "DeviceHandle":
        try:
            frame = self.command(Address.broadcast(), 11, pack_ascii(tag, 8))
        except HartTimeout:
            raise LookupError(f"no device with tag {tag!r} answered") from None
        if frame.response_code != 0 or len(frame.data) < 12:
            raise LookupError(f"no device with tag {tag!r}")
        return DeviceHandle(self, _long_address(frame.data))

    def handle(self, polling: int) -> "DeviceHandle":
        """Handle for the device at a polling address (long address learnt via command 0)."""
        frame = self.command(Address.short(polling), 0)
        if frame.response_code != 0 or len(frame.data) < 12:
            raise HartError(f"command 0 failed, response code {frame.response_code}")
        return DeviceHandle(self, _long_address(frame.data))


def _long_address(d0: bytes) -> Address:
    return Address.long(int.from_bytes(d0[1:3], "big"), int.from_bytes(d0[9:12], "big"))


def _device_info(polling: int, d0: bytes, f13: Frame) -> DeviceInfo | None:
    if len(d0) < 21 or f13.response_code != 0 or len(f13.data) < 6:
        return None
    return DeviceInfo(polling, int.from_bytes(d0[1:3], "big"), int.from_bytes(d0[9:12], "big"),
                      int.from_bytes(d0[17:19], "big"), d0[4], unpack_ascii(f13.data[:6]))


class DeviceHandle:
    def __init__(self, client: HartIPClient, address: Address) -> None:
        self.client, self.address = client, address

    def _ok(self, number: int, data: bytes = b"") -> Frame:
        frame = self.client.command(self.address, number, data)
        if frame.response_code != 0:
            raise HartError(f"command {number}: response code {frame.response_code}")
        return frame

    def read_variables(self) -> Variables:
        frame = self._ok(3)
        d = frame.data
        if len(d) < 9:
            raise HartError("command 3: response too short")
        sv = sv_unit = None
        if len(d) >= 14:
            sv_unit, sv = d[9], unpack_float(d[10:14])
        return Variables(unpack_float(d[0:4]), unpack_float(d[5:9]), d[4], sv, sv_unit,
                         frame.device_status)

    def read_range(self) -> tuple[float, float, int]:
        d = self._ok(15).data
        if len(d) < 11:
            raise HartError("command 15: response too short")
        return unpack_float(d[7:11]), unpack_float(d[3:7]), d[2]

    def write_range(self, lower: float, upper: float, unit: int | None = None) -> int:
        """Command 35 (wire order: upper, lower). Returns the response code."""
        if unit is None:
            unit = self.read_range()[2]
        data = bytes([unit]) + pack_float(upper) + pack_float(lower)
        return self.client.command(self.address, 35, data).response_code

    @staticmethod
    def status_text(status: int) -> list[str]:
        return [text for bit, text in _STATUS_BITS if status & bit]
