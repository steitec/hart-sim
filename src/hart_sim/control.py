"""Control channel of a running bench: inject faults and set process values from outside.

One JSON object per line in, one per line out::

    {"device": "PT-101", "inject": "sensor_failure", "params": {}}  -> {"ok": true}
    {"device": "PT-101", "clear": true}      (or "clear": "drift")  -> {"ok": true}
    {"device": "PT-101", "set": {"pv": 123.4}}   (null = back to the sources; "sv" likewise)
    {"list": true} -> {"ok": true, "devices": {"PT-101": {"faults": {...}, "pv": 400.0,
                                                          "current_ma": 10.4,
                                                          "polling_address": 1}}}

Anything invalid gets ``{"ok": false, "error": "..."}``; the connection stays open.
"""
from __future__ import annotations

import json
import math
import socket
import socketserver
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .bench import Bench

_SETTABLE = ("pv", "sv")


class _Handler(socketserver.StreamRequestHandler):
    def setup(self) -> None:
        super().setup()
        self.server.connections.add(self.request)

    def finish(self) -> None:
        self.server.connections.discard(self.request)
        super().finish()

    def handle(self) -> None:
        try:
            for raw in self.rfile:
                if not raw.strip():
                    continue
                answer = self.server.control.handle(raw)
                self.wfile.write(json.dumps(answer).encode() + b"\n")
        except OSError:
            return


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler):
        self.address_family = socket.getaddrinfo(
            address[0], address[1], type=socket.SOCK_STREAM)[0][0]
        self.connections: set[socket.socket] = set()
        super().__init__(address, handler)


class ControlServer:
    def __init__(self, bench: "Bench", host: str = "127.0.0.1", port: int = 5095):
        self.bench = bench
        self.host = host
        self.port = port
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        self._server = _Server((self.host, self.port), _Handler)
        self._server.control = self
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.05}, daemon=True,
                                        name="hart-sim-control")
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        for connection in list(self._server.connections):
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._thread.join(timeout=5)
        self._server = self._thread = None

    def __enter__(self) -> "ControlServer":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def handle(self, raw: bytes) -> dict[str, Any]:
        try:
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("a request is a JSON object")
            return self._handle(request)
        except ValueError as error:  # includes JSONDecodeError and FaultError
            return {"ok": False, "error": str(error)}
        except Exception as error:  # never drop the connection with a traceback
            return {"ok": False, "error": f"{type(error).__name__}: {error}"}

    def _handle(self, request: dict) -> dict[str, Any]:
        devices = self.bench.devices
        if request.get("list"):
            return {"ok": True, "devices": {tag: self._describe(dev)
                                            for tag, dev in devices.items()}}
        name = request.get("device")
        device = devices.get(name) if isinstance(name, str) else None
        if device is None:
            raise ValueError(f"unknown device {name!r} (bench has: {', '.join(devices)})")
        if "clear" in request and request["clear"]:
            kind = request["clear"]
            if kind is True:
                device.faults.clear()
            elif isinstance(kind, str):
                device.faults.clear(kind)
            else:
                raise ValueError("'clear' must be true or a fault name")
        elif "inject" in request:
            kind = request["inject"]
            params = request.get("params")
            if params is None:
                params = {}
            if not isinstance(kind, str):
                raise ValueError("'inject' must be a fault name")
            if not isinstance(params, dict):
                raise ValueError("'params' must be an object")
            try:
                device.faults.inject(kind, **params)
            except TypeError as error:
                raise ValueError(str(error)) from None
        elif "set" in request:
            self._set(device, request["set"])
        else:
            raise ValueError("a request needs 'list', 'clear', 'inject' or 'set'")
        return {"ok": True}

    @staticmethod
    def _set(device, values: Any) -> None:
        if not isinstance(values, dict) or not values:
            raise ValueError("'set' must be a non-empty object like {\"pv\": 1.5}")
        parsed = {}
        for key, value in values.items():
            if key not in _SETTABLE:
                raise ValueError(f"cannot set {key!r} (allowed: {', '.join(_SETTABLE)})")
            variable = getattr(device, key)
            if variable is None:
                raise ValueError(f"device {device.tag!r} has no {key}")
            if value is not None and (isinstance(value, bool)
                                      or not isinstance(value, (int, float))
                                      or not math.isfinite(value)):
                raise ValueError(f"{key}: must be a finite number or null, got {value!r}")
            parsed[key] = (variable, None if value is None else float(value))
        with device.lock:  # all or nothing, atomic against the HART side
            for variable, value in parsed.values():
                variable.override(value)

    @staticmethod
    def _describe(device) -> dict[str, Any]:
        with device.lock:  # device lock before the fault lock, never the reverse
            current = device.loop().current_ma
            pv = device.pv.value()
            faults = device.faults.snapshot()
        return {"faults": faults, "pv": pv, "current_ma": current,
                "polling_address": device.polling_address}


def send_control(request: dict, host: str = "127.0.0.1", port: int = 5095,
                 timeout: float = 2.0) -> dict:
    """Send one request to a bench's control port; raises OSError if it cannot be reached."""
    with socket.create_connection((host, port), timeout=timeout) as sock, \
            sock.makefile("rwb") as stream:
        stream.write(json.dumps(request).encode() + b"\n")
        stream.flush()
        line = stream.readline()
    if not line:
        raise OSError("control port closed the connection")
    return json.loads(line)
