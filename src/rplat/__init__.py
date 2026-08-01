"""rplat — a point-in-time research platform.

Phase 1 provides the data layer: an append-only bitemporal store, a swappable
source interface, and query functions that reconstruct exactly what a researcher
could have known on a given date.
"""

from __future__ import annotations

from rplat.fundamentals import get_fundamentals, latest_known, restatement_history
from rplat.prices import get_bars, get_corporate_actions
from rplat.store.store import Store
from rplat.types import (
    ActionType,
    BarRecord,
    CorporateActionRecord,
    Dataset,
    FundamentalRecord,
    SecurityRecord,
    TickerRecord,
)
from rplat.universe import get_universe, resolve_ticker

__version__ = "0.1.0"

__all__ = [
    "ActionType",
    "BarRecord",
    "CorporateActionRecord",
    "Dataset",
    "FundamentalRecord",
    "SecurityRecord",
    "Store",
    "TickerRecord",
    "__version__",
    "get_bars",
    "get_corporate_actions",
    "get_fundamentals",
    "get_universe",
    "latest_known",
    "resolve_ticker",
    "restatement_history",
]
