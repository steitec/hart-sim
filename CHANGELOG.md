# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

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
