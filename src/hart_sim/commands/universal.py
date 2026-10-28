"""Universal commands 0-3."""
from __future__ import annotations

import math

from ..frame import pack_float
from . import register


@register(0)
def cmd0(dev, data, primary):
    mfr = dev.profile.manufacturer_id
    bad = any(not math.isfinite(v.value()) for v in (dev.pv, dev.sv) if v is not None)
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
