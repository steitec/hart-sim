"""HART-IP client (master). Uses only the frame, hartip and units modules so it also works
as an independent tool against real gateways."""
from __future__ import annotations

import collections
import math
import socket
import struct
import time
from dataclasses import dataclass
from typing import Iterable

from . import hartip
from .frame import Address, Frame, FrameError, pack_ascii, pack_float, unpack_ascii, unpack_float

_DEFAULT_INACTIVITY_MS = 60_000
_POLL_TIMEOUT = 0.2

# Subscription flags of command 533 (same values as hart_sim.server; the client does not import
# the server).
PROCESS_DATA = 0x0001  # bursts of commands 1, 2, 3, 9, 33
DEVICE_STATUS = 0x0004  # bursts of command 48

_TIME_UNIT = 32000  # HART "Time": unsigned 32 bit, 1/32 ms
_UNUSED_SLOT = 250
_DEFAULT_MAX_PERIOD = 60.0
_WARNING_ADJUSTED = 8  # "update times adjusted" (command 103)
_MAX_PUBLISHES = 1000

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


@dataclass(frozen=True)
class Publish:
    """A burst frame received in a HART-IP Publish message."""
    frame: Frame
    received: float  # time.monotonic() when the client parsed it (not when it was sent)


@dataclass(frozen=True)
class BurstConfig:
    """Burst message configuration as read with command 105 (times in seconds)."""
    control: int
    command: int
    slots: tuple[int, ...]
    period: float
    max_period: float
    trigger_mode: int
    trigger_class: int
    trigger_unit: int
    trigger_level: float


class HartIPClient:
    def __init__(self, host: str = "127.0.0.1", port: int = hartip.PORT,
                 transport: str = "tcp", timeout: float = 2.0, primary: bool = True,
                 inactivity_ms: int = _DEFAULT_INACTIVITY_MS) -> None:
        if transport not in ("tcp", "udp"):
            raise ValueError("transport must be 'tcp' or 'udp'")
        self.host, self.port, self.transport = host, port, transport
        self.timeout, self.primary = timeout, primary
        self.inactivity_ms = inactivity_ms  # requested; the server may raise it
        self._inactivity_s = inactivity_ms / 1000.0  # negotiated (Session Initiate response)
        self._last_sent = time.monotonic()  # when the last request went out
        self._sock: socket.socket | None = None
        self._seq = 0
        self._rx = bytearray()  # TCP bytes read but not yet part of a complete message
        # Publish messages read while waiting for something else, oldest first.
        self.publishes: collections.deque[Publish] = collections.deque(maxlen=_MAX_PUBLISHES)

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
            sock = None
            try:
                family, addr = hartip.resolve(self.host, self.port, socket.SOCK_DGRAM)
                sock = socket.socket(family, socket.SOCK_DGRAM)
                sock.connect(addr)
            except OSError as exc:  # e.g. socket.gaierror for an unknown host name
                if sock is not None:
                    sock.close()
                raise HartConnectionError(f"cannot reach {self.host}:{self.port}: {exc}") from exc
            self._sock = sock
        try:
            body = bytes([1 if self.primary else 0]) + struct.pack(">I", self.inactivity_ms)
            header, reply = self._exchange(hartip.SESSION_INITIATE, body, self.timeout)
            if header.status != 0 or header.msg_type != hartip.RESPONSE:
                raise HartError(f"session initiate failed, HART-IP status {header.status}")
            if len(reply) >= 5:  # host type + inactivity close time the server accepted
                self._inactivity_s = struct.unpack(">I", reply[1:5])[0] / 1000.0
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
        self._rx.clear()
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

    def _fill(self, count: int, deadline: float) -> None:
        """Read until `_rx` holds `count` bytes. Bytes read stay buffered if time runs out, so
        a timeout never leaves the stream in the middle of a message."""
        sock = self._sock
        while len(self._rx) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HartTimeout("no answer")
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(max(count - len(self._rx), 4096))
            except socket.timeout:
                raise HartTimeout("no answer") from None
            except OSError as exc:
                self._close_socket()
                raise HartConnectionError(f"connection lost: {exc}") from exc
            if not chunk:
                self._close_socket()
                raise HartConnectionError("connection closed by the server")
            self._rx += chunk

    def _read_message(self, deadline: float) -> tuple[hartip.Header, bytes]:
        """Next complete HART-IP message (any type) before `deadline`."""
        if self._sock is None:
            raise HartConnectionError("client is not connected")
        if self.transport == "tcp":
            self._fill(hartip.HEADER_SIZE, deadline)
            try:
                header = hartip.decode_header(bytes(self._rx[:hartip.HEADER_SIZE]))
            except hartip.HartIPError as exc:
                self._close_socket()
                raise HartError(str(exc)) from exc
            self._fill(header.byte_count, deadline)
            payload = bytes(self._rx[hartip.HEADER_SIZE:header.byte_count])
            del self._rx[:header.byte_count]
            return header, payload
        while True:
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
            return header, data[hartip.HEADER_SIZE:header.byte_count]

    def _queue_publish(self, header: hartip.Header, payload: bytes) -> None:
        if header.msg_id != hartip.PDU:
            return
        try:
            frame = Frame.decode(payload)
        except FrameError:
            return  # not a usable burst frame: drop it
        self.publishes.append(Publish(frame, time.monotonic()))

    def _exchange(self, msg_id: int, body: bytes, timeout: float) -> tuple[hartip.Header, bytes]:
        if self._sock is None:
            raise HartConnectionError("client is not connected")
        self._seq = (self._seq + 1) & 0xFFFF
        seq = self._seq
        message = hartip.encode_message(hartip.REQUEST, msg_id, seq, body)
        deadline = time.monotonic() + timeout
        self._last_sent = time.monotonic()
        try:
            self._sock.sendall(message)
        except OSError as exc:
            self._close_socket()
            raise HartConnectionError(f"connection lost: {exc}") from exc
        while True:
            header, payload = self._read_message(deadline)
            if header.msg_type == hartip.PUBLISH:
                # Publish numbers are the server's own counter: never a response, even if the
                # number coincides with the request's.
                self._queue_publish(header, payload)
                continue
            if header.msg_type not in (hartip.RESPONSE, hartip.ERROR, hartip.NAK) \
                    or header.sequence != seq:
                continue  # stale or foreign message
            return header, payload

    # --- burst ---

    def wait_burst(self, command: int | None = None, timeout: float | None = None) -> Publish:
        """Oldest queued Publish of `command` (any command if None), else wait for one.
        Publishes of other commands stay queued. Raises HartTimeout.

        While waiting, a Keep Alive goes out whenever no request was sent for half the
        negotiated inactivity time, so a listening session is not closed by the server."""
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            for item in self.publishes:
                if command is None or item.frame.command == command:
                    self.publishes.remove(item)
                    return item
            keep_alive_at = self._last_sent + self._inactivity_s / 2
            try:
                header, payload = self._read_message(min(deadline, keep_alive_at))
            except HartTimeout:
                if time.monotonic() >= deadline:
                    raise
                self._exchange(hartip.KEEP_ALIVE, b"", self.timeout)  # queues Publishes met
                continue
            if header.msg_type == hartip.PUBLISH:
                self._queue_publish(header, payload)

    def subscribe(self, address: Address, flags: int = PROCESS_DATA,
                  all_devices: bool = True) -> int:
        """Command 533 through the device at `address` (any device of the server).
        Subscribes to all devices or only to the one at `address`. Returns the flags."""
        if all_devices:
            uid = bytes(5)
        else:
            if address.unique_id is None:
                raise ValueError("a single-device subscription needs a long address")
            dev_type, dev_id = address.unique_id
            uid = bytes([(dev_type >> 8) & 0x3F, dev_type & 0xFF]) + dev_id.to_bytes(3, "big")
        frame = self.command(address, 533, uid + struct.pack(">H", flags))
        if frame.response_code != 0:
            raise HartError(f"command 533: response code {frame.response_code}")
        if len(frame.data) < 7:
            raise HartError("command 533: response too short")
        return int.from_bytes(frame.data[5:7], "big")

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

    def _ok(self, number: int, data: bytes = b"", accept: tuple[int, ...] = (0,)) -> Frame:
        frame = self.client.command(self.address, number, data)
        if frame.response_code not in accept:
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

    # --- burst mode ---

    def subscribe(self, flags: int = PROCESS_DATA) -> int:
        """Subscribe this session to this device's bursts (command 533). Returns the flags."""
        return self.client.subscribe(self.address, flags, all_devices=False)

    def burst(self, command: int, period: float, *, max_period: float | None = None,
              variables: list[int] | None = None, message: int = 0, trigger: int = 0,
              level: float = 0.0) -> None:
        """Configure burst `message` and switch it on: 108, 107 (if `variables`), 103, 104,
        109. 104 is always sent, so `trigger` 0 (continuous) resets an earlier trigger mode.
        `variables=None` keeps the device's current slots. `max_period` defaults to 60 s (at least `period`). A trigger
        needs the unit and classification of the message's first variable, which are read
        from the device (commands 3 and 8). Response code 8 (times adjusted) is accepted."""
        if not (math.isfinite(period) and period > 0):
            raise ValueError("period must be a finite number greater than 0")
        if max_period is not None and not (math.isfinite(max_period) and max_period > 0):
            raise ValueError("max_period must be a finite number greater than 0")
        if not 0 <= command <= 65535:
            raise ValueError("command must be 0-65535")
        if not 0 <= message <= 255:
            raise ValueError("message must be 0-255")
        index = bytes([message])
        self._ok(108, command.to_bytes(2, "big") + index)
        if variables is not None:
            if len(variables) > 8:
                raise ValueError("a burst message has at most 8 device variable slots")
            self._ok(107, bytes(list(variables) + [_UNUSED_SLOT] * (8 - len(variables))) + index)
        if max_period is None:
            max_period = max(period, _DEFAULT_MAX_PERIOD)
        self._ok(103, index + _to_time(period).to_bytes(4, "big")
                 + _to_time(max_period).to_bytes(4, "big"), accept=(0, _WARNING_ADJUSTED))
        if trigger:
            first = variables[0] if variables else self.burst_config(message).slots[0]
            unit, klass = self._slot_unit_class(first)
        else:  # continuous: the device checks neither classification nor unit
            unit, klass = _UNUSED_SLOT, 0
        self._ok(104, bytes([message, trigger, klass, unit]) + pack_float(level))
        self._ok(109, bytes([1]) + index)

    def burst_off(self, message: int = 0) -> None:
        self._ok(109, bytes([0, message]))

    def burst_config(self, message: int = 0) -> BurstConfig:
        """Command 105 (layout: spec section 3.3)."""
        d = self._ok(105, bytes([message])).data
        if len(d) < 29:
            raise HartError("command 105: response too short")
        return BurstConfig(d[0], int.from_bytes(d[12:14], "big"), tuple(d[2:10]),
                           int.from_bytes(d[14:18], "big") / _TIME_UNIT,
                           int.from_bytes(d[18:22], "big") / _TIME_UNIT,
                           d[22], d[23], d[24], unpack_float(d[25:29]))

    def _slot_unit_class(self, slot_code: int) -> tuple[int, int]:
        """(unit, classification) of PV (codes 0, 246) or SV (1, 247), via commands 3 and 8."""
        is_sv = slot_code in (1, 247)
        d3 = self._ok(3).data
        d8 = self._ok(8).data
        if is_sv:
            if len(d3) < 14 or len(d8) < 2:
                raise HartError("the device has no SV to trigger on")
            return d3[9], d8[1]
        if len(d3) < 9 or len(d8) < 1:
            raise HartError("command 3/8: response too short")
        return d3[4], d8[0]


def _to_time(seconds: float) -> int:
    return min(round(seconds * _TIME_UNIT), 0xFFFFFFFF)
