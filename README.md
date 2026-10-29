# hart-sim

[![CI](https://github.com/steitec/hart-sim/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/steitec/hart-sim/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/hart-sim)](https://pypi.org/project/hart-sim/)
[![Python](https://img.shields.io/pypi/pyversions/hart-sim)](https://pypi.org/project/hart-sim/)
[![License](https://img.shields.io/pypi/l/hart-sim)](https://github.com/steitec/hart-sim/blob/main/LICENSE)

Simulated HART field devices, a pressure and a temperature transmitter, that
speak HART-IP over TCP and UDP on port 5094. Develop and test HART software
(control-system links, asset management, commissioning scripts, gateways)
without devices, a HART modem or a commercial simulator, also in CI. The
devices keep state like real ones, turn a process value into a 4-20 mA loop
current, and can be made to fail on purpose while they run.

```console
$ hart-sim serve --bench plant.yaml &
hart-sim: 2 devices on HART-IP 127.0.0.1:5094 (tcp+udp), control on :5095
$ hart-sim poll
addr 1  PT-101  pressure-transmitter  id 0x000001
addr 2  TT-201  temperature-transmitter  id 0x000002
$ hart-sim cmd --tag PT-101 3
loop 10.70 mA | PV 418.9 kPa | SV 23.5 degC
$ hart-sim inject PT-101 sensor_failure
$ hart-sim cmd --tag PT-101 3
status: device malfunction, more status available, PV out of limits | loop 21.00 mA | PV 416.9 kPa (bad) | SV 23.5 degC
```

## Why

HART software is usually written against the one real device on the bench, or
against an expensive simulator, and is rarely tested in CI at all. hart-sim
gives you devices that:

- **are reachable over HART-IP** (TCP and UDP, port 5094), so anything that
  speaks HART-IP can talk to them, with no hardware
- **keep state** like a real device: tag, range, unit, damping, write
  protect, configuration-change counter, status bits
- **behave plausibly**: the process value becomes a 4-20 mA loop current with
  saturation and NAMUR NE 43 alarm currents
- **are strict where real devices are strict**: a bad checksum, a wrong
  address or a malformed frame gets no answer, never a lenient guess
- **break on purpose**: sensor failure, saturation, busy, communication
  errors, missing answers, drift, injected at runtime through a control
  channel

## Install

Requires Python 3.10 or newer; the only dependency is PyYAML.

```bash
pip install hart-sim
```

From source, with the test dependencies:

```bash
git clone https://github.com/steitec/hart-sim.git && cd hart-sim
pip install -e ".[dev]"
pytest
```

## Quick start

```bash
hart-sim serve pressure                  # one device, polling address 0
hart-sim serve --bench plant.yaml        # several devices, see Bench files
```

`plant.yaml` is the two-device bench shown under [Bench files](#bench-files);
the repository has it as
[`examples/plant.yaml`](https://github.com/steitec/hart-sim/blob/main/examples/plant.yaml).

The server listens on `127.0.0.1`; HART-IP uses port 5094 (TCP and UDP) and
the control channel port 5095. Change them with `--host`, `--port` and
`--control-port` (`0` picks a free port). From a second terminal:

```console
$ hart-sim poll                    # which devices answer (command 0 + 13, all 64 addresses)
$ hart-sim cmd --tag PT-101 3      # command 3 to the device with this tag
$ hart-sim cmd --address 1 15      # ... or to a polling address
$ hart-sim cmd --address 1 35 0c4416000000000000   # raw request data as hex
$ hart-sim inject PT-101 drift per_second=0.5
$ hart-sim clear PT-101 drift      # or: hart-sim clear PT-101  (all faults)
$ hart-sim set PT-101 pv 123.4     # override the process value; "auto" releases it
$ hart-sim profiles                # bundled profiles
```

`poll` and `cmd` accept `--host`, `--port` and `--udp`; `inject`, `clear` and
`set` accept `--host` and `--control-port`. The decoded one-line output covers
commands 1, 2, 3, 13, 15 and 48; every other command prints the response code,
the device status and the data as hex.

## Profiles

A profile describes one device type in YAML. Two are bundled:

| Profile | Device type | PV | SV |
|---|---|---|---|
| `pressure` | `0xFA01` | pressure, kPa, sensor limits -100 .. 2500, range 0 .. 1000 | sensor temperature, degC |
| `temperature` | `0xFA02` | temperature, degC, sensor limits -200 .. 850, range 0 .. 150 | terminal temperature, degC |

```yaml
kind: pressure-transmitter
manufacturer_id: 0x00FA      # deliberately not a real manufacturer
device_type: 0xFA01
device_revision: 1
tag: PT-101
long_tag: "Pump 1 discharge pressure"
descriptor: "SIM PRESSURE"
min_span: 10
alarm: high                  # alarm current: high (21.0 mA) or low (3.6 mA)
pv:
  unit: kPa
  sensor_limits: [-100, 2500]
  range: [0, 1000]           # LRV, URV
  damping: 0.5               # s
  source: {type: sine, offset: 400, amplitude: 50, period: 30}
sv: {name: sensor temperature, unit: degC, source: {type: constant, value: 23.5}}
```

Process value sources: `constant` (`value`), `ramp` (`start`, `stop`,
`period`), `sine` (`offset`, `amplitude`, `period`) and `noise` (`sigma`).
`source` may also be a list; the values of all its entries are summed:

```yaml
  source:
    - {type: constant, value: 400}
    - {type: sine, offset: 0, amplitude: 20, period: 30}
    - {type: noise, sigma: 0.5}
```

Units: `inH2O psi bar mbar kPa degC degF K mA %`. The generic profiles use
manufacturer ID `0x00FA` and expanded device types `0xFA01` / `0xFA02`, which
deliberately belong to no real manufacturer.

## Bench files

A bench puts several devices behind one HART-IP server. Each entry names a
profile (a bundled name or a YAML file, relative to the bench file), a unique
polling address (0-63) and a device ID; any other key overrides the profile:

```yaml
devices:
  - {profile: pressure, polling_address: 1, device_id: 0x000001, tag: PT-101}
  - {profile: temperature, polling_address: 2, device_id: 0x000002, tag: TT-201}
```

Duplicate tags, polling addresses or device IDs are rejected with the offending
entry named.

## Supported commands

HART revision 7 universal and common practice commands. Write commands are
refused with response code 7 in write protect mode.

| # | Name | |
|---|---|---|
| 0 | Read unique identifier | read |
| 1 | Read primary variable | read |
| 2 | Read loop current and percent of range | read |
| 3 | Read dynamic variables and loop current | read |
| 6 | Write polling address | write |
| 7 | Read loop configuration | read |
| 8 | Read dynamic variable classifications | read |
| 9 | Read device variables with status | read |
| 11 | Read unique identifier associated with tag | read |
| 12 | Read message | read |
| 13 | Read tag, descriptor, date | read |
| 14 | Read primary variable transducer information | read |
| 15 | Read device information | read |
| 16 | Read final assembly number | read |
| 17 | Write message | write |
| 18 | Write tag, descriptor, date | write |
| 19 | Write final assembly number | write |
| 20 | Read long tag | read |
| 21 | Read unique identifier associated with long tag | read |
| 22 | Write long tag | write |
| 33 | Read device variables | read |
| 34 | Write primary variable damping value | write |
| 35 | Write primary variable range values | write |
| 36 | Set primary variable upper range value | write |
| 37 | Set primary variable lower range value | write |
| 38 | Reset configuration changed flag | write |
| 40 | Enter/exit fixed current mode | write |
| 41 | Perform device self test | read |
| 42 | Perform device reset | write |
| 44 | Write primary variable units | write |
| 45 | Trim loop current zero | write |
| 46 | Trim loop current gain | write |
| 48 | Read additional device status | read |
| 59 | Write number of response preambles | write |

Commands 38 and 48 are not blocked by write protect. Unknown commands get
response code 64. Requests with too little data get code 5, bad values the
usual codes (2, 3, 4, 9-12, 14); a request with a bad checksum, for another
address, or malformed gets no answer.

## Fault injection

Faults are injected at runtime with `hart-sim inject` or through the
[control channel](#control-channel). Parameters are `KEY=VALUE`.

| kind | parameters | effect | example |
|---|---|---|---|
| `no_response` | `count=1` | the next `count` requests get no answer | `hart-sim inject PT-101 no_response count=3` |
| `delay` | `seconds`, `count` (default: forever) | answers are sent `seconds` late | `hart-sim inject PT-101 delay seconds=0.5` |
| `busy` | `count=1` | response code 32 (busy), no state change | `hart-sim inject PT-101 busy count=2` |
| `comm_error` | `flags=0x08`, `count=1` | communication error byte instead of a response code | `hart-sim inject PT-101 comm_error flags=0x10` |
| `sensor_failure` | none | device malfunction, PV out of limits and bad, alarm current, details in command 48 | `hart-sim inject PT-101 sensor_failure` |
| `saturate` | `direction=high` or `low` | loop current pinned to 20.5 / 3.8 mA, "loop current saturated" | `hart-sim inject PT-101 saturate direction=high` |
| `cold_start` | none | cold start bit set for both masters, as after a restart | `hart-sim inject PT-101 cold_start` |
| `more_status` | `bytes` (6 integers) | "more status available" until command 48 reads it; the bytes appear in its device specific status | `hart-sim inject PT-101 more_status bytes=1,2,3,4,5,6` |
| `drift` | `per_second` | the PV drifts away; `clear` removes it | `hart-sim inject PT-101 drift per_second=0.5` |
| `write_protect` | `on` | write protect switched on or off | `hart-sim inject PT-101 write_protect on=on` |

Integers may be written in decimal or as `0x..` hex; booleans as `true`/`false`,
`on`/`off` or `1`/`0`; `bytes` as a comma-separated list. The same forms work in
the JSON `params` of the control channel (lists and JSON booleans too).

## Control channel

The bench listens on a second TCP port (default 5095) for one JSON object per
line and answers each with one line, `{"ok": true}` or `{"ok": false, "error": ...}`:

```json
{"device": "PT-101", "inject": "sensor_failure", "params": {}}
{"device": "PT-101", "clear": true}
{"device": "PT-101", "clear": "drift"}
{"device": "PT-101", "set": {"pv": 123.4}}
{"list": true}
```

`set` with `null` returns the PV (or `sv`) to the simulated process; `list`
returns faults, PV, loop current and polling address per device. `pv` and
`sv` values must satisfy |value| <= 3.4e38 (what a HART float can carry).

The control channel binds `127.0.0.1` whatever `--host` is; use
`hart-sim serve --control-host ADDRESS` to listen elsewhere. A request line is
limited to 64 KiB (`{"ok": false, "error": "line too long"}`, then the
connection is closed).

**No authentication:** neither HART-IP nor the control port checks who
connects. Binding either to `0.0.0.0` (or any non-loopback address) exposes the
simulated devices to everyone who can reach the machine.

## Python client

A small HART-IP master, usable on its own against real gateways too:

```python
from hart_sim.client import HartIPClient

with HartIPClient("127.0.0.1") as client:        # transport="udp" for UDP
    device = client.find("PT-101")                # command 11 (broadcast by tag)
    print(device.read_variables())                # command 3
    device.write_range(0, 600)                    # command 35
    print(client.poll(range(8)))                  # devices at polling addresses 0-7
```

Lower level: `client.command(address, number, data)` returns the response frame
(`response_code`, `device_status`, `data`). Communication errors and missing
answers raise `HartError` / `HartTimeout`.

## pytest plugin

Installing the package registers a pytest plugin with a fresh bench for every
test, on free ports, stopped afterwards:

```python
def test_range_change(hart_bench):
    with hart_bench.client() as client:             # connected HartIPClient
        device = client.handle(0)
        assert device.write_range(0, 600) == 0

def test_sensor_failure(hart_bench):
    hart_bench.devices["PT-101"].faults.inject("sensor_failure")
    ...
```

- `hart_bench`: the bench from `--hart-bench FILE` or the ini key `hart_bench`
  (relative to the rootdir). Without either: one `pressure` device at polling
  address 0 with tag `PT-101`.
- `hart_bench_factory(path_or_dict_or_profile)`: more benches in one test,
  from a bench file, a bench dict or a bundled profile name.
- `hart_bench.client(**kwargs)` returns a connected `HartIPClient`
  (`transport`, `timeout`, `primary`); it is closed when the bench stops.

See [`examples/test_example.py`](https://github.com/steitec/hart-sim/blob/main/examples/test_example.py).

## Limitations

- HART-IP version 1 only, unencrypted. No serial HART (no FSK), no burst
  mode, no device descriptions (DD), no secure connections.
- The byte layouts of the commands come from public sources, not from the
  paid HART specification. The simulator is not registered with FieldComm
  Group, uses a made-up manufacturer ID and is for development and testing
  only.
- The long address carries only 14 bits of the expanded device type (`0xFA01`
  is addressed as `0x3A01`).
- Unit code 1 is inH2O at 68 degF.
- Commands 36 and 37 apply the same limit checks as 35. A real device's
  command 37 keeps the span.
- Fixed 4 mA ("multidrop") is only set by command 6 (loop current mode). A
  polling address other than 0 in a bench file or profile does not fix the
  current; this deviates from the wording of the HART specification, which
  ties multidrop to the polling address.
- Clearing a `drift` fault stops the drift but keeps the offset that has
  already accumulated; it does not return the PV to where it started.
- The control channel and the bench (`inject`, `clear`, `set`, `list`) address
  a device by its original tag, also after command 18 or 22 has renamed it.
- HART-IP is IPv4 only.
- A `delay` fault on UDP delays all UDP clients (single UDP thread).
- `no_response` is consumed even by requests the device would ignore anyway.

## Licence

MIT, see [LICENSE](https://github.com/steitec/hart-sim/blob/main/LICENSE).
