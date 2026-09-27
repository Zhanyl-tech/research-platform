"""Invariants checked on every append, before anything is written.

The store cannot tell whether a ``knowledge_date`` is *honest*. A bar stamped
with its own session date looks the same whether the vendor delivered it that
evening or a week later. It can tell when a stamp is *impossible*, though: a
bar known before its session happened, or a quarter filed before the quarter
ended. Those are free to detect, and letting them in means a query as of the
earlier date returns a fact from the future. This is the first small, real
piece of leakage detection in the platform. It catches impossible stamps
only; plausible-but-wrong stamps are still the source's responsibility (see
``docs/point-in-time.md``).

Everything else here is the same idea applied to the data itself. A price
series with ``high < low`` or a split with no ratio is not a restatement
waiting to be corrected. It is a record that would poison every query touching
it, and a NULL ratio used to crash ``get_bars`` for every security in the call.
"""

from __future__ import annotations

import math
import numbers
from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import Any, Final

from rplat.types import (
    ActionType,
    BarRecord,
    CorporateActionRecord,
    Dataset,
    FundamentalRecord,
    SecurityRecord,
    TickerRecord,
)

Record = SecurityRecord | TickerRecord | BarRecord | CorporateActionRecord | FundamentalRecord

#: The record class each dataset accepts.
RECORD_TYPES: Final[dict[Dataset, type[Record]]] = {
    Dataset.SECURITIES: SecurityRecord,
    Dataset.TICKER_MAP: TickerRecord,
    Dataset.BARS: BarRecord,
    Dataset.CORPORATE_ACTIONS: CorporateActionRecord,
    Dataset.FUNDAMENTALS: FundamentalRecord,
}

#: Date fields per dataset, and which of them may be None.
_DATE_FIELDS: Final[dict[Dataset, tuple[str, ...]]] = {
    Dataset.SECURITIES: ("listing_date", "knowledge_date", "delisting_date"),
    Dataset.TICKER_MAP: ("start_date", "knowledge_date", "end_date"),
    Dataset.BARS: ("effective_date", "knowledge_date"),
    Dataset.CORPORATE_ACTIONS: ("effective_date", "knowledge_date"),
    Dataset.FUNDAMENTALS: ("period_end", "knowledge_date"),
}
_OPTIONAL_DATES: Final = frozenset({"delisting_date", "end_date"})

#: How many offending rows an error message quotes.
_EXAMPLES = 5


class RecordValidationError(ValueError):
    """A batch contained records that cannot be true. Nothing was written."""


def validate_records(dataset: Dataset, records: Sequence[object]) -> None:
    """Raise if any record is the wrong type or violates an invariant.

    Raises:
        TypeError: a record is not the dataset's record class. That is a
            programming error, not bad data.
        RecordValidationError: one or more records are impossible. The message
            counts them and quotes the first few with their batch positions.
    """
    expected = RECORD_TYPES[dataset]
    check = _CHECKS[dataset]
    problems: list[tuple[int, str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, expected):
            raise TypeError(
                f"{dataset.value} takes {expected.__name__}, got {type(record).__name__} "
                f"at position {index}"
            )
        reason = _date_types(dataset, record) or check(record)
        if reason:
            problems.append((index, reason, record))

    if problems:
        shown = "\n".join(
            f"  #{index}: {reason}: {record!r}" for index, reason, record in problems[:_EXAMPLES]
        )
        more = f"\n  … and {len(problems) - _EXAMPLES} more" if len(problems) > _EXAMPLES else ""
        raise RecordValidationError(
            f"rejected {dataset.value} batch: {len(problems)} of {len(records)} records "
            f"are impossible; nothing was written\n{shown}{more}"
        )


def _date_types(dataset: Dataset, record: Record) -> str | None:
    # datetime subclasses date, and pyarrow silently truncates one into a DATE
    # column, so the time of day would vanish without a trace. Reject it.
    for name in _DATE_FIELDS[dataset]:
        value = getattr(record, name)
        if value is None and name in _OPTIONAL_DATES:
            continue
        if isinstance(value, datetime) or not isinstance(value, date):
            return f"{name} must be a datetime.date, got {type(value).__name__}"
    return None


def _finite_positive(value: object) -> bool:
    # numbers.Real rather than float: numpy scalars register as Real, and a
    # source built on numpy should not have to cast every field.
    return isinstance(value, numbers.Real) and math.isfinite(value) and float(value) > 0


def _finite_non_negative(value: object) -> bool:
    return isinstance(value, numbers.Real) and math.isfinite(value) and float(value) >= 0


def _non_negative_integer(value: object) -> bool:
    return isinstance(value, numbers.Integral) and int(value) >= 0


def _check_security(record: SecurityRecord) -> str | None:
    if record.delisting_date is not None and record.delisting_date <= record.listing_date:
        return "delisting_date must be after listing_date (it is the first non-trading date)"
    return None


def _check_ticker(record: TickerRecord) -> str | None:
    if record.end_date is not None and record.end_date <= record.start_date:
        return "end_date must be after start_date (end_date is exclusive)"
    return None


def _check_bar(record: BarRecord) -> str | None:
    if record.knowledge_date < record.effective_date:
        return "knowledge_date is before effective_date: a bar cannot be known before its session"
    prices = (record.open, record.high, record.low, record.close)
    if not all(_finite_positive(price) for price in prices):
        return "prices must be finite and positive"
    if not (record.low <= min(record.open, record.close) <= max(record.open, record.close)):
        return "low must not exceed open or close"
    if max(record.open, record.close) > record.high:
        return "high must not be below open or close"
    if not _non_negative_integer(record.volume):
        return "volume must be a non-negative integer"
    return None


def _check_action(record: CorporateActionRecord) -> str | None:
    if record.action_type not in set(ActionType):
        return f"unknown action_type {record.action_type!r}"
    if record.action_type == ActionType.SPLIT and not _finite_positive(record.ratio):
        return "a split needs a finite ratio > 0"
    if (
        record.action_type == ActionType.CASH_DIVIDEND
        and record.amount is not None
        and not _finite_non_negative(record.amount)
    ):
        return "a dividend amount must be finite and >= 0"
    return None


def _check_fundamental(record: FundamentalRecord) -> str | None:
    if record.knowledge_date < record.period_end:
        return "knowledge_date is before period_end: a period cannot be filed before it ends"
    return None


_CHECKS: Final[dict[Dataset, Callable[[Any], str | None]]] = {
    Dataset.SECURITIES: _check_security,
    Dataset.TICKER_MAP: _check_ticker,
    Dataset.BARS: _check_bar,
    Dataset.CORPORATE_ACTIONS: _check_action,
    Dataset.FUNDAMENTALS: _check_fundamental,
}
