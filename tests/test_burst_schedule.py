import pytest

from hart_sim.burst import FALLING, ON_CHANGE, RISING, WINDOW, to_time
from hart_sim.commands.universal import additional_status
from hart_sim.device import Device
from hart_sim.frame import Address, Frame, pack_float
from hart_sim.profile import load_profile


@pytest.fixture
def dev():
    d = Device(load_profile("pressure"), clock=lambda: 0.0)
    d.damping = 0
    d.pv.override(400.0)
    return d


def cmd(dev, n, data=b""):
    r = dev.handle(Frame(Address.long(0xFA01, 1), n, data))
    return r


def t4(seconds):
    return to_time(seconds).to_bytes(4, "big")


def start(dev, index=0, command=1, period=0.5, now=0.0):
    msg = dev.burst[index]
    msg.command, msg.period = command, period
    msg.control = 1
    msg.next_due = None
    return msg


def test_first_frame_at_once_then_every_period(dev):
    start(dev)
    assert len(dev.burst_due(0.0)) == 1
    assert dev.burst_due(0.25) == []
    assert len(dev.burst_due(0.5)) == 1
    assert dev.burst_due(0.75) == []
    assert len(dev.burst_due(1.0)) == 1


def test_late_call_does_not_catch_up(dev):
    start(dev)
    dev.burst_due(0.0)
    assert len(dev.burst_due(5.0)) == 1
    assert dev.burst_due(5.2) == []
    assert len(dev.burst_due(5.5)) == 1


def test_off_messages_send_nothing(dev):
    assert dev.burst_due(0.0) == []
    assert dev.next_burst_due() is None


def test_frame_layout_cmd3(dev):
    start(dev, command=3)
    (f,) = dev.burst_due(0.0)
    assert f.is_burst and f.is_response and f.command == 3 and f.response_code == 0
    assert f.address.primary and f.address.burst and f.address.unique_id == dev.unique_id
    status = dev.status_byte(True)
    assert f.data == cmd(dev, 3).data
    assert f.device_status == status


def test_cmd9_burst_uses_slots(dev):
    msg = start(dev, command=9)
    msg.slots = [1, 0] + [250] * 6
    (f,) = dev.burst_due(0.0)
    assert f.data[1] == 1 and f.data[1 + 8] == 0 and len(f.data) == 1 + 2 * 8 + 4
    msg.slots = [246] + [250] * 7
    msg.next_due = None
    (f,) = dev.burst_due(0.0)
    assert len(f.data) == 1 + 8 + 4 and f.data[1] == 246


def test_cmd33_burst_at_most_four_slots(dev):
    msg = start(dev, command=33)
    msg.slots = [0, 1, 0, 1, 0, 1, 250, 250]
    (f,) = dev.burst_due(0.0)
    assert len(f.data) == 4 * 6 and f.data[0] == 0 and f.data[6] == 1


def test_cmd48_burst_does_not_acknowledge_more_status(dev):
    dev.faults.inject("more_status", bytes=[1, 0, 0, 0, 0, 0])
    dev.more_status_pending = True
    start(dev, command=48)
    (f,) = dev.burst_due(0.0)
    assert f.data == additional_status(dev) and f.device_status & 0x10
    assert dev.more_status_pending and dev.faults.active("more_status") is not None


def test_burst_does_not_consume_cold_start(dev):
    start(dev)
    (f,) = dev.burst_due(0.0)
    assert f.device_status & 0x20  # pending state is shown, never cleared by a burst
    assert len(dev.burst_due(0.5)) == 1
    assert cmd(dev, 0).device_status & 0x20
    assert not cmd(dev, 0).device_status & 0x20


def test_burst_does_not_consume_config_changed(dev):
    cmd(dev, 0)
    cmd(dev, 108, b"\x00\x02\x00")
    start(dev, command=1)
    (f,) = dev.burst_due(0.0)
    assert f.device_status & 0x40
    assert dev.status_byte(True) & 0x40


def test_window_trigger_and_max_period(dev):
    msg = start(dev, period=0.5)
    msg.trigger_mode, msg.trigger_level, msg.max_period = WINDOW, 5.0, 3.0
    assert len(dev.burst_due(0.0)) == 1  # first frame
    dev.pv.override(403.0)
    assert dev.burst_due(0.5) == []
    dev.pv.override(406.0)
    assert len(dev.burst_due(1.0)) == 1
    assert dev.burst_due(1.5) == []  # 406 vs last 406
    assert dev.burst_due(3.5) == []  # 2.5 s since last frame
    assert len(dev.burst_due(4.0)) == 1  # max period reached


def test_rising_falling_on_change(dev):
    msg = start(dev, period=0.5)
    msg.trigger_mode, msg.trigger_level, msg.max_period = RISING, 410.0, 100.0
    dev.burst_due(0.0)  # first frame regardless
    assert dev.burst_due(0.5) == []
    dev.pv.override(420.0)
    assert len(dev.burst_due(1.0)) == 1
    msg.trigger_mode, msg.trigger_level = FALLING, 390.0
    assert dev.burst_due(1.5) == []
    dev.pv.override(380.0)
    assert len(dev.burst_due(2.0)) == 1
    msg.trigger_mode = ON_CHANGE
    assert dev.burst_due(2.5) == []
    dev.pv.override(381.0)
    assert len(dev.burst_due(3.0)) == 1
    assert dev.burst_due(3.5) == []


def test_trigger_uses_sv_when_slot0_is_sv(dev):
    msg = start(dev)
    msg.slots = [1] + [250] * 7
    msg.trigger_mode, msg.trigger_level, msg.max_period = RISING, 1e9, 100.0
    dev.burst_due(0.0)
    assert dev.burst_due(0.5) == []  # SV far below the level; PV is irrelevant


def test_burst_stop_fault_pauses_and_resumes(dev):
    start(dev)
    assert len(dev.burst_due(0.0)) == 1
    dev.faults.inject("burst_stop")
    assert dev.burst_due(0.5) == [] and dev.burst_due(1.0) == []
    assert cmd(dev, 0).address.burst  # configuration unchanged
    dev.faults.clear("burst_stop")
    assert dev.burst_due(1.2) == []
    assert len(dev.burst_due(1.5)) == 1


def test_changing_command_while_on_restarts(dev):
    start(dev, command=1)
    dev.burst_due(0.0)
    assert cmd(dev, 108, b"\x00\x02\x00").response_code == 0
    (f,) = dev.burst_due(0.2)
    assert f.command == 2
    assert dev.burst_due(0.4) == []
    assert [x.command for x in dev.burst_due(0.7)] == [2]


@pytest.mark.parametrize("n, data", [(108, b"\x00\x02\x00"),
                                     (103, b"\x00" + t4(2.0) + t4(60.0))])
def test_writes_to_running_message_reset_schedule(dev, n, data):
    msg = start(dev)
    dev.burst_due(0.0)
    assert msg.next_due == 0.5
    assert cmd(dev, n, data).response_code == 0
    assert msg.next_due is None
    assert len(dev.burst_due(0.1)) == 1


def test_reply_to_109_carries_burst_bit_after_the_command(dev):
    assert cmd(dev, 109, b"\x01\x00").address.burst
    assert not cmd(dev, 109, b"\x00\x00").address.burst


def test_three_messages_independent(dev):
    start(dev, 0, command=1, period=0.5)
    start(dev, 1, command=2, period=0.3)
    start(dev, 2, command=3, period=1.0)
    assert [f.command for f in dev.burst_due(0.0)] == [1, 2, 3]
    assert [f.command for f in dev.burst_due(0.3)] == [2]
    assert [f.command for f in dev.burst_due(0.5)] == [1]
    assert [f.command for f in dev.burst_due(0.6)] == [2]
    assert [f.command for f in dev.burst_due(1.0)] == [1, 2, 3]


def test_sensor_failure_in_burst_status(dev):
    start(dev)
    dev.faults.inject("sensor_failure")
    (f,) = dev.burst_due(0.0)
    assert f.device_status & 0x81 == 0x81


def test_next_burst_due(dev):
    assert dev.next_burst_due() is None
    start(dev, 0, period=0.5)
    start(dev, 1, period=0.3)
    assert dev.next_burst_due() == 0.0  # restarts from now (fake clock at 0)
    dev.burst_due(2.0)
    assert dev.next_burst_due() == pytest.approx(2.3)
    dev.burst[1].control = 0
    assert dev.next_burst_due() == pytest.approx(2.5)
