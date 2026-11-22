<a href="https://steitec.net"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/steitec/.github/main/brand/steitec-logo-on-dark.svg">
  <img align="right" height="32" alt="steitec" src="https://raw.githubusercontent.com/steitec/.github/main/brand/steitec-logo-on-light.svg">
</picture></a>

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

Walkthroughs, CI recipes and troubleshooting are in the
[wiki](https://github.com/steitec/hart-sim/wiki); this README is the reference.

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
- **burst**: up to three burst messages per device, sent to HART-IP clients
  that subscribe to them (see [Burst mode](#burst-mode))
- **break on purpose**: sensor failure, saturation, busy, communication
  errors, missing answers, drift, stopped bursts, injected at runtime through
  a control channel

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
$ hart-sim clear PT-101 --reset    # all faults, and zero the drift offset too
$ hart-sim set PT-101 pv 123.4     # override the process value; "auto" releases it
$ hart-sim burst PT-101 --command 3 --period 0.5   # burst mode on, see Burst mode
$ hart-sim listen PT-101 --count 5                 # print five bursts
$ hart-sim profiles                # bundled profiles
```

`poll`, `cmd`, `burst` and `listen` accept `--host`, `--port` and `--udp`;
`inject`, `clear` and `set` accept `--host` and `--control-port`. `--host` can
be an IPv6 address (`hart-sim serve --host ::` listens on IPv6 and, where the
platform allows it, IPv4). The decoded one-line output covers
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
loop_current: enabled        # or: fixed (multidrop, the loop current stays at 4 mA)
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

`loop_current: fixed` starts the device in multidrop mode, as if command 6 had
disabled the loop current; command 6 can switch it back. A profile can also
preconfigure burst messages, see [Burst mode](#burst-mode).

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
entry named. The optional top-level key `subscribe_all: true` sends bursts to
every HART-IP session, with or without a subscription (`hart-sim serve
--publish-all` does the same for any bench).

## Supported commands

HART revision 7 universal and common practice commands, the burst mode
commands and the HART-IP subscription commands. Write commands are refused
with response code 7 in write protect mode.

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
| 37 | Set primary variable lower range value (keeps the span) | write |
| 38 | Reset configuration changed flag | write |
| 40 | Enter/exit fixed current mode | write |
| 41 | Perform device self test | read |
| 42 | Perform device reset | write |
| 44 | Write primary variable units | write |
| 45 | Trim loop current zero | write |
| 46 | Trim loop current gain | write |
| 48 | Read additional device status | read |
| 59 | Write number of response preambles | write |
| 103 | Write burst period | write |
| 104 | Write burst trigger | write |
| 105 | Read burst mode configuration | read |
| 107 | Write burst device variables | write |
| 108 | Write burst mode command number | write |
| 109 | Burst mode control | write |
| 532 | Read client subscription (HART-IP) | read |
| 533 | Write client subscription (HART-IP) | session |

Commands 38, 48, 532 and 533 are not blocked by write protect (533 changes
the subscription of the HART-IP session, not the device). Unknown commands get
response code 64. Requests with too little data get code 5, bad values the
usual codes (2, 3, 4, 9-12, 14); a request with a bad checksum, for another
address, or malformed gets no answer.

## Fault injection

Faults are injected at runtime with `hart-sim inject` or through the
[control channel](#control-channel). Parameters are `KEY=VALUE`.

| kind | parameters | effect | example |
|---|---|---|---|
| `no_response` | `count=1` | the next `count` requests the device would answer get no answer; the device still carries the command out, only the reply is lost | `hart-sim inject PT-101 no_response count=3` |
| `delay` | `seconds`, `count` (default: forever) | answers are sent `seconds` late (per client, also on UDP) | `hart-sim inject PT-101 delay seconds=0.5` |
| `busy` | `count=1` | response code 32 (busy), no state change | `hart-sim inject PT-101 busy count=2` |
| `comm_error` | `flags=0x08`, `count=1` | communication error byte instead of a response code | `hart-sim inject PT-101 comm_error flags=0x10` |
| `sensor_failure` | none | device malfunction, PV out of limits and bad, alarm current, details in command 48 | `hart-sim inject PT-101 sensor_failure` |
| `saturate` | `direction=high` or `low` | loop current pinned to 20.5 / 3.8 mA, "loop current saturated" | `hart-sim inject PT-101 saturate direction=high` |
| `cold_start` | none | cold start bit set for both masters, as after a restart | `hart-sim inject PT-101 cold_start` |
| `more_status` | `bytes` (6 integers) | "more status available" until command 48 reads it; the bytes appear in its device specific status | `hart-sim inject PT-101 more_status bytes=1,2,3,4,5,6` |
| `drift` | `per_second` | the PV drifts away; `clear` removes it and keeps the offset already accumulated, `clear --reset` zeroes it | `hart-sim inject PT-101 drift per_second=0.5` |
| `burst_stop` | none | no burst frames are sent until cleared; the burst configuration stays | `hart-sim inject PT-101 burst_stop` |
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
{"device": "PT-101", "clear": "drift", "reset": true}
{"device": "PT-101", "set": {"pv": 123.4}}
{"list": true}
```

`set` with `null` returns the PV (or `sv`) to the simulated process. `clear`
with `"reset": true` (drift or all faults) also zeroes the drift offset.
`list` returns tag, faults, PV, loop current and polling address per device,
and a `burst` list with message number, command and period of every burst
message that is on (`"burst": [{"message": 0, "on": true, "command": 3,
"period": 1.0}]`). `pv` and `sv` values must satisfy |value| <= 3.4e38 (what a
HART float can carry); `NaN` and infinity show up as `null` in `list`.

`device` is the device's current tag; a device renamed with command 18 or 22
is found by its new tag, and still by the tag it started with unless another
device has taken that name.

The control channel binds `127.0.0.1` whatever `--host` is; use
`hart-sim serve --control-host ADDRESS` to listen elsewhere. A request line is
limited to 64 KiB (`{"ok": false, "error": "line too long"}`, then the
connection is closed).

**No authentication:** neither HART-IP nor the control port checks who
connects. Binding either to `0.0.0.0` (or any non-loopback address) exposes the
simulated devices to everyone who can reach the machine.

## Burst mode

Every device has three burst messages (0-2), off by default. A burst message
sends one of commands 1, 2, 3, 9, 33 or 48 as an unsolicited frame every
period. Burst frames reach a HART-IP client as a Publish message, but only
if the client has subscribed with command 533 (or the bench has
`subscribe_all: true`). Defaults per message: command 1, variable slots PV and
SV (PV only for a device without SV), period 1 s, maximum period 60 s, trigger
mode continuous.

| command | meaning |
|---|---|
| 103 | burst period and maximum period (seconds, at least 0.1; shorter values are raised, code 8) |
| 104 | trigger mode (continuous, window, rising, falling, on change), classification, unit, level |
| 105 | read the configuration of one message |
| 107 | device variable slots of a message (PV, SV, 250 = unused) |
| 108 | command number to burst |
| 109 | burst mode control: 0 off, 1, 3 and 4 on |
| 532 / 533 | read / write the subscription of this HART-IP session |

A subscription (533) carries a device unique ID (all zeros = every device) and
flags: `0x0001` process data (bursts of commands 1, 2, 3, 9, 33) and `0x0004`
device status (bursts of command 48); other bits are stored and read back but
never trigger a Publish. A session either subscribes to all devices or to
single ones, not both (response code 9). Subscriptions end with the session.

With a trigger other than continuous, a due message only sends when the first
variable meets the condition (window: moved by at least the level since the
last burst; rising: above the level; falling: below; on change: different);
after the maximum period it sends anyway.

On the command line, switch bursts on, watch them, and switch them off:

```console
$ hart-sim burst PT-101 --command 3 --period 0.5
burst on: PT-101 message 0 command 3 every 0.5 s
$ hart-sim listen PT-101 --count 3
23:42:23.180 PT-101 cmd 3 loop 10.96 mA | PV 434.8 kPa | SV 23.5 degC
23:42:23.680 PT-101 cmd 3 loop 11.01 mA | PV 438.4 kPa | SV 23.5 degC
23:42:24.180 PT-101 cmd 3 loop 11.06 mA | PV 441.5 kPa | SV 23.5 degC
$ hart-sim burst PT-101 --off
burst off: PT-101 message 0
```

`burst` takes `--command` and `--period` (both needed unless `--off`),
`--max-period`, `--message K` (0-2, default 0) and `--variables 0,1` (slot
codes, comma separated; without it the message keeps its slots). `listen`
subscribes, prints the decoded bursts (commands 1, 2, 3 and 48; other commands
as hex) with a local time stamp, `--status` adds the device status bursts
(command 48), and `--count N` stops after N bursts; it exits with 1 if no
burst arrives for 10 s. Without `--count`, Ctrl-C ends it. A bench started with
`serve --publish-all` sends the bursts of all devices to every session;
`listen` prints only those of the tag you named.

A profile can preconfigure up to three messages, in order:

```yaml
burst:
  - {enabled: true, command: 3, period: 0.5}
  - {command: 1, period: 1, trigger: window, level: 2.0, max_period: 30, variables: [0]}
```

Keys: `enabled` (default false), `command`, `period`, `max_period`,
`variables` (slot codes), `trigger` (`continuous`, `window`, `rising`,
`falling`, `on-change`) and `level`.

From Python, `DeviceHandle.burst()` configures and starts a message (commands
108, 107, 103, 104 and 109), `burst_off()` stops it, `burst_config()` reads it
with command 105, `DeviceHandle.subscribe()` subscribes this connection, and
`HartIPClient.wait_burst()` returns the next received burst. Bursts that
arrive while a request is waiting for its response are kept for `wait_burst`.

```python
from hart_sim.client import HartIPClient

with HartIPClient("127.0.0.1") as client:
    device = client.find("PT-101")
    device.subscribe()                          # command 533, process data
    device.burst(3, 0.5)                        # command 3 every 0.5 s
    for _ in range(5):
        publish = client.wait_burst(3, timeout=2.0)
        print(publish.frame.data.hex())
    device.burst_off()
```

`burst()` always sends command 104, with trigger continuous unless you pass
`trigger=` and `level=`; `variables=None` keeps the slots the device has.
The complete example is
[`examples/burst_listen.py`](https://github.com/steitec/hart-sim/blob/main/examples/burst_listen.py).

While any burst message is on, the burst bit (address bit 6) is set in every
reply of the device, including the reply to command 109 itself. `burst_stop`
keeps the bit and the configuration; only the sending pauses. The `delay` and
`no_response` faults do not affect bursts, `sensor_failure` shows in them, and
writes to commands 103-109 are refused with code 7 in write protect mode.
Burst frames never consume the cold start, configuration changed or more
status bits of a reply.

## Docker

For clients that are not written in Python: the image runs a pressure
transmitter (`PT-101`, polling address 1) and a temperature transmitter
(`TT-201`, polling address 2) behind one HART-IP port.

```bash
docker run --rm --name sim -p 5094:5094 -p 5094:5094/udp -p 127.0.0.1:5095:5095 ghcr.io/steitec/hart-sim
```

Publish both `5094` lines if your client uses both transports; Docker maps TCP
and UDP separately. The control channel has no authentication, hence
`127.0.0.1:` in front of it. It listens on all interfaces inside the container,
so anything on the same Docker network can reach it. Inject a fault without
installing anything, or send the [JSON lines](#control-channel) to port 5095
from your own test code:

```bash
docker exec sim hart-sim inject PT-101 sensor_failure
docker exec sim hart-sim clear PT-101
```

Your own bench file goes over the default one:

```bash
docker run --rm -p 5094:5094 -v ./bench.yaml:/bench/bench.yaml ghcr.io/steitec/hart-sim
```

The image is built with every release and tagged with the version and `latest`;
pin the version in CI. It reports healthy once the HART-IP port accepts
connections, so a service container in GitHub Actions is ready when your job starts.

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
answers raise `HartError` / `HartTimeout`. For bursts see
[Burst mode](#burst-mode).

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

- HART-IP version 1 only, unencrypted (no version 2, no TLS). No serial HART
  (no FSK), no events (commands 115-119), no device descriptions (DD), no
  WirelessHART.
- The byte layouts of the commands come from public sources, not from the
  paid HART specification; layouts that no source settles are marked in the
  code. The simulator is not registered with FieldComm Group, uses a made-up
  manufacturer ID and is for development and testing only.
- A TCP client that is subscribed but stops reading can delay the bursts to
  all sessions until its inactivity timeout drops it.

## Behaviour notes

- The long address carries 14 bits of the expanded device type (`0xFA01` is
  addressed as `0x3A01`).
- Unit code 1 is inH2O at 68 degF.
- The shortest burst period is 0.1 s (real devices are usually at 0.5 s or
  slower); command 103 raises shorter values and answers with code 8.
- Command 37 keeps the span (the upper range value moves with the lower one).
  What a real device does at the sensor limit is not verified: here the upper
  range value is clipped at the upper sensor limit and the reply is code 14.
  Commands 36 and 35 apply the usual limit checks.
- The polling address does not fix the loop current. Fixed 4 mA (multidrop)
  is set by command 6 or by `loop_current: fixed` in the profile; this
  deviates from the wording of the HART specification, which ties multidrop to
  a polling address other than 0.
- Replies to short (HART 5 form) 107, 108 and 109 requests use the full HART 7
  layout.
- `no_response` is consumed only by requests the device would answer; the
  command is still carried out.
- `delay` and `no_response` affect replies only, not bursts. `burst_stop`
  pauses them.
- The control channel and the bench address a device by its current tag.
- Wireshark 4.4 decodes all frames of the HART-IP dissector that hart-sim
  sends, including Publish, burst frames and command 533; CI checks this with
  `pytest -m tshark` where tshark is installed.

## Licence

MIT, see [LICENSE](https://github.com/steitec/hart-sim/blob/main/LICENSE).
