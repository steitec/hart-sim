import pytest

from hart_sim.device import Device
from hart_sim.faults import FAULT_KINDS, FaultError, FaultSet
from hart_sim.profile import load_profile


@pytest.fixture
def dev():
    t = [0.0]
    d = Device(load_profile("pressure"), clock=lambda: t[0])
    d.clock_t = t
    return d


def test_fault_validation_messages():
    fs = FaultSet()
    for kind, params, text in [
            ("bogus", {}, "unknown fault 'bogus'"),
            ("delay", {"seconds": 0}, "seconds"),
            ("delay", {}, "missing parameter 'seconds'"),
            ("comm_error", {"flags": 0x80}, "flags"),
            ("comm_error", {"flags": 0}, "flags"),
            ("busy", {"count": 0}, "count"),
            ("busy", {"count": float("nan")}, "count"),
            ("busy", {"x": 1}, "unknown parameter 'x'"),
            ("saturate", {"direction": "up"}, "direction"),
            ("more_status", {"bytes": [1, 2]}, "bytes"),
            ("more_status", {"bytes": [1, 2, 3, 4, 5, 256]}, "bytes"),
            ("more_status", {"bytes": "abcdef"}, "bytes"),
            ("drift", {"per_second": float("inf")}, "per_second"),
            ("write_protect", {"on": "yes"}, "on")]:
        with pytest.raises(FaultError, match=text):
            fs.inject(kind, **params)
        assert isinstance(FaultError("x"), ValueError)
    assert fs.snapshot() == {}
    assert "no_response" in FAULT_KINDS


def test_take_counts():
    fs = FaultSet()
    fs.inject("busy", count=2)
    assert fs.take("busy") == {"count": 2}
    assert fs.active("busy") == {"count": 1}
    assert fs.take("busy") == {"count": 1}
    assert fs.take("busy") is None and fs.active("busy") is None


def test_take_without_count_does_not_consume():
    fs = FaultSet()
    fs.inject("delay", seconds=0.5)
    assert fs.take("delay")["seconds"] == 0.5
    assert fs.take("delay")["seconds"] == 0.5
    assert fs.snapshot() == {"delay": {"seconds": 0.5, "count": None}}
    fs.clear("delay")
    assert fs.take("delay") is None


def test_clear_all_and_one():
    fs = FaultSet()
    fs.inject("no_response")
    fs.inject("busy")
    fs.clear("busy")
    assert set(fs.snapshot()) == {"no_response"}
    fs.clear()
    assert fs.snapshot() == {}
    fs.clear("busy")  # clearing something absent is fine


def test_instant_faults_on_device(dev):
    dev.faults.inject("write_protect", on=True)
    assert dev.write_protect and dev.faults.active("write_protect") is None
    dev.faults.inject("write_protect", on=False)
    assert not dev.write_protect
    dev._cold_start[True] = False
    dev.faults.inject("cold_start")
    assert dev.status_byte(True) & 0x20 and dev.status_byte(False) & 0x20
    assert dev.faults.active("cold_start") is None


def test_drift_and_clear(dev):
    dev.damping = 0.0
    dev.pv.override(None)
    dev.pv.spec = dev.pv.spec.__class__(dev.pv.spec.name, dev.pv.spec.unit, ())
    dev.faults.inject("drift", per_second=2.0)
    dev.clock_t[0] = 10.0
    assert dev.pv.value() == pytest.approx(20.0)
    dev.faults.clear("drift")
    dev.clock_t[0] = 20.0
    assert dev.pv.value() == pytest.approx(20.0)
    assert dev.faults.active("drift") is None


def test_inject_replaces_params():
    fs = FaultSet()
    fs.inject("busy", count=5)
    fs.inject("busy", count=1)
    assert fs.active("busy") == {"count": 1}


def test_drift_clear_after_unit_change(dev):
    from hart_sim.frame import Address, Frame
    dev.damping = 0.0
    dev.pv.spec = dev.pv.spec.__class__(dev.pv.spec.name, dev.pv.spec.unit, ())
    dev.faults.inject("drift", per_second=2.0)
    dev.clock_t[0] = 10.0
    assert dev.handle(Frame(Address.long(0xFA01, 1), 44, bytes([7]))).response_code == 0
    assert dev.pv.value() == pytest.approx(0.2)  # 20 kPa in bar
    dev.faults.clear("drift")
    dev.clock_t[0] = 20.0
    assert dev.pv.value() == pytest.approx(0.2)
    dev.faults.inject("drift", per_second=0.01)  # bar/s
    dev.faults.inject("drift", per_second=0.02)  # replaces
    dev.clock_t[0] = 30.0
    assert dev.pv.value() == pytest.approx(0.4)
    dev.faults.clear()
    dev.clock_t[0] = 40.0
    assert dev.pv.value() == pytest.approx(0.4)


def test_threaded_inject_and_handle_do_not_deadlock(dev):
    import threading
    import time
    from hart_sim.frame import Address, Frame
    stop = time.monotonic() + 1.0
    errors = []

    def handler():
        try:
            while time.monotonic() < stop:
                dev.handle(Frame(Address.long(0xFA01, 1), 48))
                dev.handle(Frame(Address.long(0xFA01, 1), 2))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def injector():
        try:
            while time.monotonic() < stop:
                dev.faults.inject("cold_start")
                dev.faults.inject("drift", per_second=0.1)
                dev.faults.inject("more_status", bytes=[1, 2, 3, 4, 5, 6])
                dev.faults.clear("drift")
                dev.faults.clear()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=handler, daemon=True),
               threading.Thread(target=injector, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not any(t.is_alive() for t in threads) and not errors
