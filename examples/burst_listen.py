"""Example: receive burst messages from a simulated device with the hart-sim client.

Start a device, then run this script:

    hart-sim serve pressure
    python examples/burst_listen.py [HOST [PORT]]

It switches burst mode on (command 3, twice a second), subscribes this connection and
prints five bursts. The pytest plugin version is in examples/test_example.py.
"""
import sys

from hart_sim import units
from hart_sim.client import HartIPClient
from hart_sim.frame import unpack_float


def listen(client, address, count=5, period=0.5):
    """Burst command 3 of the device at the polling `address`; return `count` decoded lines."""
    device = client.handle(address)
    device.subscribe()                       # command 533: send this connection the bursts
    device.burst(3, period)                  # command 3 every `period` seconds, message 0
    lines = []
    try:
        for _ in range(count):
            data = client.wait_burst(3, timeout=5.0).frame.data
            loop_ma, pv_unit, pv = unpack_float(data[0:4]), data[4], unpack_float(data[5:9])
            lines.append(f"loop {loop_ma:.2f} mA | PV {pv:.1f} {units.name(pv_unit)}")
    finally:
        device.burst_off()
    return lines


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 5094
    with HartIPClient(host, port) as hart:
        for line in listen(hart, 0):
            print(line)
