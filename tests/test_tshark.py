"""Cross-check against the Wireshark HART-IP dissector.

A real client session (commands 0, 3, 35, 13) runs against a bench; every HART-IP
message is recorded and written as a classic pcap file (Ethernet / IPv4 / UDP,
port 5094), so no capture privileges are needed. ``tshark`` must decode all of it
without "Malformed" entries. Run with ``pytest -m tshark``; skipped without tshark.
"""
import shutil
import socket
import struct
import subprocess

import pytest

from hart_sim import hartip
from hart_sim.bench import Bench
from hart_sim.client import HartIPClient
from hart_sim.frame import Address, pack_float

pytestmark = [pytest.mark.tshark,
              pytest.mark.skipif(shutil.which("tshark") is None, reason="tshark not installed")]

CLIENT_PORT = 40000
MAC_CLIENT, MAC_SERVER = bytes.fromhex("020000000001"), bytes.fromhex("020000000002")


class _Recorder:
    """Socket proxy that logs every datagram sent and received."""

    def __init__(self, sock, log):
        self._sock, self._log = sock, log

    def sendall(self, data):
        self._log.append(("out", bytes(data)))
        return self._sock.sendall(data)

    def recv(self, size):
        data = self._sock.recv(size)
        self._log.append(("in", bytes(data)))
        return data

    def __getattr__(self, name):
        return getattr(self._sock, name)


class RecordingClient(HartIPClient):
    def __init__(self, *args, **kw):
        super().__init__(*args, transport="udp", **kw)
        self.log = []

    def _exchange(self, msg_id, body, timeout):
        if self._sock is not None and not isinstance(self._sock, _Recorder):
            self._sock = _Recorder(self._sock, self.log)
        return super()._exchange(msg_id, body, timeout)


def _checksum(header: bytes) -> int:
    total = sum(struct.unpack(">10H", header))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def _packet(payload: bytes, outgoing: bool, ident: int) -> bytes:
    src_port, dst_port = (CLIENT_PORT, hartip.PORT) if outgoing else (hartip.PORT, CLIENT_PORT)
    udp = struct.pack(">HHHH", src_port, dst_port, 8 + len(payload), 0) + payload
    ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), ident, 0, 64, 17, 0,
                     socket.inet_aton("127.0.0.1"), socket.inet_aton("127.0.0.1"))
    ip = ip[:10] + struct.pack(">H", _checksum(ip[:10] + b"\0\0" + ip[12:])) + ip[12:]
    macs = MAC_SERVER + MAC_CLIENT if outgoing else MAC_CLIENT + MAC_SERVER
    return macs + b"\x08\x00" + ip + udp


def write_pcap(path, log) -> None:
    out = bytearray(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
    for i, (direction, payload) in enumerate(log):
        frame = _packet(payload, direction == "out", i + 1)
        out += struct.pack("<IIII", 1_000_000_000 + i, 0, len(frame), len(frame)) + frame
    path.write_bytes(bytes(out))


def tshark(*args) -> str:
    done = subprocess.run(["tshark", *args], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.fixture(scope="module")
def pcap(tmp_path_factory):
    with Bench.single("pressure") as bench:
        client = RecordingClient("127.0.0.1", bench.server.port, timeout=2.0)
        with client:
            first = client.command(Address.short(0), 0)
            address = Address.long(int.from_bytes(first.data[1:3], "big"),
                                   int.from_bytes(first.data[9:12], "big"))
            client.command(address, 3)
            reply = client.command(address, 35, bytes([12]) + pack_float(600.0) + pack_float(0.0))
            assert reply.response_code == 0
            client.command(address, 13)
    path = tmp_path_factory.mktemp("pcap") / "session.pcap"
    write_pcap(path, client.log)
    return path, len(client.log)


def test_dissector_finds_every_message_without_malformed(pcap):
    path, count = pcap
    verbose = tshark("-r", str(path), "-V")
    assert "Malformed" not in verbose
    assert tshark("-r", str(path), "-Y", "_ws.malformed").strip() == ""
    rows = tshark("-r", str(path), "-Y", "hart_ip", "-T", "fields", "-e", "hart_ip.message_type",
                  "-e", "hart_ip.message_id").splitlines()
    assert len(rows) == count == 12  # initiate, 4 commands, close: request + response each
    assert rows[0] == "0\t0" and rows[1] == "1\t0"   # session initiate
    assert rows[-2] == "0\t1" and rows[-1] == "1\t1"  # session close


def _rows(path, *names):
    args = ["-r", str(path), "-Y", "hart_ip.pt.command", "-T", "fields"]
    for name in names:
        args += ["-e", name]
    return [line.split("\t") for line in tshark(*args).splitlines()]


def test_dissector_decodes_commands(pcap):
    rows = _rows(pcap[0], "hart_ip.pt.command", "hart_ip.pt.response_code")
    # request (no response code) and response for each command
    assert rows == [["0", ""], ["0", "0"], ["3", ""], ["3", "0"],
                    ["35", ""], ["35", "0"], ["13", ""], ["13", "0"]]


def test_dissector_decodes_identity_values(pcap):
    names = ["hart_ip.pt.rsp.expanded_device_type", "hart_ip.pt.rsp.hart_univ_rev",
             "hart_ip.pt.rsp.manufacturer_Id", "hart_ip.pt.rsp.device_id"]
    row = _rows(pcap[0], *names)[1]
    assert row == ["0xfa01", "7", "250", "000001"]


def test_dissector_decodes_tag_and_date(pcap):
    names = ["hart_ip.pt.rsp.tag", "hart_ip.pt.rsp.descriptor", "hart_ip.pt.rsp.day",
             "hart_ip.pt.rsp.month", "hart_ip.pt.rsp.year"]
    row = _rows(pcap[0], *names)[7]
    assert row[0].strip() == "PT-101" and row[1].strip() == "SIM PRESSURE"
    assert (row[2], row[3], row[4]) == ("1", "1", "126")


def test_payloads_of_commands_3_and_35(pcap):
    rows = _rows(pcap[0], "hart_ip.pt.command", "hart_ip.pt.payload")
    # command 3 response (raw bytes): loop current, PV unit, PV, SV unit, SV
    payload3 = bytes.fromhex(rows[3][1])
    assert len(payload3) == 14 and payload3[4] == 12 and payload3[9] == 32
    # command 35 request: unit kPa, URV 600.0, LRV 0.0 (IEEE 754 single, big endian)
    assert rows[4][1] == "0c4416000000000000"
