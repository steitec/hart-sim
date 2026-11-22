import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hart_sim.bench import Bench
from hart_sim.cli import _parser, main

PLANT = Path(__file__).parent.parent / "examples" / "plant.yaml"


@pytest.fixture
def bench():
    with Bench.from_yaml(PLANT) as b:
        for dev in b.devices.values():
            dev.damping = 0.0
        yield b


def conn(bench):
    return ["--port", str(bench.server.port)]


def ctl(bench):
    return ["--control-port", str(bench.control_port)]


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_serve_requires_one_source(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["serve"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main(["serve", "pressure", "--bench", str(PLANT)])
    assert exc.value.code == 2
    assert "exactly one" in capsys.readouterr().err


def test_poll_output(bench, capsys):
    code, out, _ = run(capsys, "poll", "--max-address", "3", *conn(bench))
    assert code == 0
    assert out.splitlines() == [
        "addr 1  PT-101  pressure-transmitter  id 0x000001",
        "addr 2  TT-201  temperature-transmitter  id 0x000002",
    ]


def test_poll_udp(bench, capsys):
    code, out, _ = run(capsys, "poll", "--udp", "--max-address", "3", *conn(bench))
    assert code == 0 and "PT-101" in out


def test_cmd3_decoded_output(bench, capsys):
    code, out, _ = run(capsys, "cmd", "--tag", "PT-101", "3", *conn(bench))
    assert code == 0
    line = out.strip()
    assert line.startswith("loop ") and " mA | PV " in line and " kPa | SV " in line
    assert line.endswith("degC") and not line.startswith("status")


def test_cmd_by_address_and_other_decoders(bench, capsys):
    for number, expect in ((1, "PV "), (2, "loop "), (13, "PT-101"), (15, "range "), (48, "status bytes")):
        code, out, _ = run(capsys, "cmd", "--address", "1", str(number), *conn(bench))
        assert code == 0 and expect in out, (number, out)


def test_cmd_status_prefix_after_inject(bench, capsys):
    code, out, _ = run(capsys, "inject", "PT-101", "sensor_failure", *ctl(bench))
    assert code == 0
    code, out, _ = run(capsys, "cmd", "--tag", "PT-101", "3", *conn(bench))
    assert code == 0
    assert out.startswith("status: ")
    assert "device malfunction" in out and "PV out of limits" in out
    assert "loop 21.00 mA" in out
    code, _, _ = run(capsys, "clear", "PT-101", *ctl(bench))
    assert code == 0
    code, out, _ = run(capsys, "cmd", "--tag", "PT-101", "3", *conn(bench))
    assert not out.startswith("status")


def test_set_pv(bench, capsys):
    assert run(capsys, "set", "PT-101", "pv", "250", *ctl(bench))[0] == 0
    code, out, _ = run(capsys, "cmd", "--tag", "PT-101", "1", *conn(bench))
    assert out.strip() == "PV 250.0 kPa"
    assert run(capsys, "set", "PT-101", "pv", "auto", *ctl(bench))[0] == 0


def test_inject_parses_json_values(bench, capsys):
    code, _, err = run(capsys, "inject", "PT-101", "drift", "per_second=0.5", *ctl(bench))
    assert code == 0, err
    code, _, err = run(capsys, "inject", "PT-101", "nosuchfault", *ctl(bench))
    assert code == 1 and "nosuchfault" in err


def test_cmd_raw_output(bench, capsys):
    code, out, _ = run(capsys, "cmd", "--tag", "PT-101", "16", *conn(bench))
    assert code == 0
    assert out.strip().startswith("code 0 status 0x") and " data " in out


def test_cmd_nonzero_response_code_exit_1(bench, capsys):
    code, out, err = run(capsys, "cmd", "--tag", "PT-101", "35", "00", *conn(bench))
    assert code == 1
    assert "code 5" in out + err


def test_unknown_tag_exit_1(bench, capsys):
    code, _, err = run(capsys, "cmd", "--tag", "NOPE", "3", *conn(bench))
    assert code == 1 and "NOPE" in err


def test_bad_hexdata_exit_2(bench, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["cmd", "--tag", "PT-101", "35", "zz", *conn(bench)])
    assert exc.value.code == 2


def test_connection_refused_exit_2(capsys):
    code, _, err = run(capsys, "poll", "--port", "1")
    assert code == 2
    assert err.startswith("hart-sim: error:") and len(err.splitlines()) == 1
    code, _, err = run(capsys, "inject", "PT-101", "sensor_failure", "--control-port", "1")
    assert code == 2 and len(err.splitlines()) == 1


def test_profiles(capsys):
    code, out, _ = run(capsys, "profiles")
    assert code == 0 and "pressure" in out and "temperature" in out


@pytest.mark.skipif(os.name == "nt", reason="needs POSIX signals")
def test_serve_ctrl_c():
    proc = subprocess.Popen(
        [sys.executable, "-m", "hart_sim", "serve", "pressure", "--port", "0", "--control-port", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        first = proc.stdout.readline()
        assert first.startswith("hart-sim: 1 device on HART-IP 127.0.0.1:")
        time.sleep(0.2)
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=10)
        assert proc.returncode == 0
        assert "Traceback" not in err
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def test_inject_readme_forms_work_verbatim(bench, capsys):
    dev = bench.devices["PT-101"]
    for kind, *params in (("comm_error", "flags=0x10", "count=2"), ("busy", "count=2"),
                          ("no_response", "count=1"), ("delay", "seconds=0.01", "count=1"),
                          ("saturate", "direction=high"), ("more_status", "bytes=1,2,3,4,5,6"),
                          ("drift", "per_second=0.5"), ("sensor_failure",), ("cold_start",)):
        code, _, err = run(capsys, "inject", "PT-101", kind, *params, *ctl(bench))
        assert code == 0, (kind, err)
    assert dev.faults.active("comm_error") == {"flags": 0x10, "count": 2}
    assert dev.faults.active("more_status") == {"bytes": [1, 2, 3, 4, 5, 6]}
    code, _, err = run(capsys, "inject", "PT-101", "write_protect", "on=on", *ctl(bench))
    assert code == 0, err
    assert dev.write_protect is True
    code, _, err = run(capsys, "inject", "PT-101", "write_protect", "on=off", *ctl(bench))
    assert code == 0 and dev.write_protect is False


def test_poll_max_address(bench, capsys):
    code, out, _ = run(capsys, "poll", "--max-address", "1", *conn(bench))
    assert code == 0 and out.splitlines() == [
        "addr 1  PT-101  pressure-transmitter  id 0x000001"]
    with pytest.raises(SystemExit) as exc:
        main(["poll", "--max-address", "64", *conn(bench)])
    assert exc.value.code == 2


@pytest.mark.parametrize("argv", [["poll", "--udp"], ["cmd", "--udp", "--address", "1", "1"]])
def test_unresolvable_host_is_one_line_exit_2(capsys, argv):
    code, _, err = run(capsys, *argv, "--host", "no-such-host.invalid")
    assert code == 2
    assert err.startswith("hart-sim: error:") and len(err.splitlines()) == 1


def test_connection_reset_mid_command_no_traceback(capsys):
    import socket
    import struct
    from hart_sim import hartip

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve():
        conn_, _ = listener.accept()
        conn_.recv(13)  # session initiate
        conn_.sendall(hartip.encode_message(hartip.RESPONSE, 0, 1, struct.pack(">BI", 1, 60000)))
        conn_.recv(8)   # first PDU header, then reset the connection
        conn_.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        conn_.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    code, _, err = run(capsys, "cmd", "--address", "1", "1",
                       "--port", str(listener.getsockname()[1]))
    assert code == 2
    assert err.startswith("hart-sim: error:") and len(err.splitlines()) == 1
    thread.join(5)
    listener.close()


def test_cmd_warning_code_14_is_not_an_error(bench, capsys):
    import struct
    data = bytes([12]) + struct.pack(">ff", 505.0, 500.0)  # span 5 < min span 10: applied
    code, out, err = run(capsys, "cmd", "--tag", "PT-101", "35", data.hex(), *conn(bench))
    assert code == 0
    assert out.startswith("code 14 status 0x")
    assert err.strip() == "warning: code 14"
    code, out, err = run(capsys, "cmd", "--tag", "PT-101", "99", *conn(bench))
    assert code == 1 and "response code 64" in err  # real errors stay errors


def test_serve_control_host_option(monkeypatch, capsys):
    seen = {}

    def fake_start(self, **kwargs):
        seen.update(kwargs)
        raise OSError(98, "stop here")

    monkeypatch.setattr(Bench, "start", fake_start)
    assert main(["serve", "pressure", "--host", "0.0.0.0"]) == 2
    assert seen["host"] == "0.0.0.0" and seen["control_host"] == "127.0.0.1"
    assert main(["serve", "pressure", "--control-host", "10.1.2.3"]) == 2
    assert seen["control_host"] == "10.1.2.3"


# --- burst, listen, publish-all, clear --reset ---

def test_burst_and_listen(bench, capsys):
    code, out, err = run(capsys, "burst", "PT-101", "--command", "3", "--period", "0.1", *conn(bench))
    assert code == 0, err
    assert out.strip() == "burst on: PT-101 message 0 command 3 every 0.1 s"
    code, out, err = run(capsys, "listen", "PT-101", "--count", "2", *conn(bench))
    assert code == 0, err
    lines = out.splitlines()
    assert len(lines) == 2
    for line in lines:
        assert re.match(r"\d\d:\d\d:\d\d\.\d{3} PT-101 cmd 3 ", line), line
        assert "mA" in line


def test_listen_status_adds_command_48(bench, capsys):
    run(capsys, "burst", "PT-101", "--command", "48", "--period", "0.1", *conn(bench))
    code, out, err = run(capsys, "listen", "PT-101", "--count", "1", "--status", *conn(bench))
    assert code == 0, err
    assert " cmd 48 " in out and "status bytes" in out


def test_burst_off(bench, capsys):
    run(capsys, "burst", "PT-101", "--command", "1", "--period", "0.1", *conn(bench))
    assert bench.devices["PT-101"].burst_on
    code, out, _ = run(capsys, "burst", "PT-101", "--off", "--message", "0", *conn(bench))
    assert code == 0 and out.strip() == "burst off: PT-101 message 0"
    assert not bench.devices["PT-101"].burst_on


def test_burst_write_protected_exit_1(bench, capsys):
    run(capsys, "inject", "PT-101", "write_protect", "on=on", *ctl(bench))
    code, _, err = run(capsys, "burst", "PT-101", "--command", "3", "--period", "1", *conn(bench))
    assert code == 1 and "response code 7" in err
    assert not bench.devices["PT-101"].burst_on


def test_burst_unknown_tag_exit_1(bench, capsys):
    code, _, err = run(capsys, "burst", "NOPE", "--command", "3", "--period", "1", *conn(bench))
    assert code == 1 and "NOPE" in err


def test_burst_connection_refused_exit_2(capsys):
    code, _, err = run(capsys, "burst", "PT-101", "--command", "3", "--period", "1",
                       "--port", "1")
    assert code == 2


def test_burst_needs_command_and_period(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["burst", "PT-101"])
    assert exc.value.code == 2


def test_listen_gives_up_without_burst(bench, capsys, monkeypatch):
    monkeypatch.setattr("hart_sim.cli._LISTEN_IDLE_LIMIT", 0.3)
    code, _, err = run(capsys, "listen", "PT-101", "--count", "1", *conn(bench))
    assert code == 1 and "no burst" in err


def test_serve_publish_all_flag_parsed():
    args = _parser().parse_args(["serve", "pressure", "--publish-all"])
    assert args.publish_all is True
    assert _parser().parse_args(["serve", "pressure"]).publish_all is False


def test_serve_publish_all_reaches_server(monkeypatch):
    seen = {}
    real_start = Bench.start

    def start(self, *a, **kw):
        seen["publish_all"] = self.publish_all
        return real_start(self, *a, **kw)

    monkeypatch.setattr(Bench, "start", start)
    monkeypatch.setattr("hart_sim.cli._stop_event", lambda: _Stopped())
    assert main(["serve", "pressure", "--publish-all", "--port", "0", "--control-port", "0"]) == 0
    assert seen["publish_all"] is True


class _Stopped:
    def wait(self, timeout=None):
        return True


def test_clear_drift_reset_cli(bench, capsys):
    dev = bench.devices["PT-101"]
    run(capsys, "inject", "PT-101", "drift", "per_second=5", *ctl(bench))
    code, _, err = run(capsys, "clear", "PT-101", "drift", "--reset", *ctl(bench))
    assert code == 0, err
    assert dev.faults.active("drift") is None
    code, _, err = run(capsys, "clear", "PT-101", "--reset", *ctl(bench))
    assert code == 0, err
    assert dev.pv._drift_offset == 0.0


def test_clear_reset_drops_offset_of_drift_cleared_before(bench, capsys):
    dev = bench.devices["PT-101"]
    run(capsys, "inject", "PT-101", "drift", "per_second=50", *ctl(bench))
    deadline = time.monotonic() + 5.0
    while dev.pv._drift_at(dev.pv._clock()) == 0.0 and time.monotonic() < deadline:
        pass  # the drift offset grows with the device clock
    code, _, err = run(capsys, "clear", "PT-101", "drift", *ctl(bench))  # offset stays
    assert code == 0, err
    assert dev.pv._drift_at(dev.pv._clock()) != 0.0
    code, _, err = run(capsys, "clear", "PT-101", "--reset", *ctl(bench))
    assert code == 0, err
    assert dev.pv._drift_at(dev.pv._clock()) == 0.0


@pytest.mark.parametrize("args", [
    ("--period", "nan"), ("--period", "inf"), ("--period", "-1"),
    ("--period", "1", "--max-period", "nan"), ("--period", "1", "--command", "70000")])
def test_burst_invalid_values_exit_2(bench, capsys, args):
    argv = ["burst", "PT-101", "--command", "3", *args, *conn(bench)]
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


def test_clear_reset_request_shape(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr("hart_sim.cli.send_control", lambda req, **kw: sent.append(req) or {"ok": True})
    main(["clear", "PT-101", "drift", "--reset"])
    main(["clear", "PT-101", "--reset"])
    main(["clear", "PT-101"])
    assert sent == [{"device": "PT-101", "clear": "drift", "reset": True},
                    {"device": "PT-101", "clear": True, "reset": True},
                    {"device": "PT-101", "clear": True}]


def test_list_shows_burst(bench):
    from hart_sim.control import send_control
    answer = send_control({"list": True}, port=bench.control_port)
    assert answer["devices"]["PT-101"]["burst"] == []
    main(["burst", "PT-101", "--command", "3", "--period", "0.5", "--message", "1", *conn(bench)])
    answer = send_control({"list": True}, port=bench.control_port)
    assert answer["devices"]["PT-101"]["burst"] == [
        {"message": 1, "on": True, "command": 3, "period": 0.5}]


def test_listen_prints_only_its_device_with_publish_all(capsys):
    with Bench.from_yaml(PLANT) as b:
        b.server.publish_all = True
        for dev in b.devices.values():
            dev.damping = 0.0
        for tag in ("TT-201", "PT-101"):
            assert main(["burst", tag, "--command", "1", "--period", "0.1", *conn(b)]) == 0
        capsys.readouterr()
        code, out, err = run(capsys, "listen", "PT-101", "--count", "3", *conn(b))
    assert code == 0, err
    assert len(out.splitlines()) == 3 and all(" PT-101 cmd 1 " in l for l in out.splitlines())
