"""Properties of as-of resolution, checked against a brute-force oracle and a date grid.

The hand-picked tests elsewhere pin the fixture's traps. These check the
mechanism itself. Random revision histories, full of same-day ties and
multi-batch corrections, must resolve exactly as a plain-Python reference
says. On the fixture, every dataset must satisfy the invariants the design
rests on, on every date in a grid, not only on the dates someone thought of.
"""

from __future__ import annotations

import itertools
from datetime import date, timedelta

import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rplat.prices import get_bars
from rplat.sources.fixture import SPLIT_ANNOUNCED, SPLIT_EX_DATE
from rplat.store.schema import FACT_KEYS
from rplat.store.store import Store
from rplat.types import Dataset, FundamentalRecord

BASE = date(2024, 1, 1)
PERIODS = (date(2023, 9, 30), date(2023, 12, 31))

#: One record: (security, period_end, knowledge offset in days from BASE).
_record = st.tuples(st.sampled_from(["A", "B"]), st.sampled_from(PERIODS), st.integers(0, 5))
#: Several appends, each a batch of records (possibly empty).
_history = st.lists(st.lists(_record, max_size=6), min_size=1, max_size=4)


@settings(max_examples=150, deadline=None)
@given(history=_history, as_of_offset=st.integers(-1, 6))
def test_as_of_matches_a_brute_force_reference(
    history: list[list[tuple[str, date, int]]], as_of_offset: int
) -> None:
    """Newest knowledge_date <= D, then latest append, then latest in batch; one row per key."""
    values = itertools.count()
    beliefs: list[tuple[tuple[str, date], tuple[date, int, int], float]] = []
    with Store.open(None) as store:
        for batch_index, batch in enumerate(history):
            records = []
            for position, (security_id, period_end, offset) in enumerate(batch):
                known = BASE + timedelta(days=offset)
                value = float(next(values))  # unique, so we know which belief won
                records.append(FundamentalRecord(security_id, period_end, "m", value, known))
                beliefs.append(((security_id, period_end), (known, batch_index, position), value))
            store.append(Dataset.FUNDAMENTALS, records, source="h")

        as_of = BASE + timedelta(days=as_of_offset)
        expected: dict[tuple[str, date], tuple[tuple[date, int, int], float]] = {}
        for key, rank, value in beliefs:
            if rank[0] <= as_of and (key not in expected or rank > expected[key][0]):
                expected[key] = (rank, value)

        frame = store.as_of(Dataset.FUNDAMENTALS, as_of)
        periods = [ts.date() for ts in pd.to_datetime(frame["period_end"])]
        got = dict(
            zip(
                zip(frame["security_id"].tolist(), periods, strict=True),
                frame["value"].astype(float).tolist(),
                strict=True,
            )
        )
    assert got == {key: value for key, (_, value) in expected.items()}


def _grid() -> list[date]:
    months = pd.date_range("2021-01-01", "2024-01-01", freq="MS").date.tolist()
    split_window = [SPLIT_ANNOUNCED + timedelta(days=k) for k in range(-3, 35)]
    return sorted(set(months) | set(split_window))


GRID = _grid()


@pytest.mark.parametrize("dataset", list(Dataset))
def test_no_future_stamps_and_one_row_per_fact(store: Store, dataset: Dataset) -> None:
    for as_of in GRID:
        frame = store.as_of(dataset, as_of)
        assert (frame["knowledge_date"] <= pd.Timestamp(as_of)).all(), as_of
        assert not frame.duplicated(list(FACT_KEYS[dataset])).any(), as_of


@pytest.mark.parametrize("dataset", list(Dataset))
def test_the_set_of_known_facts_only_grows(store: Store, dataset: Dataset) -> None:
    # Values may be restated; facts may not disappear. The original monotonicity
    # test covered three datasets by count; this covers all five by key.
    keys = list(FACT_KEYS[dataset])
    previous: set[tuple[object, ...]] = set()
    for as_of in GRID:
        frame = store.as_of(dataset, as_of)
        current = set(frame[keys].itertuples(index=False, name=None))
        assert previous <= current, f"{dataset.value} lost facts as of {as_of}"
        previous = current


def test_the_latest_adjusted_close_is_the_price_that_traded(store: Store) -> None:
    """Adjustment rewrites history, never the present.

    It holds whenever the ex-date session's own bar is visible, which is
    always true in the fixture. A split whose ex-date bar arrived late would
    legitimately adjust the latest *visible* bar.
    """
    for as_of in GRID:
        start = as_of - timedelta(days=10)
        adjusted = get_bars(store, as_of, start=start)
        if adjusted.empty:
            continue
        raw = get_bars(store, as_of, start=start, adjust=False)
        latest = adjusted.groupby("security_id").tail(1)
        latest_raw = raw.groupby("security_id").tail(1)
        assert (latest["adjustment_factor"] == 1.0).all(), as_of
        assert list(latest["close"]) == pytest.approx(list(latest_raw["close"])), as_of


def test_split_window_is_covered_by_the_grid() -> None:
    assert SPLIT_ANNOUNCED in GRID and SPLIT_EX_DATE in GRID
