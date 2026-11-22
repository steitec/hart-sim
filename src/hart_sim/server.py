"""HART-IP server: TCP and UDP on one port number, routing PDUs to simulated devices.

Lock order: the server's session lock is never held while calling into a Device
(Device.handle takes Device.lock, then FaultSet._lock), nor while sending: the burst
thread snapshots subscriptions and senders under it, releases it, then sends. A TCP
connection's send lock is taken without any other lock held.

Client subscriptions (commands 532/533) and HART-IP Publish follow the FieldComm
hipserver (Apache-2.0): Server/hssubscribe.cpp (flags PROCESS_DATA = 0x1,
DEVICE_STATUS = 0x4; code 9 for an individual vs. broadcast conflict, code 65 for an
unknown unique ID) and Server/hsqueues.cpp (Publish = message type 2, message ID 3).
"""

from __future__ import annotations

import heapq
import itertools
import logging
import os
import random
import socket
import socketserver
import struct
import threading
import time

from . import hartip
from .device import Device
from .frame import Frame, FrameError

MIN_INACTIVITY_MS = 1000
# Subscription flags of command 533 (hipserver hssubscribe.cpp, enum SubFlags). Other bits
# are stored and read back by 532 but never trigger a Publish.
PROCESS_DATA = 0x0001  # bursts of commands 1, 2, 3, 9, 33
DEVICE_STATUS = 0x0004  # bursts of command 48
_PROCESS_DATA_COMMANDS = (1, 2, 3, 9, 33)
CMD_READ_SUBSCRIPTION = 532
CMD_WRITE_SUBSCRIPTION = 533
RC_SUCCESS = 0
RC_TOO_FEW_DATA_BYTES = 5
RC_INDIVIDUAL_NOT_ALLOWED = 9  # individual vs. broadcast subscription conflict
RC_UNKNOWN_UNIQUE_ID = 65
STATUS_INVALID_SELECTION = 2
STATUS_ALL_SESSIONS_IN_USE = 15
STATUS_ACCESS_RESTRICTED = 16
_READ_TIMEOUT = 5.0  # a message that started must finish within this
_POLL = 0.05
_FREE_PORT_ATTEMPTS = 10
_DYNAMIC_PORTS = (49152, 65535)  # IANA dynamic range; also the Windows default
_log = logging.getLogger(__name__)


class Server:
    def __init__(self, devices: list[Device], host: str = "127.0.0.1", port: int = hartip.PORT,
                 max_sessions: int = 8, publish_all: bool = False):
        self.devices = list(devices)
        self.host = host
        self._port = port
        self.max_sessions = max_sessions
        self.publish_all = publish_all  # Publish to every session, with or without 533
        self._lock = threading.Lock()  # guards the session dicts below
        self._sessions: dict[object, float] = {}  # key -> deadline (monotonic)
        self._timeouts: dict[object, float] = {}  # key -> inactivity time in seconds
        # key -> {unique ID (or None = all devices): flags}
        self._subs: dict[object, dict[tuple[int, int] | None, int]] = {}
        self._publish_seq: dict[object, int] = {}  # key -> next Publish sequence number
        self._tcp_senders: dict[object, tuple[threading.Lock, socket.socket]] = {}
        self._stopping = threading.Event()
        self._tcp: _TcpServer | None = None
        self._udp: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._send_cond = threading.Condition()  # guards _send_heap
        self._send_heap: list[tuple[float, int, bytes, object]] = []
        self._send_seq = itertools.count()

    @property
    def port(self) -> int:
        return self._port

    # --- lifecycle ---

    def start(self) -> None:
        self._stopping.clear()
        tcp = udp = None
        attempts = _FREE_PORT_ATTEMPTS if self._port == 0 else 1
        for attempt in range(attempts):
            # The second try on: Windows gives out free ports one after the other and can
            # block 200 in a row for one protocol, so "any free port" stays inside the block.
            port = self._port if attempt == 0 else random.randint(*_DYNAMIC_PORTS)
            tcp = udp = None
            try:
                tcp = _TcpServer((self.host, port), _Handler, self)
                udp = socket.socket(tcp.address_family, socket.SOCK_DGRAM)
                _dual_stack(udp, self.host)
                udp.bind((self.host, tcp.server_address[1]))
                break
            except OSError:
                if tcp is not None:
                    tcp.server_close()
                if udp is not None:
                    udp.close()
                if attempt == attempts - 1:
                    raise
        self._tcp, self._udp = tcp, udp
        self._port = tcp.server_address[1]
        udp.settimeout(_POLL)
        # A short poll interval keeps stop() fast (shutdown waits for one interval).
        for target, name in ((lambda: tcp.serve_forever(poll_interval=0.05), "hart-sim-tcp"),
                             (self._udp_loop, "hart-sim-udp"),
                             (self._udp_send_loop, "hart-sim-udp-send"),
                             (self._burst_loop, "hart-sim-burst")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        if self._tcp is None:
            return
        self._stopping.set()
        with self._send_cond:
            self._send_cond.notify_all()
        self._tcp.shutdown()
        self._tcp.server_close()
        self._tcp.close_connections()
        for thread in self._threads + self._tcp.handler_threads():
            thread.join(timeout=5)
        self._threads.clear()
        with self._send_cond:
            self._send_heap.clear()  # unsent delayed replies are dropped (after all pushers ended)
        self._udp.close()
        self._tcp = self._udp = None
        with self._lock:
            self._sessions.clear()
            self._timeouts.clear()
            self._subs.clear()
            self._publish_seq.clear()
            self._tcp_senders.clear()

    def __enter__(self) -> "Server":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # --- routing ---

    def route(self, frame: Frame) -> Frame | None:
        response, _ = self._dispatch(frame, apply_faults=False)
        return response

    def _dispatch(self, frame: Frame, apply_faults: bool,
                  key=None) -> tuple[Frame | None, float]:
        """Route one frame. Returns (response or None, delay in seconds to wait first).

        Faults only act on a request the device answers: ignored requests (foreign
        address or tag) consume nothing. Commands 532/533 of a session (``key``) are
        answered by the server for any of its devices; device faults do not apply.
        """
        for dev in self.devices:
            if not dev.matches(frame.address, frame.command, frame.data):
                continue
            if key is not None and not frame.is_response and frame.command in (
                    CMD_READ_SUBSCRIPTION, CMD_WRITE_SUBSCRIPTION):
                return self._subscription_command(key, dev, frame), 0.0
            response = dev.handle(frame)
            if response is None or not apply_faults:
                return response, 0.0
            if dev.faults.take("no_response") is not None:
                return None, 0.0
            delay = dev.faults.take("delay")
            return response, float(delay["seconds"]) if delay is not None else 0.0
        return None, 0.0

    # --- subscriptions (532/533) ---

    def _unique_id(self, data: bytes) -> tuple[bool, tuple[int, int] | None]:
        """(known, unique ID) from 5 bytes in address form; None is the all-devices ID."""
        dev_type = ((data[0] & 0x3F) << 8) | data[1]  # master/burst bits are not part of it
        dev_id = int.from_bytes(data[2:5], "big")
        if dev_type == 0 and dev_id == 0:
            return True, None
        uid = (dev_type, dev_id)
        return any(_uid(dev) == uid for dev in self.devices), uid

    def _subscription_command(self, key, dev: Device, frame: Frame) -> Frame:
        data = frame.data
        need = 5 if frame.command == CMD_READ_SUBSCRIPTION else 7
        out = b""
        if len(data) < need:
            code = RC_TOO_FEW_DATA_BYTES
        else:
            known, uid = self._unique_id(data)
            if not known:
                code = RC_UNKNOWN_UNIQUE_ID
            elif frame.command == CMD_READ_SUBSCRIPTION:
                with self._lock:
                    flags = self._subs.get(key, {}).get(uid, 0)
                code, out = RC_SUCCESS, bytes(data[:5]) + struct.pack(">H", flags)
            else:
                flags = struct.unpack(">H", data[5:7])[0]
                code = self._write_subscription(key, uid, flags)
                out = bytes(data[:7]) if code == RC_SUCCESS else b""
        primary = frame.address.primary
        # Status byte as the device reports it, without consuming cold start/config changed.
        return Frame(dev._reply_address(frame, primary), frame.command, out, True, code,
                     dev.status_byte(primary))

    def _write_subscription(self, key, uid: tuple[int, int] | None, flags: int) -> int:
        with self._lock:
            if key not in self._sessions:  # closed meanwhile: store nothing
                return RC_SUCCESS
            subs = self._subs.setdefault(key, {})
            if (uid is None and any(k is not None for k in subs)) or (
                    uid is not None and None in subs):
                return RC_INDIVIDUAL_NOT_ALLOWED
            if flags:
                subs[uid] = flags
            else:
                subs.pop(uid, None)
                if not subs:
                    del self._subs[key]
            return RC_SUCCESS

    def subscriptions(self, key) -> dict[tuple[int, int] | None, int]:
        """A session's subscriptions: {unique ID or None (all devices): flags}."""
        with self._lock:
            return dict(self._subs.get(key, {}))

    # --- sessions ---

    def _forget(self, key) -> None:
        """Drop a session and everything attached to it (caller holds self._lock)."""
        self._sessions.pop(key, None)
        self._timeouts.pop(key, None)
        self._subs.pop(key, None)
        self._publish_seq.pop(key, None)

    def _purge(self, now: float) -> None:
        for key in [k for k, deadline in self._sessions.items() if deadline <= now]:
            self._forget(key)

    def _deadline(self, key) -> float | None:
        with self._lock:
            return self._sessions.get(key)

    def _drop(self, key) -> None:
        with self._lock:
            self._forget(key)

    def _handle_message(self, key, header: hartip.Header, body: bytes) -> tuple[bytes | None, bool, float]:
        """Process one request. Returns (response or None, close_afterwards, delay seconds).

        The caller applies the delay (TCP: inline wait, UDP: scheduled send).

        close_afterwards: the TCP connection must be closed (UDP ignores it: the
        datagram is simply dropped or answered). True for malformed input, for
        status 15 / error 16 replies and after Session Close.
        """
        if header.msg_type != hartip.REQUEST or header.msg_id not in (
                hartip.SESSION_INITIATE, hartip.SESSION_CLOSE, hartip.KEEP_ALIVE, hartip.PDU):
            return None, True, 0.0
        seq = header.sequence
        now = time.monotonic()
        if header.msg_id == hartip.SESSION_INITIATE:
            if len(body) != 5:
                return None, True, 0.0
            if body[0] > 1:  # Host Type: 0 = secondary, 1 = primary master
                return hartip.encode_message(hartip.RESPONSE, header.msg_id, seq, body,
                                             STATUS_INVALID_SELECTION), True, 0.0
            milliseconds = max(struct.unpack(">I", body[1:5])[0], MIN_INACTIVITY_MS)
            body = body[:1] + struct.pack(">I", milliseconds)
            with self._lock:
                self._purge(now)
                if key not in self._sessions and len(self._sessions) >= self.max_sessions:
                    return hartip.encode_message(hartip.RESPONSE, header.msg_id, seq, body,
                                                 STATUS_ALL_SESSIONS_IN_USE), True, 0.0
                if key not in self._sessions:  # a new session starts without subscriptions
                    self._forget(key)
                self._sessions[key] = now + milliseconds / 1000.0
                self._timeouts[key] = milliseconds / 1000.0
            return hartip.encode_message(hartip.RESPONSE, header.msg_id, seq, body), False, 0.0
        with self._lock:
            self._purge(now)
            active = key in self._sessions
            if active:
                self._sessions[key] = now + self._timeouts[key]
        if not active:
            return hartip.encode_message(hartip.ERROR, header.msg_id, seq, b"",
                                         STATUS_ACCESS_RESTRICTED), True, 0.0
        if header.msg_id == hartip.KEEP_ALIVE:
            return hartip.encode_message(hartip.RESPONSE, header.msg_id, seq), False, 0.0
        if header.msg_id == hartip.SESSION_CLOSE:
            self._drop(key)
            return hartip.encode_message(hartip.RESPONSE, header.msg_id, seq), True, 0.0
        if header.msg_id == hartip.PDU:
            try:
                frame = Frame.decode(body)
            except FrameError:
                return None, False, 0.0
            response, delay = self._dispatch(frame, apply_faults=True, key=key)
            if response is None:
                return None, False, 0.0
            return (hartip.encode_message(hartip.RESPONSE, hartip.PDU, seq, response.encode()),
                    False, delay)
        return None, False, 0.0

    # --- UDP ---

    def _udp_loop(self) -> None:
        sock = self._udp
        while not self._stopping.is_set():
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:  # e.g. WSAECONNRESET on Windows after an ICMP error
                if self._stopping.is_set():
                    return
                continue
            try:
                header = hartip.decode_header(data)
            except hartip.HartIPError:
                continue
            if header.byte_count != len(data):
                continue
            try:
                reply, _close, delay = self._handle_message(
                    ("udp", addr), header, data[hartip.HEADER_SIZE:])
                if reply is not None and not self._stopping.is_set():
                    if delay > 0:
                        self._send_later(time.monotonic() + delay, reply, addr)
                    else:
                        sock.sendto(reply, addr)
            except OSError:
                pass
            except Exception:  # never let one datagram kill the UDP thread
                _log.debug("error handling UDP datagram from %s", addr, exc_info=True)

    def _send_later(self, due: float, data: bytes, addr) -> None:
        """Queue a UDP reply for the monotonic time `due` (one sender thread serves all)."""
        with self._send_cond:
            heapq.heappush(self._send_heap, (due, next(self._send_seq), data, addr))
            self._send_cond.notify_all()

    def _udp_send_loop(self) -> None:
        while not self._stopping.is_set():
            with self._send_cond:
                if not self._send_heap:
                    self._send_cond.wait(_POLL)
                    continue
                wait = self._send_heap[0][0] - time.monotonic()
                if wait > 0:
                    self._send_cond.wait(min(wait, _POLL))
                    continue
                _, _, data, addr = heapq.heappop(self._send_heap)
            try:
                self._udp.sendto(data, addr)
            except OSError:
                pass
            except Exception:
                _log.debug("error sending delayed UDP reply to %s", addr, exc_info=True)


    # --- burst / Publish ---

    def _burst_loop(self) -> None:
        """Collect due burst frames of all devices and publish them (one thread per server)."""
        while not self._stopping.is_set():
            wait = _POLL
            for dev in self.devices:
                try:
                    frames = dev.burst_due(dev.clock())
                    if frames:
                        self._publish(dev, frames)
                    due = dev.next_burst_due()
                    if due is not None:
                        wait = min(wait, due - dev.clock())
                except Exception:  # never let one device stop the bursts of the others
                    _log.debug("error publishing bursts of %s", dev.tag, exc_info=True)
            if wait > 0:
                self._stopping.wait(wait)

    def _publish(self, dev: Device, frames: list[Frame]) -> None:
        uid = _uid(dev)
        messages = []  # (key, Publish message)
        with self._lock:
            self._purge(time.monotonic())
            for key in self._sessions:
                subs = self._subs.get(key, {})
                flags = subs.get(None, 0) | subs.get(uid, 0)
                for frame in frames:
                    if not (self.publish_all or _covers(flags, frame.command)):
                        continue
                    seq = self._publish_seq.get(key, 0)
                    self._publish_seq[key] = (seq + 1) & 0xFFFF
                    messages.append((key, hartip.encode_message(
                        hartip.PUBLISH, hartip.PDU, seq, frame.encode())))
            senders = {key: self._tcp_senders.get(key) for key, _ in messages}
        failed = set()
        for key, data in messages:
            if key in failed or self._stopping.is_set():
                continue
            try:
                if key[0] == "udp":
                    self._udp.sendto(data, key[1])
                else:
                    sender = senders[key]
                    if sender is None:  # connection already gone
                        raise OSError("no connection")
                    lock, sock = sender
                    with lock:
                        sock.sendall(data)
            except OSError:
                failed.add(key)
        for key in failed:
            self._drop(key)

    def _register_tcp(self, key, lock: threading.Lock, sock: socket.socket) -> None:
        with self._lock:
            self._tcp_senders[key] = (lock, sock)

    def _unregister_tcp(self, key) -> None:
        with self._lock:
            self._tcp_senders.pop(key, None)
            self._forget(key)


def _uid(dev: Device) -> tuple[int, int]:
    """A device's unique ID as the 5-byte address carries it (14-bit device type)."""
    return dev.profile.device_type & 0x3FFF, dev.device_id


def _covers(flags: int, command: int) -> bool:
    """Whether subscription flags ask for the bursts of this command (hipserver IsNeedSend)."""
    if flags & PROCESS_DATA and command in _PROCESS_DATA_COMMANDS:
        return True
    return bool(flags & DEVICE_STATUS) and command == 48


def _dual_stack(sock: socket.socket, host: str) -> None:
    """Wildcard "::" also accepts IPv4 where the platform allows it."""
    if host == "::" and hasattr(socket, "IPV6_V6ONLY"):
        try:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass


class _TcpServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # on Windows SO_REUSEADDR would allow port hijacking

    def __init__(self, address, handler, owner: Server):
        self.owner = owner
        self._conn_lock = threading.Lock()
        self._conns: set[socket.socket] = set()
        self._handlers: list[threading.Thread] = []
        self.address_family = hartip.resolve(address[0], address[1], socket.SOCK_STREAM)[0]
        super().__init__(address, handler)

    def server_bind(self) -> None:
        _dual_stack(self.socket, self.server_address[0])
        super().server_bind()

    def process_request(self, request, client_address):
        thread = threading.Thread(target=self.process_request_thread,
                                  args=(request, client_address), daemon=True,
                                  name="hart-sim-conn")
        with self._conn_lock:
            self._conns.add(request)
            self._handlers = [t for t in self._handlers if t.is_alive()]
            self._handlers.append(thread)
        thread.start()

    def shutdown_request(self, request):
        with self._conn_lock:
            self._conns.discard(request)
        super().shutdown_request(request)

    def close_connections(self) -> None:
        with self._conn_lock:
            conns = list(self._conns)
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def handler_threads(self) -> list[threading.Thread]:
        with self._conn_lock:
            return list(self._handlers)


class _Closed(Exception):
    pass


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        owner: Server = self.server.owner
        key = ("tcp", id(self.request))
        self._send_lock = threading.Lock()  # replies and Publish never interleave
        owner._register_tcp(key, self._send_lock, self.request)
        try:
            self._serve(owner, key)
        except (OSError, _Closed, hartip.HartIPError):
            pass
        except Exception:  # close this connection quietly, keep the server up
            _log.debug("error handling TCP connection", exc_info=True)
        finally:
            owner._unregister_tcp(key)

    def _read(self, count: int, first_timeout: float | None) -> bytes:
        sock = self.request
        chunks = []
        remaining = count
        timeout = first_timeout
        while remaining:
            sock.settimeout(timeout)
            try:
                chunk = sock.recv(remaining)
            except socket.timeout:
                raise _Closed from None
            if not chunk:
                raise _Closed
            chunks.append(chunk)
            remaining -= len(chunk)
            timeout = _READ_TIMEOUT
        return b"".join(chunks)

    def _serve(self, owner: Server, key) -> None:
        had_session = False
        while not owner._stopping.is_set():
            deadline = owner._deadline(key)
            if deadline is not None:
                had_session = True
                idle = deadline - time.monotonic()
                if idle <= 0:
                    raise _Closed
            elif had_session:  # expired or purged
                raise _Closed
            else:  # no Session Initiate yet
                idle = _READ_TIMEOUT
            header = hartip.decode_header(self._read(hartip.HEADER_SIZE, idle))
            body = self._read(header.byte_count - hartip.HEADER_SIZE, _READ_TIMEOUT)
            reply, close, delay = owner._handle_message(key, header, body)
            if reply is not None:
                if delay > 0 and owner._stopping.wait(delay):
                    return
                with self._send_lock:
                    self.request.settimeout(_READ_TIMEOUT)
                    self.request.sendall(reply)
            if close:
                return
