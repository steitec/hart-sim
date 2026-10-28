from pathlib import Path

import pytest

from hart_sim.bench import Bench
from hart_sim.frame import Address
from hart_sim.client import DeviceHandle, HartError, HartIPClient, HartTimeout

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
