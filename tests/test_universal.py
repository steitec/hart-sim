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


# --- Task 5: remaining universal commands ---------------------------------------

import random

from hart_sim import commands
from hart_sim.frame import pack_ascii, pack_float


def req(cmd, data=b"", primary=True):
    return Frame(Address.long(0xFA01, 1, primary), cmd, data)


def snapshot(d):
    return (d.polling_address, d.loop_current_mode, d.message, d.tag, d.descriptor, d.date,
            d.final_assembly, d.long_tag, d.config_counter, d.status_byte(True) & 0x40)


LONG_TAG = b"Pump 1 discharge pressure".ljust(32, b"\0")


def test_command6_and_7(dev):
    r = dev.handle(req(6, bytes([3, 1])))
    assert r.response_code == 0 and r.data == bytes([3, 1]) and dev.polling_address == 3
    assert dev.loop_current_mode is True and dev.config_counter == 1
    dev.handle(Frame(Address.long(0xFA01, 1), 6, bytes([0, 1])))
    assert dev.handle(req(7)).data == bytes([0, 1])


def test_command6_one_byte_sets_mode_from_address(dev):
    dev.handle(req(6, bytes([5])))
    assert dev.loop_current_mode is False
    dev.handle(req(6, bytes([0])))
    assert dev.loop_current_mode is True


def test_command6_invalid_selection(dev):
    assert dev.handle(req(6, bytes([64, 1]))).response_code == 2
    assert dev.handle(req(6, bytes([1, 2]))).response_code == 2
    assert dev.polling_address == 0 and dev.config_counter == 0


def test_command6_multidrop_fixes_current(dev):
    dev.handle(req(6, bytes([3, 0])))
    r = dev.handle(req(2))
    assert unpack_float(r.data[0:4]) == pytest.approx(4.0)
    assert r.device_status & 0x08


def test_command8(dev):
    assert call(dev, 8).data == bytes([65, 64, 0, 0])
    dev.sv = None
    assert call(dev, 8).data == bytes([65, 0, 0, 0])


def test_command9(dev):
    r = dev.handle(req(9, bytes([0, 247])))
    d = r.data
    assert r.response_code == 0 and len(d) == 1 + 2 * 8 + 4
    assert d[0] == 0
    assert d[1:4] == bytes([0, 65, 12]) and unpack_float(d[4:8]) == 400.0 and d[8] == 0xC0
    assert d[9:12] == bytes([247, 64, 32]) and unpack_float(d[12:16]) == 23.5 and d[16] == 0xC0
    assert d[17:21] == bytes(4)  # clock at 0
    dev.damping = 0.0
    dev.pv.override(float("nan"))
    r = dev.handle(req(9, bytes([246])))
    assert r.data[0] == 0x02 and r.data[8] == 0x00


def test_command9_bad_variable_code(dev):
    assert dev.handle(req(9, bytes([0, 7]))).response_code == 2
    assert dev.handle(req(9, b"")).response_code == 5


def test_command11_and_21_match(dev):
    r = dev.handle(req(11, pack_ascii("PT-101", 8)))
    assert r.response_code == 0 and r.data == call(dev, 0).data
    r = dev.handle(req(21, LONG_TAG))
    assert r.response_code == 0 and r.data == call(dev, 0).data


def test_command11_wrong_tag_silent(dev):
    assert dev.handle(req(11, pack_ascii("XX-999", 8))) is None
    assert dev.handle(req(21, b"other".ljust(32, b"\0"))) is None
    assert dev.handle(Frame(Address.short(0), 11, pack_ascii("XX-999", 8))) is None


def test_command11_broadcast_finds_tag(dev):
    assert dev.handle(Frame(Address.broadcast(), 11, pack_ascii("PT-101", 8))).data[0] == 254
    assert dev.handle(Frame(Address.broadcast(), 11, pack_ascii("XX-999", 8))) is None
    assert dev.handle(Frame(Address.broadcast(), 21, LONG_TAG)).data[0] == 254


def test_command12_and_17(dev):
    assert call(dev, 12).data == bytes.fromhex("820820" * 8)
    msg = pack_ascii("HELLO WORLD", 32)
    r = dev.handle(req(17, msg))
    assert r.response_code == 0 and r.data == msg and dev.message == "HELLO WORLD"
    assert call(dev, 12).data == msg and dev.config_counter == 1


def test_command13_and_18(dev):
    assert call(dev, 13).data.hex() == "414b71c318204c93604121534d548582082001017e"
    new = pack_ascii("TX-1", 8) + pack_ascii("NEW DESC", 16) + bytes([24, 12, 126])
    r = dev.handle(req(18, new))
    assert r.response_code == 0 and r.data == new
    assert (dev.tag, dev.descriptor, dev.date) == ("TX-1", "NEW DESC", (24, 12, 2026))
    assert call(dev, 13).data == new


def test_command18_bad_date(dev):
    bad = pack_ascii("TX-1", 8) + pack_ascii("NEW DESC", 16) + bytes([32, 12, 126])
    before = snapshot(dev)
    assert dev.handle(req(18, bad)).response_code == 2 and snapshot(dev) == before


def test_command14_and_15(dev):
    assert call(dev, 14).data.hex() == "0000010c451c4000c2c8000041200000"
    assert call(dev, 15).data.hex() == "00000c447a0000000000003f00000000fa00"
    dev.write_protect = True
    assert call(dev, 15).data[15] == 1


def test_command16_and_19(dev):
    assert call(dev, 16).data == bytes(3)
    assert dev.handle(req(19, b"\x01\x02\x03")).data == b"\x01\x02\x03"
    assert call(dev, 16).data == b"\x01\x02\x03" and dev.config_counter == 1


def test_command20_and_22(dev):
    assert call(dev, 20).data == LONG_TAG
    new = "Caf\xe9 tank".encode("latin-1").ljust(32, b"\0")
    assert dev.handle(req(22, new)).data == new
    assert dev.long_tag == "Caf\xe9 tank" and call(dev, 20).data == new


def test_write_protect_blocks_writes(dev):
    dev.write_protect = True
    new = pack_ascii("TX-1", 8) + pack_ascii("NEW DESC", 16) + bytes([24, 12, 126])
    assert dev.handle(req(18, new)).response_code == 7
    assert dev.tag == "PT-101"
    assert dev.handle(req(13)).response_code == 0


def test_command38_counter_mismatch(dev):
    dev.mark_config_changed()
    r = dev.handle(req(38, b"\x00\x07"))
    assert r.response_code == 9
    assert dev.handle(req(1)).device_status & 0x40
    r = dev.handle(req(38, b"\x00\x01"))
    assert r.response_code == 0 and r.data == b"\x00\x01"
    assert dev.handle(req(1)).device_status & 0x40 == 0


def test_command48(dev):
    d = call(dev, 48).data
    assert len(d) == 14 and d == bytes(14)
    dev.fixed_ma = 12.0
    assert call(dev, 48).data[13] == 1
    dev.fixed_ma = None
    dev.damping = 0.0
    dev.pv.override(5000.0)
    assert call(dev, 48).data[10] == 1


@pytest.mark.parametrize("cmd,nbytes", [(6, 0), (17, 23), (18, 20), (19, 2), (22, 31), (38, 1)])
def test_short_data_every_write_command(dev, cmd, nbytes):  # Review Focus 2
    before = snapshot(dev)
    r = dev.handle(req(cmd, b"\0" * nbytes))
    assert r.response_code == 5 and snapshot(dev) == before


@pytest.mark.parametrize("protected", [False, True])
def test_garbage_never_raises(dev, protected):
    dev.write_protect = protected
    rng = random.Random(1234)
    for cmd in sorted(commands.REGISTRY):
        for n in list(range(0, 40)) + [255]:
            for _ in range(5):
                data = bytes(rng.randrange(256) for _ in range(n))
                for addr in (Address.long(0xFA01, 1), Address.broadcast(), Address.short(0)):
                    r = dev.handle(Frame(addr, cmd, data))
                    assert r is None or isinstance(r, Frame)
