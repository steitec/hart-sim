from pathlib import Path

import pytest

from hart_sim.bench import Bench, BenchError

PLANT = Path(__file__).parent.parent / "examples" / "plant.yaml"


def entry(**kw):
    d = {"profile": "pressure", "polling_address": 1, "device_id": 1}
    d.update(kw)
    return d


def test_plant_example_loads():
    bench = Bench.from_yaml(PLANT)
    assert list(bench.devices) == ["PT-101", "TT-201"]
    pt = bench.devices["PT-101"]
    assert (pt.polling_address, pt.device_id) == (1, 1)
    assert bench.devices["TT-201"].polling_address == 2


def test_duplicate_polling_address_rejected():
    with pytest.raises(BenchError, match="polling address"):
        Bench.from_dict({"devices": [entry(tag="A"), entry(tag="B", device_id=2)]})


def test_duplicate_tag_and_id_rejected():
    with pytest.raises(BenchError, match="tag"):
        Bench.from_dict({"devices": [entry(tag="A"), entry(tag="A", polling_address=2, device_id=2)]})
    with pytest.raises(BenchError, match="device ID"):
        Bench.from_dict({"devices": [entry(tag="A"), entry(tag="B", polling_address=2)]})


def test_range_checks():
    for bad in (dict(polling_address=64), dict(polling_address=-1), dict(device_id=0x1000000),
                dict(device_id=-1), dict(polling_address=True), dict(device_id="1")):
        with pytest.raises(BenchError):
            Bench.from_dict({"devices": [entry(**bad)]})
    with pytest.raises(BenchError):
        Bench.from_dict({"devices": []})
    with pytest.raises(BenchError):
        Bench.from_dict({"devices": [entry(profile="nope")]})
    with pytest.raises(BenchError, match="unknown"):
        Bench.from_dict({"devices": [entry(bogus=1)]})


def test_profile_override_applied():
    bench = Bench.from_dict({"devices": [entry(tag="PT-9", damping=2.5, range=[0, 50])]})
    dev = bench.devices["PT-9"]
    assert dev.tag == "PT-9"
    assert dev.damping == 2.5
    assert (dev.lrv, dev.urv) == (0, 50)


def test_single_and_start_stop():
    bench = Bench.single("pressure")
    assert len(bench.devices) == 1
    assert next(iter(bench.devices.values())).polling_address == 0
    bench.start(port=0, control_port=0)
    assert bench.server.port > 0 and bench.control_port > 0
    bench.stop()
    bench.stop()


def test_context_manager_uses_free_ports():
    with Bench.from_yaml(PLANT) as b:
        assert b.server.port not in (0, 5094)
        assert b.control_port not in (0, 5095)


def test_non_utf8_bench_file_is_a_bench_error(tmp_path):
    path = tmp_path / "bench.yaml"
    path.write_bytes(b"devices:\n  - profile: \xff\xfe\n")
    with pytest.raises(BenchError, match="UTF-8"):
        Bench.from_yaml(path)
