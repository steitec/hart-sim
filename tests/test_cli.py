import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hart_sim.bench import Bench
from hart_sim.cli import main

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
    assert code in (1, 2)
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
