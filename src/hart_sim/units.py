"""HART engineering unit codes (the subset used by the simulator)."""
from __future__ import annotations

UNKNOWN = 250  # "not used"

_UNITS = {
    "inH2O": 1,
    "psi": 6,
    "bar": 7,
    "mbar": 8,
    "kPa": 12,
    "degC": 32,
    "degF": 33,
    "K": 35,
    "mA": 39,
    "%": 57,
}
_NAMES = {v: k for k, v in _UNITS.items()}
_NAMES[UNKNOWN] = "not used"


def code(name: str) -> int:
    try:
        return _UNITS[name]
    except (KeyError, TypeError):
        raise ValueError(f"unknown unit {name!r}; known: {', '.join(_UNITS)}") from None


def name(code: int) -> str:
    try:
        return _NAMES[code]
    except KeyError:
        raise ValueError(f"unknown unit code {code!r}") from None
