"""Price access, with split adjustment computed as of the query date."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date

import pandas as pd

from rplat.clock import require_date
from rplat.store.store import Store
from rplat.types import ActionType, Dataset

BAR_COLUMNS = ["security_id", "effective_date", "open", "high", "low", "close", "volume"]


def get_bars(
    store: Store,
    as_of_date: date,
    *,
    security_ids: Sequence[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    adjust: bool = True,
) -> pd.DataFrame:
    """Return bars knowable by the end of ``as_of_date``, optionally split-adjusted.

    Two distinct point-in-time effects are handled here, and conflating them is
    a common way to leak the future into a backtest.

    **Availability.** A bar appears only once its ``knowledge_date`` has passed.
    The fixture's late backfill makes this visible: five sessions in May 2023
    arrived a week after they happened, so a pipeline running on the session
    date correctly sees nothing, while the same query run later sees all five.

    **Adjustment.** A split is folded into prices only when it is both
    *announced* (``knowledge_date <= as_of_date``) and *effective* (ex-date
    ``<= as_of_date``). The first condition is the one vendor "adjusted close"
    columns get wrong for research. They are adjusted with every split up to
    today, so a 2019 price in a file downloaded now has been divided by a factor
    nobody could have applied in 2019. The second condition stops the opposite
    error. Between announcement and ex-date there is no discontinuity to remove
    yet, so dividing history by the ratio would leave returns unchanged but
    report price *levels* that never traded: the latest close halved for weeks
    before a 2-for-1. A split that is known but not yet effective is still
    visible, through :func:`get_corporate_actions`.

    One invariant follows, *provided the ex-date session's own bar is visible*:
    the most recent bar visible as of any date has ``adjustment_factor == 1``,
    so its adjusted close is the price that actually traded. The test suite
    checks it on a grid of dates over the fixture, where that proviso always
    holds. When the ex-date bar arrives late (a backfill), the split is still
    applied from the ex-date, because the shares already trade on the new
    basis, so the latest *visible* bar is a pre-split session expressed in
    post-split terms: a 100.00 close shows as 50.00 with factor 2.0 until the
    ex-date bar lands. That is not a price that traded. Code that reads a
    current price level from the latest bar (market cap, price filters) should
    check ``adjustment_factor == 1`` or compare against ``adjust=False``.

    Args:
        store: Bitemporal store.
        as_of_date: Reconstruct the view available at the end of this date.
            Must be a :class:`datetime.date`; see :mod:`rplat.clock`.
        security_ids: Restrict to these securities.
        start: Earliest session to return (inclusive).
        end: Latest session to return (inclusive).
        adjust: Apply split adjustment. Dividends are stored and returned by
            :func:`get_corporate_actions` but are not folded into prices —
            total-return adjustment is a modelling choice, not a data fix, so it
            belongs to the feature layer rather than here.

    Returns:
        Bars sorted by security and session. When ``adjust`` is True, an
        ``adjustment_factor`` column shows exactly what was divided out.
    """
    require_date(as_of_date)
    if start is not None:
        require_date(start, "start")
    if end is not None:
        require_date(end, "end")

    # The session range is pushed into SQL ahead of the as-of window.
    # effective_date is part of the bars fact key, so this drops whole
    # partitions and cannot change which revision of a surviving bar wins.
    # Filtering in pandas instead materialised the entire history first.
    bars = store.as_of(
        Dataset.BARS,
        as_of_date,
        security_ids=security_ids,
        key_between={"effective_date": (start, end)},
    )
    if bars.empty:
        return pd.DataFrame(columns=[*BAR_COLUMNS, "adjustment_factor"])

    bars = bars.reindex(columns=BAR_COLUMNS).sort_values(["security_id", "effective_date"])
    if not adjust:
        return bars.reset_index(drop=True)

    factors = _split_factors(store, as_of_date, security_ids=security_ids)
    bars = _apply_split_factors(bars, factors)
    return bars.reset_index(drop=True)


def get_corporate_actions(
    store: Store, as_of_date: date, *, security_ids: Sequence[str] | None = None
) -> pd.DataFrame:
    """Corporate actions announced on or before ``as_of_date``, effective or not.

    This is the channel for "known but not yet effective": a split announced
    on 2022-08-25 with an ex-date of 2022-09-19 is returned here from the 25th,
    while :func:`get_bars` only adjusts for it from the 19th.
    """
    return store.as_of(Dataset.CORPORATE_ACTIONS, as_of_date, security_ids=security_ids)


def _split_factors(
    store: Store, as_of_date: date, *, security_ids: Sequence[str] | None
) -> pd.DataFrame:
    """Splits both announced and effective by the query date, one row per ex-date."""
    splits = store.as_of(
        Dataset.CORPORATE_ACTIONS,
        as_of_date,
        security_ids=security_ids,
        key_in={"action_type": [ActionType.SPLIT.value]},
        # Known is not enough: the ex-date must have arrived too. See get_bars.
        key_between={"effective_date": (None, as_of_date)},
    )
    return splits.reindex(columns=["security_id", "effective_date", "ratio"])


def _apply_split_factors(bars: pd.DataFrame, splits: pd.DataFrame) -> pd.DataFrame:
    """Back-adjust prices for splits with an ex-date after each session.

    Standard back-adjustment: a session's prices are divided by the product of
    every split ratio taking effect *after* that session, so the series is
    continuous when read backwards from the as-of date. Volume is multiplied by
    the same factor, since share count moves the other way.

    A missing, zero, negative or non-finite ratio raises. The store rejects
    such splits at append time, so reaching this means a store written some
    other way. Treating the ratio as 1 would silently skip a split, and letting
    NaN through used to poison the whole frame and crash on the volume cast.
    """
    bars = bars.copy()
    bars["adjustment_factor"] = 1.0
    if splits.empty:
        return bars

    for _, split in splits.iterrows():
        raw_ratio = split["ratio"]
        ratio = float(raw_ratio) if not pd.isna(raw_ratio) else math.nan
        if not math.isfinite(ratio) or ratio <= 0:
            ex_date = pd.Timestamp(split["effective_date"]).date()
            raise ValueError(
                f"split for {split['security_id']} with ex-date {ex_date} has ratio "
                f"{raw_ratio!r}; a split needs a finite ratio > 0"
            )
        if ratio == 1.0:
            continue
        affected = (bars["security_id"] == split["security_id"]) & (
            bars["effective_date"] < split["effective_date"]
        )
        bars.loc[affected, "adjustment_factor"] *= ratio

    for column in ("open", "high", "low", "close"):
        bars[column] = bars[column] / bars["adjustment_factor"]
    bars["volume"] = (bars["volume"] * bars["adjustment_factor"]).round().astype("int64")
    return bars
