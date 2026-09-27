"""Shared fixtures."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from rplat.sources.fixture import FixtureSource
from rplat.store.store import Store


@pytest.fixture(scope="session")
def source() -> FixtureSource:
    """The deterministic synthetic vendor."""
    return FixtureSource()


@pytest.fixture(scope="session")
def store(source: FixtureSource) -> Iterator[Store]:
    """An in-memory store loaded with the fixture.

    Session-scoped because every test here is read-only — the store is
    append-only and no test mutates it, so rebuilding per test would only make
    the suite slower without isolating anything.
    """
    with Store.open(None) as loaded:
        loaded.ingest(source)
        yield loaded


@pytest.fixture(scope="session")
def store_file(tmp_path_factory: pytest.TempPathFactory, source: FixtureSource) -> Path:
    """A file-backed store built from the fixture, closed and ready to reopen.

    For tests that need a real file: read-only opens, several processes, the
    CLI. Tests must not write to it; build a private copy for that.
    """
    path = tmp_path_factory.mktemp("stores") / "fixture.duckdb"
    with Store.open(path) as built:
        built.ingest(source)
    return path


#: Opens a DuckDB file for writing, says so, then waits to be killed.
_HOLDER = (
    "import sys, time, duckdb\n"
    "conn = duckdb.connect(sys.argv[1])\n"
    "print('held', flush=True)\n"
    "time.sleep(120)\n"
)


@pytest.fixture
def hold_open() -> Iterator[Callable[[Path], Callable[[], None]]]:
    """Start another process holding a file open for writing; returns its release.

    DuckDB's file lock is per process, so a second connection in this process
    would not reproduce what a second job on the same store sees. Anything
    still held when the test ends is released then.
    """
    holders: list[subprocess.Popen[str]] = []

    def release(holder: subprocess.Popen[str]) -> None:
        holder.kill()
        holder.wait()

    def start(path: Path) -> Callable[[], None]:
        # Our own interpreter running a constant script: no untrusted input.
        holder = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", _HOLDER, str(path)], stdout=subprocess.PIPE, text=True
        )
        holders.append(holder)
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held", "holder never opened the file"
        return lambda: release(holder)

    yield start
    for holder in holders:
        release(holder)
