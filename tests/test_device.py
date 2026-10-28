import pytest

from hart_sim.device import Device
from hart_sim.frame import Address, Frame, pack_ascii
from hart_sim.profile import load_profile


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def dev():
    d = Device(load_profile("pressure"), polling_address=0, device_id=1, clock=Clock())
    d.pv.override(400.0)
    return d


def req(cmd, primary=True, data=b""):
    return Frame(Address.short(0, primary), cmd, data)


def test_command0_short_frame(dev):
    r = dev.handle(Frame(Address.short(0), 0))
    assert r.is_response and r.response_code == 0 and len(r.data) == 22 and r.data[0] == 254
    assert r.data[1:3] == b"\xfa\x01" and r.data[4] == 7 and r.data[17:19] == b"\x00\xfa"


def test_wrong_polling_address_silent(dev):
    assert dev.handle(Frame(Address.short(5), 0)) is None


def test_short_frame_other_command_silent(dev):
    assert dev.handle(Frame(Address.short(0), 3)) is None


def test_long_frame_matches_and_wrong_id_silent(dev):
    assert dev.handle(Frame(Address.long(0xFA01, 1), 3)) is not None
    assert dev.handle(Frame(Address.long(0xFA01, 2), 3)) is None
    assert dev.handle(Frame(Address.long(0xFA02, 1), 3)) is None


def test_long_address_decoded_masks_device_type(dev):
    decoded = Frame.decode(Frame(Address.long(0xFA01, 1), 3).encode())
    assert dev.matches(decoded.address, 3, b"")
    assert dev.handle(decoded) is not None


def test_broadcast_only_tag_commands(dev):
    assert dev.handle(Frame(Address.broadcast(), 3)) is None
    assert dev.handle(Frame(Address.broadcast(), 11, pack_ascii("PT-101", 8))) is not None
    assert dev.handle(Frame(Address.broadcast(), 11, pack_ascii("OTHER", 8))) is None
    assert dev.handle(Frame(Address.broadcast(), 11, b"\x00")) is None
    lt = b"Pump 1 discharge pressure".ljust(32)
    assert dev.matches(Address.broadcast(), 21, lt)
    assert not dev.matches(Address.broadcast(), 21, b"nope".ljust(32))


def test_response_echoes_master_bit(dev):
    assert dev.handle(req(3, primary=False)) is None  # short, not 0
    r = dev.handle(req(0, primary=False))
    assert not r.address.primary


def test_command3_values(dev):
    r = dev.handle(Frame(Address.long(0xFA01, 1), 3))
    from hart_sim.frame import unpack_float
    assert unpack_float(r.data[0:4]) == pytest.approx(10.4, abs=1e-4)
    assert r.data[4] == 12 and unpack_float(r.data[5:9]) == 400.0
    assert r.data[9] == 32 and unpack_float(r.data[10:14]) == pytest.approx(23.5)
    assert len(r.data) == 14


def test_unknown_command_64(dev):
    r = dev.handle(Frame(Address.long(0xFA01, 1), 120))
    assert r.response_code == 64


def test_cold_start_cleared_per_master(dev):
    L = lambda p: Frame(Address.long(0xFA01, 1, p), 1)
    assert dev.handle(L(True)).device_status & 0x20
    assert not dev.handle(L(True)).device_status & 0x20
    assert dev.handle(L(False)).device_status & 0x20


def test_status_bits(dev):
    dev.damping = 0.0
    dev.pv.override(5000.0)
    assert dev.status_byte(True) & 0x01
    assert dev.loop().saturated and dev.status_byte(True) & 0x04
    dev.fixed_ma = 8.0
    assert dev.status_byte(True) & 0x08
    dev.loop_current_mode = False
    assert dev.loop().fixed and dev.loop().current_ma == pytest.approx(4.0)


def test_mark_config_changed_both_masters_and_wrap(dev):
    dev.config_counter = 0xFFFF
    dev.mark_config_changed()
    assert dev.config_counter == 0
    assert dev.status_byte(True) & 0x40 and dev.status_byte(False) & 0x40


def test_write_protect_and_short_data(dev):
    from hart_sim import commands

    @commands.register(250, write=True)
    def _w(d, data, primary):
        commands.need(data, 2)
        return 0, b""

    @commands.register(251)
    def _r(d, data, primary):
        commands.need(data, 2)
        return 0, b""

    try:
        L = lambda c, data=b"": Frame(Address.long(0xFA01, 1), c, data)
        assert dev.handle(L(250, b"ab")).response_code == 0
        assert dev.handle(L(250, b"a")).response_code == 5
        assert dev.handle(L(251, b"")).response_code == 5
        dev.write_protect = True
        assert dev.handle(L(250, b"ab")).response_code == 7
        assert dev.handle(L(251, b"ab")).response_code == 0
    finally:
        commands.REGISTRY.pop(250)
        commands.REGISTRY.pop(251)


def test_ignores_response_frames(dev):
    assert dev.handle(Frame(Address.long(0xFA01, 1), 3, is_response=True)) is None


@pytest.mark.xfail(strict=True, reason="command 38 arrives in Task 5")
def test_config_changed_per_master(dev):
    dev.mark_config_changed()
    L = lambda p, c: Frame(Address.long(0xFA01, 1, p), c)
    assert dev.handle(L(True, 1)).device_status & 0x40
    assert dev.handle(L(True, 38)).response_code == 0
    assert dev.handle(L(True, 1)).device_status & 0x40 == 0
    assert dev.handle(L(False, 1)).device_status & 0x40
