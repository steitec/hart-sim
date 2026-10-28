"""Process variables (signal sources, damping, drift) and 4-20 mA loop current."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Callable

from .profile import VariableSpec

I_MIN = 3.8
I_MAX = 20.5
I_ALARM_LOW = 3.6
I_ALARM_HIGH = 21.0
I_MULTIDROP = 4.0

_MAX_SUBSTEPS = 100


class Variable:
    """A process variable: sum of sources, plus drift, first-order damped on read."""

    def __init__(self, spec: VariableSpec, damping: float,
                 clock: Callable[[], float], seed: int | None = None):
        self.spec = spec
        self.damping = damping
        self._clock = clock
        self._t0 = clock()
        self._rng = random.Random(seed)
        self._override: float | None = None
        self._drift_rate = 0.0
        self._drift_offset = 0.0
        self._drift_t = self._t0
        self._y = self._raw_at(self._t0)
        self._y_t = self._t0

    def _source_value(self, src, t: float) -> float:
        p = src.params
        el = t - self._t0
        if src.type == "constant":
            return p["value"]
        if src.type == "ramp":
            frac = (el % p["period"]) / p["period"]
            return p["start"] + (p["stop"] - p["start"]) * frac
        if src.type == "sine":
            return p["offset"] + p["amplitude"] * math.sin(2 * math.pi * el / p["period"])
        if src.type == "noise":
            return self._rng.gauss(0.0, p["sigma"])
        raise ValueError(f"unknown source type {src.type!r}")

    def _drift_at(self, t: float) -> float:
        return self._drift_offset + self._drift_rate * (t - self._drift_t)

    def _raw_at(self, t: float) -> float:
        if self._override is not None:
            return self._override
        return sum(self._source_value(s, t) for s in self.spec.sources) + self._drift_at(t)

    def _advance(self, now: float) -> None:
        """Bring the damped state up to `now` using the current inputs."""
        dt = now - self._y_t
        tau = self.damping
        if tau <= 0:
            self._y = self._raw_at(now)
        elif dt <= 0:
            return
        else:
            n = min(max(1, math.ceil(dt / (tau / 5))), _MAX_SUBSTEPS)
            step = dt / n
            for i in range(1, n + 1):
                t = self._y_t + step * i
                self._y += (1.0 - math.exp(-step / tau)) * (self._raw_at(t) - self._y)
        self._y_t = now

    def raw(self) -> float:
        return self._raw_at(self._clock())

    def value(self) -> float:
        now = self._clock()
        self._advance(now)
        return self._y

    def override(self, value: float | None) -> None:
        """Force the raw value (control channel); None returns to the sources."""
        self._advance(self._clock())
        self._override = value

    def add_drift(self, per_second: float) -> None:
        """Add a drift of `per_second` units/s, starting now."""
        now = self._clock()
        self._advance(now)
        self._drift_offset = self._drift_at(now)
        self._drift_t = now
        self._drift_rate += per_second


@dataclass(frozen=True)
class LoopState:
    current_ma: float
    percent: float
    saturated: bool
    fixed: bool
    alarm: bool


def loop_current(pv: float, lrv: float, urv: float, *, alarm: str, in_alarm: bool,
                 fixed_ma: float | None, multidrop: bool,
                 trim_zero: float = 0.0, trim_gain: float = 1.0) -> LoopState:
    span = urv - lrv
    percent = (pv - lrv) / span * 100.0 if span != 0 else 0.0
    if multidrop:
        return LoopState(I_MULTIDROP, percent, False, True, False)
    if fixed_ma is not None:
        return LoopState(fixed_ma, percent, False, True, False)
    if in_alarm:
        return LoopState(I_ALARM_LOW if alarm == "low" else I_ALARM_HIGH,
                         percent, False, False, True)
    current = 4.0 + 16.0 * percent / 100.0
    saturated = not I_MIN <= current <= I_MAX
    current = min(max(current, I_MIN), I_MAX)
    return LoopState(current * trim_gain + trim_zero, percent, saturated, False, False)
