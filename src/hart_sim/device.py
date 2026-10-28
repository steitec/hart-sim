"""Simulated HART field device: state, addressing, status bytes, command dispatch."""
from __future__ import annotations

import math
import threading
import time

from . import commands
from .commands import ShortData
from .frame import Address, Frame, pack_ascii
from .process import LoopState, Variable, loop_current
from .profile import Profile

_PRIMARY, _SECONDARY = True, False


class FaultSet:
    """Placeholder until Task 6."""

    def active(self, kind: str):
        return None


class Device:
    def __init__(self, profile: Profile, polling_address: int = 0, device_id: int = 1,
                 clock=time.monotonic):
        self.profile = profile
        self.device_id = device_id
        self.polling_address = polling_address
        self.tag = profile.tag
        self.long_tag = profile.long_tag
        self.descriptor = profile.descriptor
        self.message = profile.message
        self.date: tuple[int, int, int] = (1, 1, 2026)
        self.final_assembly = 0
        self.loop_current_mode = True  # True = enabled (not multidrop)
        self.lrv, self.urv = profile.range
        self.pv_unit = profile.pv.unit
        self.write_protect = False
        self.config_counter = 0
        self.fixed_ma: float | None = None
        self.trim_zero = 0.0
        self.trim_gain = 1.0
        self.response_preambles = 5
        self.faults = FaultSet()
        self.pv = Variable(profile.pv, profile.damping, clock)
        self.sv = Variable(profile.sv, profile.damping, clock) if profile.sv else None
        self._cold_start = {_PRIMARY: True, _SECONDARY: True}
        self._config_changed = {_PRIMARY: False, _SECONDARY: False}
        # Reentrant: handle() holds it while handlers call loop()/status_byte().
        self.lock = threading.RLock()

    @property
    def damping(self) -> float:
        return self.pv.damping

    @damping.setter
    def damping(self, value: float) -> None:
        self.pv.damping = value

    @property
    def unique_id(self) -> tuple[int, int]:
        return self.profile.device_type, self.device_id

    def mark_config_changed(self) -> None:
        with self.lock:
            self.config_counter = (self.config_counter + 1) & 0xFFFF
            self._config_changed = {_PRIMARY: True, _SECONDARY: True}

    def clear_config_changed(self, primary: bool) -> None:
        """Clear the flag for one master only (used by command 38)."""
        with self.lock:
            self._config_changed[primary] = False

    def loop(self) -> LoopState:
        with self.lock:
            return loop_current(
                self.pv.value(), self.lrv, self.urv, alarm=self.profile.alarm,
                in_alarm=False, fixed_ma=self.fixed_ma,
                multidrop=not self.loop_current_mode,
                trim_zero=self.trim_zero, trim_gain=self.trim_gain)

    def status_byte(self, primary: bool) -> int:
        with self.lock:
            st = 0
            if self._config_changed[primary]:
                st |= 0x40
            if self._cold_start[primary]:
                st |= 0x20
            state = self.loop()
            if state.fixed:
                st |= 0x08
            if state.saturated:
                st |= 0x04
            lo, hi = self.profile.sensor_limits
            pv = self.pv.value()
            if not lo <= pv <= hi:  # NaN also lands here
                st |= 0x01
            return st

    def matches(self, address: Address, command: int, data: bytes) -> bool:
        if not address.is_long:
            return address.polling == self.polling_address and command == 0
        if address.is_broadcast:
            if command == 11:
                return len(data) >= 6 and data[:6] == pack_ascii(self.tag, 8)
            if command == 21:
                if len(data) < 32:
                    return False
                want = self.long_tag.encode("latin-1")
                return data[:32].rstrip(b" \x00") == want.rstrip(b" \x00")
            return False
        dev_type, dev_id = address.unique_id
        return (dev_type & 0x3FFF, dev_id) == (self.profile.device_type & 0x3FFF, self.device_id)

    def handle(self, request: Frame) -> Frame | None:
        if request.is_response:
            return None
        with self.lock:
            if not self.matches(request.address, request.command, request.data):
                return None
            primary = request.address.primary
            entry = commands.REGISTRY.get(request.command)
            if entry is None:
                code, data = 64, b""
            else:
                fn, write = entry
                if write and self.write_protect:
                    code, data = 7, b""
                else:
                    try:
                        code, data = fn(self, request.data, primary)
                    except ShortData:
                        code, data = 5, b""
            status = self.status_byte(primary)
            self._cold_start[primary] = False
            if request.address.is_broadcast:
                addr = Address.long(*self.unique_id, primary)
            else:
                addr = Address(request.address.polling, request.address.unique_id, primary)
            return Frame(addr, request.command, data, True, code, status)
