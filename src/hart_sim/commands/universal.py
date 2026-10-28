"""Universal commands 0-3, 6-9, 11-22, 38, 48."""
from __future__ import annotations

import math

from ..frame import pack_ascii, pack_float, unpack_ascii
from . import NoResponse, need, register


def _variable_alert(dev) -> bool:
    if dev.faults.active("sensor_failure") is not None:
        return True
    return any(not math.isfinite(v.value()) for v in (dev.pv, dev.sv) if v is not None)


@register(0)
def cmd0(dev, data, primary):
    mfr = dev.profile.manufacturer_id
    bad = _variable_alert(dev)
    out = bytearray([254])
    out += dev.profile.device_type.to_bytes(2, "big")
    out += bytes([5, 7, dev.profile.device_revision, dev.profile.software_revision,
                  dev.profile.hardware_revision << 3, 0])
    out += dev.device_id.to_bytes(3, "big")
    out += bytes([dev.response_preambles, 2 if dev.sv is not None else 1])
    out += dev.config_counter.to_bytes(2, "big")
    out += bytes([0x02 if bad else 0])
    out += mfr.to_bytes(2, "big") + mfr.to_bytes(2, "big")
    out += bytes([1])
    return 0, bytes(out)


@register(1)
def cmd1(dev, data, primary):
    return 0, bytes([dev.pv_unit]) + pack_float(dev.pv.value())


@register(2)
def cmd2(dev, data, primary):
    state = dev.loop()
    return 0, pack_float(state.current_ma) + pack_float(state.percent)


@register(3)
def cmd3(dev, data, primary):
    state = dev.loop()
    out = pack_float(state.current_ma) + bytes([dev.pv_unit]) + pack_float(dev.pv.value())
    if dev.sv is not None:
        out += bytes([dev.profile.sv.unit]) + pack_float(dev.sv.value())
    return 0, out


# --- commands 6-9: polling address, loop configuration, variables ---------------

def _loop_config(dev) -> bytes:
    return bytes([dev.polling_address, 1 if dev.loop_current_mode else 0])


@register(6, write=True)
def cmd6(dev, data, primary):
    need(data, 1)
    addr = data[0]
    if addr > 63:
        return 2, b""
    if len(data) >= 2:
        if data[1] > 1:
            return 2, b""
        enabled = bool(data[1])
    else:
        enabled = addr == 0  # HART 5 style: multidrop iff address != 0
    if addr != dev.polling_address or enabled != dev.loop_current_mode:
        dev.polling_address = addr
        dev.loop_current_mode = enabled
        dev.mark_config_changed()
    return 0, _loop_config(dev)


@register(7)
def cmd7(dev, data, primary):
    return 0, _loop_config(dev)


# Variable classification codes: 64 = temperature, 65 = pressure, 0 = not classified.
# Confirmed by public device specifications (Endress+Hauser, Knick: "Temperature 64,
# Pressure 65"); the table number (Common Table 21) itself was not confirmed.
_PRESSURE_UNITS = {1, 6, 7, 8, 12}
_TEMPERATURE_UNITS = {32, 33, 35}


def _classification(unit: int) -> int:
    if unit in _PRESSURE_UNITS:
        return 65
    if unit in _TEMPERATURE_UNITS:
        return 64
    return 0


def _slots(dev):
    """Slot number -> (unit, Variable) for PV and SV; the SV slot may be missing."""
    return {0: (dev.pv_unit, dev.pv),
            1: (dev.profile.sv.unit, dev.sv) if dev.sv is not None else None}


@register(8)
def cmd8(dev, data, primary):
    slots = _slots(dev)
    sv = slots[1]
    return 0, bytes([_classification(slots[0][0]), _classification(sv[0]) if sv else 0, 0, 0])


@register(9)
def cmd9(dev, data, primary):
    need(data, 1)
    slots = _slots(dev)
    out = bytearray([0x02 if _variable_alert(dev) else 0])
    for code in data[:8]:
        slot = slots.get({0: 0, 246: 0, 1: 1, 247: 1}.get(code, -1))
        if slot is None:
            return 2, b""
        unit, var = slot
        value = var.value()
        out += bytes([code, _classification(unit), unit]) + pack_float(value)
        bad = var is dev.pv and dev.faults.active("sensor_failure") is not None
        out += bytes([0xC0 if math.isfinite(value) and not bad else 0x00])
    # Timestamp: 1/32 ms since "midnight" of the (monotonic) device clock.
    out += (int((dev.clock() % 86400) * 32000) & 0xFFFFFFFF).to_bytes(4, "big")
    return 0, bytes(out)


# --- tags, descriptor, date, message ---------------------------------------------

def _long_tag_bytes(dev) -> bytes:
    return dev.long_tag.encode("latin-1", errors="replace")[:32].ljust(32, b"\0")


@register(11)
def cmd11(dev, data, primary):
    need(data, 6)
    if data[:6] != pack_ascii(dev.tag, 8):
        raise NoResponse
    return cmd0(dev, b"", primary)


@register(21)
def cmd21(dev, data, primary):
    need(data, 32)
    if data[:32].rstrip(b" \0") != _long_tag_bytes(dev).rstrip(b" \0"):
        raise NoResponse
    return cmd0(dev, b"", primary)


@register(12)
def cmd12(dev, data, primary):
    return 0, pack_ascii(dev.message, 32)


@register(17, write=True)
def cmd17(dev, data, primary):
    need(data, 24)
    new = unpack_ascii(data[:24])  # every 6-bit value maps to a valid character
    if new != dev.message:
        dev.message = new
        dev.mark_config_changed()
    return 0, bytes(data[:24])


@register(13)
def cmd13(dev, data, primary):
    day, month, year = dev.date
    return 0, (pack_ascii(dev.tag, 8) + pack_ascii(dev.descriptor, 16)
               + bytes([day, month, year - 1900]))


@register(18, write=True)
def cmd18(dev, data, primary):
    need(data, 21)
    day, month, year = data[18], data[19], data[20] + 1900
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return 2, b""
    tag, desc = unpack_ascii(data[:6]), unpack_ascii(data[6:18])
    if (tag, desc, (day, month, year)) != (dev.tag, dev.descriptor, tuple(dev.date)):
        dev.tag, dev.descriptor, dev.date = tag, desc, (day, month, year)
        dev.mark_config_changed()
    return 0, bytes(data[:21])


# --- sensor, output and assembly information ----------------------------------------

@register(14)
def cmd14(dev, data, primary):
    lo, hi = dev.sensor_limits
    return 0, (dev.device_id.to_bytes(3, "big") + bytes([dev.pv_unit]) + pack_float(hi)
               + pack_float(lo) + pack_float(dev.min_span))


@register(15)
def cmd15(dev, data, primary):
    return 0, (bytes([0 if dev.profile.alarm == "high" else 1, 0, dev.pv_unit])
               + pack_float(dev.urv) + pack_float(dev.lrv) + pack_float(dev.damping)
               + bytes([1 if dev.write_protect else 0, 250, 0]))


@register(16)
def cmd16(dev, data, primary):
    return 0, dev.final_assembly.to_bytes(3, "big")


@register(19, write=True)
def cmd19(dev, data, primary):
    need(data, 3)
    value = int.from_bytes(data[:3], "big")
    if value != dev.final_assembly:
        dev.final_assembly = value
        dev.mark_config_changed()
    return 0, bytes(data[:3])


@register(20)
def cmd20(dev, data, primary):
    return 0, _long_tag_bytes(dev)


@register(22, write=True)
def cmd22(dev, data, primary):
    need(data, 32)
    new = bytes(data[:32]).rstrip(b"\0").decode("latin-1")
    if new != dev.long_tag:
        dev.long_tag = new
        dev.mark_config_changed()
    return 0, bytes(data[:32])


# --- configuration-changed flag, device status ----------------------------------------

@register(38)
def cmd38(dev, data, primary):
    if data:  # an empty request (older masters) clears unconditionally
        need(data, 2)
        if int.from_bytes(data[:2], "big") != dev.config_counter:
            return 9, b""  # configuration change counter mismatch
    dev.clear_config_changed(primary)
    return 0, dev.config_counter.to_bytes(2, "big")


@register(48)
def cmd48(dev, data, primary):
    state = dev.loop()
    more = dev.faults.active("more_status")
    specific = bytearray(more["bytes"] if more else bytes(6))
    if dev.faults.active("sensor_failure") is not None:
        specific[0] |= 0x01
    # Reading the additional status acknowledges "more status available".
    dev.faults.clear("more_status")
    dev.more_status_pending = False
    ext = 0x02 if _variable_alert(dev) else 0
    return 0, bytes(specific) + bytes([ext, 0, 0, 0, 1 if state.saturated else 0, 0, 0,
                                1 if state.fixed else 0])
