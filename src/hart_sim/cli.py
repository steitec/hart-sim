"""Command line interface: ``hart-sim serve``, ``poll``, ``cmd``, ``inject``, ``clear``, ``set``."""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from typing import Any

from . import __version__, hartip, units
from .bench import CONTROL_PORT, Bench, BenchError
from .client import DEVICE_STATUS, PROCESS_DATA, DeviceHandle, HartConnectionError, HartError, HartIPClient, HartTimeout
from .control import send_control
from .frame import unpack_ascii, unpack_float
from .profile import ProfileError, bundled_profiles

# Response codes 8 and 14 (span too small) are warnings: the command was carried out.
_WARNING_CODES = (8, 14)
_LISTEN_IDLE_LIMIT = 10.0  # `listen --count` gives up after this many seconds without a burst
_KINDS = {0xFA01 & 0x3FFF: "pressure-transmitter", 0xFA02 & 0x3FFF: "temperature-transmitter"}


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        port = -1
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"invalid port {text!r} (allowed: 0-65535)")
    return port


def _polling_address(text: str) -> int:
    try:
        number = int(text)
    except ValueError:
        number = -1
    if not 0 <= number <= 63:
        raise argparse.ArgumentTypeError(f"invalid polling address {text!r} (allowed: 0-63)")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hart-sim",
                                     description="Simulated HART field devices speaking HART-IP.")
    parser.add_argument("--version", action="version", version=f"hart-sim {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    def client_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--host", default="127.0.0.1", help="HART-IP server (default: 127.0.0.1)")
        p.add_argument("--port", type=_port, default=hartip.PORT,
                       help=f"HART-IP port (default: {hartip.PORT})")
        p.add_argument("--udp", action="store_true", help="use UDP instead of TCP")

    def control_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--host", default="127.0.0.1", help="bench address (default: 127.0.0.1)")
        p.add_argument("--control-port", type=_port, default=CONTROL_PORT,
                       help=f"control port (default: {CONTROL_PORT})")

    serve = commands.add_parser("serve", help="serve one bundled profile or a bench file")
    serve.add_argument("profile", nargs="?", help="bundled profile name (see: hart-sim profiles)")
    serve.add_argument("--bench", metavar="FILE", help="serve all devices of a bench file (YAML)")
    serve.add_argument("--host", default="127.0.0.1", help="address to listen on (default: 127.0.0.1)")
    serve.add_argument("--port", type=_port, default=hartip.PORT,
                       help=f"HART-IP port, TCP and UDP (default: {hartip.PORT}, 0 = any free port)")
    serve.add_argument("--control-host", default="127.0.0.1",
                       help="address of the control port (default: 127.0.0.1, whatever --host is; "
                            "the control port has no authentication)")
    serve.add_argument("--control-port", type=_port, default=CONTROL_PORT,
                       help=f"control port (default: {CONTROL_PORT}, 0 = any free port)")
    serve.add_argument("--publish-all", action="store_true",
                       help="send bursts to every HART-IP session, not only subscribed ones "
                            "(like 'subscribe_all: true' in a bench file)")

    poll = commands.add_parser("poll", help="list the devices that answer on the server")
    poll.add_argument("--max-address", type=_polling_address, default=63, metavar="N",
                      help="poll addresses 0..N (default: 63)")
    client_args(poll)

    cmd = commands.add_parser("cmd", help="send one HART command to a device")
    who = cmd.add_mutually_exclusive_group(required=True)
    who.add_argument("--tag", help="device tag")
    who.add_argument("--address", type=int, metavar="N", help="polling address 0-63")
    cmd.add_argument("number", type=int, help="command number")
    cmd.add_argument("hexdata", nargs="?", default="", help="request data as hex, e.g. 0102ff")
    client_args(cmd)

    burst = commands.add_parser("burst", help="switch a device's burst mode on or off")
    burst.add_argument("tag")
    burst.add_argument("--command", type=int, metavar="N", dest="burst_command",
                       help="command to burst: 1, 2, 3, 9, 33 or 48 (needed unless --off)")
    burst.add_argument("--period", type=float, metavar="S",
                       help="burst period in seconds, at least 0.1 (needed unless --off)")
    burst.add_argument("--max-period", type=float, metavar="S", help="maximum period in seconds")
    burst.add_argument("--message", type=int, default=0, metavar="K",
                       help="burst message 0-2 (default: 0)")
    burst.add_argument("--variables", metavar="0,1", help="device variable slots, comma separated")
    burst.add_argument("--off", action="store_true", help="switch the burst message off")
    client_args(burst)

    listen = commands.add_parser("listen", help="print the bursts of a device")
    listen.add_argument("tag")
    listen.add_argument("--count", type=int, metavar="N", help="stop after N bursts (default: Ctrl-C)")
    listen.add_argument("--status", action="store_true",
                        help="also subscribe to device status bursts (command 48)")
    client_args(listen)

    inject = commands.add_parser("inject", help="inject a fault into a running bench")
    inject.add_argument("tag")
    inject.add_argument("kind", help="e.g. sensor_failure, saturate, busy, drift, no_response, delay")
    inject.add_argument("params", nargs="*", metavar="KEY=VALUE", help="fault parameters, e.g. per_second=0.5 or direction=high")
    control_args(inject)

    clear = commands.add_parser("clear", help="clear one fault or all faults of a device")
    clear.add_argument("tag")
    clear.add_argument("kind", nargs="?", help="fault to clear (default: all)")
    clear.add_argument("--reset", action="store_true",
                       help="also zero the drift offset (only with drift or all faults)")
    control_args(clear)

    set_ = commands.add_parser("set", help="override the PV or SV of a device")
    set_.add_argument("tag")
    set_.add_argument("variable", choices=["pv", "sv"])
    set_.add_argument("value", help="a number, or 'auto' to return to the simulated process")
    control_args(set_)

    commands.add_parser("profiles", help="list the bundled device profiles")
    return parser


def _error(message: str) -> int:
    print(f"hart-sim: error: {message}", file=sys.stderr)
    return 2


# --- serve ---

def _stop_event() -> threading.Event:
    """Set on SIGINT/SIGTERM. Installed before the announcement so no signal is lost."""
    stop = threading.Event()
    # Install SIGINT explicitly: a shell starts "hart-sim serve ... &" with SIGINT ignored.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    return stop


def _serve(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if bool(args.profile) == bool(args.bench):
        parser.error("serve needs exactly one of PROFILE or --bench FILE")
    try:
        bench = Bench.from_yaml(args.bench) if args.bench else Bench.single(args.profile)
    except (BenchError, ProfileError) as exc:
        return _error(str(exc))
    bench.publish_all = bench.publish_all or args.publish_all
    try:
        bench.start(host=args.host, port=args.port, control_port=args.control_port,
                    control_host=args.control_host)
    except OSError as exc:
        return _error(f"cannot listen on {args.host}: {exc.strerror or exc}")
    stop = _stop_event()
    count = len(bench.devices)
    print(f"hart-sim: {count} device{'' if count == 1 else 's'} on HART-IP "
          f"{args.host}:{bench.server.port} (tcp+udp), control on "
          f"{args.control_host}:{bench.control_port}", flush=True)
    while not stop.wait(0.5):
        pass
    bench.stop()
    print("hart-sim: stopped", flush=True)
    return 0


# --- client commands ---

def _kind(device_type: int) -> str:
    return _KINDS.get(device_type & 0x3FFF, f"device type 0x{device_type & 0x3FFF:04X}")


def _fmt(value: float, digits: int) -> str:
    return f"{value:.{digits}f}"


def _decode(number: int, data: bytes) -> str | None:
    """Decoded one-line output for the universal read commands, None if not decodable."""
    try:
        if number == 1 and len(data) >= 5:
            return f"PV {_fmt(unpack_float(data[1:5]), 1)} {units.name(data[0])}"
        if number == 2 and len(data) >= 8:
            return f"loop {_fmt(unpack_float(data[0:4]), 2)} mA | {_fmt(unpack_float(data[4:8]), 1)} %"
        if number == 3 and len(data) >= 9:
            text = (f"loop {_fmt(unpack_float(data[0:4]), 2)} mA | "
                    f"PV {_fmt(unpack_float(data[5:9]), 1)} {units.name(data[4])}")
            if len(data) >= 14:
                text += f" | SV {_fmt(unpack_float(data[10:14]), 1)} {units.name(data[9])}"
            return text
        if number == 13 and len(data) >= 21:
            day, month, year = data[18], data[19], data[20] + 1900
            return (f"tag {unpack_ascii(data[:6]).strip()} | descriptor "
                    f"{unpack_ascii(data[6:18]).strip()} | date {year:04d}-{month:02d}-{day:02d}")
        if number == 15 and len(data) >= 17:
            alarm = "high" if data[0] == 0 else "low" if data[0] == 1 else f"code {data[0]}"
            unit = units.name(data[2])
            return (f"range {_fmt(unpack_float(data[7:11]), 1)} .. {_fmt(unpack_float(data[3:7]), 1)} "
                    f"{unit} | damping {_fmt(unpack_float(data[11:15]), 2)} s | alarm {alarm} | "
                    f"write protect {'on' if data[15] else 'off'}")
        if number == 48 and len(data) >= 6:
            extended = f" | extended 0x{data[6]:02x}" if len(data) > 6 else ""
            return f"status bytes {data[:6].hex(' ')}{extended}"
    except ValueError:  # e.g. an unit code this simulator does not know
        return None
    return None


def _cmd(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    try:
        payload = bytes.fromhex(args.hexdata)
    except ValueError:
        parser.error(f"HEXDATA {args.hexdata!r} is not a hex string")
    if args.address is not None and not 0 <= args.address <= 63:
        parser.error("--address must be 0-63")
    if not 0 <= args.number <= 255:
        parser.error("command number must be 0-255")
    transport = "udp" if args.udp else "tcp"
    client = HartIPClient(args.host, args.port, transport=transport)
    try:
        client.open()
    except HartError as exc:
        return _error(str(exc))  # connection refused or no session
    try:
        try:
            if args.tag is not None:
                handle = client.find(args.tag)
            else:
                handle = client.handle(args.address)
            frame = client.command(handle.address, args.number, payload)
        except LookupError as exc:
            print(f"hart-sim: error: {exc}", file=sys.stderr)
            return 1
        except HartTimeout:
            print("hart-sim: error: device did not answer", file=sys.stderr)
            return 1
        except HartConnectionError as exc:
            return _error(str(exc))  # transport failure, like connection refused
        except HartError as exc:
            print(f"hart-sim: error: {exc}", file=sys.stderr)
            return 1
        if frame.response_code != 0:
            print(f"code {frame.response_code} status 0x{frame.device_status:02x} "
                  f"data {frame.data.hex()}")
            if frame.response_code in _WARNING_CODES:  # applied, but with a remark
                print(f"warning: code {frame.response_code}", file=sys.stderr)
                return 0
            print(f"hart-sim: error: command {args.number}: response code {frame.response_code}",
                  file=sys.stderr)
            return 1
        text = _decode(args.number, frame.data)
        if text is None:
            print(f"code {frame.response_code} status 0x{frame.device_status:02x} "
                  f"data {frame.data.hex()}")
            return 0
        if frame.device_status:
            text = "status: " + ", ".join(DeviceHandle.status_text(frame.device_status)) + " | " + text
            if frame.device_status & 0x01 and args.number in (1, 3):
                text = text.replace(" | SV", " (bad) | SV") if " | SV" in text else text + " (bad)"
        print(text)
        return 0
    finally:
        client.close()


def _find_device(args: argparse.Namespace, client: HartIPClient) -> DeviceHandle | int:
    """Handle of the device with the tag, or the exit code after printing the error."""
    try:
        return client.find(args.tag)
    except LookupError as exc:
        print(f"hart-sim: error: {exc}", file=sys.stderr)
        return 1


def _identity(address: Any) -> tuple[int, int] | None:
    """(device type, device ID) of a long address; the two expanded-type flag bits differ
    between a burst frame (cleared) and the address learnt from command 11 (set)."""
    if address.unique_id is None:
        return None
    return address.unique_id[0] & 0x3FFF, address.unique_id[1]


def _burst(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if not 0 <= args.message <= 255:
        parser.error("--message must be 0-255")
    variables = None
    if args.variables is not None:
        try:
            variables = [int(v) for v in args.variables.split(",")]
        except ValueError:
            parser.error(f"--variables {args.variables!r} is not a comma separated list of numbers")
        if not all(0 <= v <= 255 for v in variables):
            parser.error("--variables: slot codes are 0-255")
    if not args.off:
        if args.burst_command is None or args.period is None:
            parser.error("burst needs --command and --period (or --off)")
        if args.period <= 0:
            parser.error("--period must be greater than 0")
    client = HartIPClient(args.host, args.port, transport="udp" if args.udp else "tcp")
    try:
        client.open()
    except HartError as exc:
        return _error(str(exc))
    try:
        try:
            handle = _find_device(args, client)
            if isinstance(handle, int):
                return handle
            if args.off:
                handle.burst_off(args.message)
                print(f"burst off: {args.tag} message {args.message}")
            else:
                handle.burst(args.burst_command, args.period, max_period=args.max_period,
                             variables=variables, message=args.message)
                print(f"burst on: {args.tag} message {args.message} command {args.burst_command} "
                      f"every {args.period:g} s")
        except ValueError as exc:
            parser.error(str(exc))
        except HartTimeout:
            print("hart-sim: error: device did not answer", file=sys.stderr)
            return 1
        except HartConnectionError as exc:
            return _error(str(exc))
        except HartError as exc:  # the device refused, e.g. "command 108: response code 7"
            print(f"hart-sim: error: {exc}", file=sys.stderr)
            return 1
        return 0
    finally:
        client.close()


def _listen(args: argparse.Namespace) -> int:
    if args.count is not None and args.count < 1:
        return _error("--count must be at least 1")
    client = HartIPClient(args.host, args.port, transport="udp" if args.udp else "tcp")
    try:
        client.open()
    except HartError as exc:
        return _error(str(exc))
    try:
        try:
            handle = _find_device(args, client)
            if isinstance(handle, int):
                return handle
            handle.subscribe(PROCESS_DATA | (DEVICE_STATUS if args.status else 0))
            seen = 0
            idle_since = time.monotonic()
            while args.count is None or seen < args.count:
                try:
                    publish = client.wait_burst(timeout=0.5)
                except HartTimeout:
                    if args.count is not None and time.monotonic() - idle_since > _LISTEN_IDLE_LIMIT:
                        print(f"hart-sim: error: no burst from {args.tag} for "
                              f"{_LISTEN_IDLE_LIMIT:g} s (is burst mode on? see: hart-sim burst)",
                              file=sys.stderr)
                        return 1
                    continue
                frame = publish.frame
                if _identity(frame.address) != _identity(handle.address):
                    continue  # another device, e.g. with serve --publish-all
                idle_since = time.monotonic()
                now = time.time()
                stamp = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"
                text = _decode(frame.command, frame.data)
                if text is None:
                    text = f"data {frame.data.hex()}"
                print(f"{stamp} {args.tag} cmd {frame.command} {text}", flush=True)
                seen += 1
        except KeyboardInterrupt:
            return 0
        except HartTimeout:
            print("hart-sim: error: device did not answer", file=sys.stderr)
            return 1
        except HartConnectionError as exc:
            return _error(str(exc))
        except HartError as exc:
            print(f"hart-sim: error: {exc}", file=sys.stderr)
            return 1
        return 0
    finally:
        client.close()


def _poll(args: argparse.Namespace) -> int:
    client = HartIPClient(args.host, args.port, transport="udp" if args.udp else "tcp")
    try:
        with client:
            found = client.poll(max_address=args.max_address)
    except HartError as exc:
        return _error(str(exc))
    for info in found:
        print(f"addr {info.polling_address}  {info.tag}  {_kind(info.device_type)}  "
              f"id 0x{info.device_id:06x}")
    return 0


# --- control commands ---

def _value(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _control(args: argparse.Namespace, request: dict) -> int:
    try:
        answer = send_control(request, host=args.host, port=args.control_port)
    except OSError as exc:
        return _error(f"cannot connect to control port {args.host}:{args.control_port}: "
                      f"{getattr(exc, 'strerror', None) or exc}")
    if not answer.get("ok"):
        print(f"hart-sim: error: {answer.get('error')}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(parser, args)
    if args.command in ("poll", "cmd", "burst", "listen"):
        try:
            if args.command == "poll":
                return _poll(args)
            if args.command == "cmd":
                return _cmd(parser, args)
            return _burst(parser, args) if args.command == "burst" else _listen(args)
        except OSError as exc:  # anything the client did not turn into a HartError
            return _error(getattr(exc, "strerror", None) or str(exc))
    if args.command == "profiles":
        for name in bundled_profiles():
            print(name)
        return 0
    if args.command == "inject":
        params = {}
        for item in args.params:
            key, sep, value = item.partition("=")
            if not sep or not key:
                parser.error(f"parameter {item!r} is not KEY=VALUE")
            params[key] = _value(value)
        return _control(args, {"device": args.tag, "inject": args.kind, "params": params})
    if args.command == "clear":
        request = {"device": args.tag, "clear": args.kind or True}
        if args.reset:
            request["reset"] = True
        return _control(args, request)
    if args.value == "auto":
        value = None
    else:
        try:
            value = float(args.value)
        except ValueError:
            parser.error(f"value {args.value!r} is not a number or 'auto'")
    return _control(args, {"device": args.tag, "set": {args.variable: value}})


if __name__ == "__main__":
    sys.exit(main())
