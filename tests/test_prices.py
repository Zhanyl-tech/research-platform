"""Split adjustment and data availability, both resolved as of the query date."""

from __future__ import annotations

from datetime import date

import pytest

from rplat.prices import get_bars
from rplat.sources.fixture import (
    LATE_ARRIVED,
    LATE_FIRST,
    LATE_LAST,
    LATE_SECURITY,
    SPLIT_ANNOUNCED,
    SPLIT_RATIO,
    SPLIT_SECURITY,
)
from rplat.store.store import Store

PRE_SPLIT_SESSION = date(2022, 8, 1)


def _close(store: Store, as_of: date, *, adjust: bool = True) -> float:
    frame = get_bars(
        store,
        as_of,
        security_ids=[SPLIT_SECURITY],
        start=PRE_SPLIT_SESSION,
        end=PRE_SPLIT_SESSION,
        adjust=adjust,
    )
    assert len(frame) == 1
    return float(frame.iloc[0]["close"])


class TestSplitAdjustment:
    """Adjustment must use only actions announced by the as-of date."""

    def test_unadjusted_before_announcement(self, store: Store) -> None:
        # Announced 2022-08-25. The day before, nobody could adjust for it.
        raw = _close(store, SPLIT_ANNOUNCED, adjust=False)
        assert _close(store, date(2022, 8, 24)) == pytest.approx(raw)

    def test_adjusted_once_announced_even_before_the_ex_date(self, store: Store) -> None:
        """The announcement, not the ex-date, is when the factor becomes usable.

        A researcher on 2022-08-26 knows the split is coming and can build a
        continuous series through it. Waiting for the ex-date would understate
        what was knowable.
        """
        raw = _close(store, SPLIT_ANNOUNCED, adjust=False)
        after_news = _close(store, date(2022, 8, 26))
        assert after_news == pytest.approx(raw / SPLIT_RATIO)

    def test_factor_reported_alongside_price(self, store: Store) -> None:
        frame = get_bars(
            store,
            date(2023, 1, 3),
            security_ids=[SPLIT_SECURITY],
            start=PRE_SPLIT_SESSION,
            end=PRE_SPLIT_SESSION,
        )
        assert float(frame.iloc[0]["adjustment_factor"]) == pytest.approx(SPLIT_RATIO)

    def test_sessions_after_the_ex_date_are_never_adjusted(self, store: Store) -> None:
        after = date(2022, 10, 3)
        frame = get_bars(
            store, date(2023, 6, 1), security_ids=[SPLIT_SECURITY], start=after, end=after
        )
        assert float(frame.iloc[0]["adjustment_factor"]) == pytest.approx(1.0)

    def test_no_discontinuity_across_the_split(self, store: Store) -> None:
        """The point of adjusting: the series must not jump on the ex-date.

        Unadjusted, a 2-for-1 halves the quoted price overnight and any return
        feature reads -50%. Adjusted, the seam disappears.
        """
        frame = get_bars(
            store,
            date(2023, 1, 3),
            security_ids=[SPLIT_SECURITY],
            start=date(2022, 9, 12),
            end=date(2022, 9, 23),
        ).sort_values("effective_date")
        returns = frame["close"].pct_change().dropna().abs()
        assert returns.max() < 0.25, "adjusted series still shows the split as a jump"

        unadjusted = get_bars(
            store,
            date(2023, 1, 3),
            security_ids=[SPLIT_SECURITY],
            start=date(2022, 9, 12),
            end=date(2022, 9, 23),
            adjust=False,
        ).sort_values("effective_date")
        raw_returns = unadjusted["close"].pct_change().dropna().abs()
        assert raw_returns.max() > 0.4, "fixture should contain a raw split discontinuity"

    def test_volume_moves_opposite_to_price(self, store: Store) -> None:
        adjusted = get_bars(
            store,
            date(2023, 1, 3),
            security_ids=[SPLIT_SECURITY],
            start=PRE_SPLIT_SESSION,
            end=PRE_SPLIT_SESSION,
        )
        raw = get_bars(
            store,
            date(2023, 1, 3),
            security_ids=[SPLIT_SECURITY],
            start=PRE_SPLIT_SESSION,
            end=PRE_SPLIT_SESSION,
            adjust=False,
        )
        assert int(adjusted.iloc[0]["volume"]) == pytest.approx(
            int(raw.iloc[0]["volume"]) * SPLIT_RATIO, rel=1e-6
        )


class TestLateArrival:
    """Backfilled data must be invisible until it actually arrived."""

    @pytest.mark.parametrize(
        ("as_of", "expected"),
        [
            (LATE_LAST, 0),
            (date(2023, 5, 11), 0),
            (LATE_ARRIVED, 5),
            (date(2023, 6, 1), 5),
        ],
    )
    def test_backfilled_week_appears_only_after_ingest(
        self, store: Store, as_of: date, expected: int
    ) -> None:
        frame = get_bars(
            store, as_of, security_ids=[LATE_SECURITY], start=LATE_FIRST, end=LATE_LAST
        )
        assert len(frame) == expected

    def test_other_securities_unaffected_that_week(self, store: Store) -> None:
        # Only Cirrus is late; a blanket date filter would have hidden everyone.
        frame = get_bars(
            store, LATE_LAST, security_ids=["SEC0001"], start=LATE_FIRST, end=LATE_LAST
        )
        assert len(frame) == 5


class TestEmptyResults:
    """Queries before any data must return an empty frame, not raise."""

    def test_bars_before_history_starts(self, store: Store) -> None:
        frame = get_bars(store, date(2019, 1, 1))
        assert frame.empty
        assert "adjustment_factor" in frame.columns
