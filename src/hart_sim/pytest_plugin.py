"""pytest plugin: a fresh simulated HART bench for every test.

Fixtures:

- ``hart_bench`` -- the bench from ``--hart-bench FILE`` or the ini key ``hart_bench``
  (relative to the rootdir); without either, one ``pressure`` device at polling
  address 0. Started on free ports, stopped after the test. ``hart_bench.client()``
  returns a connected :class:`~hart_sim.client.HartIPClient`.
- ``hart_bench_factory`` -- ``factory(path_or_dict_or_profile)`` for more benches:
  a bench file, a bench dict, or the name of a bundled profile (one device at
  polling address 0). Relative paths are relative to the rootdir, like the ini key.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from .bench import Bench, BenchError
from .profile import ProfileError, bundled_profiles

DEFAULT_PROFILE = "pressure"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("hart-sim")
    group.addoption("--hart-bench", metavar="FILE",
                    help="bench file (YAML) for the hart_bench fixture")
    parser.addini("hart_bench", "bench file (YAML) for the hart_bench fixture, "
                  "relative to the rootdir")


class BenchPool:
    """Benches started for one test; all of them are stopped at the end."""

    def __init__(self, root: Path | None):
        self.root = root
        self.benches: list[Bench] = []

    def start(self, source: str | Path | dict) -> Bench:
        message = None
        try:
            if isinstance(source, dict):
                bench = Bench.from_dict(source)
            elif isinstance(source, str) and source in bundled_profiles():
                bench = Bench.single(source)
            else:
                path = Path(source)
                if self.root is not None and not path.is_absolute():
                    path = self.root / path
                bench = Bench.from_yaml(path)
            bench.start(port=0, control_port=0)
        except (BenchError, ProfileError, OSError) as error:
            message = f"hart-sim bench: {error}"
        if message is not None:
            pytest.fail(message, pytrace=False)
        self.benches.append(bench)
        return bench

    def stop_all(self) -> None:
        """Stop every bench; re-raise the first failure afterwards."""
        failure = None
        for bench in self.benches:
            try:
                bench.stop()
            except Exception as error:  # keep stopping the others
                failure = failure or error
        self.benches.clear()
        if failure is not None:
            raise failure


def _configured(config: pytest.Config) -> Path | str:
    option = config.getoption("--hart-bench")
    if option:
        return config.invocation_params.dir / option
    ini = config.getini("hart_bench")
    if ini:
        return config.rootpath / ini
    return DEFAULT_PROFILE


@pytest.fixture
def hart_bench_factory(request: pytest.FixtureRequest):
    pool = BenchPool(request.config.rootpath)
    yield pool.start
    pool.stop_all()


@pytest.fixture
def hart_bench(request: pytest.FixtureRequest):
    pool = BenchPool(None)  # _configured() already resolved the path
    yield pool.start(_configured(request.config))
    pool.stop_all()
