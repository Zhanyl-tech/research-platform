"""The core guarantee: a query returns the belief held on the as-of date."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from rplat.fundamentals import get_fundamentals, restatement_history
from rplat.sources.fixture import (
    ORIGINAL_FILING,
    ORIGINAL_VALUE,
    RESTATED_METRIC,
    RESTATED_PERIOD_END,
    RESTATED_SECURITY,
    RESTATED_VALUE,
    RESTATEMENT_FILING,
)
from rplat.store.store import Store
from rplat.types import Dataset, FundamentalRecord


def _eps(store: Store, as_of: date) -> float:
    frame = get_fundamentals(
        store, as_of, security_ids=[RESTATED_SECURITY], metrics=[RESTATED_METRIC]
    )
    row = frame[frame["period_end"] == pd.Timestamp(RESTATED_PERIOD_END)]
    assert len(row) == 1, "as-of resolution must collapse revisions to exactly one row"
    return float(row.iloc[0]["value"])


class TestRestatement:
    """A restated value must not leak backwards in time."""

    def test_before_any_filing_the_fact_does_not_exist(self, store: Store) -> None:
        frame = get_fundamentals(
            store,
            ORIGINAL_FILING - timedelta(days=1),
            security_ids=[RESTATED_SECURITY],
            metrics=[RESTATED_METRIC],
        )
        matching = frame[frame["period_end"] == pd.Timestamp(RESTATED_PERIOD_END)]
        assert matching.empty, "a fundamental cannot be known before it is filed"

    def test_original_value_visible_between_filings(self, store: Store) -> None:
        assert _eps(store, date(2022, 9, 1)) == ORIGINAL_VALUE

    def test_on_the_filing_date_itself_the_value_is_known(self, store: Store) -> None:
        # Inclusive boundary: knowledge_date <= as_of. A fact filed today is
        # knowable today.
        assert _eps(store, ORIGINAL_FILING) == ORIGINAL_VALUE

    def test_restated_value_visible_after_correction(self, store: Store) -> None:
        assert _eps(store, date(2022, 12, 1)) == RESTATED_VALUE

    def test_restatement_boundary_is_inclusive(self, store: Store) -> None:
        assert _eps(store, RESTATEMENT_FILING) == RESTATED_VALUE
        assert _eps(store, RESTATEMENT_FILING - timedelta(days=1)) == ORIGINAL_VALUE

    def test_both_beliefs_are_retained(self, store: Store) -> None:
        history = restatement_history(
            store, RESTATED_SECURITY, RESTATED_PERIOD_END, RESTATED_METRIC
        )
        assert list(history["value"]) == [ORIGINAL_VALUE, RESTATED_VALUE]


def _fact(*, value: float, knowledge_date: date) -> FundamentalRecord:
    """One synthetic fundamental, varying only in value and filing date."""
    return FundamentalRecord(
        security_id="TEST",
        period_end=date(2024, 3, 31),
        metric="eps_diluted",
        value=value,
        knowledge_date=knowledge_date,
    )


class TestAppendOnly:
    """The store must never overwrite; a correction is a new row."""

    def test_reingesting_a_changed_value_appends(self) -> None:
        with Store.open(None) as store:
            store.append(
                Dataset.FUNDAMENTALS,
                [_fact(value=2.0, knowledge_date=date(2024, 5, 1))],
                source="t",
            )
            store.append(
                Dataset.FUNDAMENTALS,
                [_fact(value=2.5, knowledge_date=date(2024, 8, 1))],
                source="t",
            )

            assert store.row_count(Dataset.FUNDAMENTALS) == 2, "the old belief must survive"

            before = get_fundamentals(store, date(2024, 6, 1), security_ids=["TEST"])
            after = get_fundamentals(store, date(2024, 9, 1), security_ids=["TEST"])
            assert float(before.iloc[0]["value"]) == 2.0
            assert float(after.iloc[0]["value"]) == 2.5

    def test_same_day_correction_resolves_to_the_later_ingest(self) -> None:
        """Ties on knowledge_date break on ingest order, not arbitrarily."""
        with Store.open(None) as store:
            filed = date(2024, 5, 1)
            store.append(Dataset.FUNDAMENTALS, [_fact(value=1.0, knowledge_date=filed)], source="t")
            store.append(Dataset.FUNDAMENTALS, [_fact(value=9.9, knowledge_date=filed)], source="t")
            frame = get_fundamentals(store, filed, security_ids=["TEST"])
            assert len(frame) == 1
            assert float(frame.iloc[0]["value"]) == 9.9


class TestMonotonicity:
    """Moving the as-of date forward may add facts, never remove them."""

    @pytest.mark.parametrize(
        "dataset", [Dataset.BARS, Dataset.FUNDAMENTALS, Dataset.CORPORATE_ACTIONS]
    )
    def test_known_fact_count_is_non_decreasing(self, store: Store, dataset: Dataset) -> None:
        # A property the whole design rests on: knowledge only accumulates. If
        # this ever fails, some query is filtering on effective rather than
        # knowledge time.
        dates = [date(2021, 6, 30), date(2022, 6, 30), date(2023, 6, 30), date(2023, 12, 29)]
        counts = [len(store.as_of(dataset, when)) for when in dates]
        assert counts == sorted(counts), f"{dataset.value} lost known facts over time: {counts}"

    def test_revisions_rejects_non_key_columns(self, store: Store) -> None:
        with pytest.raises(ValueError, match="not fact keys"):
            store.revisions(Dataset.FUNDAMENTALS, value=1.0)
