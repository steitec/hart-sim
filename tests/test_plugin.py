"""The pytest plugin, exercised in separate pytest runs (pytester).

The plugin is loaded through its installed entry point, like for a user.
"""
from pathlib import Path

PLANT = Path(__file__).parent.parent / "examples" / "plant.yaml"


def test_default_bench_answers_command_0(pytester):
    pytester.makepyfile("""
        from hart_sim.frame import Address

        def test_it(hart_bench):
            assert list(hart_bench.devices) == ["PT-101"]
            with hart_bench.client() as client:
                frame = client.command(Address.short(0), 0)
            assert frame.response_code == 0
            assert frame.data[0] == 254 and frame.data[4] == 7
    """)
    pytester.runpytest().assert_outcomes(passed=1)


def test_client_is_connected_and_closed_with_bench(pytester):
    pytester.makepyfile("""
        def test_it(hart_bench):
            client = hart_bench.client()
            handle = client.handle(0)
            assert handle.read_variables().pv_unit == 12
            hart_bench.stop()
            assert client._sock is None
    """)
    pytester.runpytest().assert_outcomes(passed=1)


def test_option_gives_two_devices(pytester):
    pytester.makepyfile("""
        def test_it(hart_bench):
            assert set(hart_bench.devices) == {"PT-101", "TT-201"}
            with hart_bench.client() as client:
                assert [i.tag for i in client.poll(range(4))] == ["PT-101", "TT-201"]
    """)
    pytester.runpytest("--hart-bench", str(PLANT)).assert_outcomes(passed=1)


def test_bench_from_ini_relative_to_rootdir(pytester):
    pytester.makefile(".yaml", bench=PLANT.read_text())
    pytester.makeini("[pytest]\nhart_bench = bench.yaml\n")
    pytester.makepyfile("""
        def test_it(hart_bench):
            assert set(hart_bench.devices) == {"PT-101", "TT-201"}
    """)
    pytester.runpytest().assert_outcomes(passed=1)


def test_option_wins_over_ini(pytester):
    pytester.makefile(".yaml", bench=PLANT.read_text())
    pytester.makeini("[pytest]\nhart_bench = missing.yaml\n")
    pytester.makepyfile("""
        def test_it(hart_bench):
            assert len(hart_bench.devices) == 2
    """)
    pytester.runpytest("--hart-bench", "bench.yaml").assert_outcomes(passed=1)


def test_fresh_bench_per_test_and_fault(pytester):
    pytester.makepyfile("""
        def test_one(hart_bench):
            hart_bench.devices["PT-101"].faults.inject("sensor_failure")

        def test_two(hart_bench):
            assert not hart_bench.devices["PT-101"].faults.active("sensor_failure")
    """)
    pytester.runpytest().assert_outcomes(passed=2)


def test_factory_takes_dict_profile_and_file(pytester):
    pytester.makefile(".yaml", bench=PLANT.read_text())
    pytester.makepyfile("""
        def test_it(hart_bench, hart_bench_factory):
            a = hart_bench_factory("temperature")
            b = hart_bench_factory("bench.yaml")
            c = hart_bench_factory({"devices": [
                {"profile": "pressure", "polling_address": 3, "device_id": 9}]})
            assert len(b.devices) == 2 and len(c.devices) == 1
            ports = {x.server.port for x in (hart_bench, a, b, c)}
            assert len(ports) == 4
            with a.client() as client:
                assert client.handle(0).read_variables().pv_unit == 32
    """)
    pytester.runpytest().assert_outcomes(passed=1)


def test_benches_are_stopped_after_the_test(pytester):
    pytester.makepyfile("""
        import socket
        ports = []

        def test_one(hart_bench):
            ports.append(hart_bench.server.port)

        def test_two():
            s = socket.socket()
            s.settimeout(1)
            assert s.connect_ex(("127.0.0.1", ports[0])) != 0
            s.close()
    """)
    pytester.runpytest().assert_outcomes(passed=2)


def test_bad_bench_file_fails_with_message(pytester):
    pytester.makepyfile("""
        def test_it(hart_bench):
            pass
    """)
    result = pytester.runpytest("--hart-bench", "nope.yaml")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*hart-sim bench*nope.yaml*"])
