"""Data sources. Implement DataSource to add a vendor."""

from __future__ import annotations

from rplat.sources.base import DataSource
from rplat.sources.fixture import FixtureSource
from rplat.sources.stooq import StooqFetchError, StooqSource

__all__ = ["DataSource", "FixtureSource", "StooqFetchError", "StooqSource"]
