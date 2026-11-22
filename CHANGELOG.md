# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## 0.2.0 - unreleased

### Added

- Burst mode: three burst messages per device with commands 103, 104, 105, 107, 108 and 109,
  trigger modes (continuous, window, rising, falling, on change), burstable commands 1, 2, 3,
  9, 33 and 48, burst bit in all replies while a message is on, and a `burst:` block in
  profiles
- HART-IP client subscriptions (commands 532 and 533) and Publish messages; the bench key
  `subscribe_all` and `hart-sim serve --publish-all` send bursts to every session
- Client: `wait_burst()`, `DeviceHandle.subscribe()`, `burst()`, `burst_off()`, `burst_config()`;
  `wait_burst()` sends Keep Alive at half the negotiated inactivity time (new `inactivity_ms`
  parameter, default 60000), so a listening session and its subscription do not expire
- CLI: `hart-sim burst` and `hart-sim listen`; `hart-sim clear --reset` (and `"reset": true` in
  the control channel) zeroes the drift offset
- Fault `burst_stop`; `list` shows the running burst messages
- Profile key `loop_current: fixed` (multidrop, 4 mA)
- HART-IP over IPv6 (`--host ::`, `--host ::1`)
- Expanded command numbers and burst frames in the frame codec; example
  `examples/burst_listen.py`; Wireshark cross-check of Publish, burst frames and command 533

### Fixed

- Command 37 keeps the span instead of moving only the lower range value
- A `delay` fault on UDP no longer delays the other UDP clients
- `no_response` is consumed only by requests the device would answer
- The control channel and `Bench.find` find a device renamed with command 18 or 22
- `list` no longer produces invalid JSON for a NaN process value
- `Bench.stop` stops the server even if stopping the control channel fails

## 0.1.2 - 2026-11-09

- Port 0: when TCP and UDP cannot share the free port, further tries pick a random port.
  Windows can block 200 consecutive ports for one protocol, which ten tries in a row never left

## 0.1.1 - 2026-11-08

- Docker image `ghcr.io/steitec/hart-sim`, built with every release: HART-IP on TCP and
  UDP port 5094, control channel on 5095, unprivileged user, health check

## 0.1.0 - 2026-10-28

- HART-IP version 1 server (TCP and UDP, port 5094) with simulated pressure and
  temperature transmitters (HART revision 7), universal and common practice
  commands, per-master configuration-changed and cold-start bits
- Process model with 4-20 mA loop current, saturation and NAMUR NE 43 alarm currents
- YAML profiles and bench files (several devices behind one server)
- Fault injection (`no_response`, `delay`, `busy`, `comm_error`, `sensor_failure`,
  `saturate`, `cold_start`, `more_status`, `drift`, `write_protect`) through a JSON control
  channel and `hart-sim inject`
- Command line (`serve`, `poll`, `cmd`, `inject`, `clear`, `set`, `profiles`) and a
  HART-IP client in Python
- pytest plugin with the `hart_bench` and `hart_bench_factory` fixtures
- Cross-check of the wire format with the Wireshark HART-IP dissector (`pytest -m tshark`)
