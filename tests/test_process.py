import math

import pytest

from hart_sim.process import Variable, loop_current
from hart_sim.profile import SourceSpec, VariableSpec


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def spec(*sources):
    return VariableSpec("v", 32, tuple(sources))


def const(v):
    return SourceSpec("constant", {"value": v})


def lc(pv, **kw):
    args = dict(alarm="high", in_alarm=False, fixed_ma=None, multidrop=False)
    args.update(kw)
    return loop_current(pv, 0, 1000, **args)


def test_loop_current_midscale():
    s = loop_current(500, 0, 1000, alarm="high", in_alarm=False, fixed_ma=None, multidrop=False)
    assert s.current_ma == pytest.approx(12.0)
    assert s.percent == pytest.approx(50.0)
    assert not s.saturated and not s.fixed and not s.alarm


def test_saturation_high_and_low():
    hi = lc(1100)
    assert hi.current_ma == pytest.approx(20.5) and hi.saturated
    lo = lc(-100)
    assert lo.current_ma == pytest.approx(3.8) and lo.saturated


def test_alarm_currents():
    h = lc(500, in_alarm=True)
    assert h.current_ma == pytest.approx(21.0) and h.alarm
    low = lc(500, in_alarm=True, alarm="low")
    assert low.current_ma == pytest.approx(3.6) and low.alarm


def test_multidrop_beats_fixed_and_alarm():
    s = lc(500, multidrop=True, fixed_ma=7.0, in_alarm=True)
    assert s.current_ma == 4.0 and s.fixed


def test_fixed_current():
    s = lc(500, fixed_ma=7.0)
    assert s.current_ma == 7.0 and s.fixed


def test_fixed_beats_alarm():
    s = lc(500, fixed_ma=7.0, in_alarm=True)
    assert s.current_ma == 7.0 and s.fixed and not s.alarm


def test_trim():
    s = lc(500, trim_zero=0.1, trim_gain=1.01)
    assert s.current_ma == pytest.approx(12.0 * 1.01 + 0.1)


def test_zero_span_never_divides():
    s = loop_current(5, 10, 10, alarm="high", in_alarm=False, fixed_ma=None, multidrop=False)
    assert s.percent == 0
    assert s.current_ma == pytest.approx(4.0)


def test_damping_step_response():
    clk = Clock()
    v = Variable(spec(const(0)), 1.0, clk)
    assert v.value() == pytest.approx(0)
    v.override(100)
    assert v.raw() == 100
    clk.t = 1.0
    assert v.value() == pytest.approx(63.2, abs=0.5)
    clk.t = 20.0
    assert v.value() == pytest.approx(100, abs=0.01)


def test_damping_zero_is_raw_and_writable():
    clk = Clock()
    v = Variable(spec(const(3)), 0.0, clk)
    v.override(7)
    assert v.value() == 7
    v.damping = 2.0
    assert v.damping == 2.0


def test_sources_ramp_sine_constant_sum():
    clk = Clock()
    s = spec(
        const(10),
        SourceSpec("ramp", {"start": 0, "stop": 100, "period": 10}),
        SourceSpec("sine", {"offset": 0, "amplitude": 5, "period": 4}),
    )
    clk.t = 100.0  # time origin is construction time
    v = Variable(s, 0.0, clk)
    assert v.raw() == pytest.approx(10 + 0 + 0)
    clk.t = 102.5
    assert v.raw() == pytest.approx(10 + 25 + 5 * math.sin(2 * math.pi * 2.5 / 4))
    clk.t = 111.0  # ramp wraps: sawtooth
    assert v.raw() == pytest.approx(10 + 10 + 5 * math.sin(2 * math.pi * 11 / 4))


def test_noise_seeded_deterministic():
    clk = Clock()
    a = Variable(spec(SourceSpec("noise", {"sigma": 1.0})), 0.0, clk, seed=1)
    b = Variable(spec(SourceSpec("noise", {"sigma": 1.0})), 0.0, clk, seed=1)
    xs = [a.raw() for _ in range(5)]
    assert xs == [b.raw() for _ in range(5)]
    assert len(set(xs)) > 1


def test_override_and_drift():
    clk = Clock()
    v = Variable(spec(const(1)), 0.0, clk)
    v.override(42)
    assert v.value() == 42
    v.override(None)
    assert v.value() == 1
    v.add_drift(2.0)
    clk.t = 5.0
    assert v.value() == pytest.approx(11.0)
