"""Common practice commands 33-37, 40-42, 44-46, 59."""
from __future__ import annotations

import math

from .. import units
from ..frame import pack_float, unpack_float
from ..process import I_ALARM_HIGH, I_ALARM_LOW
from . import need, register

_EPS = 1e-5  # 3.6 and 21.0 are not exact in single precision
_TRIM_ZERO_MAX = 1.0  # mA
_TRIM_GAIN = (0.95, 1.05)
_SLOT_OF_CODE = {0: 0, 246: 0, 1: 1, 247: 1}


def _float(data: bytes, offset: int = 0) -> float:
    return unpack_float(bytes(data[offset:offset + 4]))


# --- 33: read device variables ------------------------------------------------------

@register(33)
def cmd33(dev, data, primary):
    need(data, 1)
    slots = {0: (dev.pv_unit, dev.pv),
             1: (dev.profile.sv.unit, dev.sv) if dev.sv is not None else None}
    out = bytearray()
    for code in data[:4]:
        slot = slots.get(_SLOT_OF_CODE.get(code, -1))
        if slot is None:
            return 2, b""
        unit, var = slot
        out += bytes([code, unit]) + pack_float(var.value())
    return 0, bytes(out)


# --- 34: damping ---------------------------------------------------------------------

@register(34, write=True)
def cmd34(dev, data, primary):
    need(data, 4)
    value = _float(data)
    if math.isnan(value):
        return 2, b""
    if value < 0:
        return 4, b""
    if value > 60:
        return 3, b""
    if value != dev.damping:
        dev.damping = value
        dev.mark_config_changed()
    return 0, pack_float(value)


# --- 35-37: range values ----------------------------------------------------------------

def _apply_range(dev, urv: float, lrv: float) -> int:
    """Validate and apply a range; returns the response code (14 = applied with warning)."""
    if not (math.isfinite(urv) and math.isfinite(lrv)):
        return 2
    lo, hi = dev.sensor_limits
    lrv_out = not lo <= lrv <= hi
    urv_out = not lo <= urv <= hi
    if lrv_out and urv_out:
        return 13
    if lrv < lo:
        return 10
    if lrv > hi:
        return 9
    if urv > hi:
        return 11
    if urv < lo or urv <= lrv:
        return 12
    if (urv, lrv) != (dev.urv, dev.lrv):
        dev.urv, dev.lrv = urv, lrv
        dev.mark_config_changed()
    return 14 if urv - lrv < dev.min_span else 0


@register(35, write=True)
def cmd35(dev, data, primary):
    need(data, 9)
    if data[0] != dev.pv_unit:
        return 2, b""
    urv, lrv = _float(data, 1), _float(data, 5)
    code = _apply_range(dev, urv, lrv)
    if code not in (0, 14):
        return code, b""
    return code, bytes([dev.pv_unit]) + pack_float(dev.urv) + pack_float(dev.lrv)


@register(36, write=True)
def cmd36(dev, data, primary):
    code = _apply_range(dev, dev.pv.value(), dev.lrv)
    return code, b""


@register(37, write=True)
def cmd37(dev, data, primary):
    code = _apply_range(dev, dev.urv, dev.pv.value())
    return code, b""


# --- 40-42: fixed current, self test, reset ---------------------------------------------------

@register(40, write=True)
def cmd40(dev, data, primary):
    need(data, 4)
    if not dev.loop_current_mode:
        return 11, b""  # multidrop: the current is fixed at 4 mA already
    value = _float(data)
    if math.isnan(value):
        return 2, b""
    if value == 0:
        dev.fixed_ma = None
    elif value < I_ALARM_LOW - _EPS:
        return 4, b""
    elif value > I_ALARM_HIGH + _EPS:
        return 3, b""
    else:
        dev.fixed_ma = value
    return 0, pack_float(dev.loop().current_ma)


@register(41)
def cmd41(dev, data, primary):
    return 0, b""  # the simulated device always passes; a failure shows in the status bytes


@register(42, write=True)
def cmd42(dev, data, primary):
    dev.fixed_ma = None
    dev.reset_pending = True  # cold start is re-armed after this reply; configuration is kept
    return 0, b""


# --- 44: PV unit ---------------------------------------------------------------------------------

@register(44, write=True)
def cmd44(dev, data, primary):
    need(data, 1)
    new = data[0]
    if not units.same_family(new, dev.profile.pv.unit):
        return 2, b""
    if new != dev.pv_unit:
        scale, shift = units.conversion(dev.pv_unit, new)
        dev.lrv = scale * dev.lrv + shift
        dev.urv = scale * dev.urv + shift
        lo, hi = dev.sensor_limits
        lo, hi = scale * lo + shift, scale * hi + shift
        dev.sensor_limits = (lo, hi)
        dev.min_span *= scale
        dev.pv.rescale(scale, shift)
        dev.pv_unit = new
        dev.mark_config_changed()
    return 0, bytes([dev.pv_unit])


# --- 45/46: DAC trim --------------------------------------------------------------------------------

def _trim(dev, data, zero: bool):
    """Trim the DAC (output = trim_gain * commanded + trim_zero).

    The master has fixed the loop current at I_cmd (command 40) and reports the value
    I_meas it measured. The simulator maps the commanded current to the reported one:
    zero trim (45, normally at 4 mA) changes only trim_zero, gain trim (46, normally
    at 20 mA) only trim_gain, so that output(I_cmd) == I_meas afterwards. Trim applies
    in every mode. Trim is limited to +-1.0 mA zero and 0.95-1.05 gain (codes 3/4).
    """
    need(data, 4)
    if dev.fixed_ma is None:
        return 9, b""
    measured = _float(data)
    if not math.isfinite(measured):
        return 2, b""
    cmd = dev.fixed_ma  # >= 3.6, so the division is safe
    if zero:
        new = measured - dev.trim_gain * cmd
        if abs(new) > _TRIM_ZERO_MAX + _EPS:
            return (3 if new > 0 else 4), b""
        dev.trim_zero = new
    else:
        new = (measured - dev.trim_zero) / cmd
        if not _TRIM_GAIN[0] - _EPS <= new <= _TRIM_GAIN[1] + _EPS:
            return (3 if new > 1 else 4), b""
        dev.trim_gain = new
    dev.mark_config_changed()
    return 0, pack_float(measured)


@register(45, write=True)
def cmd45(dev, data, primary):
    return _trim(dev, data, zero=True)


@register(46, write=True)
def cmd46(dev, data, primary):
    return _trim(dev, data, zero=False)


# --- 59: response preambles -----------------------------------------------------------------------

@register(59, write=True)
def cmd59(dev, data, primary):
    need(data, 1)
    n = data[0]
    if n < 5:
        return 4, b""
    if n > 20:
        return 3, b""
    if n != dev.response_preambles:
        dev.response_preambles = n
        dev.mark_config_changed()
    return 0, bytes([n])
