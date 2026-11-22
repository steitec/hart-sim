"""Server: client subscriptions (532/533) and HART-IP Publish of burst frames (real sockets)."""
import socket
import struct
import time

from hart_sim import hartip
from hart_sim.frame import Address, Frame
from hart_sim.server import DEVICE_STATUS, PROCESS_DATA

from test_server import Tcp, Udp, make_device, open_session, start

PERIOD = 0.1
WINDOW = 0.5  # "no publish" is asserted over this window (5 burst periods)


def uid_bytes(dev_type, dev_id):
    return bytes([(dev_type >> 8) & 0x3F, dev_type & 0xFF]) + dev_id.to_bytes(3, "big")


ALL = bytes(5)


class Session:
    """A HART-IP client that keeps Publish messages apart from responses."""

    def __init__(self, client, inactivity_ms=30000):
        self.c = client
        self.seq = 1
        self.publishes = []  # (header, body)
        open_session(client, inactivity_ms)

    def send(self, msg_id, seq, body=b""):
        raw = hartip.encode_message(hartip.REQUEST, msg_id, seq, body)
        if isinstance(self.c, Udp):
            self.c.sock.sendto(raw, ("127.0.0.1", self.c.port))
        else:
            self.c.sock.sendall(raw)

    def call(self, msg_id, body=b""):
        self.seq += 1
        self.send(msg_id, self.seq, body)
        deadline = time.monotonic() + 5.0
        while True:
            header, data = self.c.read(max(deadline - time.monotonic(), 0.01))
            if header.msg_type == hartip.PUBLISH:
                self.publishes.append((header, data))
                continue
            assert header.sequence == self.seq
            return header, data

    def pdu(self, dev, command, data=b""):
        header, body = self.call(hartip.PDU, Frame(Address.long(*dev.unique_id), command,
                                                   data).encode())
        assert header.msg_type == hartip.RESPONSE
        return Frame.decode(body)

    def subscribe(self, dev, target=ALL, flags=PROCESS_DATA):
        return self.pdu(dev, 533, target + struct.pack(">H", flags))

    def collect(self, seconds, want=None):
        """Publishes arriving within ``seconds`` (stops early once ``want`` are there)."""
        deadline = time.monotonic() + seconds
        got = []
        while (remaining := deadline - time.monotonic()) > 0:
            if want is not None and len(got) >= want:
                break
            try:
                header, data = self.c.read(remaining)
            except socket.timeout:
                break
            assert header.msg_type == hartip.PUBLISH, header
            got.append((header, data))
        self.publishes += got
        return got

    def drain(self, seconds=0.3):
        self.collect(seconds)


def burst_on(s, dev, command=1, message=0):
    assert s.pdu(dev, 108, command.to_bytes(2, "big") + bytes([message])).response_code == 0
    period = round(PERIOD * 32000).to_bytes(4, "big")
    assert s.pdu(dev, 103, bytes([message]) + period + period).response_code == 0
    assert s.pdu(dev, 109, bytes([1, message])).response_code == 0


def udp_key(client):
    return ("udp", ("127.0.0.1", client.sock.getsockname()[1]))  # as the server sees it


def test_533_subscribe_and_receive_publish():
    dev = make_device()
    srv = start([dev])
    try:
        s = Session(Tcp(srv.port))
        request = ALL + struct.pack(">H", PROCESS_DATA)
        reply = s.subscribe(dev)
        assert (reply.command, reply.response_code, reply.data) == (533, 0, request)
        assert reply.device_status == dev.status_byte(True)
        burst_on(s, dev)
        got = s.publishes + s.collect(1.0, want=2)
        assert len(got) >= 2
        assert [h.sequence for h, _ in got[:2]] == [0, 1]
        for header, body in got:
            assert (header.msg_type, header.msg_id, header.status) == (2, 3, 0)
            frame = Frame.decode(body)
            assert frame.is_burst and frame.command == 1 and frame.response_code == 0
            assert frame.address.primary and frame.address.burst
            assert frame.address.unique_id == (dev.unique_id[0] & 0x3FFF, dev.unique_id[1])
        s.c.close()
    finally:
        srv.stop()


def test_no_publish_without_subscription():
    dev = make_device()
    srv = start([dev])
    try:
        s = Session(Tcp(srv.port))
        burst_on(s, dev)
        assert s.collect(WINDOW) == [] and s.publishes == []
        s.c.close()
    finally:
        srv.stop()


def test_publish_all_without_533():
    dev = make_device()
    srv = start([dev], publish_all=True)
    try:
        s = Session(Tcp(srv.port))
        burst_on(s, dev)
        assert len(s.publishes + s.collect(1.0, want=2)) >= 2
        s.c.close()
    finally:
        srv.stop()


def test_device_status_flag_gets_only_cmd48():
    dev = make_device()
    srv = start([dev])
    try:
        s = Session(Tcp(srv.port))
        assert s.subscribe(dev, flags=DEVICE_STATUS).response_code == 0
        burst_on(s, dev, command=1, message=0)
        burst_on(s, dev, command=48, message=1)
        got = s.publishes + s.collect(1.0)
        assert len(got) >= 3
        assert {Frame.decode(body).command for _, body in got} == {48}
        s.c.close()
    finally:
        srv.stop()


def test_532_reads_flags():
    dev = make_device()
    srv = start([dev])
    try:
        uid = uid_bytes(*dev.unique_id)
        s = Session(Tcp(srv.port))
        assert s.subscribe(dev, uid, 0x0005).response_code == 0
        reply = s.pdu(dev, 532, uid)
        assert (reply.command, reply.response_code, reply.data) == (532, 0, uid + b"\x00\x05")
        assert reply.device_status == dev.status_byte(True)
        assert s.pdu(dev, 532, ALL).data == ALL + b"\x00\x00"
        assert s.pdu(dev, 532, uid_bytes(0x1234, 9)).response_code == 65
        assert s.pdu(dev, 532, uid[:4]).response_code == 5
        fresh = Session(Tcp(srv.port))
        assert fresh.pdu(dev, 532, uid).data == uid + b"\x00\x00"
        s.c.close()
        fresh.c.close()
    finally:
        srv.stop()


def test_533_unknown_unique_id_code_65():
    dev = make_device()
    srv = start([dev])
    try:
        s = Session(Udp(srv.port))
        reply = s.subscribe(dev, uid_bytes(0x1234, 9))
        assert reply.response_code == 65
        assert srv.subscriptions(udp_key(s.c)) == {}
        assert s.pdu(dev, 533, ALL + b"\x00").response_code == 5  # fewer than 7 bytes
        s.c.close()
    finally:
        srv.stop()


def test_533_individual_after_broadcast_code_9():
    dev = make_device()
    srv = start([dev])
    try:
        uid = uid_bytes(*dev.unique_id)
        key_dev = (dev.unique_id[0] & 0x3FFF, dev.unique_id[1])
        s = Session(Udp(srv.port))
        assert s.subscribe(dev).response_code == 0
        assert s.subscribe(dev, uid).response_code == 9
        assert srv.subscriptions(udp_key(s.c)) == {None: PROCESS_DATA}
        other = Session(Udp(srv.port))
        assert other.subscribe(dev, uid).response_code == 0
        assert other.subscribe(dev).response_code == 9  # broadcast after individual
        assert srv.subscriptions(udp_key(other.c)) == {key_dev: PROCESS_DATA}
        # The unique ID is in address form: master/burst bits of byte 0 are ignored.
        masked = bytes([uid[0] | 0xC0]) + uid[1:]
        assert other.subscribe(dev, masked, 0x0105).response_code == 0
        assert srv.subscriptions(udp_key(other.c)) == {key_dev: 0x0105}
        s.c.close()
        other.c.close()
    finally:
        srv.stop()


def test_533_flags_zero_unsubscribes():
    dev = make_device()
    srv = start([dev])
    try:
        s = Session(Udp(srv.port))
        assert s.subscribe(dev).response_code == 0
        burst_on(s, dev)
        assert len(s.publishes + s.collect(1.0, want=2)) >= 2
        assert s.subscribe(dev, flags=0).response_code == 0
        assert srv.subscriptions(udp_key(s.c)) == {}
        s.drain()  # a publish already on its way may still arrive
        assert s.collect(WINDOW) == []
        s.c.close()
    finally:
        srv.stop()


def test_subscriptions_end_with_the_session():
    dev = make_device()
    srv = start([dev])
    try:
        s = Session(Udp(srv.port))
        assert s.subscribe(dev).response_code == 0
        key = udp_key(s.c)
        assert srv.subscriptions(key) == {None: PROCESS_DATA}
        s.call(hartip.SESSION_CLOSE)
        assert srv.subscriptions(key) == {}
        Session(s.c)  # a new session on the same address starts without subscriptions
        assert srv.subscriptions(key) == {}
        s.c.close()
    finally:
        srv.stop()


def test_two_sessions_only_subscriber_gets_publish():
    dev = make_device()
    srv = start([dev])
    try:
        a = Session(Tcp(srv.port))
        b = Session(Tcp(srv.port))
        assert a.subscribe(dev).response_code == 0
        burst_on(b, dev)
        deadline = time.monotonic() + WINDOW
        while time.monotonic() < deadline:  # b keeps polling; its replies stay its own
            reply = b.pdu(dev, 1)
            assert reply.command == 1 and not reply.is_burst
        assert b.publishes == [] and b.collect(0.2) == []
        assert len(a.publishes + a.collect(1.0, want=2)) >= 2
        a.c.close()
        b.c.close()
    finally:
        srv.stop()


def test_session_closed_while_bursting_others_continue():
    dev = make_device()
    srv = start([dev])
    try:
        reset = Session(Tcp(srv.port))
        closing = Session(Tcp(srv.port))
        udp = Session(Udp(srv.port))
        for s in (reset, closing, udp):
            assert s.subscribe(dev).response_code == 0
        burst_on(udp, dev)
        assert len(reset.collect(1.0, want=1)) == 1
        # TCP reset: linger 0 makes close() send RST instead of FIN.
        reset.c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        reset.c.close()
        header, _ = closing.call(hartip.SESSION_CLOSE)
        assert header.msg_type == hartip.RESPONSE
        closing.c.close()
        udp.drain(0.1)
        got = udp.collect(1.0)
        assert len(got) >= 5
        sequences = [h.sequence for h, _ in got]
        assert sequences == list(range(sequences[0], sequences[0] + len(got)))
        assert srv.subscriptions(udp_key(udp.c)) == {None: PROCESS_DATA}
        started = time.monotonic()
        srv.stop()
        assert time.monotonic() - started < 2.0
        udp.c.close()
    finally:
        srv.stop()


def test_udp_subscription_receives_publish():
    dev = make_device()
    srv = start([dev])
    try:
        s = Session(Udp(srv.port))
        assert s.subscribe(dev).response_code == 0
        burst_on(s, dev)
        got = s.publishes + s.collect(1.0, want=2)
        assert [h.sequence for h, _ in got[:2]] == [0, 1]
        assert all(Frame.decode(body).is_burst for _, body in got)
        s.c.close()
    finally:
        srv.stop()


def test_no_response_and_delay_do_not_touch_bursts():
    dev = make_device()
    srv = start([dev])
    try:
        s = Session(Udp(srv.port))
        assert s.subscribe(dev).response_code == 0
        burst_on(s, dev)
        dev.faults.inject("no_response", count=1)
        dev.faults.inject("delay", seconds=1.0)
        dev.faults.inject("sensor_failure")
        cmd0 = Frame(Address.long(*dev.unique_id), 0).encode()
        s.send(hartip.PDU, 50, cmd0)  # swallowed by no_response
        got = s.collect(WINDOW)
        assert len(got) >= 3  # bursts keep coming while answers are suppressed
        assert all(Frame.decode(body).device_status & 0x81 == 0x81 for _, body in got[-1:])
        s.send(hartip.PDU, 51, cmd0)  # answered 1 s late
        before_reply = 0
        deadline = time.monotonic() + 3.0
        while True:
            header, _ = s.c.read(max(deadline - time.monotonic(), 0.01))
            if header.msg_type == hartip.RESPONSE:
                assert header.sequence == 51
                break
            before_reply += 1
        assert before_reply >= 5  # publishes are not held back by the delayed answer
        s.c.close()
    finally:
        srv.stop()



def test_silent_subscribed_session_expires_while_another_keeps_receiving():
    dev = make_device()
    srv = start([dev])
    try:
        silent = Session(Tcp(srv.port), inactivity_ms=1000)
        live = Session(Udp(srv.port), inactivity_ms=1000)
        assert silent.subscribe(dev).response_code == 0
        assert live.subscribe(dev).response_code == 0
        burst_on(live, dev)
        silent_key = next(k for k in srv._sessions if k[0] != "udp")
        live_key = udp_key(live.c)
        assert srv.subscriptions(silent_key) == {None: PROCESS_DATA}
        deadline = time.monotonic() + 5.0
        received = 0
        while srv.subscriptions(silent_key) and time.monotonic() < deadline:
            received += len(live.collect(0.2))
            live.call(hartip.KEEP_ALIVE)  # only the live session keeps itself open
        assert srv.subscriptions(silent_key) == {}
        assert received >= 3
        assert srv.subscriptions(live_key) == {None: PROCESS_DATA}
        live.c.close()
        silent.c.close()
    finally:
        srv.stop()
