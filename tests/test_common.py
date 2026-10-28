import math

import pytest

from hart_sim import units
from hart_sim.device import Device
from hart_sim.frame import Address, Frame, pack_float, unpack_float
from hart_sim.profile import load_profile


@pytest.fixture
def dev():
    t = [0.0]
    d = Device(load_profile("pressure"), clock=lambda: t[0])
    d.damping = 0.0
    d.pv.override(400.0)
    return d


def req(cmd, data=b"", primary=True):
    return Frame(Address.long(0xFA01, 1, primary), cmd, data)


def send(dev, cmd, data=b"", primary=True):
    return dev.handle(req(cmd, data, primary))


def f(*xs):
    return b"".join(pack_float(x) for x in xs)


def cmd35(dev, unit, urv, lrv):
    return send(dev, 35, bytes([unit]) + f(urv, lrv))


def current(dev):
    return unpack_float(send(dev, 2).data[:4])


def test_cmd33(dev):
    r = send(dev, 33, bytes([0, 247, 1]))
    assert r.response_code == 0 and len(r.data) == 18
    assert r.data[0:2] == bytes([0, 12]) and unpack_float(r.data[2:6]) == 400.0
    assert r.data[6:8] == bytes([247, 32]) and unpack_float(r.data[8:12]) == 23.5
    assert send(dev, 33, bytes([5])).response_code == 2
    assert send(dev, 33).response_code == 5


def test_cmd34(dev):
    r = send(dev, 34, f(2.5))
    assert r.response_code == 0 and unpack_float(r.data) == 2.5 and dev.damping == 2.5
    assert send(dev, 34, f(61)).response_code == 3
    assert send(dev, 34, f(-1)).response_code == 4
    assert send(dev, 34, f(float("nan"))).response_code == 2
    assert send(dev, 34, b"\0\0").response_code == 5
    assert dev.damping == 2.5


def test_range_write_rejections(dev):  # Review Focus 4
    for urv, lrv, code in [(500, 500, 12), (100, 200, 12), (3000, 0, 11),
                           (1000, -200, 10), (float("nan"), 0, 2),
                           (1000, float("inf"), 2), (3000, -200, 13), (1000, 3000, 9)]:
        assert cmd35(dev, 12, urv, lrv).response_code == code, (urv, lrv)
    assert cmd35(dev, 7, 1000, 0).response_code == 2  # wrong unit
    assert (dev.lrv, dev.urv) == (0, 1000)


def test_range_write_applies_and_changes_current(dev):
    r = cmd35(dev, 12, 600, 0)
    assert r.response_code == 0 and r.data == bytes([12]) + f(600, 0)
    assert (dev.lrv, dev.urv) == (0, 600) and dev.config_counter == 1
    assert current(dev) == pytest.approx(4 + 16 * 400 / 600, abs=1e-4)


def test_span_too_small_warning(dev):
    r = cmd35(dev, 12, 5, 0)
    assert r.response_code == 14 and r.data == bytes([12]) + f(5, 0)
    assert dev.urv == 5


def test_cmd36_37(dev):
    r = send(dev, 36)
    assert r.response_code == 0 and dev.urv == 400.0
    dev.pv.override(100.0)
    assert send(dev, 37).response_code == 0 and dev.lrv == 100.0
    dev.pv.override(5000.0)
    assert send(dev, 36).response_code == 11 and dev.urv == 400.0
    dev.pv.override(-500.0)
    assert send(dev, 37).response_code == 10
    dev.pv.override(float("nan"))
    assert send(dev, 37).response_code == 2
    dev.pv.override(400.0)
    assert send(dev, 37).response_code == 12  # LRV == URV
    assert (dev.lrv, dev.urv) == (100.0, 400.0)


def test_fixed_current_mode(dev):
    r = send(dev, 40, f(12.0))
    assert r.response_code == 0 and unpack_float(r.data) == pytest.approx(12.0)
    r = send(dev, 2)
    assert unpack_float(r.data[:4]) == pytest.approx(12.0) and r.device_status & 0x08
    assert send(dev, 40, f(0.0)).response_code == 0
    assert dev.fixed_ma is None and send(dev, 2).device_status & 0x08 == 0


def test_fixed_current_rejections(dev):
    assert send(dev, 40, f(21.5)).response_code == 3
    assert send(dev, 40, f(3.0)).response_code == 4
    assert send(dev, 40, f(float("nan"))).response_code == 2
    assert send(dev, 40, b"\0").response_code == 5
    assert send(dev, 40, f(3.6)).response_code == 0 and send(dev, 40, f(21.0)).response_code == 0
    dev.fixed_ma = None
    dev.loop_current_mode = False
    assert send(dev, 40, f(12.0)).response_code == 11 and dev.fixed_ma is None


def test_self_test_and_reset(dev):
    r = send(dev, 41)
    assert r.response_code == 0 and r.data == b""
    send(dev, 40, f(12.0))
    send(dev, 34, f(3.0))
    assert dev.status_byte(True) & 0x20 == 0
    r = send(dev, 42)
    assert r.response_code == 0 and dev.fixed_ma is None and dev.damping == 3.0
    assert r.device_status & 0x20  # cold start re-armed, visible to the requester
    assert dev.status_byte(False) & 0x20


def test_unit_change_converts_range(dev):
    r = send(dev, 44, bytes([7]))
    assert r.response_code == 0 and r.data == bytes([7])
    assert dev.pv_unit == 7 and dev.urv == pytest.approx(10.0) and dev.lrv == 0
    assert send(dev, 1).data[0] == 7
    assert unpack_float(send(dev, 1).data[1:5]) == pytest.approx(4.0)
    assert current(dev) == pytest.approx(4 + 16 * 0.4, abs=1e-4)  # unchanged by conversion
    lo, hi = dev.sensor_limits
    assert (lo, hi) == pytest.approx((-1.0, 25.0))
    assert dev.min_span == pytest.approx(0.1)
    d = send(dev, 14).data
    assert d[3] == 7 and unpack_float(d[4:8]) == pytest.approx(25.0)
    # and a range write now uses bar
    assert cmd35(dev, 7, 5, 0).response_code == 0
    assert cmd35(dev, 12, 5, 0).response_code == 2


def test_unit_change_rejections(dev):
    assert send(dev, 44, bytes([32])).response_code == 2  # temperature on pressure
    assert send(dev, 44, bytes([99])).response_code == 2
    assert send(dev, 44).response_code == 5
    assert dev.pv_unit == 12


def tsend(d, cmd, data=b""):
    return d.handle(Frame(Address.long(0xFA02, 1), cmd, data))


def test_unit_change_temperature():
    t = [0.0]
    d = Device(load_profile("temperature"), clock=lambda: t[0])
    d.damping = 0.0
    d.pv.override(100.0)
    assert d.profile.pv.unit == 32
    assert tsend(d, 44, bytes([12])).response_code == 2
    assert tsend(d, 44, bytes([33])).response_code == 0
    assert unpack_float(tsend(d, 1).data[1:5]) == pytest.approx(212.0)
    assert d.lrv == pytest.approx(d.profile.range[0] * 9 / 5 + 32)
    assert tsend(d, 44, bytes([35])).response_code == 0
    assert unpack_float(tsend(d, 1).data[1:5]) == pytest.approx(373.15)


def test_unit_conversion_table():
    assert units.convert(1.0, 7, 12) == pytest.approx(100.0)
    assert units.convert(0.0, 32, 33) == pytest.approx(32.0)
    assert units.convert(14.5, 6, 6) == pytest.approx(14.5)
    with pytest.raises(ValueError):
        units.convert(1.0, 7, 32)


def test_trim_zero_output_equals_measurement(dev):
    send(dev, 40, f(4.0))
    r = send(dev, 45, f(4.05))
    assert r.response_code == 0 and unpack_float(r.data) == pytest.approx(4.05)
    assert current(dev) == pytest.approx(4.05, abs=1e-5)
    send(dev, 40, f(20.0))
    r = send(dev, 46, f(19.9))
    assert r.response_code == 0
    assert current(dev) == pytest.approx(19.9, abs=1e-5)
    assert dev.trim_zero == pytest.approx(0.05, abs=1e-5)  # gain trim leaves the zero alone
    send(dev, 40, f(4.0))
    assert current(dev) == pytest.approx(4.0 * dev.trim_gain + 0.05, abs=1e-5)
    send(dev, 40, f(0.0))  # back to normal mode: trim still applies
    assert current(dev) == pytest.approx(10.4 * dev.trim_gain + dev.trim_zero, abs=1e-4)


def test_trim_needs_fixed_mode(dev):
    assert send(dev, 45, f(4.0)).response_code == 9
    assert send(dev, 46, f(20.0)).response_code == 9
    assert (dev.trim_zero, dev.trim_gain) == (0.0, 1.0)
    send(dev, 40, f(4.0))
    assert send(dev, 45, f(float("nan"))).response_code == 2
    assert send(dev, 45, b"\0").response_code == 5
    assert (dev.trim_zero, dev.trim_gain) == (0.0, 1.0)


def test_preambles(dev):
    r = send(dev, 59, bytes([8]))
    assert r.response_code == 0 and r.data == bytes([8]) and dev.response_preambles == 8
    assert send(dev, 59, bytes([21])).response_code == 3
    assert send(dev, 59, bytes([4])).response_code == 4
    assert send(dev, 59).response_code == 5
    assert dev.response_preambles == 8


def test_write_protect_blocks_common_writes(dev):
    dev.write_protect = True
    for cmd, data in [(34, f(1)), (35, bytes([12]) + f(600, 0)), (36, b""), (37, b""),
                      (40, f(12)), (42, b""), (44, bytes([7])), (45, f(4)), (46, f(20)),
                      (59, bytes([6]))]:
        assert send(dev, cmd).response_code == 7, cmd
    assert send(dev, 33, bytes([0])).response_code == 0
    assert send(dev, 41).response_code == 0


@pytest.mark.parametrize("cmd,nbytes", [(34, 3), (35, 8), (40, 3), (44, 0), (45, 3),
                                        (46, 3), (59, 0)])
def test_short_data_common_write_commands(dev, cmd, nbytes):  # Review Focus 2
    before = (dev.lrv, dev.urv, dev.damping, dev.pv_unit, dev.fixed_ma, dev.trim_zero,
              dev.trim_gain, dev.response_preambles, dev.config_counter)
    assert send(dev, cmd, b"\0" * nbytes).response_code == 5
    assert before == (dev.lrv, dev.urv, dev.damping, dev.pv_unit, dev.fixed_ma, dev.trim_zero,
                      dev.trim_gain, dev.response_preambles, dev.config_counter)


# --- fault effects -------------------------------------------------------------

def test_sensor_failure_fault(dev):
    send(dev, 1)  # consume the first-contact cold start bit
    dev.faults.inject("sensor_failure")
    r = send(dev, 2)
    assert r.device_status == 0x91 and unpack_float(r.data[:4]) == 21.0
    d = send(dev, 48).data
    assert d[0] & 0x01
    assert d[6] & 0x02
    r = send(dev, 9, bytes([0]))
    assert r.data[0] == 0x02 and r.data[8] == 0x00
    assert r.device_status & 0x10 == 0  # 48 was read
    assert r.device_status & 0x80
    dev.faults.clear("sensor_failure")
    assert send(dev, 2).device_status == 0
    assert unpack_float(send(dev, 2).data[:4]) == pytest.approx(10.4, abs=1e-3)


def test_sensor_failure_does_not_fail_self_test(dev):
    dev.faults.inject("sensor_failure")
    r = send(dev, 41)
    assert r.response_code == 0 and r.device_status & 0x80


def test_saturate_fault(dev):
    dev.faults.inject("saturate", direction="high")
    r = send(dev, 2)
    assert unpack_float(r.data[:4]) == pytest.approx(20.5) and r.device_status & 0x04
    dev.faults.inject("saturate", direction="low")
    assert unpack_float(send(dev, 2).data[:4]) == pytest.approx(3.8)
    dev.faults.clear()
    assert send(dev, 2).device_status & 0x04 == 0


def test_busy_fault_counts(dev):
    dev.faults.inject("busy", count=2)
    before = (dev.damping, dev.config_counter)
    for _ in range(2):
        r = send(dev, 34, f(7.0))
        assert r.response_code == 32 and r.data == b""
        assert (dev.damping, dev.config_counter) == before
    assert send(dev, 34, f(7.0)).response_code == 0 and dev.damping == 7.0


def test_busy_keeps_cold_start_and_address(dev):
    dev.faults.inject("busy")
    r = dev.handle(Frame(Address.broadcast(False), 0))
    assert r is None  # broadcast with command 0 is not addressed to us
    r = send(dev, 1, primary=False)
    assert r.response_code == 32 and not r.address.primary and r.address.is_long
    assert send(dev, 1, primary=False).device_status & 0x20  # cold start not consumed


def test_comm_error_fault(dev):
    dev.faults.inject("comm_error")
    r = send(dev, 34, f(7.0))
    assert r.response_code == 0x88 and r.device_status == 0 and r.data == b""
    assert dev.damping != 7.0
    r = send(dev, 1)  # count exhausted
    assert r.response_code == 0
    dev.faults.inject("comm_error", flags=0x40, count=2)
    assert send(dev, 1).response_code == 0xC0 and send(dev, 1).response_code == 0xC0
    assert send(dev, 1).response_code == 0


def test_comm_error_roundtrips_on_wire(dev):
    dev.faults.inject("comm_error")
    raw = send(dev, 1).encode()
    r = Frame.decode(raw)
    assert r.response_code == 0x88 and r.device_status == 0


def test_more_status_consumed_by_48(dev):
    dev.faults.inject("more_status", bytes=[1, 2, 3, 4, 5, 6])
    assert send(dev, 1).device_status & 0x10
    d = send(dev, 48).data
    assert d[:6] == bytes([1, 2, 3, 4, 5, 6])
    assert dev.faults.active("more_status") is None
    assert send(dev, 1).device_status & 0x10 == 0
    assert send(dev, 48).data[:6] == bytes(6)


def test_no_response_and_delay_not_applied_by_device(dev):
    dev.faults.inject("no_response")
    dev.faults.inject("delay", seconds=5)
    assert send(dev, 1).response_code == 0
    assert dev.faults.active("no_response") == {"count": 1}


def test_inactive_faults_do_not_change_status(dev):
    assert send(dev, 2).device_status == 0x20  # only cold start on first contact
    assert send(dev, 2).device_status == 0
