"""The as-of contract, to the time of day. See rplat.clock and docs/point-in-time.md."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from rplat.clock import EXCHANGE_TZ, as_of_for_decision, require_date
from rplat.fundamentals import get_fundamentals, latest_known, restatement_history
from rplat.prices import get_bars, get_corporate_actions
from rplat.sources.fixture import (
    LATE_ARRIVED,
    LATE_FIRST,
    LATE_LAST,
    LATE_SECURITY,
    ORIGINAL_FILING,
    ORIGINAL_VALUE,
    RESTATED_METRIC,
    RESTATED_PERIOD_END,
    RESTATED_SECURITY,
)
from rplat.store.store import Store
from rplat.types import Dataset
from rplat.universe import get_universe, resolve_ticker

NY = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")

#: Inputs that used to be accepted and meant something machine- or
#: time-of-day-dependent. 09:30 on the filing date is the audit's case: it
#: returned the 2022-08-01 bar, whose close is only known at 16:00.
DATETIME_INPUTS: list[Any] = [
    datetime(2022, 8, 1, 9, 30),
    datetime(2022, 8, 1, tzinfo=UTC),
    pd.Timestamp("2022-08-01"),
]


class TestRequireDate:
    def test_plain_date_passes_through(self) -> None:
        assert require_date(date(2022, 8, 1)) == date(2022, 8, 1)

    @pytest.mark.parametrize(
        "value", [*DATETIME_INPUTS, "2022-08-01", np.datetime64("2022-08-01"), None, 20220801]
    )
    def test_everything_else_is_a_type_error(self, value: object) -> None:
        with pytest.raises(TypeError, match=r"must be a datetime\.date.*as-of-semantics"):
            require_date(value)


def _query_calls(value: Any) -> list[Callable[[Store], object]]:
    """Every public entry point that takes a date, fed ``value`` in that slot."""
    ok = date(2022, 9, 1)
    return [
        lambda s: s.as_of(Dataset.BARS, value),
        lambda s: get_bars(s, value),
        lambda s: get_bars(s, ok, start=value),
        lambda s: get_bars(s, ok, end=value),
        lambda s: get_corporate_actions(s, value),
        lambda s: get_universe(s, value),
        lambda s: resolve_ticker(s, "ZZZ", value),
        lambda s: get_fundamentals(s, value),
        lambda s: get_fundamentals(s, ok, start=value),
        lambda s: latest_known(s, value, "eps_diluted"),
        lambda s: restatement_history(s, RESTATED_SECURITY, value, RESTATED_METRIC),
        lambda s: s.revisions(Dataset.BARS, effective_date=value),
    ]


@pytest.mark.parametrize("value", DATETIME_INPUTS)
def test_every_query_rejects_datetimes(store: Store, value: Any) -> None:
    for call in _query_calls(value):
        with pytest.raises(TypeError, match=r"datetime\.date"):
            call(store)


class TestInclusiveBounds:
    """as_of = D includes every stamp <= D and nothing stamped D + 1."""

    def _eps(self, store: Store, as_of: date) -> list[float]:
        frame = get_fundamentals(
            store,
            as_of,
            security_ids=[RESTATED_SECURITY],
            metrics=[RESTATED_METRIC],
            start=RESTATED_PERIOD_END,
            end=RESTATED_PERIOD_END,
        )
        return [float(v) for v in frame["value"]]

    def test_a_fact_stamped_d_is_visible_as_of_d(self, store: Store) -> None:
        assert self._eps(store, ORIGINAL_FILING) == [ORIGINAL_VALUE]

    def test_and_not_one_day_earlier(self, store: Store) -> None:
        assert self._eps(store, ORIGINAL_FILING - timedelta(days=1)) == []

    def test_late_bars_appear_on_their_arrival_date_exactly(self, store: Store) -> None:
        def visible(as_of: date) -> int:
            frame = get_bars(
                store, as_of, security_ids=[LATE_SECURITY], start=LATE_FIRST, end=LATE_LAST
            )
            return len(frame)

        assert visible(LATE_ARRIVED - timedelta(days=1)) == 0
        assert visible(LATE_ARRIVED) == 5

    def test_session_range_includes_both_ends(self, store: Store) -> None:
        start, end = date(2022, 8, 1), date(2022, 8, 5)  # Monday..Friday
        frame = get_bars(store, date(2023, 1, 3), security_ids=["SEC0001"], start=start, end=end)
        assert list(frame["effective_date"]) == list(pd.date_range(start, end, freq="D"))

    def test_period_range_includes_both_ends(self, store: Store) -> None:
        frame = get_fundamentals(
            store,
            date(2023, 12, 29),
            security_ids=["SEC0001"],
            metrics=["revenue"],
            start=date(2022, 3, 31),
            end=date(2022, 12, 31),
        )
        assert [ts.date() for ts in frame["period_end"]] == [
            date(2022, 3, 31),
            date(2022, 6, 30),
            date(2022, 9, 30),
            date(2022, 12, 31),
        ]


class TestLifecycleBounds:
    """Starts are inclusive; ends (delisting_date, ticker end_date) are exclusive."""

    def test_listing_date_is_inclusive(self, store: Store) -> None:
        # Zenith lists 2023-01-09.
        assert "SEC0011" not in set(get_universe(store, date(2023, 1, 8))["security_id"])
        assert "SEC0011" in set(get_universe(store, date(2023, 1, 9))["security_id"])

    def test_delisting_date_is_exclusive(self, store: Store) -> None:
        # Northwind delists 2023-03-10: in on the 9th (its last session), out on the 10th.
        assert "SEC0010" in set(get_universe(store, date(2023, 3, 9))["security_id"])
        assert "SEC0010" not in set(get_universe(store, date(2023, 3, 10))["security_id"])

    def test_ticker_end_date_is_exclusive(self, store: Store) -> None:
        # Vela's ZZZ ends 2022-06-15.
        assert resolve_ticker(store, "ZZZ", date(2022, 6, 14)) == "SEC0009"
        assert resolve_ticker(store, "ZZZ", date(2022, 6, 15)) is None


class TestAsOfForDecision:
    """Turning a real, zoned decision instant into a leak-free as-of date."""

    def test_naive_datetime_is_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            as_of_for_decision(datetime(2022, 8, 1, 9, 30))

    def test_a_date_is_refused(self) -> None:
        with pytest.raises(TypeError, match="must be a datetime"):
            as_of_for_decision(date(2022, 8, 1))  # type: ignore[arg-type]

    def test_zoned_day_complete_at_is_refused(self) -> None:
        with pytest.raises(ValueError, match="without tzinfo"):
            as_of_for_decision(
                datetime(2022, 8, 1, 18, tzinfo=NY), day_complete_at=time(17, 30, tzinfo=UTC)
            )

    @pytest.mark.parametrize(
        ("local", "expected"),
        [
            (time(0, 0), date(2022, 7, 31)),
            (time(9, 30), date(2022, 7, 31)),
            (time(16, 0), date(2022, 7, 31)),
            (time(23, 59, 59), date(2022, 7, 31)),
        ],
    )
    def test_by_default_today_is_never_safe(self, local: time, expected: date) -> None:
        # A fact stamped today may land as late as 10 p.m. ET (17 CFR 232.13(a)).
        decision = datetime.combine(date(2022, 8, 1), local, tzinfo=NY)
        assert as_of_for_decision(decision) == expected

    def test_midnight_completes_the_previous_day(self) -> None:
        assert as_of_for_decision(datetime(2022, 8, 2, 0, 0, tzinfo=NY)) == date(2022, 8, 1)

    def test_the_same_instant_in_any_zone_gives_the_same_date(self) -> None:
        # 2022-08-01 21:00 EDT, written three ways.
        instants = [
            datetime(2022, 8, 1, 21, 0, tzinfo=NY),
            datetime(2022, 8, 2, 1, 0, tzinfo=UTC),
            datetime(2022, 8, 2, 10, 0, tzinfo=TOKYO),
        ]
        assert {as_of_for_decision(t) for t in instants} == {date(2022, 7, 31)}
        complete = time(17, 30)
        assert {as_of_for_decision(t, day_complete_at=complete) for t in instants} == {
            date(2022, 8, 1)
        }

    @pytest.mark.parametrize(
        ("utc", "expected"),
        [
            # Summer (EDT, UTC-4): 21:31Z is 17:31 local, after the cutoff.
            (datetime(2022, 8, 1, 21, 31, tzinfo=UTC), date(2022, 8, 1)),
            (datetime(2022, 8, 1, 21, 29, tzinfo=UTC), date(2022, 7, 31)),
            # Winter (EST, UTC-5): the same 21:31Z is 16:31 local, before it.
            (datetime(2022, 12, 1, 21, 31, tzinfo=UTC), date(2022, 11, 30)),
            (datetime(2022, 12, 1, 22, 31, tzinfo=UTC), date(2022, 12, 1)),
        ],
    )
    def test_day_complete_at_is_wall_clock_across_dst(self, utc: datetime, expected: date) -> None:
        assert as_of_for_decision(utc, day_complete_at=time(17, 30)) == expected

    def test_monday_open_sees_friday_but_not_monday(self, store: Store) -> None:
        monday_open = datetime(2022, 8, 8, 9, 30, tzinfo=NY)
        as_of = as_of_for_decision(monday_open)
        assert as_of == date(2022, 8, 7)  # Sunday: calendar day, not session
        frame = get_bars(
            store, as_of, security_ids=["SEC0001"], start=date(2022, 8, 5), end=date(2022, 8, 8)
        )
        assert [ts.date() for ts in frame["effective_date"]] == [date(2022, 8, 5)]

    def test_the_audit_probe_no_longer_leaks(self, store: Store) -> None:
        """09:30 on 2022-08-01: neither that day's bar nor that day's filing is visible."""
        as_of = as_of_for_decision(datetime(2022, 8, 1, 9, 30, tzinfo=EXCHANGE_TZ))
        bars = get_bars(store, as_of, security_ids=["SEC0001"], start=date(2022, 8, 1))
        assert bars.empty
        assert TestInclusiveBounds()._eps(store, as_of) == []
