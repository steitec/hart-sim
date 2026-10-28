import pytest

from hart_sim import hartip


def test_header_roundtrip_and_errors():
    raw = hartip.encode_message(hartip.REQUEST, hartip.PDU, 0x1234, b"\x01\x02", status=0)
    assert raw == bytes([1, 0, 3, 0, 0x12, 0x34, 0, 10, 1, 2])
    h = hartip.decode_header(raw)
    assert (h.version, h.msg_type, h.msg_id, h.status, h.sequence, h.byte_count) == (
        1, 0, 3, 0, 0x1234, 10)
    with pytest.raises(hartip.HartIPError):
        hartip.decode_header(raw[:7])
    with pytest.raises(hartip.HartIPError):
        hartip.decode_header(bytes([2, 0, 3, 0, 0, 1, 0, 8]))
    with pytest.raises(hartip.HartIPError):
        hartip.decode_header(bytes([1, 0, 3, 0, 0, 1, 0, 7]))
    assert hartip.PORT == 5094
