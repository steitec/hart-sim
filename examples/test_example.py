"""Example: testing HART client code against simulated devices with the hart-sim pytest plugin.

Run with: pytest -v examples/test_example.py
"""
import pytest

from hart_sim.client import HartError


def test_default_bench_reads_the_pressure_transmitter(hart_bench):
    with hart_bench.client() as client:
        variables = client.handle(0).read_variables()
    assert variables.pv_unit == 12            # kPa
    assert 3.8 <= variables.current_ma <= 20.5


def test_range_can_be_changed_with_command_35(hart_bench):
    with hart_bench.client() as client:
        device = client.handle(0)
        assert device.write_range(0.0, 600.0) == 0
        assert device.read_range() == (0.0, 600.0, 12)


def test_sensor_failure_sets_status_and_alarm_current(hart_bench):
    hart_bench.devices["PT-101"].faults.inject("sensor_failure")
    with hart_bench.client() as client:
        variables = client.handle(0).read_variables()
    assert variables.current_ma == pytest.approx(21.0)   # alarm high
    assert variables.status & 0x80                       # device malfunction


def test_no_response_fault_times_out(hart_bench):
    with hart_bench.client(timeout=0.3) as client:
        device = client.handle(0)
        hart_bench.devices["PT-101"].faults.inject("no_response", count=1)
        with pytest.raises(HartError):
            device.read_variables()
        assert device.read_variables().current_ma > 0     # the next request is answered


def test_hart_bench_factory_for_a_second_device(hart_bench_factory):
    bench = hart_bench_factory("temperature")
    with bench.client() as client:
        assert client.handle(0).read_variables().pv_unit == 32   # degC
