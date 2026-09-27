"""Impossible records are refused at append time, and nothing from the batch lands."""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pytest

from rplat.prices import get_bars
from rplat.store.store import Store
from rplat.store.validate import RecordValidationError, validate_records
from rplat.types import (
    ActionType,
    BarRecord,
    CorporateActionRecord,
    Dataset,
    FundamentalRecord,
    SecurityRecord,
    TickerRecord,
)

D = date(2022, 8, 5)


def _bar(**overrides: object) -> BarRecord:
    fields: dict[str, object] = {
        "security_id": "X",
        "effective_date": D,
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.5,
        "volume": 100,
        "knowledge_date": D,
    }
    fields.update(overrides)
    return BarRecord(**fields)  # type: ignore[arg-type]


IMPOSSIBLE: list[tuple[Dataset, object, str]] = [
    # The audit's probe6 records: accepted, then returned as of 2022-08-01.
    (Dataset.BARS, _bar(knowledge_date=date(2022, 8, 1)), "before its session"),
    (Dataset.BARS, _bar(high=9.0, low=11.0), "low must not exceed"),
    (Dataset.BARS, _bar(close=-3.0), "finite and positive"),
    (Dataset.BARS, _bar(open=float("nan")), "finite and positive"),
    (Dataset.BARS, _bar(high=10.2), "high must not be below"),
    (Dataset.BARS, _bar(volume=-1), "non-negative integer"),
    (Dataset.BARS, _bar(volume=1.5), "non-negative integer"),
    (Dataset.BARS, _bar(effective_date=datetime(2022, 8, 5, 16)), "must be a datetime.date"),
    (
        Dataset.FUNDAMENTALS,
        FundamentalRecord("X", date(2022, 9, 30), "eps", 1.0, date(2022, 8, 1)),
        "filed before it ends",
    ),
    (
        Dataset.CORPORATE_ACTIONS,
        CorporateActionRecord("X", D, ActionType.SPLIT, date(2022, 8, 1), ratio=None),
        "finite ratio > 0",
    ),
    (
        Dataset.CORPORATE_ACTIONS,
        CorporateActionRecord("X", D, ActionType.SPLIT, date(2022, 8, 1), ratio=0.0),
        "finite ratio > 0",
    ),
    (
        Dataset.CORPORATE_ACTIONS,
        CorporateActionRecord("X", D, ActionType.CASH_DIVIDEND, D, amount=-0.1),
        "finite and >= 0",
    ),
    (
        Dataset.TICKER_MAP,
        TickerRecord("X", "XX", start_date=D, knowledge_date=D, end_date=D),
        "end_date must be after start_date",
    ),
    (
        Dataset.SECURITIES,
        SecurityRecord("X", "X", listing_date=D, knowledge_date=D, delisting_date=D),
        "delisting_date must be after listing_date",
    ),
]


@pytest.mark.parametrize(("dataset", "record", "reason"), IMPOSSIBLE)
def test_impossible_record_is_rejected_and_nothing_is_written(
    dataset: Dataset, record: object, reason: str
) -> None:
    with Store.open(None) as store:
        with pytest.raises(RecordValidationError, match=reason):
            store.append(dataset, [record], source="bad")  # type: ignore[list-item]
        assert store.row_count(dataset) == 0


def test_the_audit_probe_records_are_no_longer_queryable() -> None:
    with Store.open(None) as store:
        with pytest.raises(RecordValidationError, match="1 of 1"):
            store.append(Dataset.BARS, [_bar(knowledge_date=date(2022, 8, 1))], source="bad")
        assert get_bars(store, date(2022, 8, 1)).empty


def test_null_split_ratio_is_rejected_at_ingest() -> None:
    # A NULL ratio used to crash get_bars for every security in the query.
    split = CorporateActionRecord("X", D, ActionType.SPLIT, date(2022, 8, 1))
    with Store.open(None) as store, pytest.raises(RecordValidationError, match="ratio"):
        store.append(Dataset.CORPORATE_ACTIONS, [split], source="bad")


def test_error_counts_and_quotes_offenders() -> None:
    good = [_bar(effective_date=date(2022, 8, d), knowledge_date=date(2022, 8, d)) for d in (1, 2)]
    bad = [_bar(close=-1.0)] * 7
    with pytest.raises(RecordValidationError) as caught:
        validate_records(Dataset.BARS, [*good, *bad])
    message = str(caught.value)
    assert "7 of 9 records" in message
    assert "#2:" in message and "… and 2 more" in message


def test_wrong_record_class_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="bars takes BarRecord, got FundamentalRecord"):
        validate_records(Dataset.BARS, [FundamentalRecord("X", date(2022, 6, 30), "eps", 1.0, D)])


def test_numpy_scalars_are_accepted() -> None:
    # Sources are often built on numpy; np.int64 is not an int, but it is Integral.
    validate_records(Dataset.BARS, [_bar(volume=np.int64(5), close=np.float64(10.5))])


def test_valid_edge_cases_pass() -> None:
    validate_records(
        Dataset.BARS,
        [_bar(open=9.0, low=9.0, high=10.5, close=10.5)],  # touches both extremes
    )
    validate_records(
        Dataset.FUNDAMENTALS,
        [FundamentalRecord("X", D, "eps", 1.0, D)],  # filed the day it ends
    )
    validate_records(
        Dataset.CORPORATE_ACTIONS,
        [CorporateActionRecord("X", D, ActionType.CASH_DIVIDEND, D)],  # amount unknown yet
    )
