"""Bench: several simulated HART devices behind one HART-IP server, from a YAML file."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import hartip
from .control import ControlServer
from .device import Device
from .profile import ProfileError, bundled_profiles, load_profile
from .server import Server

CONTROL_PORT = 5095
_BENCH_KEYS = {"profile", "polling_address", "device_id"}


class BenchError(ValueError):
    """Invalid bench description; the message names the offending entry."""


def _int_in(value: Any, low: int, high: int, what: str, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BenchError(f"{where}: {what} must be an integer, got {value!r}")
    if not low <= value <= high:
        raise BenchError(f"{where}: {what} {value} is outside {low}-{high}")
    return value


class Bench:
    def __init__(self) -> None:
        self.devices: dict[str, Device] = {}
        self.server: Server | None = None
        self.control: ControlServer | None = None
        self.control_port = 0

    # --- construction ---

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Bench":
        path = Path(path)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise BenchError(f"{path}: {exc.strerror or exc}") from None
        except yaml.YAMLError as exc:
            raise BenchError(f"{path}: invalid YAML: {exc}") from None
        return cls.from_dict(data, base=path.parent)

    @classmethod
    def single(cls, profile: str) -> "Bench":
        """One device with the given profile at polling address 0."""
        return cls.from_dict({"devices": [{"profile": profile, "polling_address": 0,
                                           "device_id": 1}]})

    @classmethod
    def from_dict(cls, d: dict, base: Path | None = None) -> "Bench":
        if not isinstance(d, dict) or not isinstance(d.get("devices"), list) or not d["devices"]:
            raise BenchError("a bench is a mapping with a non-empty 'devices' list")
        unknown = set(d) - {"devices"}
        if unknown:
            raise BenchError(f"unknown key {sorted(unknown)[0]!r} (allowed: devices)")
        bench = cls()
        polling: dict[int, str] = {}
        ids: dict[tuple[int, int], str] = {}
        for index, entry in enumerate(d["devices"], 1):
            where = f"devices[{index}]"
            if not isinstance(entry, dict):
                raise BenchError(f"{where}: expected a mapping")
            if "profile" not in entry:
                raise BenchError(f"{where}: missing 'profile'")
            profile_ref = entry["profile"]
            if not isinstance(profile_ref, str):
                raise BenchError(f"{where}: profile must be a name or file path")
            for key in ("polling_address", "device_id"):
                if key not in entry:
                    raise BenchError(f"{where}: missing {key!r}")
            addr = _int_in(entry["polling_address"], 0, 63, "polling address", where)
            dev_id = _int_in(entry["device_id"], 0, 0xFFFFFF, "device ID", where)
            if base is not None and profile_ref not in bundled_profiles():
                candidate = base / profile_ref
                if candidate.is_file():
                    profile_ref = str(candidate)
            overrides = {k: v for k, v in entry.items() if k not in _BENCH_KEYS}
            try:
                profile = load_profile(profile_ref, overrides)
            except ProfileError as exc:
                raise BenchError(f"{where}: {exc}") from None
            device = Device(profile, polling_address=addr, device_id=dev_id)
            label = f"{where} ({device.tag})"
            if device.tag in bench.devices:
                raise BenchError(f"{label}: duplicate tag {device.tag!r}")
            if addr in polling:
                raise BenchError(
                    f"{label}: duplicate polling address {addr} (also used by {polling[addr]})")
            uid = (device.profile.device_type & 0x3FFF, dev_id)
            if uid in ids:
                raise BenchError(f"{label}: duplicate device ID {dev_id:#08x} "
                                 f"(also used by {ids[uid]})")
            bench.devices[device.tag] = device
            polling[addr] = device.tag
            ids[uid] = device.tag
        return bench

    # --- lifecycle ---

    def start(self, host: str = "127.0.0.1", port: int = hartip.PORT,
              control_port: int = CONTROL_PORT) -> None:
        """Start the HART-IP server and the control channel; port 0 picks a free port."""
        if self.server is not None:
            raise BenchError("bench is already running")
        server = Server(list(self.devices.values()), host=host, port=port)
        server.start()
        control = ControlServer(self, host, control_port)
        try:
            control.start()
        except BaseException:
            server.stop()
            raise
        self.server, self.control = server, control
        self.control_port = control.port

    def stop(self) -> None:
        """Stop server and control channel; safe to call more than once."""
        control, server = self.control, self.server
        self.control = self.server = None
        if control is not None:
            control.stop()
        if server is not None:
            server.stop()

    def __enter__(self) -> "Bench":
        """Start on free ports (0/0) so tests never collide; use start() for fixed ports."""
        self.start(port=0, control_port=0)
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
