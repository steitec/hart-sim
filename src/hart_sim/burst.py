"""Burst-mode state: up to three burst messages per device.

Layouts and limits follow the PR electronics 5437/6437 field device specification
(three burst messages, burstable commands 1, 2, 3, 9, 33, 48) and the HART 7 universal
command table as quoted in public device manuals; see ``commands/burst.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MESSAGES = 3
MIN_PERIOD = 0.1  # seconds; real devices are usually at 0.5 s, 0.1 s keeps CI tests fast
BURSTABLE = (1, 2, 3, 9, 33, 48)
UNUSED_SLOT = 250
SLOTS = 8
# Slot code -> device variable (0 = PV, 1 = SV); 246/247 are the PV/SV codes of command 9/33.
SLOT_OF_CODE = {0: 0, 246: 0, 1: 1, 247: 1}

CONTINUOUS, WINDOW, RISING, FALLING, ON_CHANGE = range(5)
TRIGGER_NAMES = ("continuous", "window", "rising", "falling", "on-change")

_TIME_UNIT = 32000  # HART "Time": unsigned 32 bit, 1/32 ms


def to_seconds(time_u32: int) -> float:
    return time_u32 / _TIME_UNIT


def to_time(seconds: float) -> int:
    return min(round(seconds * _TIME_UNIT), 0xFFFFFFFF)


def default_slots(has_sv: bool) -> list[int]:
    return [0, 1 if has_sv else UNUSED_SLOT] + [UNUSED_SLOT] * (SLOTS - 2)


def should_send(msg: "BurstMessage", value: float | None, now: float) -> bool:
    """Whether a due message sends a frame now (pure; ``value`` is slot 0's variable)."""
    if msg.trigger_mode == CONTINUOUS:
        return True
    if msg.last_sent is None or now - msg.last_sent >= msg.max_period:
        return True
    if value is None:
        return False
    if msg.trigger_mode == WINDOW:
        return msg.last_value is None or abs(value - msg.last_value) >= msg.trigger_level
    if msg.trigger_mode == RISING:
        return value > msg.trigger_level
    if msg.trigger_mode == FALLING:
        return value < msg.trigger_level
    return value != msg.last_value  # ON_CHANGE


@dataclass
class BurstMessage:
    control: int = 0  # 0 = off, 1..4 = on (command 109)
    command: int = 1
    slots: list[int] = field(default_factory=lambda: default_slots(True))
    period: float = 1.0  # seconds
    max_period: float = 60.0
    trigger_mode: int = CONTINUOUS
    trigger_class: int = 0
    trigger_unit: int = UNUSED_SLOT
    trigger_level: float = 0.0
    # Scheduling state, managed by Device.burst_due (None = restart from now).
    next_due: float | None = None
    last_sent: float | None = None
    last_value: float | None = None

    @property
    def on(self) -> bool:
        return self.control != 0
