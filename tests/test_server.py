import socket
import struct
import time

import pytest

from hart_sim import hartip
from hart_sim.device import Device
from hart_sim.frame import Address, Frame
from hart_sim.profile import load_profile
from hart_sim.server import Server

TIMEOUT = 5.0


def make_device(polling=0, device_id=1, profile="pressure"):
    return Device(load_profile(profile), polling_address=polling, device_id=device_id)


def start(devices, **kw):
    srv = Server(devices, port=0, **kw)
    srv.start()
    return srv


@pytest.fixture
def server():
    srv = start([make_device()])
    yield srv
    srv.stop()


def init_body(ms=30000):
    return struct.pack(">BI", 1, ms)


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


class Tcp:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT)

    def request(self, msg_id, seq=1, body=b""):
        self.sock.sendall(hartip.encode_message(hartip.REQUEST, msg_id, seq, body))
        return self.read()

    def read(self):
        header = hartip.decode_header(recv_exact(self.sock, 8))
        return header, recv_exact(self.sock, header.byte_count - 8)

    def close(self):
        self.sock.close()


class Udp:
    def __init__(self, port):
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(TIMEOUT)

    def request(self, msg_id, seq=1, body=b""):
        self.sock.sendto(hartip.encode_message(hartip.REQUEST, msg_id, seq, body),
                         ("127.0.0.1", self.port))
        return self.read()

    def read(self):
        data, _ = self.sock.recvfrom(65535)
        header = hartip.decode_header(data)
        assert header.byte_count == len(data)
        return header, data[8:]

    def close(self):
        self.sock.close()


def open_session(client, ms=30000):
    header, body = client.request(hartip.SESSION_INITIATE, 1, init_body(ms))
    assert header.msg_type == hartip.RESPONSE and header.status == 0
    return header, body


def cmd0(polling=0):
    return Frame(Address.short(polling), 0).encode()


def test_tcp_session_and_command0(server):
    c = Tcp(server.port)
    open_session(c)
    header, body = c.request(hartip.PDU, 2, cmd0())
    assert (header.msg_type, header.msg_id, header.status) == (hartip.RESPONSE, hartip.PDU, 0)
    frame = Frame.decode(body)
    assert frame.is_response and frame.command == 0 and frame.data[4] == 7
    header, _ = c.request(hartip.KEEP_ALIVE, 3)
    assert header.msg_type == hartip.RESPONSE and header.msg_id == hartip.KEEP_ALIVE
    header, _ = c.request(hartip.SESSION_CLOSE, 4)
    assert header.msg_id == hartip.SESSION_CLOSE
    assert c.sock.recv(1) == b""  # server closed the connection
    c.close()


def test_udp_session_and_command0(server):
    c = Udp(server.port)
    open_session(c)
    header, body = c.request(hartip.PDU, 2, cmd0())
    assert header.msg_type == hartip.RESPONSE and Frame.decode(body).command == 0
    c.close()


def test_udp_command3_with_long_address():
    dev = make_device()
    srv = start([dev])
    try:
        c = Udp(srv.port)
        open_session(c)
        request = Frame(Address.long(*dev.unique_id), 3).encode()
        header, body = c.request(hartip.PDU, 2, request)
        frame = Frame.decode(body)
        assert frame.command == 3 and frame.response_code == 0
        c.close()
    finally:
        srv.stop()


def test_pdu_without_session_rejected(server):
    for client in (Tcp(server.port), Udp(server.port)):
        header, body = client.request(hartip.PDU, 7, cmd0())
        assert header.msg_type == hartip.ERROR and header.status == 16
        assert header.sequence == 7 and body == b""
        client.close()


def test_session_timeout(server):
    tcp = Tcp(server.port)
    open_session(tcp, ms=1000)  # minimum enforced
    udp = Udp(server.port)
    header, body = open_session(udp, ms=10)  # below minimum: raised to 1000 ms
    assert struct.unpack(">I", body[1:])[0] == 1000
    time.sleep(1.6)
    assert tcp.sock.recv(1) == b""
    header, _ = udp.request(hartip.PDU, 9, cmd0())
    assert header.msg_type == hartip.ERROR and header.status == 16
    tcp.close()
    udp.close()


def test_keep_alive_refreshes_session(server):
    c = Tcp(server.port)
    open_session(c, ms=1000)
    for seq in range(4):
        time.sleep(0.3)
        header, _ = c.request(hartip.KEEP_ALIVE, seq)
        assert header.msg_type == hartip.RESPONSE
    c.close()


def test_garbage_does_not_kill_server(server):
    for junk in (b"\x01\x00",
                 bytes([1, 0, 3, 0, 0, 1, 0x01, 0xF4]) + b"abc",
                 bytes([9, 0, 3, 0, 0, 1, 0, 8]),
                 bytes([1, 0, 3, 0, 0, 1, 0, 3])):
        s = socket.create_connection(("127.0.0.1", server.port), timeout=TIMEOUT)
        s.sendall(junk)
        if junk[:1] == b"\x09" or junk[-1:] == b"\x03":
            s.settimeout(TIMEOUT)
            assert s.recv(1) == b""  # closed by the server
        s.close()
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.sendto(b"\x01\x00", ("127.0.0.1", server.port))
    u.sendto(bytes([1, 0, 3, 0, 0, 1, 0x01, 0xF4]) + b"abc", ("127.0.0.1", server.port))
    u.close()
    for client in (Tcp(server.port), Udp(server.port)):
        open_session(client)
        header, body = client.request(hartip.PDU, 1, cmd0())
        assert Frame.decode(body).command == 0
        client.close()


def test_bad_pdu_gets_no_answer_but_session_survives(server):
    c = Tcp(server.port)
    open_session(c)
    bad = bytearray(cmd0())
    bad[-1] ^= 0xFF
    c.sock.sendall(hartip.encode_message(hartip.REQUEST, hartip.PDU, 1, bytes(bad)))
    c.sock.settimeout(0.5)
    with pytest.raises(socket.timeout):
        c.sock.recv(1)
    c.sock.settimeout(TIMEOUT)
    header, body = c.request(hartip.PDU, 2, cmd0())
    assert Frame.decode(body).command == 0
    c.close()


def test_max_sessions():
    srv = start([make_device()], max_sessions=1)
    try:
        a, b = Tcp(srv.port), Udp(srv.port)
        open_session(a)
        header, _ = b.request(hartip.SESSION_INITIATE, 1, init_body())
        assert header.status == 15
        header, _ = b.request(hartip.PDU, 2, cmd0())
        assert header.msg_type == hartip.ERROR
        a.request(hartip.SESSION_CLOSE, 3)
        time.sleep(0.2)
        open_session(b)  # slot is free again
        a.close()
        b.close()
    finally:
        srv.stop()


def test_two_devices_routed_by_address():
    d1, d2 = make_device(1, 11), make_device(2, 22, "temperature")
    srv = start([d1, d2])
    try:
        for client in (Tcp(srv.port), Udp(srv.port)):
            open_session(client)
            for dev, poll in ((d1, 1), (d2, 2)):
                header, body = client.request(hartip.PDU, 1, cmd0(poll))
                assert Frame.decode(body).data == dev.handle(Frame(Address.short(poll), 0)).data
            assert Frame.decode(client.request(hartip.PDU, 1, cmd0(1))[1]).data != \
                Frame.decode(client.request(hartip.PDU, 1, cmd0(2))[1]).data
            client.close()
        assert srv.route(Frame(Address.short(3), 0)) is None
        assert srv.route(Frame(Address.short(2), 0)).data == \
            d2.handle(Frame(Address.short(2), 0)).data
    finally:
        srv.stop()


def test_no_response_and_delay_faults():
    dev = make_device()
    srv = start([dev])
    try:
        for client in (Tcp(srv.port), Udp(srv.port)):
            open_session(client)
            dev.faults.inject("no_response")
            client.sock.sendall(hartip.encode_message(hartip.REQUEST, hartip.PDU, 1, cmd0())) \
                if isinstance(client, Tcp) else \
                client.sock.sendto(hartip.encode_message(hartip.REQUEST, hartip.PDU, 1, cmd0()),
                                   ("127.0.0.1", srv.port))
            client.sock.settimeout(0.5)
            with pytest.raises(socket.timeout):
                client.read()
            client.sock.settimeout(TIMEOUT)
            # the fault was consumed: the next request is answered
            assert client.request(hartip.PDU, 2, cmd0())[0].msg_type == hartip.RESPONSE
            dev.faults.inject("delay", seconds=0.3)
            start_t = time.monotonic()
            client.request(hartip.PDU, 3, cmd0())
            assert time.monotonic() - start_t >= 0.29
            dev.faults.clear("delay")
            client.close()
    finally:
        srv.stop()


def test_sequence_number_echoed(server):
    for client in (Tcp(server.port), Udp(server.port)):
        header, _ = client.request(hartip.SESSION_INITIATE, 0xBEEF, init_body())
        assert header.sequence == 0xBEEF
        header, _ = client.request(hartip.PDU, 0xFFFF, cmd0())
        assert header.sequence == 0xFFFF
        header, _ = client.request(hartip.KEEP_ALIVE, 0)
        assert header.sequence == 0
        client.close()


def test_stop_closes_connections_and_ports():
    srv = start([make_device()])
    port = srv.port
    c = Tcp(port)
    open_session(c)
    srv.stop()
    assert c.sock.recv(1) == b""
    c.close()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.bind(("127.0.0.1", port))  # UDP port is free again
    u.close()
    srv.stop()  # idempotent


def closed_by_server(sock, timeout=TIMEOUT):
    sock.settimeout(timeout)
    try:
        return sock.recv(1) == b""
    except ConnectionError:
        return True


def test_tcp_closed_after_status_15_and_error_16():
    srv = start([make_device()], max_sessions=1)
    try:
        a = Tcp(srv.port)
        open_session(a)
        b = Tcp(srv.port)
        header, _ = b.request(hartip.SESSION_INITIATE, 1, init_body())
        assert header.status == 15
        assert closed_by_server(b.sock)
        c = Tcp(srv.port)
        header, _ = c.request(hartip.PDU, 1, cmd0())
        assert header.msg_type == hartip.ERROR and header.status == 16
        assert closed_by_server(c.sock)
        for x in (a, b, c):
            x.close()
    finally:
        srv.stop()


def test_tcp_without_session_times_out(monkeypatch, server):
    import hart_sim.server as module
    monkeypatch.setattr(module, "_READ_TIMEOUT", 0.5)
    s = socket.create_connection(("127.0.0.1", server.port), timeout=TIMEOUT)
    assert closed_by_server(s)
    s.close()


def test_tcp_closed_when_session_expired_at_request():
    srv = start([make_device()])
    try:
        c = Tcp(srv.port)
        open_session(c, ms=1000)
        time.sleep(1.2)
        try:
            header, _ = c.request(hartip.PDU, 1, cmd0())
            assert header.msg_type == hartip.ERROR
        except ConnectionError:
            pass
        assert closed_by_server(c.sock)
        c.close()
    finally:
        srv.stop()


@pytest.mark.parametrize("raw", [
    hartip.encode_message(hartip.REQUEST, hartip.SESSION_INITIATE, 1, b"\x01\x00"),
    hartip.encode_message(hartip.RESPONSE, hartip.KEEP_ALIVE, 1),
    hartip.encode_message(hartip.REQUEST, 9, 1),
])
def test_malformed_requests_close_tcp_and_drop_udp(server, raw):
    tcp = Tcp(server.port)
    open_session(tcp)
    tcp.sock.sendall(raw)
    assert closed_by_server(tcp.sock)
    tcp.close()
    udp = Udp(server.port)
    open_session(udp)
    udp.sock.sendto(raw, ("127.0.0.1", server.port))
    udp.sock.settimeout(0.5)
    with pytest.raises(socket.timeout):
        udp.read()
    udp.sock.settimeout(TIMEOUT)
    assert udp.request(hartip.PDU, 2, cmd0())[0].msg_type == hartip.RESPONSE
    udp.close()


def test_unknown_message_id_does_not_refresh_timer(server):
    udp = Udp(server.port)
    open_session(udp, ms=1000)
    for _ in range(3):
        time.sleep(0.4)
        udp.sock.sendto(hartip.encode_message(hartip.REQUEST, 9, 1), ("127.0.0.1", server.port))
    time.sleep(0.2)
    header, _ = udp.request(hartip.PDU, 2, cmd0())
    assert header.msg_type == hartip.ERROR and header.status == 16
    udp.close()
