import dataclasses
from pathlib import Path

import pytest
import yaml

import hart_sim

from hart_sim.burst import BURSTABLE, MESSAGES, MIN_PERIOD, to_seconds, to_time
from hart_sim.commands.universal import additional_status
from hart_sim.device import Device
from hart_sim.faults import FaultError
from hart_sim.frame import Address, Frame, pack_ascii, pack_float
from hart_sim.profile import ProfileError, load_profile


@pytest.fixture
def dev():
    t = [0.0]
    d = Device(load_profile("pressure"), clock=lambda: t[0])
    d.pv.override(400.0)
    return d


def cmd(dev, n, data=b""):
    r = dev.handle(Frame(Address.long(0xFA01, 1), n, data))
    return r.response_code, r.data


def t4(seconds):
    return to_time(seconds).to_bytes(4, "big")


def secs(raw):
    return to_seconds(int.from_bytes(raw, "big"))


def test_time_conversion():
    assert to_time(1.0) == 32000 and to_seconds(32000) == 1.0
    assert MESSAGES == 3 and MIN_PERIOD == 0.1 and BURSTABLE == (1, 2, 3, 9, 33, 48)


def test_105_default_configuration(dev):
    code, d = cmd(dev, 105, b"\x00")
    assert code == 0 and len(d) == 29
    assert d[0] == 0 and d[1] == 31 and d[2:10] == bytes([0, 1] + [250] * 6)
    assert d[10] == 0 and d[11] == 3 and d[12:14] == (1).to_bytes(2, "big")
    assert to_seconds(int.from_bytes(d[14:18], "big")) == pytest.approx(1.0)
    assert to_seconds(int.from_bytes(d[18:22], "big")) == pytest.approx(60.0)
    assert d[22] == 0


def test_105_default_slots_without_sv():
    assert Device(load_profile("pressure")).burst[0].slots[:2] == [0, 1]
    p = dataclasses.replace(load_profile("pressure"), sv=None)
    assert Device(p).burst[0].slots == [0] + [250] * 7


def test_105_without_data_reads_message_0(dev):
    cmd(dev, 108, b"\x00\x03\x00")
    assert cmd(dev, 105, b"") == cmd(dev, 105, b"\x00")
    assert cmd(dev, 105, b"")[1][12:14] == b"\x00\x03"
    assert cmd(dev, 105, b"\x01")[1][12:14] == b"\x00\x01"
    assert cmd(dev, 105, b"\x01")[1][10] == 1


def test_103_sets_period(dev):
    req = b"\x00" + t4(0.5) + t4(10.0)
    code, d = cmd(dev, 103, req)
    assert code == 0 and d == req
    assert cmd(dev, 105, b"\x00")[1][14:22] == req[1:]


def test_103_clamps_to_min_period_code_8(dev):
    code, d = cmd(dev, 103, b"\x00" + t4(0.01) + t4(10.0))
    assert code == 8 and d[0] == 0
    assert secs(d[1:5]) == pytest.approx(0.1) and secs(d[5:9]) == pytest.approx(10.0)
    assert secs(cmd(dev, 105, b"\x00")[1][14:18]) == pytest.approx(0.1)


def test_103_max_below_period_is_raised_code_8(dev):
    code, d = cmd(dev, 103, b"\x00" + t4(5.0) + t4(2.0))
    assert code == 8
    assert secs(d[1:5]) == pytest.approx(5.0) and secs(d[5:9]) == pytest.approx(5.0)
    assert secs(cmd(dev, 105, b"\x00")[1][18:22]) == pytest.approx(5.0)


def test_103_short_request_code_5(dev):
    assert cmd(dev, 103, b"\x00" + t4(1.0))[0] == 5


def test_104_window_trigger(dev):
    req = bytes([0, 1, 65, 12]) + pack_float(5.0)
    code, d = cmd(dev, 104, req)
    assert code == 0 and d == req
    r = cmd(dev, 105, b"\x00")[1]
    assert r[22:25] == bytes([1, 65, 12]) and r[25:29] == pack_float(5.0)


def test_104_continuous_stores_anything(dev):
    req = bytes([0, 0, 0, 250]) + pack_float(0.0)
    assert cmd(dev, 104, req) == (0, req)


def test_104_wrong_unit_code_12(dev):
    assert cmd(dev, 104, bytes([0, 1, 65, 7]) + pack_float(1.0))[0] == 12
    assert cmd(dev, 105, b"\x00")[1][22] == 0


def test_104_wrong_class_code_11(dev):
    assert cmd(dev, 104, bytes([0, 2, 64, 12]) + pack_float(1.0))[0] == 11


def test_104_follows_slot_0_variable(dev):
    cmd(dev, 107, bytes([1, 250, 250, 250, 250, 250, 250, 250, 0]))  # SV (degC) first
    assert cmd(dev, 104, bytes([0, 4, 64, 32]) + pack_float(0.5))[0] == 0
    assert cmd(dev, 104, bytes([0, 4, 65, 12]) + pack_float(0.5))[0] == 11


def test_104_mode_5_code_13(dev):
    assert cmd(dev, 104, bytes([0, 5, 65, 12]) + pack_float(1.0))[0] == 13


def test_107_slots_and_short_request(dev):
    code, d = cmd(dev, 107, bytes([0, 1]))
    assert code == 0 and d == bytes([0, 1] + [250] * 6 + [0])
    code, d = cmd(dev, 107, bytes([247, 246, 0, 250, 250, 250, 250, 250, 1]))
    assert code == 0 and d[8] == 1
    assert cmd(dev, 105, b"\x01")[1][2:10] == bytes([247, 246, 0] + [250] * 5)
    assert dev.burst[0].slots[:2] == [0, 1]


def test_107_unknown_code_2(dev):
    before = cmd(dev, 105, b"\x00")[1]
    assert cmd(dev, 107, bytes([0, 7]))[0] == 2
    assert cmd(dev, 105, b"\x00")[1] == before


def test_107_empty_request_code_5(dev):
    assert cmd(dev, 107, b"")[0] == 5


def test_108_command_3(dev):
    code, d = cmd(dev, 108, b"\x00\x03\x01")
    assert code == 0 and d == b"\x00\x03\x01"
    assert cmd(dev, 105, b"\x01")[1][12:14] == b"\x00\x03"
    assert cmd(dev, 105, b"\x00")[1][12:14] == b"\x00\x01"


def test_108_one_byte_hart5_form(dev):
    code, d = cmd(dev, 108, b"\x09")
    assert code == 0 and d == b"\x00\x09\x00"
    assert cmd(dev, 105, b"\x00")[1][12:14] == b"\x00\x09"


@pytest.mark.parametrize("n", [13, 0, 4, 258])
def test_108_not_burstable_code_2(dev, n):
    assert cmd(dev, 108, n.to_bytes(2, "big") + b"\x00")[0] == 2
    assert cmd(dev, 105, b"\x00")[1][12:14] == b"\x00\x01"


def test_109_on_off(dev):
    assert cmd(dev, 109, b"\x01\x00") == (0, b"\x01\x00")
    assert dev.burst_on and cmd(dev, 105, b"\x00")[1][0] == 1
    assert cmd(dev, 109, b"\x00") == (0, b"\x00\x00")
    assert not dev.burst_on
    assert cmd(dev, 109, b"\x04\x02")[0] == 0 and dev.burst[2].on and dev.burst_on


def test_109_code_2_and_over_4_rejected(dev):
    assert cmd(dev, 109, b"\x02\x00")[0] == 2
    assert cmd(dev, 109, b"\x05\x00")[0] == 2
    assert not dev.burst_on


@pytest.mark.parametrize("n", [103, 104, 105, 107, 108, 109])
def test_message_index_3_code_9(dev, n):
    data = {103: b"\x03" + t4(1.0) + t4(2.0), 104: bytes([3, 0, 65, 12]) + pack_float(0.0),
            105: b"\x03", 107: bytes([0] * 8 + [3]), 108: b"\x00\x01\x03",
            109: b"\x01\x03"}[n]
    assert cmd(dev, n, data) == (9, b"")


WRITES = {103: b"\x00" + t4(2.0) + t4(9.0), 104: bytes([0, 1, 65, 12]) + pack_float(2.0),
          107: bytes([1, 0] + [250] * 6 + [0]), 108: b"\x00\x03\x00", 109: b"\x01\x00"}


def test_writes_refused_in_write_protect(dev):
    dev.write_protect = True
    before = cmd(dev, 105, b"\x00")
    counter = dev.config_counter
    for n, data in WRITES.items():
        assert cmd(dev, n, data) == (7, b""), n
    assert cmd(dev, 105, b"\x00") == before == (0, before[1])
    assert dev.config_counter == counter and not dev.burst_on


@pytest.mark.parametrize("n", sorted(WRITES))
def test_writes_bump_config_counter(dev, n):
    before = dev.config_counter
    assert cmd(dev, n, WRITES[n])[0] == 0
    assert dev.config_counter == before + 1
    assert cmd(dev, n, WRITES[n])[0] == 0  # unchanged: no bump
    assert dev.config_counter == before + 1


def test_reply_address_has_burst_bit_while_on(dev):
    r = dev.handle(Frame(Address.long(0xFA01, 1), 1))
    assert not r.address.burst
    cmd(dev, 109, b"\x01\x01")
    r = dev.handle(Frame(Address.long(0xFA01, 1), 1))
    assert r.address.burst and Frame.decode(r.encode()).address.burst
    r = dev.handle(Frame(Address.broadcast(), 11, pack_ascii(dev.tag, 8)))
    assert r.address.burst
    cmd(dev, 109, b"\x00\x01")
    assert not dev.handle(Frame(Address.long(0xFA01, 1), 1)).address.burst


def test_cmd38_and_42_keep_burst_state(dev):
    cmd(dev, 109, b"\x01\x00")
    cmd(dev, 38, b"")
    cmd(dev, 42, b"")
    assert dev.burst[0].on and dev.burst_on


def test_cmd48_split_has_no_side_effect(dev):
    dev.faults.inject("more_status", bytes=[1, 2, 3, 4, 5, 6])
    a = additional_status(dev)
    assert additional_status(dev) == a and a[:6] == bytes([1, 2, 3, 4, 5, 6])
    assert dev.more_status_pending
    code, d = cmd(dev, 48)
    assert d == a and not dev.more_status_pending


def test_burst_stop_fault(dev):
    dev.faults.inject("burst_stop")
    assert dev.faults.active("burst_stop") == {}
    with pytest.raises(FaultError):
        dev.faults.inject("burst_stop", count=1)
    dev.faults.clear("burst_stop")
    assert dev.faults.active("burst_stop") is None


def _profile(tmp_path, burst):
    raw = yaml.safe_load((Path(hart_sim.__file__).parent / "profiles" / "pressure.yaml")
                         .read_text(encoding="utf-8"))
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump({**raw, "burst": burst}))
    return load_profile(path)


def test_profile_burst_block_starts_enabled(tmp_path):
    p = _profile(tmp_path, [{"command": 3, "period": 0.5, "enabled": True}])
    d = Device(p)
    assert d.burst_on and d.burst[0].on and d.burst[0].command == 3
    assert d.burst[0].period == 0.5 and d.burst[0].max_period == 60.0
    assert not d.burst[1].on and d.burst[1].command == 1
    assert len(d.burst) == 3


def test_profile_burst_block_all_keys(tmp_path):
    p = _profile(tmp_path, [{"command": 9, "period": 2, "max_period": 30,
                             "variables": [1, 0], "trigger": "window", "level": 2.5}])
    m = Device(p).burst[0]
    assert m.slots == [1, 0] + [250] * 6 and m.trigger_mode == 1 and m.trigger_level == 2.5
    assert m.max_period == 30.0 and not m.on
    assert (m.trigger_class, m.trigger_unit) == (64, 32)  # slot 0 = SV (degC)


def test_profile_burst_invalid_entries(tmp_path):
    ok = {"command": 3}
    for bad in ([ok] * 4,
                [{"command": 13}],
                [{"period": 0}],
                [{"period": 0.05}],
                [{"period": 5, "max_period": 1}],
                [{"bogus": 1}],
                [{"trigger": "x"}],
                [{"variables": [7]}],
                [{"variables": [0] * 9}],
                [{"enabled": "yes"}],
                [{"level": "high"}],
                "nope", [3]):
        with pytest.raises(ProfileError, match="burst"):
            _profile(tmp_path, bad)
