"""Device profiles: YAML loading and validation."""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import units
from .frame import pack_ascii

_PROFILE_DIR = Path(__file__).parent / "profiles"

_SOURCE_PARAMS = {
    "constant": ("value",),
    "ramp": ("start", "stop", "period"),
    "sine": ("offset", "amplitude", "period"),
    "noise": ("sigma",),
}


class ProfileError(ValueError):
    """Invalid profile; the message names the offending key."""


@dataclass(frozen=True)
class SourceSpec:
    type: str
    params: dict[str, float]


@dataclass(frozen=True)
class VariableSpec:
    name: str
    unit: int
    sources: tuple[SourceSpec, ...]


@dataclass(frozen=True)
class Profile:
    kind: str
    manufacturer_id: int
    device_type: int
    device_revision: int
    tag: str
    long_tag: str
    descriptor: str
    pv: VariableSpec
    sensor_limits: tuple[float, float]
    range: tuple[float, float]
    min_span: float
    damping: float
    software_revision: int = 1
    hardware_revision: int = 1
    message: str = ""
    alarm: str = "high"
    sv: VariableSpec | None = None


_TOP_KEYS = {
    "kind", "manufacturer_id", "device_type", "device_revision", "software_revision",
    "hardware_revision", "tag", "long_tag", "descriptor", "message", "pv", "sv",
    "sensor_limits", "range", "min_span", "damping", "alarm",
}
_PV_KEYS = {"name", "unit", "source", "sensor_limits", "range", "damping"}
_SV_KEYS = {"name", "unit", "source"}
_PV_TO_TOP = ("sensor_limits", "range", "damping")


def bundled_profiles() -> list[str]:
    return sorted(p.stem for p in _PROFILE_DIR.glob("*.yaml"))


def _num(value: Any, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(f"{key}: expected a number, got {value!r}")
    if not math.isfinite(value):
        raise ProfileError(f"{key}: must be finite, got {value!r}")
    return float(value)


def _int(value: Any, key: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileError(f"{key}: expected an integer, got {value!r}")
    if not lo <= value <= hi:
        raise ProfileError(f"{key}: must be {lo}..{hi}, got {value}")
    return value


def _pair(value: Any, key: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ProfileError(f"{key}: expected a two-element list, got {value!r}")
    return _num(value[0], f"{key}[0]"), _num(value[1], f"{key}[1]")


def _text(value: Any, key: str, length: int) -> str:
    if not isinstance(value, str):
        raise ProfileError(f"{key}: expected a string, got {value!r}")
    try:
        pack_ascii(value, length)
    except ValueError as exc:
        raise ProfileError(f"{key}: {exc}") from None
    return value


def _source(raw: Any, key: str) -> SourceSpec:
    if not isinstance(raw, dict):
        raise ProfileError(f"{key}: expected a mapping, got {raw!r}")
    stype = raw.get("type")
    if stype not in _SOURCE_PARAMS:
        raise ProfileError(f"{key}.type: unknown source type {stype!r}")
    expected = _SOURCE_PARAMS[stype]
    for k in raw:
        if k != "type" and k not in expected:
            raise ProfileError(f"{key}.{k}: unknown parameter for {stype} source")
    params = {}
    for k in expected:
        if k not in raw:
            raise ProfileError(f"{key}.{k}: missing parameter for {stype} source")
        params[k] = _num(raw[k], f"{key}.{k}")
    if "period" in params and params["period"] <= 0:
        raise ProfileError(f"{key}.period: must be > 0")
    if "sigma" in params and params["sigma"] < 0:
        raise ProfileError(f"{key}.sigma: must be >= 0")
    return SourceSpec(stype, params)


def _variable(raw: Any, key: str, allowed: set[str], default_name: str) -> VariableSpec:
    if not isinstance(raw, dict):
        raise ProfileError(f"{key}: expected a mapping, got {raw!r}")
    for k in raw:
        if k not in allowed:
            raise ProfileError(f"{key}.{k}: unknown key")
    if "unit" not in raw:
        raise ProfileError(f"{key}.unit: missing")
    try:
        unit = units.code(raw["unit"])
    except ValueError as exc:
        raise ProfileError(f"{key}.unit: {exc}") from None
    name = raw.get("name", default_name)
    if not isinstance(name, str):
        raise ProfileError(f"{key}.name: expected a string")
    src = raw.get("source")
    if src is None:
        raise ProfileError(f"{key}.source: missing")
    items = src if isinstance(src, list) else [src]
    if not items:
        raise ProfileError(f"{key}.source: empty list")
    sources = tuple(_source(s, f"{key}.source") for s in items)
    return VariableSpec(name, unit, sources)


def _build(raw: dict[str, Any]) -> Profile:
    for k in raw:
        if k not in _TOP_KEYS:
            raise ProfileError(f"{k}: unknown key")
    raw = dict(raw)
    pv_raw = raw.get("pv")
    if not isinstance(pv_raw, dict):
        raise ProfileError("pv: missing or not a mapping")
    # Spec layout nests these under pv; Profile holds them at top level.
    for k in _PV_TO_TOP:
        if k in pv_raw:
            if k in raw:
                raise ProfileError(f"{k}: given both at top level and under pv")
            raw[k] = pv_raw[k]
    for k in ("kind", "manufacturer_id", "device_type", "device_revision", "tag",
              "long_tag", "descriptor", "min_span", "damping", "sensor_limits", "range"):
        if k not in raw:
            raise ProfileError(f"{k}: missing")

    if not isinstance(raw["kind"], str):
        raise ProfileError("kind: expected a string")
    tag = _text(raw["tag"], "tag", 8)
    descriptor = _text(raw["descriptor"], "descriptor", 16)
    message = _text(raw.get("message", ""), "message", 32)
    long_tag = raw["long_tag"]
    if not isinstance(long_tag, str) or len(long_tag) > 32:
        raise ProfileError("long_tag: expected a string of at most 32 characters")
    try:
        long_tag.encode("latin-1")
    except UnicodeEncodeError:
        raise ProfileError("long_tag: must be Latin-1 encodable") from None

    limits = _pair(raw["sensor_limits"], "sensor_limits")
    rng = _pair(raw["range"], "range")
    min_span = _num(raw["min_span"], "min_span")
    damping = _num(raw["damping"], "damping")
    if limits[0] >= limits[1]:
        raise ProfileError("sensor_limits: lower limit must be below upper limit")
    if rng[0] >= rng[1]:
        raise ProfileError("range: LRV must be below URV")
    if rng[0] < limits[0] or rng[1] > limits[1]:
        raise ProfileError("range: must lie within sensor_limits")
    if min_span <= 0:
        raise ProfileError("min_span: must be > 0")
    if rng[1] - rng[0] < min_span:
        raise ProfileError("range: span is smaller than min_span")
    if damping < 0:
        raise ProfileError("damping: must be >= 0")
    alarm = raw.get("alarm", "high")
    if alarm not in ("high", "low"):
        raise ProfileError(f"alarm: must be 'high' or 'low', got {alarm!r}")

    pv = _variable(pv_raw, "pv", _PV_KEYS, "primary variable")
    sv = None
    if raw.get("sv") is not None:
        sv = _variable(raw["sv"], "sv", _SV_KEYS, "secondary variable")

    return Profile(
        kind=raw["kind"],
        manufacturer_id=_int(raw["manufacturer_id"], "manufacturer_id", 0, 0xFFFF),
        device_type=_int(raw["device_type"], "device_type", 0, 0xFFFF),
        device_revision=_int(raw["device_revision"], "device_revision", 0, 255),
        software_revision=_int(raw.get("software_revision", 1), "software_revision", 0, 255),
        hardware_revision=_int(raw.get("hardware_revision", 1), "hardware_revision", 0, 31),
        tag=tag,
        long_tag=long_tag,
        descriptor=descriptor,
        message=message,
        pv=pv,
        sensor_limits=limits,
        range=rng,
        min_span=min_span,
        damping=damping,
        alarm=alarm,
        sv=sv,
    )


def load_profile(name_or_path: str | Path, overrides: dict | None = None) -> Profile:
    """Load a bundled profile by name, or a YAML file by path."""
    if isinstance(name_or_path, str) and name_or_path in bundled_profiles():
        path = _PROFILE_DIR / f"{name_or_path}.yaml"
    else:
        path = Path(name_or_path)
        if not path.is_file():
            raise ProfileError(
                f"{name_or_path}: not a bundled profile ({', '.join(bundled_profiles())}) "
                "and not an existing file"
            )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path}: invalid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ProfileError(f"{path}: expected a mapping at top level")
    if overrides:
        raw = {**raw, **overrides}
        pv = raw.get("pv")
        if isinstance(pv, dict):
            # An override of a pv-nested key replaces the nested value.
            raw["pv"] = {k: v for k, v in pv.items()
                         if not (k in _PV_TO_TOP and k in overrides)}
    return _build(raw)
