"""latest_known: the newest *filed* period, not the newest period."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pandas as pd
import pytest

from rplat.fundamentals import get_fundamentals, latest_known
from rplat.store.store import Store
from rplat.types import Dataset, FundamentalRecord


@pytest.fixture
def filings() -> Iterator[Store]:
    """Q1 filed in May, Q2 filed in August, then Q1 restated in September."""
    with Store.open(None) as store:
        yield _load(store)


def _load(store: Store) -> Store:
    store.append(
        Dataset.FUNDAMENTALS,
        [
            FundamentalRecord("A", date(2024, 3, 31), "eps", 1.0, date(2024, 5, 1)),
            FundamentalRecord("A", date(2024, 6, 30), "eps", 2.0, date(2024, 8, 1)),
            FundamentalRecord("A", date(2024, 3, 31), "eps", 1.5, date(2024, 9, 1)),
            FundamentalRecord("A", date(2024, 6, 30), "revenue", 9.0, date(2024, 8, 1)),
        ],
        source="t",
    )
    return store


def _latest(store: Store, as_of: date) -> list[tuple[date, float]]:
    frame = latest_known(store, as_of, "eps")
    periods = [ts.date() for ts in pd.to_datetime(frame["period_end"])]
    return list(zip(periods, frame["value"].astype(float).tolist(), strict=True))


def test_nothing_before_the_first_filing(filings: Store) -> None:
    assert latest_known(filings, date(2024, 4, 30), "eps").empty


def test_only_q1_is_filed_in_july(filings: Store) -> None:
    # Q2 has ended (30 June) but is not filed until 1 August.
    assert _latest(filings, date(2024, 7, 1)) == [(date(2024, 3, 31), 1.0)]


def test_a_restated_older_period_does_not_displace_the_newer_one(filings: Store) -> None:
    # Q1's restatement is the newest *filing* in September, but Q2 is still the
    # newest *period* on record.
    assert _latest(filings, date(2024, 9, 15)) == [(date(2024, 6, 30), 2.0)]


def test_metric_filter_is_applied(filings: Store) -> None:
    frame = get_fundamentals(filings, date(2024, 9, 15), metrics=["revenue"])
    assert list(frame["metric"]) == ["revenue"]
    assert get_fundamentals(filings, date(2024, 9, 15), metrics=[]).empty
