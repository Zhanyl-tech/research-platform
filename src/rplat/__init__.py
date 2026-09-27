"""rplat — a point-in-time research platform.

Phase 1 provides the data layer: an append-only bitemporal store, a swappable
source interface, and query functions that reconstruct exactly what a researcher
could have known by the end of a given date (the precise contract is in
:mod:`rplat.clock`). Feature lineage, leakage detection, evaluation and
scale-out are later phases and do not exist yet.
"""

from __future__ import annotations

from rplat.clock import EXCHANGE_TZ, as_of_for_decision
from rplat.fundamentals import get_fundamentals, latest_known, restatement_history
from rplat.prices import get_bars, get_corporate_actions
from rplat.store.store import Store, StoreBusyError, StoreError, StoreSchemaError
from rplat.store.validate import RecordValidationError
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
    "EXCHANGE_TZ",
    "ActionType",
    "BarRecord",
    "CorporateActionRecord",
    "Dataset",
    "FundamentalRecord",
    "RecordValidationError",
    "SecurityRecord",
    "Store",
    "StoreBusyError",
    "StoreError",
    "StoreSchemaError",
    "TickerRecord",
    "__version__",
    "as_of_for_decision",
    "get_bars",
    "get_corporate_actions",
    "get_fundamentals",
    "get_universe",
    "latest_known",
    "resolve_ticker",
    "restatement_history",
]
