"""A deterministic synthetic vendor that encodes the traps on purpose.

Real free data is a poor teacher here. Price feeds hand you a clean adjusted
series with no restatement history, so the failures this platform exists to
prevent are invisible in it — you cannot demonstrate that you handle a
restatement correctly using data that has never been restated.

So the reference dataset is synthetic and deliberately hostile. It is generated
from a fixed seed, so ``make demo`` is byte-identical on every machine and needs
no network or credentials, and it contains, by construction:

============================  ==================================================
Trap                          How it appears here
============================  ==================================================
Survivorship bias             Two names delist mid-history. Each has *two*
                              ``securities`` rows: the original with no
                              delisting date, and a later one announcing it.
                              Query before the announcement and the name is
                              still in the universe, as it must be.
Ticker recycling              ``ZZZ`` belongs to Vela Mining until it is
                              acquired, then to Zenith Robotics from 2023. Any
                              pipeline keyed on ticker silently splices two
                              unrelated companies into one series.
Restatement                   Ardent's Q2-2022 diluted EPS is filed at 1.50,
                              then restated to 1.20 in November. A backtest run
                              as of September must see 1.50.
Split adjustment              Beacon splits 2-for-1, announced three weeks
                              before the ex-date. Adjusting with today's factor
                              halves prices on dates when they had not halved.
Late-arriving data            One week of Cirrus bars lands seven days after the
                              sessions they describe — a vendor backfill. As of
                              the session date those bars did not exist.
============================  ==================================================
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import assert_never

import numpy as np

from rplat.sources.base import DataSource
from rplat.types import (
    ActionType,
    BarRecord,
    CorporateActionRecord,
    Dataset,
    FundamentalRecord,
    SecurityRecord,
    TickerRecord,
)

START = date(2021, 1, 4)
END = date(2023, 12, 29)
SEED = 20260101


@dataclass(frozen=True, slots=True)
class _Spec:
    """Blueprint for one synthetic issuer."""

    security_id: str
    name: str
    ticker: str
    listing_date: date
    initial_price: float
    annual_drift: float
    annual_vol: float
    delisting_date: date | None = None
    delisting_reason: str | None = None
    #: When the delisting became public. Always before the delisting itself —
    #: that gap is exactly what a survivorship check has to respect.
    delisting_announced: date | None = None


SPECS: tuple[_Spec, ...] = (
    _Spec("SEC0001", "Ardent Systems", "ARD", date(2018, 1, 2), 84.0, 0.11, 0.28),
    _Spec("SEC0002", "Beacon Semiconductor", "BCN", date(2016, 6, 1), 212.0, 0.18, 0.41),
    _Spec("SEC0003", "Cirrus Logistics", "CIR", date(2019, 9, 17), 41.5, 0.05, 0.24),
    _Spec("SEC0004", "Delta Health", "DHL", date(2015, 3, 11), 128.0, 0.07, 0.19),
    _Spec("SEC0005", "Ember Energy", "EMB", date(2017, 11, 6), 63.0, -0.02, 0.35),
    _Spec("SEC0006", "Fathom Analytics", "FTH", date(2020, 2, 14), 27.5, 0.22, 0.52),
    _Spec("SEC0007", "Granite Materials", "GRN", date(2014, 8, 22), 96.0, 0.04, 0.17),
    _Spec("SEC0008", "Harbor Financial", "HBR", date(2013, 5, 30), 55.0, 0.06, 0.22),
    # Acquired mid-2022. Announced three weeks before the tape stops.
    _Spec(
        "SEC0009",
        "Vela Mining",
        "ZZZ",
        date(2019, 3, 1),
        18.0,
        -0.06,
        0.44,
        delisting_date=date(2022, 6, 15),
        delisting_reason="acquired",
        delisting_announced=date(2022, 5, 24),
    ),
    # Bankrupt in 2023. Announced two weeks out.
    _Spec(
        "SEC0010",
        "Northwind Freight",
        "NWF",
        date(2017, 5, 2),
        33.0,
        -0.25,
        0.61,
        delisting_date=date(2023, 3, 10),
        delisting_reason="bankruptcy",
        delisting_announced=date(2023, 2, 24),
    ),
    # Reuses ZZZ after Vela is gone. Unrelated company, same symbol.
    _Spec("SEC0011", "Zenith Robotics", "ZZZ", date(2023, 1, 9), 47.0, 0.31, 0.58),
    _Spec("SEC0012", "Orchid Biosciences", "ORC", date(2018, 7, 23), 71.0, 0.09, 0.47),
)

#: Beacon's 2-for-1, announced well before it takes effect.
SPLIT_SECURITY = "SEC0002"
SPLIT_EX_DATE = date(2022, 9, 19)
SPLIT_ANNOUNCED = date(2022, 8, 25)
SPLIT_RATIO = 2.0

#: Ardent's restated quarter.
RESTATED_SECURITY = "SEC0001"
RESTATED_PERIOD_END = date(2022, 6, 30)
RESTATED_METRIC = "eps_diluted"
ORIGINAL_FILING = date(2022, 8, 1)
ORIGINAL_VALUE = 1.50
RESTATEMENT_FILING = date(2022, 11, 15)
RESTATED_VALUE = 1.20

#: Cirrus bars that arrived a week late.
LATE_SECURITY = "SEC0003"
LATE_FIRST = date(2023, 5, 1)
LATE_LAST = date(2023, 5, 5)
LATE_ARRIVED = date(2023, 5, 12)


def sessions(start: date, end: date) -> Iterator[date]:
    """Weekday sessions in ``[start, end]``.

    Holidays are ignored on purpose. A real calendar belongs to an exchange
    calendar library, and Phase 3's calendar-misalignment check is where that
    gap gets flagged rather than papered over.
    """
    day = start
    while day <= end:
        if day.weekday() < 5:
            yield day
        day += timedelta(days=1)


class FixtureSource(DataSource):
    """Deterministic synthetic vendor. No network, no credentials."""

    name = "fixture"

    def __init__(self, *, start: date = START, end: date = END, seed: int = SEED) -> None:
        self.start = start
        self.end = end
        self.seed = seed

    def describe(self) -> str:
        return (
            f"synthetic point-in-time vendor: {len(SPECS)} securities, "
            f"{self.start} to {self.end}, with restatements, delistings, "
            "a ticker reuse, a split and a late backfill"
        )

    def records(self, dataset: Dataset) -> Iterable[object]:
        match dataset:
            case Dataset.SECURITIES:
                return self._securities()
            case Dataset.TICKER_MAP:
                return self._tickers()
            case Dataset.BARS:
                return self._bars()
            case Dataset.CORPORATE_ACTIONS:
                return self._actions()
            case Dataset.FUNDAMENTALS:
                return self._fundamentals()
            case _:
                # This source implements every dataset, so reaching here means a
                # new Dataset member was added without a branch. assert_never
                # turns that into a type-check failure rather than a silently
                # empty table.
                assert_never(dataset)

    # ── datasets ───────────────────────────────────────────────────────────

    def _securities(self) -> list[SecurityRecord]:
        """Two rows for anything that dies: before the news, and after."""
        out: list[SecurityRecord] = []
        for spec in SPECS:
            out.append(
                SecurityRecord(
                    security_id=spec.security_id,
                    name=spec.name,
                    listing_date=spec.listing_date,
                    knowledge_date=spec.listing_date,
                )
            )
            if spec.delisting_date and spec.delisting_announced:
                out.append(
                    SecurityRecord(
                        security_id=spec.security_id,
                        name=spec.name,
                        listing_date=spec.listing_date,
                        knowledge_date=spec.delisting_announced,
                        delisting_date=spec.delisting_date,
                        delisting_reason=spec.delisting_reason,
                    )
                )
        return out

    def _tickers(self) -> list[TickerRecord]:
        return [
            TickerRecord(
                security_id=spec.security_id,
                ticker=spec.ticker,
                start_date=spec.listing_date,
                knowledge_date=spec.listing_date,
                end_date=spec.delisting_date,
            )
            for spec in SPECS
        ]

    def _bars(self) -> list[BarRecord]:
        """Geometric brownian motion, seeded per security so it is stable."""
        out: list[BarRecord] = []
        for index, spec in enumerate(SPECS):
            rng = np.random.default_rng(self.seed + index)
            first = max(self.start, spec.listing_date)
            last = min(self.end, spec.delisting_date or self.end)
            days = list(sessions(first, last))
            if not days:
                continue

            dt = 1.0 / 252.0
            shocks = rng.normal(
                (spec.annual_drift - 0.5 * spec.annual_vol**2) * dt,
                spec.annual_vol * np.sqrt(dt),
                size=len(days),
            )
            closes = spec.initial_price * np.exp(np.cumsum(shocks))

            # Prices before the ex-date are quoted pre-split, so undo the split
            # on the raw tape. The store holds raw prices; adjustment happens at
            # query time from actions knowable then.
            if spec.security_id == SPLIT_SECURITY:
                closes = np.array(
                    [
                        c * SPLIT_RATIO if d < SPLIT_EX_DATE else c
                        for c, d in zip(closes, days, strict=True)
                    ]
                )

            intraday = rng.uniform(0.004, 0.022, size=len(days))
            opens = closes * (1.0 + rng.normal(0.0, 0.004, size=len(days)))
            highs = np.maximum(opens, closes) * (1.0 + intraday)
            lows = np.minimum(opens, closes) * (1.0 - intraday)
            volumes = rng.integers(120_000, 4_800_000, size=len(days))

            rows = zip(days, opens, highs, lows, closes, volumes, strict=True)
            for day, o, h, low_, c, v in rows:
                knowledge = day
                if spec.security_id == LATE_SECURITY and LATE_FIRST <= day <= LATE_LAST:
                    knowledge = LATE_ARRIVED
                out.append(
                    BarRecord(
                        security_id=spec.security_id,
                        effective_date=day,
                        open=round(float(o), 4),
                        high=round(float(h), 4),
                        low=round(float(low_), 4),
                        close=round(float(c), 4),
                        volume=int(v),
                        knowledge_date=knowledge,
                    )
                )
        return out

    def _actions(self) -> list[CorporateActionRecord]:
        out: list[CorporateActionRecord] = [
            CorporateActionRecord(
                security_id=SPLIT_SECURITY,
                effective_date=SPLIT_EX_DATE,
                action_type=ActionType.SPLIT,
                knowledge_date=SPLIT_ANNOUNCED,
                ratio=SPLIT_RATIO,
            )
        ]
        # Quarterly dividends for the steady payers, announced a month ahead.
        for spec in SPECS:
            if spec.security_id not in {"SEC0004", "SEC0007", "SEC0008"}:
                continue
            rng = np.random.default_rng(self.seed + hash(spec.security_id) % 1000)
            for year in (2021, 2022, 2023):
                for month in (3, 6, 9, 12):
                    ex_date = date(year, month, 15)
                    if not (self.start <= ex_date <= self.end):
                        continue
                    out.append(
                        CorporateActionRecord(
                            security_id=spec.security_id,
                            effective_date=ex_date,
                            action_type=ActionType.CASH_DIVIDEND,
                            knowledge_date=ex_date - timedelta(days=30),
                            amount=round(float(rng.uniform(0.18, 0.62)), 4),
                        )
                    )
        return out

    def _fundamentals(self) -> list[FundamentalRecord]:
        """Quarterly filings, lagged, with one genuine restatement."""
        out: list[FundamentalRecord] = []
        for index, spec in enumerate(SPECS):
            rng = np.random.default_rng(self.seed + 500 + index)
            for year in (2021, 2022, 2023):
                for quarter, period_end in enumerate(
                    (date(year, 3, 31), date(year, 6, 30), date(year, 9, 30), date(year, 12, 31)),
                    start=1,
                ):
                    if period_end < spec.listing_date:
                        continue
                    if spec.delisting_date and period_end > spec.delisting_date:
                        continue
                    # Filings land roughly a month after the period closes. That
                    # lag is the whole reason fundamentals need as-of handling.
                    filing = period_end + timedelta(days=int(rng.integers(28, 47)))
                    if filing > self.end:
                        continue
                    for metric, value in (
                        ("eps_diluted", round(float(rng.normal(1.1, 0.45)), 4)),
                        ("revenue", round(float(rng.uniform(2.0e8, 4.0e9)), 2)),
                        ("total_assets", round(float(rng.uniform(1.0e9, 2.5e10)), 2)),
                    ):
                        is_restated_cell = (
                            spec.security_id == RESTATED_SECURITY
                            and period_end == RESTATED_PERIOD_END
                            and metric == RESTATED_METRIC
                        )
                        if is_restated_cell:
                            # The original belief, and the correction. Both kept.
                            out.append(
                                FundamentalRecord(
                                    security_id=spec.security_id,
                                    period_end=period_end,
                                    metric=metric,
                                    value=ORIGINAL_VALUE,
                                    knowledge_date=ORIGINAL_FILING,
                                    fiscal_period=f"Q{quarter} {year}",
                                )
                            )
                            out.append(
                                FundamentalRecord(
                                    security_id=spec.security_id,
                                    period_end=period_end,
                                    metric=metric,
                                    value=RESTATED_VALUE,
                                    knowledge_date=RESTATEMENT_FILING,
                                    fiscal_period=f"Q{quarter} {year}",
                                )
                            )
                            continue
                        out.append(
                            FundamentalRecord(
                                security_id=spec.security_id,
                                period_end=period_end,
                                metric=metric,
                                value=value,
                                knowledge_date=filing,
                                fiscal_period=f"Q{quarter} {year}",
                            )
                        )
        return out
