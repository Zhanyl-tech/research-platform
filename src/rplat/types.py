"""Record types crossing the source -> store boundary.

Every record carries two dates, and keeping them straight is the whole point of
this package:

``effective_date``
    The date the fact is *about*. The trading session a bar describes; the
    fiscal period a fundamental covers; the ex-date of a split.

``knowledge_date``
    The first date on which a researcher could have known the fact. For a bar
    that is the session itself (known at the close). For a fundamental it is the
    filing date, which can be months after the period it describes. For a
    corporate action it is the announcement date, not the ex-date. It is a
    calendar date in America/New_York with no time of day, and a fact stamped D
    may have landed at any instant during D. :mod:`rplat.clock` defines exactly
    what an as-of query on D can therefore see.

A record whose ``knowledge_date`` is *impossible* (a bar known before its
session, a quarter filed before it ended) is rejected at append time; see
:mod:`rplat.store.validate`.

A row is never updated. When a vendor restates a value, a *new* record arrives
with the same ``effective_date`` and a later ``knowledge_date``, and the store
keeps both. "What did we believe on date D" is then a query, not an archaeology
project.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class Dataset(StrEnum):
    """Tables a source can supply. A source may implement any subset."""

    SECURITIES = "securities"
    TICKER_MAP = "ticker_map"
    BARS = "bars"
    CORPORATE_ACTIONS = "corporate_actions"
    FUNDAMENTALS = "fundamentals"


class ActionType(StrEnum):
    """Corporate action kinds that affect a price series."""

    SPLIT = "split"
    CASH_DIVIDEND = "cash_dividend"


@dataclass(frozen=True, slots=True)
class SecurityRecord:
    """The existence and lifecycle of a tradable instrument.

    ``security_id`` is permanent and opaque. Tickers are not identity — they get
    recycled between unrelated companies — so they live in :class:`TickerRecord`
    with their own validity window.

    A delisting is expressed as a *second* record for the same ``security_id``
    with a later ``knowledge_date`` and ``delisting_date`` set. That is what
    makes survivorship bias impossible to introduce by accident: as of a date
    before the announcement, the resolved record still has no delisting date, so
    the name is still in the universe.

    ``delisting_date`` is **exclusive**: it is the first date on which the
    security no longer trades. Its last bar is the session before it, and it is
    in :func:`rplat.get_universe` on every date that has a bar. ``listing_date``
    is inclusive.
    """

    security_id: str
    name: str
    listing_date: date
    knowledge_date: date
    delisting_date: date | None = None
    delisting_reason: str | None = None


@dataclass(frozen=True, slots=True)
class TickerRecord:
    """A ticker symbol's assignment to a security over ``[start_date, end_date)``.

    ``end_date`` is exclusive, matching ``SecurityRecord.delisting_date``. Like
    a delisting, an end date is news. A source must not put it on the row that
    is knowable from the listing date. It belongs on a *second* row for the same
    ``(security_id, ticker, start_date)`` whose ``knowledge_date`` is when the
    end was announced. Otherwise every as-of query before the announcement
    learns when the symbol will stop.
    """

    security_id: str
    ticker: str
    start_date: date
    knowledge_date: date
    end_date: date | None = None


@dataclass(frozen=True, slots=True)
class BarRecord:
    """One session of raw, *unadjusted* OHLCV.

    Deliberately raw. An "adjusted close" column is a look-ahead trap: today's
    adjusted history reflects splits that had not happened yet on the date each
    row describes. Adjustment is applied at query time from the actions knowable
    as of the query date — see :mod:`rplat.prices`.
    """

    security_id: str
    effective_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    knowledge_date: date


@dataclass(frozen=True, slots=True)
class CorporateActionRecord:
    """A split or cash dividend.

    ``effective_date`` is the ex-date; ``knowledge_date`` is the announcement.
    The gap between them is real and usable: a split announced on the 25th and
    effective on the 19th of the next month was knowable for over three weeks,
    and :func:`rplat.get_corporate_actions` returns it from the announcement.
    Knowing about a split is not the same as applying it, though. Prices are
    only adjusted for it from the ex-date on; see :func:`rplat.get_bars`.

    A ``SPLIT`` must carry a finite ``ratio > 0`` (new shares per old share).
    """

    security_id: str
    effective_date: date
    action_type: ActionType
    knowledge_date: date
    ratio: float | None = None
    amount: float | None = None


@dataclass(frozen=True, slots=True)
class FundamentalRecord:
    """One reported metric for one fiscal period.

    Stored long rather than wide so a restatement is a new row rather than a
    schema migration. ``knowledge_date`` is the filing date.
    """

    security_id: str
    period_end: date
    metric: str
    value: float
    knowledge_date: date
    fiscal_period: str | None = None
