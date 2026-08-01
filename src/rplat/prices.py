"""Price access, with split adjustment computed as of the query date."""

from __future__ import annotations

from datetime import date

import pandas as pd

from rplat.store.store import Store
from rplat.types import ActionType, Dataset

BAR_COLUMNS = ["security_id", "effective_date", "open", "high", "low", "close", "volume"]


def get_bars(
    store: Store,
    as_of_date: date,
    *,
    security_ids: list[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    adjust: bool = True,
) -> pd.DataFrame:
    """Return bars knowable on ``as_of_date``, optionally split-adjusted.

    Two distinct point-in-time effects are handled here, and conflating them is
    a common way to leak the future into a backtest.

    **Availability.** A bar appears only once its ``knowledge_date`` has passed.
    The fixture's late backfill makes this visible: five sessions in May 2023
    arrived a week after they happened, so a pipeline running on the session
    date correctly sees nothing, while the same query run later sees all five.

    **Adjustment.** The adjustment factor is built *only* from corporate actions
    whose announcement was knowable by ``as_of_date``. This is the part that
    vendor "adjusted close" columns get wrong for research: those are adjusted
    with every split up to today, so a 2019 price in a file downloaded now has
    been divided by a factor nobody could have applied in 2019. Feeding that
    into a momentum signal computed as of 2019 is look-ahead, and it is
    invisible because the series looks perfectly well-behaved.

    Args:
        store: Bitemporal store.
        as_of_date: Reconstruct the view available on this date.
        security_ids: Restrict to these securities.
        start: Earliest session to return.
        end: Latest session to return.
        adjust: Apply split adjustment. Dividends are stored and returned by
            :func:`get_corporate_actions` but are not folded into prices —
            total-return adjustment is a modelling choice, not a data fix, so it
            belongs to the feature layer rather than here.

    Returns:
        Bars sorted by security and session. When ``adjust`` is True, an
        ``adjustment_factor`` column shows exactly what was divided out.
    """
    bars = store.as_of(Dataset.BARS, as_of_date, security_ids=security_ids)
    if bars.empty:
        return pd.DataFrame(columns=[*BAR_COLUMNS, "adjustment_factor"])

    if start is not None:
        bars = bars[bars["effective_date"] >= pd.Timestamp(start)]
    if end is not None:
        bars = bars[bars["effective_date"] <= pd.Timestamp(end)]

    bars = bars.reindex(columns=BAR_COLUMNS).sort_values(["security_id", "effective_date"])
    if not adjust:
        return bars.reset_index(drop=True)

    factors = _split_factors(store, as_of_date, security_ids=security_ids)
    bars = _apply_split_factors(bars, factors)
    return bars.reset_index(drop=True)


def get_corporate_actions(
    store: Store, as_of_date: date, *, security_ids: list[str] | None = None
) -> pd.DataFrame:
    """Corporate actions announced on or before ``as_of_date``."""
    return store.as_of(Dataset.CORPORATE_ACTIONS, as_of_date, security_ids=security_ids)


def _split_factors(
    store: Store, as_of_date: date, *, security_ids: list[str] | None
) -> pd.DataFrame:
    """Splits knowable as of the query date, one row per (security, ex-date)."""
    actions = store.as_of(Dataset.CORPORATE_ACTIONS, as_of_date, security_ids=security_ids)
    if actions.empty:
        return pd.DataFrame(columns=["security_id", "effective_date", "ratio"])
    splits = actions[actions["action_type"] == ActionType.SPLIT.value]
    return splits.reindex(columns=["security_id", "effective_date", "ratio"])


def _apply_split_factors(bars: pd.DataFrame, splits: pd.DataFrame) -> pd.DataFrame:
    """Back-adjust prices for splits with an ex-date after each session.

    Standard back-adjustment: a session's prices are divided by the product of
    every split ratio taking effect *after* that session, so the series is
    continuous when read backwards from the as-of date. Volume is multiplied by
    the same factor, since share count moves the other way.
    """
    bars = bars.copy()
    bars["adjustment_factor"] = 1.0
    if splits.empty:
        return bars

    for _, split in splits.iterrows():
        ratio = float(split["ratio"] or 1.0)
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
