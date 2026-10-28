import pytest

from hart_sim.frame import (
    Address,
    Frame,
    FrameError,
    NAN_BYTES,
    checksum,
    pack_ascii,
    pack_float,
    unpack_ascii,
    unpack_float,
)


def test_checksum_command0_short_request():
    raw = Frame(Address.short(0), 0).encode()
    assert raw == bytes([0x02, 0x80, 0x00, 0x00, 0x82])


def test_checksum_function():
    assert checksum(bytes([0x02, 0x80, 0x00, 0x00])) == 0x82


def test_long_address_layout():
    a = Address.long(0xFA01, 0x123456)
    raw = Frame(a, 3).encode()
    assert raw[0] == 0x82
    assert raw[1:6] == bytes([0x80 | 0x3A, 0x01, 0x12, 0x34, 0x56])


def test_response_roundtrip_with_status():
    f = Frame(Address.long(0xFA01, 1), 1, b"\x0c" + pack_float(1.5), True, 0, 0x40)
    raw = f.encode()
    assert raw[0] == 0x86 and raw[7] == 2 + 5 and raw[8:10] == b"\x00\x40"
    decoded = Frame.decode(raw)
    assert decoded.address.unique_id == (0xFA01 & 0x3FFF, 1)
    assert decoded == Frame(Address.long(0xFA01 & 0x3FFF, 1), 1, f.data, True, 0, 0x40)


def test_decode_masks_device_type_to_14_bits():
    raw = Frame(Address.long(0xFA02, 7), 3).encode()
    assert Frame.decode(raw).address.unique_id == (0x3A02, 7)


def test_unpack_float_wrong_length():
    for bad in (b"", b"\x00" * 3, b"\x00" * 5):
        with pytest.raises(ValueError):
            unpack_float(bad)


def test_request_roundtrip_secondary_burst():
    a = Address(polling=5, unique_id=None, primary=False, burst=True)
    f = Frame(a, 2, b"\x01\x02")
    raw = f.encode()
    assert raw[1] == 0x40 | 5
    assert Frame.decode(raw) == f


def test_bad_checksum_rejected():
    raw = bytearray(Frame(Address.short(0), 0).encode())
    raw[-1] ^= 1
    with pytest.raises(FrameError):
        Frame.decode(bytes(raw))


def test_byte_count_mismatch_rejected():
    raw = bytearray([0x02, 0x80, 0x05, 0x05, 1, 2, 3])
    raw.append(checksum(bytes(raw)))
    with pytest.raises(FrameError):
        Frame.decode(bytes(raw))


def test_bad_delimiter_and_short_input_rejected():
    with pytest.raises(FrameError):
        Frame.decode(b"\x03\x80\x00\x00\x83")
    with pytest.raises(FrameError):
        Frame.decode(b"\x02\x80")
    with pytest.raises(FrameError):
        Frame.decode(b"")


def test_response_byte_count_below_two_rejected():
    raw = bytearray([0x06, 0x80, 0x00, 0x01, 0x00])
    raw.append(checksum(bytes(raw)))
    with pytest.raises(FrameError):
        Frame.decode(bytes(raw))


def test_pack_ascii_roundtrip_and_upper():
    assert len(pack_ascii("pt-101", 8)) == 6
    assert unpack_ascii(pack_ascii("pt-101", 8)) == "PT-101"


def test_pack_ascii_rejects_too_long_and_invalid():
    with pytest.raises(ValueError):
        pack_ascii("TOOLONGTAG", 8)
    with pytest.raises(ValueError):
        pack_ascii("ä", 8)


def test_float_roundtrip_and_nan():
    assert unpack_float(pack_float(1.5)) == 1.5
    assert pack_float(float("nan")) == NAN_BYTES
    assert unpack_float(NAN_BYTES) != unpack_float(NAN_BYTES)


def test_broadcast_address():
    assert Frame.decode(Frame(Address.broadcast(), 11, b"\0" * 6).encode()).address.is_broadcast
