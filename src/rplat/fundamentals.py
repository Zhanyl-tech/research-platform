"""Fundamentals access, resolved to the belief held on the query date."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import pandas as pd

from rplat.clock import require_date
from rplat.store.store import Store
from rplat.types import Dataset


def get_fundamentals(
    store: Store,
    as_of_date: date,
    *,
    security_ids: Sequence[str] | None = None,
    metrics: Sequence[str] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Return fundamentals as they were understood at the end of ``as_of_date``.

    Two lags matter and they are different.

    The first is publication: a quarter ending 30 June is not knowable until it
    is filed, typically four to six weeks later. Joining a fundamental to its
    ``period_end`` — which is what a naive merge on date does — hands the model
    a number weeks before anyone had it.

    The second is restatement: the value filed in August is not always the value
    that stands in November. Today's database shows the corrected figure with no
    trace of what was originally reported, so a backtest reading it is trading
    on a number that did not exist at the time. The fixture restates Ardent's
    Q2-2022 EPS from 1.50 to 1.20 precisely so this is testable.

    Both are handled by the same as-of resolution: filter to filings already
    made, then keep the most recent filing per fact.

    Args:
        store: Bitemporal store.
        as_of_date: A :class:`datetime.date`; see :mod:`rplat.clock`.
        security_ids: Restrict to these securities.
        metrics: Restrict to these metrics.
        start: Earliest ``period_end`` to return (inclusive).
        end: Latest ``period_end`` to return (inclusive).

    ``security_id``, ``metric`` and ``period_end`` are all fact-key columns, so
    each filter is pushed into SQL ahead of the as-of window without changing
    which revision of a surviving fact wins.
    """
    require_date(as_of_date)
    key_in = {"metric": metrics} if metrics is not None else None
    frame = store.as_of(
        Dataset.FUNDAMENTALS,
        as_of_date,
        security_ids=security_ids,
        key_in=key_in,
        key_between={"period_end": (start, end)},
    )
    if frame.empty:
        return frame
    return frame.sort_values(["security_id", "period_end", "metric"]).reset_index(drop=True)


def latest_known(
    store: Store,
    as_of_date: date,
    metric: str,
    *,
    security_ids: Sequence[str] | None = None,
) -> pd.DataFrame:
    """The most recent *reported period* for one metric, per security.

    This is the shape a cross-sectional feature usually wants: one number per
    name, reflecting the newest filing available on the date — not the newest
    fiscal period, which may not have been filed yet.
    """
    frame = get_fundamentals(store, as_of_date, security_ids=security_ids, metrics=[metric])
    if frame.empty:
        return frame
    newest = frame.sort_values(["security_id", "period_end"]).groupby("security_id").tail(1)
    return newest.reset_index(drop=True)


def restatement_history(
    store: Store, security_id: str, period_end: date, metric: str
) -> pd.DataFrame:
    """Every value ever reported for one fact, in filing order.

    The audit trail that answers "why is this number this number" — which is the
    question the planned lineage layer (Phase 2) would generalise to computed
    features.
    """
    return store.revisions(
        Dataset.FUNDAMENTALS,
        security_id=security_id,
        period_end=period_end,
        metric=metric,
    )
