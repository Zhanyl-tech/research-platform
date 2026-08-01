"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Iterator

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
