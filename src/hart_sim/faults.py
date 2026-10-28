"""Faults that can be injected into a running simulated device.

==============  ===========================================  ================================================
kind            parameters                                   effect
==============  ===========================================  ================================================
no_response     count=1                                      the next ``count`` requests get no answer
                                                             (applied by the server, not by Device)
delay           seconds (> 0), count=None (forever)          each answer is sent ``seconds`` late
                                                             (applied by the server, not by Device)
busy            count=1                                      the next ``count`` requests get code 32,
                                                             no state change
comm_error      flags=0x08 (bit 7 clear), count=1            the next ``count`` answers carry a
                                                             communication error byte ``0x80 | flags``
sensor_failure  -                                            device malfunction (0x80), PV out of limits
                                                             (0x01), alarm current, PV status bad in cmd 9,
                                                             extended status 0x02, cmd 48 byte 0 bit 0,
                                                             "more status available" until cmd 48 is read
saturate        direction="high" | "low"                     loop current pinned to 20.5 / 3.8 mA (0x04)
cold_start      -                                            re-arms cold start for both masters; one-shot
more_status     bytes (list of 6 ints, 0-255)                "more status available" (0x10) until cmd 48
                                                             reads it; the bytes appear in its device
                                                             specific status
drift           per_second (finite)                          the PV drifts by ``per_second`` units/s;
                                                             ``clear`` removes it
write_protect   on (bool)                                    sets ``Device.write_protect``; one-shot
==============  ===========================================  ================================================
"""
from __future__ import annotations

import math
import contextlib
import threading
from typing import Any, Callable

_REQUIRED = object()


class FaultError(ValueError):
    """Invalid fault kind or parameter."""


def _finite(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("must be a finite number")
    return number


def _whole(value: Any) -> int:
    """An integer given as int, integral float, or a string such as "16" or "0x10"."""
    if isinstance(value, bool):
        raise ValueError("must be a whole number")
    if isinstance(value, str):
        try:
            return int(value.strip(), 0)
        except ValueError:
            raise ValueError("must be a whole number (decimal or 0x.. hex)") from None
    number = _finite(value)
    if number != int(number):
        raise ValueError("must be a whole number")
    return int(number)


def _count(value: Any) -> int | None:
    if value is None:
        return None
    number = _whole(value)
    if number < 1:
        raise ValueError("must be a whole number >= 1")
    return number


def _positive(value: Any) -> float:
    if _finite(value) <= 0:
        raise ValueError("must be > 0")
    return float(value)


def _number(value: Any) -> float:
    return _finite(value)


def _flags(value: Any) -> int:
    number = _whole(value)
    if not 1 <= number <= 0x7F:
        raise ValueError("must be a whole number 1-127 (bit 7 is set by the device)")
    return number


def _direction(value: Any) -> str:
    if value not in ("high", "low"):
        raise ValueError("must be 'high' or 'low'")
    return value


def _six_bytes(value: Any) -> list[int]:
    """Six integers 0-255, as a list or a comma-separated string such as "1,2,3,4,5,6"."""
    if isinstance(value, str):
        items: list = value.split(",")
    elif isinstance(value, (bytes, bytearray)) or not hasattr(value, "__iter__"):
        raise ValueError("must be a list of 6 integers (0-255)")
    else:
        items = list(value)
    if len(items) != 6:
        raise ValueError("must be a list of exactly 6 integers (0-255)")
    out = []
    for item in items:
        try:
            number = _whole(item)
        except ValueError:
            raise ValueError("must be a list of 6 integers (0-255)") from None
        if not 0 <= number <= 255:
            raise ValueError("must be a list of 6 integers (0-255)")
        out.append(number)
    return out


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "on", "1"):
            return True
        if text in ("false", "off", "0"):
            return False
    raise ValueError("must be true/false, on/off or 1/0")


FAULTS: dict[str, dict[str, tuple[Any, Callable[[Any], Any]]]] = {
    "no_response": {"count": (1, _count)},
    "delay": {"seconds": (_REQUIRED, _positive), "count": (None, _count)},
    "busy": {"count": (1, _count)},
    "comm_error": {"flags": (0x08, _flags), "count": (1, _count)},
    "sensor_failure": {},
    "saturate": {"direction": ("high", _direction)},
    "cold_start": {},
    "more_status": {"bytes": (_REQUIRED, _six_bytes)},
    "drift": {"per_second": (_REQUIRED, _number)},
    "write_protect": {"on": (_REQUIRED, _bool)},
}
FAULT_KINDS = tuple(FAULTS)
# Kinds that act once on the device when injected and are not stored.
_ONE_SHOT = ("cold_start", "write_protect")


def validate(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """Return the complete, converted parameters, or raise FaultError."""
    spec = FAULTS.get(kind)
    if spec is None:
        raise FaultError(f"unknown fault {kind!r} (allowed: {', '.join(FAULT_KINDS)})")
    allowed = ", ".join(spec) or "none"
    unknown = set(params) - set(spec)
    if unknown:
        raise FaultError(
            f"fault {kind!r}: unknown parameter {sorted(unknown)[0]!r} (parameters: {allowed})")
    result = {}
    for name, (default, convert) in spec.items():
        if name not in params:
            if default is _REQUIRED:
                raise FaultError(
                    f"fault {kind!r}: missing parameter {name!r} (parameters: {allowed})")
            result[name] = default
            continue
        try:
            result[name] = convert(params[name])
        except (TypeError, ValueError, OverflowError) as error:
            raise FaultError(f"fault {kind!r}: {name} {error}") from None
    return result


class FaultSet:
    """Active faults. Thread-safe; `device` receives the one-shot and drift effects.

    Lock order everywhere: Device.lock first, then FaultSet._lock. take/active/snapshot
    only need the fault lock (Device.handle calls them while holding its own lock);
    inject/clear take the device lock first because they change device state.
    """

    def __init__(self, device=None):
        self._device = device
        self._lock = threading.Lock()
        self._drift_raw = 0.0  # raw-unit drift rate actually added to the PV
        self._active: dict[str, dict[str, Any]] = {}

    def inject(self, kind: str, **params: Any) -> None:
        values = validate(kind, params)
        with self._device_lock(), self._lock:
            if kind == "drift":
                self._remove_drift()
            if kind not in _ONE_SHOT:
                self._active[kind] = values
            if self._device is not None:
                self._apply(kind, values)

    def _device_lock(self):
        return self._device.lock if self._device is not None else contextlib.nullcontext()

    def clear(self, kind: str | None = None) -> None:
        with self._device_lock(), self._lock:
            if kind is None:
                kinds = list(self._active)
            elif kind in FAULTS:
                kinds = [kind]
            else:
                raise FaultError(f"unknown fault {kind!r} (allowed: {', '.join(FAULT_KINDS)})")
            for k in kinds:
                if k == "drift":
                    self._remove_drift()
                self._active.pop(k, None)
            if self._device is not None and not (
                    "sensor_failure" in self._active or "more_status" in self._active):
                self._device.more_status_pending = False

    def active(self, kind: str) -> dict[str, Any] | None:
        with self._lock:
            params = self._active.get(kind)
            return dict(params) if params is not None else None

    def take(self, kind: str) -> dict[str, Any] | None:
        """Return the fault's parameters; a counted fault loses one count (gone at 0).

        A fault whose count is None (or that has no count) is returned unconsumed.
        """
        with self._lock:
            params = self._active.get(kind)
            if params is None:
                return None
            taken = dict(params)
            count = params.get("count")
            if count is not None:
                if count <= 1:
                    del self._active[kind]
                else:
                    params["count"] = count - 1
            return taken

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._active.items()}

    # --- device effects (lock held) ---

    def _remove_drift(self) -> None:
        # Subtract exactly what was added, whatever unit the PV is in now.
        if self._drift_raw and self._device is not None:
            self._device.pv.add_raw_drift(-self._drift_raw)
        self._drift_raw = 0.0

    def _apply(self, kind: str, values: dict[str, Any]) -> None:
        dev = self._device
        if kind == "drift":
            self._drift_raw = dev.pv.add_drift(values["per_second"])
        elif kind == "cold_start":
            dev.rearm_cold_start()
        elif kind == "write_protect":
            dev.write_protect = values["on"]
        elif kind in ("sensor_failure", "more_status"):
            dev.more_status_pending = True
