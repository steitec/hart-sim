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


# Conversion to a base unit per family: base = a * value + b.
# Pressure base is kPa (inH2O at 4 degC); temperature base is degC.
_PRESSURE = {1: (0.24908891, 0.0), 6: (6.894757293, 0.0), 7: (100.0, 0.0),
             8: (0.1, 0.0), 12: (1.0, 0.0)}
_TEMPERATURE = {32: (1.0, 0.0), 33: (5.0 / 9.0, -160.0 / 9.0), 35: (1.0, -273.15)}


def _family(unit: int):
    for table in (_PRESSURE, _TEMPERATURE):
        if unit in table:
            return table
    return None


def same_family(a: int, b: int) -> bool:
    """True if both are pressure units or both are temperature units."""
    fam = _family(a)
    return fam is not None and fam is _family(b)


def conversion(src: int, dst: int) -> tuple[float, float]:
    """(scale, shift) with value_dst = scale * value_src + shift. Spans use `scale` only."""
    if not same_family(src, dst):
        raise ValueError(f"cannot convert unit {src} to unit {dst}")
    table = _family(src)
    a_s, b_s = table[src]
    a_d, b_d = table[dst]
    return a_s / a_d, (b_s - b_d) / a_d


def convert(value: float, src: int, dst: int) -> float:
    scale, shift = conversion(src, dst)
    return scale * value + shift
