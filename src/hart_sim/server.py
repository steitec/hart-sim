"""HART-IP server: TCP and UDP on one port number, routing PDUs to simulated devices.

Lock order: the server's session lock is never held while calling into a Device
(Device.handle takes Device.lock, then FaultSet._lock).
"""

from __future__ import annotations

import logging
import os
import socket
import socketserver
import struct
import threading
import time

from . import hartip
from .device import Device
from .frame import Frame, FrameError

MIN_INACTIVITY_MS = 1000
STATUS_INVALID_SELECTION = 2
STATUS_ALL_SESSIONS_IN_USE = 15
STATUS_ACCESS_RESTRICTED = 16
_READ_TIMEOUT = 5.0  # a message that started must finish within this
_POLL = 0.05
_log = logging.getLogger(__name__)


class Server:
    def __init__(self, devices: list[Device], host: str = "127.0.0.1", port: int = hartip.PORT,
                 max_sessions: int = 8):
        self.devices = list(devices)
        self.host = host
        self._port = port
        self.max_sessions = max_sessions
        self._lock = threading.Lock()  # guards _sessions only
        self._sessions: dict[object, float] = {}  # key -> deadline (monotonic)
        self._timeouts: dict[object, float] = {}  # key -> inactivity time in seconds
        self._stopping = threading.Event()
        self._tcp: _TcpServer | None = None
        self._udp: socket.socket | None = None
        self._threads: list[threading.Thread] = []

    @property
    def port(self) -> int:
        return self._port

    # --- lifecycle ---

    def start(self) -> None:
        self._stopping.clear()
        tcp = udp = None
        attempts = 10 if self._port == 0 else 1
        for attempt in range(attempts):
            tcp = _TcpServer((self.host, self._port), _Handler, self)
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                udp.bind((self.host, tcp.server_address[1]))
                break
            except OSError:
                tcp.server_close()
                udp.close()
                if attempt == attempts - 1:
                    raise
        self._tcp, self._udp = tcp, udp
        self._port = tcp.server_address[1]
        udp.settimeout(_POLL)
        # A short poll interval keeps stop() fast (shutdown waits for one interval).
        for target, name in ((lambda: tcp.serve_forever(poll_interval=0.05), "hart-sim-tcp"),
                             (self._udp_loop, "hart-sim-udp")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        if self._tcp is None:
            return
        self._stopping.set()
        self._tcp.shutdown()
        self._tcp.server_close()
        self._tcp.close_connections()
        for thread in self._threads + self._tcp.handler_threads():
            thread.join(timeout=5)
        self._threads.clear()
        self._udp.close()
        self._tcp = self._udp = None
        with self._lock:
            self._sessions.clear()
            self._timeouts.clear()

    def __enter__(self) -> "Server":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # --- routing ---

    def route(self, frame: Frame) -> Frame | None:
        return self._dispatch(frame, apply_faults=False)

    def _dispatch(self, frame: Frame, apply_faults: bool) -> Frame | None:
        for dev in self.devices:
            if not dev.matches(frame.address, frame.command, frame.data):
                continue
            if apply_faults and dev.faults.take("no_response") is not None:
                return None
            response = dev.handle(frame)
            if response is not None and apply_faults:
                delay = dev.faults.take("delay")
                if delay is not None:
                    self._stopping.wait(delay["seconds"])
            return response
        return None

    # --- sessions ---

    def _purge(self, now: float) -> None:
        for key in [k for k, deadline in self._sessions.items() if deadline <= now]:
            del self._sessions[key]
            self._timeouts.pop(key, None)

    def _deadline(self, key) -> float | None:
        with self._lock:
            return self._sessions.get(key)

    def _drop(self, key) -> None:
        with self._lock:
            self._sessions.pop(key, None)
            self._timeouts.pop(key, None)

    def _handle_message(self, key, header: hartip.Header, body: bytes) -> tuple[bytes | None, bool]:
        """Process one request. Returns (response or None, close_afterwards).

        close_afterwards: the TCP connection must be closed (UDP ignores it: the
        datagram is simply dropped or answered). True for malformed input, for
        status 15 / error 16 replies and after Session Close.
        """
        if header.msg_type != hartip.REQUEST or header.msg_id not in (
                hartip.SESSION_INITIATE, hartip.SESSION_CLOSE, hartip.KEEP_ALIVE, hartip.PDU):
            return None, True
        seq = header.sequence
        now = time.monotonic()
        if header.msg_id == hartip.SESSION_INITIATE:
            if len(body) != 5:
                return None, True
            if body[0] > 1:  # Host Type: 0 = secondary, 1 = primary master
                return hartip.encode_message(hartip.RESPONSE, header.msg_id, seq, body,
                                             STATUS_INVALID_SELECTION), True
            milliseconds = max(struct.unpack(">I", body[1:5])[0], MIN_INACTIVITY_MS)
            body = body[:1] + struct.pack(">I", milliseconds)
            with self._lock:
                self._purge(now)
                if key not in self._sessions and len(self._sessions) >= self.max_sessions:
                    return hartip.encode_message(hartip.RESPONSE, header.msg_id, seq, body,
                                                 STATUS_ALL_SESSIONS_IN_USE), True
                self._sessions[key] = now + milliseconds / 1000.0
                self._timeouts[key] = milliseconds / 1000.0
            return hartip.encode_message(hartip.RESPONSE, header.msg_id, seq, body), False
        with self._lock:
            self._purge(now)
            active = key in self._sessions
            if active:
                self._sessions[key] = now + self._timeouts[key]
        if not active:
            return hartip.encode_message(hartip.ERROR, header.msg_id, seq, b"",
                                         STATUS_ACCESS_RESTRICTED), True
        if header.msg_id == hartip.KEEP_ALIVE:
            return hartip.encode_message(hartip.RESPONSE, header.msg_id, seq), False
        if header.msg_id == hartip.SESSION_CLOSE:
            self._drop(key)
            return hartip.encode_message(hartip.RESPONSE, header.msg_id, seq), True
        if header.msg_id == hartip.PDU:
            try:
                frame = Frame.decode(body)
            except FrameError:
                return None, False
            response = self._dispatch(frame, apply_faults=True)
            if response is None:
                return None, False
            return hartip.encode_message(hartip.RESPONSE, hartip.PDU, seq, response.encode()), False
        return None, False

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
                reply, close = self._handle_message(("udp", addr), header, data[hartip.HEADER_SIZE:])
                if reply is not None and not self._stopping.is_set():
                    sock.sendto(reply, addr)
            except OSError:
                pass
            except Exception:  # never let one datagram kill the UDP thread
                _log.debug("error handling UDP datagram from %s", addr, exc_info=True)


class _TcpServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # on Windows SO_REUSEADDR would allow port hijacking

    def __init__(self, address, handler, owner: Server):
        self.owner = owner
        self._conn_lock = threading.Lock()
        self._conns: set[socket.socket] = set()
        self._handlers: list[threading.Thread] = []
        super().__init__(address, handler)

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
        try:
            self._serve(owner, key)
        except (OSError, _Closed, hartip.HartIPError):
            pass
        except Exception:  # close this connection quietly, keep the server up
            _log.debug("error handling TCP connection", exc_info=True)
        finally:
            owner._drop(key)

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
            reply, close = owner._handle_message(key, header, body)
            if reply is not None:
                self.request.settimeout(_READ_TIMEOUT)
                self.request.sendall(reply)
            if close:
                return
