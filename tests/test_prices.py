"""Split adjustment and data availability, both resolved as of the query date."""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import date, timedelta

import pandas as pd
import pytest

from rplat.prices import _apply_split_factors, get_bars, get_corporate_actions
from rplat.sources.fixture import (
    LATE_ARRIVED,
    LATE_FIRST,
    LATE_LAST,
    LATE_SECURITY,
    SPLIT_ANNOUNCED,
    SPLIT_EX_DATE,
    SPLIT_RATIO,
    SPLIT_SECURITY,
)
from rplat.store.store import Store
from rplat.types import ActionType, BarRecord, CorporateActionRecord, Dataset

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
    """A split is applied once it is both announced and effective, never before."""

    def test_unadjusted_before_announcement(self, store: Store) -> None:
        # Announced 2022-08-25. The day before, nobody could adjust for it.
        raw = _close(store, SPLIT_ANNOUNCED, adjust=False)
        assert _close(store, date(2022, 8, 24)) == pytest.approx(raw)

    @pytest.mark.parametrize(
        "as_of",
        [SPLIT_ANNOUNCED, date(2022, 8, 26), SPLIT_EX_DATE - timedelta(days=3)],
    )
    def test_known_but_not_effective_is_not_applied(self, store: Store, as_of: date) -> None:
        """Between announcement and ex-date, prices stay the prices that traded.

        There is no discontinuity to remove until the ex-date. Dividing history
        by the ratio here would leave returns unchanged and halve every price
        *level* (market cap, P/E, price filters) for the weeks in between. This
        test used to assert the opposite; the 2026-09 audit showed the latest
        close as of 2022-09-16 reported at 395.50 when 791.00 traded.
        """
        assert _close(store, as_of) == pytest.approx(_close(store, as_of, adjust=False))

    def test_known_split_is_visible_through_corporate_actions(self, store: Store) -> None:
        # Knowing about a split and applying it are different channels.
        actions = get_corporate_actions(store, date(2022, 8, 26), security_ids=[SPLIT_SECURITY])
        splits = actions[actions["action_type"] == ActionType.SPLIT.value]
        assert list(splits["effective_date"]) == [pd.Timestamp(SPLIT_EX_DATE)]
        assert list(splits["ratio"]) == [SPLIT_RATIO]

    def test_applied_from_the_ex_date_itself(self, store: Store) -> None:
        # Inclusive: on the ex-date the post-split bar exists, so history before
        # it must be divided to stay continuous with it.
        raw = _close(store, SPLIT_EX_DATE, adjust=False)
        assert _close(store, SPLIT_EX_DATE) == pytest.approx(raw / SPLIT_RATIO)

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


class TestLateExDateBar:
    """The documented exception to "the latest visible bar has factor 1".

    get_bars and docs/point-in-time.md both state the invariant with the
    proviso that the ex-date session's own bar is visible. This pins what
    happens when it is not, so the proviso cannot quietly go stale.
    """

    @pytest.fixture
    def late(self) -> Iterator[Store]:
        # A 2-for-1 split, ex-date 2024-01-05, announced a month earlier.
        before = BarRecord("X", date(2024, 1, 4), 100, 100, 100, 100, 1000, date(2024, 1, 4))
        # The ex-date session's own bar is backfilled four days late.
        ex_day = BarRecord("X", date(2024, 1, 5), 50, 50, 50, 50, 2000, date(2024, 1, 9))
        split = CorporateActionRecord(
            "X", date(2024, 1, 5), ActionType.SPLIT, date(2023, 12, 1), ratio=2.0
        )
        with Store.open(None) as store:
            store.append(Dataset.BARS, [before, ex_day], source="test")
            store.append(Dataset.CORPORATE_ACTIONS, [split], source="test")
            yield store

    @pytest.mark.parametrize("as_of", [date(2024, 1, 5), date(2024, 1, 8)])
    def test_latest_visible_bar_is_adjusted_until_the_ex_date_bar_lands(
        self, late: Store, as_of: date
    ) -> None:
        frame = get_bars(late, as_of)
        assert list(frame["effective_date"]) == [pd.Timestamp(2024, 1, 4)]
        assert frame.iloc[0]["adjustment_factor"] == 2.0
        assert frame.iloc[0]["close"] == 50.0  # 100.00 traded; 50.00 did not

    def test_invariant_holds_again_once_it_does(self, late: Store) -> None:
        frame = get_bars(late, date(2024, 1, 9))
        latest = frame.iloc[-1]
        assert latest["effective_date"] == pd.Timestamp(2024, 1, 5)
        assert latest["adjustment_factor"] == 1.0
        assert latest["close"] == 50.0

    def test_before_the_ex_date_nothing_is_adjusted(self, late: Store) -> None:
        frame = get_bars(late, date(2024, 1, 4))
        assert list(frame["adjustment_factor"]) == [1.0]
        assert frame.iloc[0]["close"] == 100.0


class TestSplitRatioGuard:
    """A split without a usable ratio must fail loudly, not poison the frame."""

    @pytest.mark.parametrize("ratio", [float("nan"), None, 0.0, -2.0, math.inf])
    def test_bad_ratio_raises_naming_the_split(self, ratio: float | None) -> None:
        # The store rejects such splits on append (tests/test_validation.py), so
        # this exercises the second line of defence directly. It used to turn
        # every adjusted price into NaN and then crash casting volume to int.
        bars = pd.DataFrame(
            {
                "security_id": ["X", "X"],
                "effective_date": [pd.Timestamp(2024, 1, 2), pd.Timestamp(2024, 1, 3)],
                "open": [10.0, 10.0],
                "high": [11.0, 11.0],
                "low": [9.0, 9.0],
                "close": [10.0, 10.0],
                "volume": [100, 100],
            }
        )
        splits = pd.DataFrame(
            {"security_id": ["X"], "effective_date": [pd.Timestamp(2024, 1, 3)], "ratio": [ratio]}
        )
        with pytest.raises(ValueError, match=r"X with ex-date 2024-01-03"):
            _apply_split_factors(bars, splits)


class TestEmptyResults:
    """Queries before any data must return an empty frame, not raise."""

    def test_bars_before_history_starts(self, store: Store) -> None:
        frame = get_bars(store, date(2019, 1, 1))
        assert frame.empty
        assert "adjustment_factor" in frame.columns
