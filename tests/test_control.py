import json
import socket
import struct

import pytest

from hart_sim.bench import Bench
from hart_sim.control import send_control
from hart_sim.frame import Address, Frame

PLANT = {"devices": [
    {"profile": "pressure", "polling_address": 1, "device_id": 1, "tag": "PT-101", "damping": 0},
    {"profile": "temperature", "polling_address": 2, "device_id": 2, "tag": "TT-201"},
]}


@pytest.fixture
def bench():
    with Bench.from_dict(PLANT) as b:
        yield b


def ctl(bench, **req):
    return send_control(req, port=bench.control_port)


def test_control_inject_and_list(bench):
    assert ctl(bench, device="PT-101", inject="sensor_failure", params={}) == {"ok": True}
    r = ctl(bench, list=True)
    assert r["ok"]
    pt = r["devices"]["PT-101"]
    assert "sensor_failure" in pt["faults"]
    assert pt["polling_address"] == 1
    assert pt["current_ma"] > 20.5
    assert isinstance(pt["pv"], float)
    assert r["devices"]["TT-201"]["faults"] == {}
    assert ctl(bench, device="PT-101", clear="sensor_failure") == {"ok": True}
    assert ctl(bench, list=True)["devices"]["PT-101"]["faults"] == {}
    ctl(bench, device="PT-101", inject="busy")
    assert ctl(bench, device="PT-101", clear=True) == {"ok": True}


def test_control_set_pv_changes_command3(bench):
    dev = bench.devices["PT-101"]
    assert ctl(bench, device="PT-101", set={"pv": 123.4}) == {"ok": True}
    resp = bench.server.route(Frame(Address.long(0xFA01, 1), 3))
    assert resp is not None and resp.response_code == 0
    assert struct.unpack(">f", resp.data[5:9])[0] == pytest.approx(123.4)
    assert dev.pv.value() == pytest.approx(123.4)
    assert ctl(bench, list=True)["devices"]["PT-101"]["pv"] == pytest.approx(123.4)
    assert ctl(bench, device="PT-101", set={"pv": None}) == {"ok": True}
    assert dev.pv.value() != pytest.approx(123.4)


def test_control_set_rejects_bad_values(bench):
    for bad in (float("nan"), float("inf"), True, "5", [1]):
        r = ctl(bench, device="PT-101", set={"pv": bad})
        assert r["ok"] is False
    assert ctl(bench, device="PT-101", set={"sv": 1}) == {"ok": True}
    assert bench.devices["PT-101"].sv.value() == pytest.approx(1)
    assert ctl(bench, device="PT-101", set={"x": 1})["ok"] is False
    assert ctl(bench, device="PT-101", set=5)["ok"] is False


def test_control_invalid_json_keeps_connection(bench):
    with socket.create_connection(("127.0.0.1", bench.control_port), timeout=5) as s, \
            s.makefile("rwb") as f:
        for line in (b"{not json\n", b"[1]\n", b'{"device": 5, "clear": true}\n',
                     b'{"device": "PT-101", "inject": "nonsense"}\n',
                     b'{"device": "PT-101", "inject": "delay", "params": 3}\n',
                     b'{"device": "PT-101"}\n'):
            f.write(line)
            f.flush()
            r = json.loads(f.readline())
            assert r["ok"] is False and r["error"]
        f.write(b'{"list": true}\n')
        f.flush()
        assert json.loads(f.readline())["ok"] is True


def test_control_unknown_device(bench):
    r = ctl(bench, device="XX-1", clear=True)
    assert r["ok"] is False and "XX-1" in r["error"]


def test_control_set_sv_without_sv(tmp_path):
    from hart_sim.profile import _PROFILE_DIR
    lines = (_PROFILE_DIR / "pressure.yaml").read_text().splitlines()
    (tmp_path / "nosv.yaml").write_text("\n".join(l for l in lines if not l.startswith("sv")))
    d = {"devices": [{"profile": "nosv.yaml", "polling_address": 1, "device_id": 1}]}
    with Bench.from_dict(d, base=tmp_path) as b:
        tag = next(iter(b.devices))
        r = send_control({"device": tag, "set": {"sv": 1}}, port=b.control_port)
        assert r["ok"] is False and "sv" in r["error"]


def test_control_binds_loopback_regardless_of_host():
    bench = Bench.from_dict(PLANT)
    bench.start(host="127.0.0.2", port=0, control_port=0)
    try:
        assert bench.control.host == "127.0.0.1"
        assert bench.control._server.server_address[0] == "127.0.0.1"
        assert send_control({"list": True}, port=bench.control_port)["ok"]
        with pytest.raises(OSError):
            send_control({"list": True}, host="127.0.0.2", port=bench.control_port, timeout=1)
    finally:
        bench.stop()


def test_control_host_option():
    bench = Bench.from_dict(PLANT)
    bench.start(host="127.0.0.1", port=0, control_port=0, control_host="127.0.0.2")
    try:
        assert send_control({"list": True}, host="127.0.0.2", port=bench.control_port)["ok"]
        with pytest.raises(OSError):
            send_control({"list": True}, host="127.0.0.1", port=bench.control_port, timeout=1)
    finally:
        bench.stop()


def test_control_line_too_long_closes_connection(bench):
    with socket.create_connection(("127.0.0.1", bench.control_port), timeout=5) as sock:
        sock.sendall(b'{"list": true, "pad": "' + b"x" * 70000)
        stream = sock.makefile("rb")
        assert json.loads(stream.readline()) == {"ok": False, "error": "line too long"}
        assert stream.readline() == b""  # connection closed
    # a long but legal line still works, and the server keeps serving
    assert ctl(bench, list=True, pad="y" * 60000)["ok"]



def test_control_server_reuse_address_matches_platform():
    import os
    from hart_sim import control, server
    assert control._Server.allow_reuse_address == (os.name != "nt")
    assert server._TcpServer.allow_reuse_address == (os.name != "nt")


def test_control_rejects_overflowing_value(bench):
    for bad in (1e39, -1e39, 3.5e38):
        assert ctl(bench, device="PT-101", set={"pv": bad})["ok"] is False
    assert ctl(bench, device="PT-101", set={"pv": 3.4e38}) == {"ok": True}
