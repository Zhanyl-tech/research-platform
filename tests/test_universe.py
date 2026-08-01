"""Survivorship bias and ticker identity."""

from __future__ import annotations

from datetime import date

import pytest

from rplat.store.store import Store
from rplat.universe import get_universe, resolve_ticker


class TestSurvivorship:
    """Dead names must be present in the universes that predate their death."""

    @pytest.mark.parametrize(
        ("as_of", "expected"),
        [
            (date(2021, 6, 30), True),  # long before the bankruptcy
            (date(2023, 2, 23), True),  # one day before it is announced
            (date(2023, 2, 24), True),  # announced, but still trading
            (date(2023, 3, 9), True),  # last day before delisting
            (date(2023, 3, 10), False),  # delisting date itself
            (date(2023, 6, 30), False),  # after
        ],
    )
    def test_bankrupt_name_present_until_it_delists(
        self, store: Store, as_of: date, expected: bool
    ) -> None:
        # Northwind (SEC0010) fails in March 2023. The universe on any earlier
        # date must contain it — that is the entire difference between a
        # point-in-time universe and a survivor list.
        universe = get_universe(store, as_of)
        assert ("SEC0010" in set(universe["security_id"])) is expected

    def test_announcement_does_not_remove_the_name_early(self, store: Store) -> None:
        """Knowing a company will delist is not the same as it having delisted.

        Between announcement and delisting the name still trades, so it belongs
        in the universe. Dropping it on the announcement date would be its own
        subtle look-ahead.
        """
        announced = get_universe(store, date(2023, 2, 27))
        assert "SEC0010" in set(announced["security_id"])

    def test_include_delisted_recovers_dead_names(self, store: Store) -> None:
        as_of = date(2023, 6, 30)
        alive = get_universe(store, as_of)
        everything = get_universe(store, as_of, include_delisted=True)
        assert "SEC0010" not in set(alive["security_id"])
        assert "SEC0010" in set(everything["security_id"])

    def test_universe_grows_then_shrinks_as_names_die(self, store: Store) -> None:
        sizes = {
            when: len(get_universe(store, when))
            for when in (date(2021, 6, 30), date(2022, 12, 30), date(2023, 12, 29))
        }
        assert sizes[date(2021, 6, 30)] > 0
        # Two names delist over the window; one lists in 2023.
        assert sizes[date(2023, 12, 29)] < sizes[date(2021, 6, 30)]

    def test_unlisted_name_absent_before_listing(self, store: Store) -> None:
        # Zenith lists 2023-01-09.
        assert "SEC0011" not in set(get_universe(store, date(2022, 12, 30))["security_id"])
        assert "SEC0011" in set(get_universe(store, date(2023, 6, 1))["security_id"])


class TestTickerIdentity:
    """A recycled ticker must resolve to whoever held it at the time."""

    def test_zzz_resolves_to_vela_then_zenith(self, store: Store) -> None:
        assert resolve_ticker(store, "ZZZ", date(2021, 10, 1)) == "SEC0009"
        assert resolve_ticker(store, "ZZZ", date(2023, 6, 1)) == "SEC0011"

    def test_zzz_is_unassigned_between_owners(self, store: Store) -> None:
        # Vela delists 2022-06-15; Zenith lists 2023-01-09. In between, nobody.
        assert resolve_ticker(store, "ZZZ", date(2022, 9, 1)) is None

    def test_unknown_ticker_returns_none(self, store: Store) -> None:
        assert resolve_ticker(store, "NOPE", date(2022, 9, 1)) is None

    def test_universe_ticker_matches_the_holder_of_record(self, store: Store) -> None:
        early = get_universe(store, date(2021, 10, 1))
        holder = early.loc[early["ticker"] == "ZZZ", "security_id"]
        assert list(holder) == ["SEC0009"]

    def test_one_row_per_security(self, store: Store) -> None:
        """A bad ticker join is the classic way to silently duplicate names."""
        universe = get_universe(store, date(2023, 6, 30))
        assert universe["security_id"].is_unique
