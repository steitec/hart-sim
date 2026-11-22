"""Burst-mode commands 103-105 and 107-109.

Sources: PR electronics 5437/6437 field device specification V6R0 (response codes per
command, three burst messages) and the HART 7 universal command table as reproduced in
public device manuals. Request and response share one layout unless noted; the response
echoes the values as applied (and short requests are answered in the full form).
"""
from __future__ import annotations

import math

from ..burst import (BURSTABLE, MESSAGES, MIN_PERIOD, SLOT_OF_CODE, SLOTS, TRIGGER_NAMES,
                     UNUSED_SLOT, BurstMessage, default_slots, to_seconds, to_time)
from ..frame import pack_float_or_nan as pack_float, unpack_float
from . import need, register
from .universal import _classification, _slots


def _message(dev, index: int) -> BurstMessage | None:
    return dev.burst[index] if index < MESSAGES else None


def _changed(dev, msg: BurstMessage, **values) -> None:
    """Apply values; a real change counts as a configuration change and restarts the schedule."""
    if any(getattr(msg, k) != v for k, v in values.items()):
        for k, v in values.items():
            setattr(msg, k, v)
        msg.next_due = None
        dev.mark_config_changed()


def initial_messages(dev) -> list[BurstMessage]:
    """The device's burst messages: defaults, overlaid with the profile's ``burst`` block."""
    has_sv = dev.profile.sv is not None
    messages = [BurstMessage(slots=default_slots(has_sv)) for _ in range(MESSAGES)]
    for msg, spec in zip(messages, dev.profile.burst):
        msg.command = spec.get("command", msg.command)
        msg.period = spec.get("period", msg.period)
        msg.max_period = spec.get("max_period", max(msg.max_period, msg.period))
        if "variables" in spec:
            msg.slots = (list(spec["variables"]) + [UNUSED_SLOT] * SLOTS)[:SLOTS]
        msg.trigger_mode = TRIGGER_NAMES.index(spec.get("trigger", "continuous"))
        msg.trigger_level = spec.get("level", 0.0)
        if msg.trigger_mode:
            slot = _slots(dev).get(SLOT_OF_CODE.get(msg.slots[0], -1))
            if slot is not None:
                msg.trigger_unit = slot[0]
                msg.trigger_class = _classification(slot[0])
        msg.control = 1 if spec.get("enabled") else 0
    return messages


# --- 103: write burst period ----------------------------------------------------------

@register(103, write=True)
def cmd103(dev, data, primary):
    need(data, 1)
    msg = _message(dev, data[0])
    if msg is None:
        return 9, b""
    need(data, 9)
    period = to_seconds(int.from_bytes(data[1:5], "big"))
    max_period = to_seconds(int.from_bytes(data[5:9], "big"))
    code = 0
    if period < MIN_PERIOD:
        period, code = MIN_PERIOD, 8
    if max_period < period:
        max_period, code = period, 8
    _changed(dev, msg, period=period, max_period=max_period)
    return code, bytes([data[0]]) + to_time(period).to_bytes(4, "big") \
        + to_time(max_period).to_bytes(4, "big")


# --- 104: write burst trigger ---------------------------------------------------------

@register(104, write=True)
def cmd104(dev, data, primary):
    need(data, 1)
    msg = _message(dev, data[0])
    if msg is None:
        return 9, b""
    need(data, 8)
    mode, klass, unit = data[1], data[2], data[3]
    level = unpack_float(bytes(data[4:8]))
    if mode >= len(TRIGGER_NAMES):
        return 13, b""
    if math.isnan(level):
        return 2, b""
    if mode != 0:
        # The classification and unit must be those of the message's first variable.
        slot = _slots(dev).get(SLOT_OF_CODE.get(msg.slots[0], -1))
        if slot is None or klass != _classification(slot[0]):
            return 11, b""
        if unit != slot[0]:
            return 12, b""
    _changed(dev, msg, trigger_mode=mode, trigger_class=klass, trigger_unit=unit,
             trigger_level=level)
    return 0, bytes(data[:4]) + pack_float(level)


# --- 105: read burst mode configuration -----------------------------------------------

@register(105)
def cmd105(dev, data, primary):
    index = data[0] if data else 0  # no data: message 0 (HART 5 behaviour)
    msg = _message(dev, index)
    if msg is None:
        return 9, b""
    out = bytearray([msg.control, 31])  # not verified against the HART specification
    out += bytes(msg.slots)
    out += bytes([index, MESSAGES]) + msg.command.to_bytes(2, "big")
    out += to_time(msg.period).to_bytes(4, "big") + to_time(msg.max_period).to_bytes(4, "big")
    out += bytes([msg.trigger_mode, msg.trigger_class, msg.trigger_unit])
    out += pack_float(msg.trigger_level)
    return 0, bytes(out)


# --- 107: write burst device variables ------------------------------------------------

@register(107, write=True)
def cmd107(dev, data, primary):
    need(data, 1)
    index = data[8] if len(data) > 8 else 0
    msg = _message(dev, index)
    if msg is None:
        return 9, b""
    slots = (list(data[:SLOTS]) + [UNUSED_SLOT] * SLOTS)[:SLOTS]
    available = _slots(dev)
    for code in slots:
        if code != UNUSED_SLOT and available.get(SLOT_OF_CODE.get(code, -1)) is None:
            return 2, b""
    _changed(dev, msg, slots=slots)
    return 0, bytes(slots) + bytes([index])


# --- 108: write burst mode command number ---------------------------------------------

@register(108, write=True)
def cmd108(dev, data, primary):
    need(data, 1)
    if len(data) == 1:  # HART 5 form: one byte, message 0
        number, index = data[0], 0
    else:
        need(data, 3)
        number, index = int.from_bytes(data[:2], "big"), data[2]
    msg = _message(dev, index)
    if msg is None:
        return 9, b""
    if number not in BURSTABLE:
        return 2, b""
    _changed(dev, msg, command=number)
    return 0, number.to_bytes(2, "big") + bytes([index])


# --- 109: burst mode control ----------------------------------------------------------

@register(109, write=True)
def cmd109(dev, data, primary):
    need(data, 1)
    index = data[1] if len(data) > 1 else 0
    msg = _message(dev, index)
    if msg is None:
        return 9, b""
    # 0 off; 1, 3, 4 on; 2 is TDMA/wireless only; anything above 4 is invalid.
    # not verified against the HART specification
    if data[0] == 2 or data[0] > 4:
        return 2, b""
    _changed(dev, msg, control=data[0])
    return 0, bytes([data[0], index])
