import pytest

from hart_sim.device import Device
from hart_sim.frame import Address, Frame, unpack_float
from hart_sim.profile import load_profile


@pytest.fixture
def dev():
    t = [0.0]
    d = Device(load_profile("pressure"), clock=lambda: t[0])
    d.pv.override(400.0)
    return d


def call(dev, cmd):
    return dev.handle(Frame(Address.long(0xFA01, 1), cmd))


def test_command0_fields(dev):
    d = call(dev, 0).data
    assert d[3] == 5 and d[5] == 1 and d[6] == 1 and d[7] == 1 << 3 and d[8] == 0
    assert d[9:12] == b"\x00\x00\x01" and d[12] == 5 and d[13] == 2
    assert d[14:16] == b"\x00\x00" and d[16] == 0
    assert d[19:21] == b"\x00\xfa" and d[21] == 1


def test_command0_no_sv_and_alert(dev):
    dev.sv = None
    assert call(dev, 0).data[13] == 1
    dev.damping = 0.0
    dev.pv.override(float("nan"))
    assert call(dev, 0).data[16] == 0x02


def test_command1_and_2(dev):
    d = call(dev, 1).data
    assert d[0] == 12 and unpack_float(d[1:5]) == 400.0
    d = call(dev, 2).data
    assert unpack_float(d[0:4]) == pytest.approx(10.4, abs=1e-4)
    assert unpack_float(d[4:8]) == pytest.approx(40.0)


def test_multidrop_loop_current(dev):
    dev.loop_current_mode = False
    assert unpack_float(call(dev, 2).data[0:4]) == pytest.approx(4.0)
