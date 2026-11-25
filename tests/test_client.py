import socket
import threading
import time
from pathlib import Path

import pytest

from hart_sim import hartip
from hart_sim.bench import Bench
from hart_sim.frame import Address, Frame
from hart_sim.client import (BurstConfig, DeviceHandle, HartError, HartIPClient, HartTimeout,
                             Publish)

PLANT = Path(__file__).parent.parent / "examples" / "plant.yaml"


@pytest.fixture
def bench():
    with Bench.from_yaml(PLANT) as b:
        yield b


def connect(bench, **kw):
    kw.setdefault("timeout", 1.0)
    return HartIPClient("127.0.0.1", bench.server.port, **kw)


def test_poll_finds_both(bench):
    with connect(bench) as client:
        infos = client.poll(range(4))
    assert [(i.polling_address, i.tag) for i in infos] == [(1, "PT-101"), (2, "TT-201")]
    assert infos[0].device_type == 0xFA01 and infos[1].device_type == 0xFA02
    assert infos[0].device_id == 1 and infos[0].manufacturer_id == 0x00FA
    assert infos[0].hart_revision == 7


def test_find_by_tag_and_read_variables(bench):
    dev = bench.devices["PT-101"]
    dev.damping = 0
    dev.pv.override(400.0)
    with connect(bench) as client:
        handle = client.find("PT-101")
        assert isinstance(handle, DeviceHandle)
        v = handle.read_variables()
        assert v.current_ma == pytest.approx(10.4, abs=1e-3)
        assert v.pv == pytest.approx(400.0)
        assert v.pv_unit == dev.pv_unit
        assert v.status == 0
        with pytest.raises(LookupError):
            client.find("NOPE")


def test_write_range_roundtrip(bench):
    with connect(bench) as client:
        handle = client.find("PT-101")
        assert handle.write_range(0, 600) == 0
        assert handle.read_range() == (0, 600, bench.devices["PT-101"].pv_unit)


def test_udp_transport(bench):
    with connect(bench, transport="udp") as client:
        assert len(client.poll(range(4))) == 2
        assert client.find("TT-201").read_variables().pv_unit in (32, 33, 35)


def test_timeout_raises(bench):
    with connect(bench, timeout=0.3) as client:
        handle = client.find("PT-101")
        bench.devices["PT-101"].faults.inject("no_response")
        with pytest.raises(HartTimeout):
            handle.read_variables()


def test_status_text():
    assert DeviceHandle.status_text(0) == []
    assert DeviceHandle.status_text(0x81) == ["device malfunction", "PV out of limits"]


def test_session_error_raises(bench):
    bench.server.max_sessions = 0
    with pytest.raises(HartError):
        connect(bench).__enter__()


def test_closed_connection_raises_harterror(bench):
    with connect(bench) as client:
        bench.server._tcp.close_connections()
        with pytest.raises(HartError):
            client.command(Address.short(1), 0)


def test_session_initiate_host_type_on_the_wire(bench):
    for primary, expected in ((True, 1), (False, 0)):
        client = connect(bench, primary=primary)
        sent = []
        original = client._exchange

        def spy(msg_id, body, timeout, original=original, sent=sent):
            sent.append((msg_id, body))
            return original(msg_id, body, timeout)

        client._exchange = spy
        with client:
            pass
        assert sent[0][0] == 0 and sent[0][1][0] == expected


def test_poll_max_address(bench):
    with connect(bench) as client:
        assert [i.tag for i in client.poll(max_address=1)] == ["PT-101"]
        assert [i.tag for i in client.poll(max_address=3)] == ["PT-101", "TT-201"]
        assert [i.tag for i in client.poll([2])] == ["TT-201"]  # addresses= still works


# --- burst: Publish handling and burst API ---------------------------------------------

@pytest.fixture
def single():
    with Bench.single("pressure") as b:
        yield b


def _burst_client(single, **kw):
    kw.setdefault("timeout", 1.0)
    return HartIPClient("127.0.0.1", single.server.port, **kw)


@pytest.mark.parametrize("transport", ["tcp", "udp"])
def test_burst_end_to_end_tcp_and_udp(single, transport):
    with _burst_client(single, transport=transport) as client:
        handle = client.handle(0)
        assert handle.subscribe() == 1
        handle.burst(3, 0.1)
        first = client.wait_burst(3, timeout=2.0)
        second = client.wait_burst(3, timeout=2.0)
        handle.burst_off()
    assert isinstance(first, Publish) and first.frame.is_burst and first.frame.command == 3
    assert second.received >= first.received  # parse times: two Publishes can share a recv


def test_response_matched_while_publish_arrives_first(single):
    with _burst_client(single) as client:
        handle = client.handle(0)
        handle.subscribe()
        handle.burst(3, 0.1)
        deadline = time.monotonic() + 0.6
        reads = 0
        while reads < 20 or time.monotonic() < deadline:
            assert handle.read_variables().pv_unit is not None
            reads += 1
        assert len(client.publishes) >= 2
        assert all(p.frame.is_burst for p in client.publishes)


_FAKE_ADDR = Address.long(0xFA01, 1)


def _burst_message(command, data, seq):
    frame = Frame(Address.long(0xFA01, 1, burst=True), command, data, True, 0, 0, True).encode()
    return hartip.encode_message(hartip.PUBLISH, hartip.PDU, seq, frame)


def _reply_message(seq, msg_type=hartip.RESPONSE, data=b"\x07"):
    frame = Frame(_FAKE_ADDR, 1, data, True, 0, 0).encode()
    return hartip.encode_message(msg_type, hartip.PDU, seq, frame)


def _fake_server(on_pdu, inactivity_ms=60_000, keep_alives=None):
    """One-session HART-IP server; `on_pdu(header)` returns the messages to send for a PDU.
    It accepts `inactivity_ms` in Session Initiate and appends the time of each Keep Alive
    to `keep_alives` (when given)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        with conn:
            buf = b""
            while True:
                while len(buf) < 8 or len(buf) < hartip.decode_header(buf).byte_count:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                header = hartip.decode_header(buf)
                buf = buf[header.byte_count:]
                if header.msg_id == hartip.SESSION_INITIATE:
                    conn.sendall(hartip.encode_message(hartip.RESPONSE, header.msg_id,
                                                       header.sequence,
                                                       b"\x01" + inactivity_ms.to_bytes(4, "big")))
                elif header.msg_id == hartip.KEEP_ALIVE and keep_alives is not None:
                    keep_alives.append(time.monotonic())
                    conn.sendall(hartip.encode_message(hartip.RESPONSE, header.msg_id,
                                                       header.sequence, b""))
                elif header.msg_id == hartip.PDU:
                    for message in on_pdu(header):
                        conn.sendall(message)
                else:
                    return

    threading.Thread(target=serve, daemon=True).start()
    return srv


def test_publish_with_coinciding_sequence_is_not_a_response():
    srv = _fake_server(lambda h: [_burst_message(1, b"\x00\x00", h.sequence),
                                  _reply_message(h.sequence)])
    with HartIPClient("127.0.0.1", srv.getsockname()[1], timeout=1.0) as client:
        frame = client.command(_FAKE_ADDR, 1)
        assert frame.data == b"\x07" and not frame.is_burst
        assert len(client.publishes) == 1 and client.publishes[0].frame.data == b"\x00\x00"
    srv.close()


def test_request_typed_message_is_not_a_response():
    srv = _fake_server(lambda h: [_reply_message(h.sequence, hartip.REQUEST, b"\x99"),
                                  _reply_message(h.sequence)])
    with HartIPClient("127.0.0.1", srv.getsockname()[1], timeout=1.0) as client:
        assert client.command(_FAKE_ADDR, 1).data == b"\x07"
    srv.close()


def test_wait_burst_filters_by_command_and_keeps_the_rest():
    srv = _fake_server(lambda h: [_burst_message(1, b"\x01", 0), _burst_message(3, b"\x03", 1),
                                  _reply_message(h.sequence)])
    with HartIPClient("127.0.0.1", srv.getsockname()[1], timeout=1.0) as client:
        client.command(_FAKE_ADDR, 1)
        assert [p.frame.command for p in client.publishes] == [1, 3]
        assert client.wait_burst(3, timeout=1.0).frame.data == b"\x03"
        assert [p.frame.command for p in client.publishes] == [1]
        assert client.wait_burst(timeout=1.0).frame.command == 1
        assert not client.publishes
    srv.close()


def test_wait_burst_timeout(single):
    with _burst_client(single) as client:
        client.handle(0).subscribe()
        started = time.monotonic()
        with pytest.raises(HartTimeout):
            client.wait_burst(timeout=0.2)
        assert time.monotonic() - started < 1.0
        # The session is still usable after a timeout.
        assert client.handle(0).read_variables() is not None


def test_burst_config_roundtrip(single):
    with _burst_client(single) as client:
        handle = client.handle(0)
        handle.burst(3, 0.1, max_period=5.0, message=1)
        cfg = handle.burst_config(1)
        assert cfg == BurstConfig(control=1, command=3, slots=cfg.slots, period=0.1,
                                  max_period=5.0, trigger_mode=0, trigger_class=cfg.trigger_class,
                                  trigger_unit=cfg.trigger_unit, trigger_level=0.0)
        handle.burst(33, 0.2, variables=[0, 250], message=2, trigger=2, level=1.5)
        cfg = handle.burst_config(2)
        assert (cfg.command, cfg.slots[:2], cfg.period, cfg.trigger_mode, cfg.trigger_level) == (
            33, (0, 250), 0.2, 2, 1.5)
        assert cfg.trigger_unit != 0 and cfg.trigger_class != 0
        handle.burst_off(1)
        handle.burst_off(2)
        assert handle.burst_config(1).control == 0


def test_burst_adjusted_period_is_accepted_and_bad_command_raises(single):
    with _burst_client(single) as client:
        handle = client.handle(0)
        handle.burst(3, 0.01)  # raised to 0.1 s: warning code 8
        assert handle.burst_config().period == pytest.approx(0.1)
        with pytest.raises(HartError, match="command 108.*2"):
            handle.burst(5, 1.0)
        handle.burst_off()


def test_burst_always_resets_the_trigger(single):
    with _burst_client(single) as client:
        handle = client.handle(0)
        handle.burst(3, 0.5, trigger=2, level=1.5)
        assert handle.burst_config().trigger_mode == 2
        handle.burst(3, 1.0)
        cfg = handle.burst_config()
        assert cfg.trigger_mode == 0 and cfg.period == 1.0
        handle.burst_off()


def test_burst_off_stops_publishes(single):
    with _burst_client(single) as client:
        handle = client.handle(0)
        handle.subscribe()
        handle.burst(3, 0.1)
        client.wait_burst(3, timeout=2.0)
        handle.burst_off()
        client.handle(0).read_variables()  # round trip: the off command is applied
        client.publishes.clear()
        with pytest.raises(HartTimeout):
            client.wait_burst(timeout=0.4)


def test_subscribe_unknown_device_raises(single):
    with _burst_client(single, timeout=0.3) as client:
        handle = client.handle(0)
        # No device answers at an unknown address, so nothing can be subscribed through it.
        with pytest.raises(HartError):
            client.subscribe(Address.long(0x1234, 99), all_devices=False)
        # A known device relays an unknown unique ID: the server answers code 65.
        bad = bytes([0x12, 0x34, 0, 0, 99]) + b"\x00\x01"
        assert client.command(handle.address, 533, bad).response_code == 65


def test_subscribe_conflict_raises(single):
    with _burst_client(single) as client:
        handle = client.handle(0)
        assert client.subscribe(handle.address) == 1  # all devices
        with pytest.raises(HartError, match="533.*9"):
            handle.subscribe()  # individual next to a broadcast subscription


@pytest.mark.parametrize("transport", ["tcp", "udp"])
def test_wait_burst_keeps_the_session_alive(single, transport):
    # 1000 ms is the server's minimum; a listener sends nothing but Keep Alive.
    with _burst_client(single, transport=transport, inactivity_ms=1000) as client:
        handle = client.handle(0)
        handle.subscribe()
        handle.burst(1, 0.1)
        key = next(iter(single.server._subs))
        deadline = time.monotonic() + 3.5
        received = 0
        while time.monotonic() < deadline:
            client.wait_burst(1, timeout=1.0)
            received += 1
        assert received >= 10
        assert single.server.subscriptions(key)
        handle.burst_off()


def test_burst_argument_validation(single):
    with _burst_client(single) as client:
        handle = client.handle(0)
        for kwargs in ({"command": 1, "period": float("nan")},
                       {"command": 1, "period": float("inf")},
                       {"command": 1, "period": -1.0},
                       {"command": 1, "period": 0.0},
                       {"command": 1, "period": 1.0, "max_period": float("nan")},
                       {"command": 1, "period": 1.0, "max_period": -5.0},
                       {"command": 70000, "period": 1.0},
                       {"command": -1, "period": 1.0},
                       {"command": 1, "period": 1.0, "message": 256}):
            with pytest.raises(ValueError):
                handle.burst(**kwargs)
        assert not next(iter(single.devices.values())).burst_on  # nothing was sent


def test_wait_burst_does_not_spin_when_the_server_grants_no_inactivity_time():
    # A gateway that answers Session Initiate with 0 ms must not get a Keep Alive flood.
    keep_alives = []
    srv = _fake_server(lambda h: [], inactivity_ms=0, keep_alives=keep_alives)
    with HartIPClient("127.0.0.1", srv.getsockname()[1], timeout=1.0) as client:
        with pytest.raises(HartTimeout):
            client.wait_burst(timeout=1.2)
    srv.close()
    assert 1 <= len(keep_alives) <= 3


def test_burst_rejects_bad_slots_before_sending_anything(single):
    dev = next(iter(single.devices.values()))
    with _burst_client(single) as client:
        handle = client.handle(0)
        before = handle.burst_config()
        for variables in ([0] * 9, [256], [-1], [0, 300]):
            with pytest.raises(ValueError):
                handle.burst(48, 1.0, variables=variables)
        with pytest.raises(ValueError, match="trigger"):
            handle.burst(48, 1.0, variables=[], trigger=2)
        assert handle.burst_config() == before  # no 108/107 reached the device
        assert not dev.burst_on


def test_burst_trigger_on_an_unused_first_slot_raises(single):
    with _burst_client(single) as client:
        handle = client.handle(0)
        handle.burst(3, 1.0, variables=[], message=1)  # all slots unused
        before = handle.burst_config(1)
        with pytest.raises(ValueError, match="trigger"):
            handle.burst(48, 2.0, trigger=2, message=1)  # keeps the (unused) slots
        assert handle.burst_config(1) == before  # nothing was written
        handle.burst_off(1)
