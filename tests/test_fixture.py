"""The reference fixture must not leak, and must be the same data in every process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from datetime import date, timedelta

import pandas as pd
import pytest

from rplat.prices import get_bars
from rplat.sources.fixture import SPECS, sessions
from rplat.store.store import Store
from rplat.types import Dataset
from rplat.universe import get_universe

#: When each dying name's end became public, straight from the fixture specs.
ANNOUNCED = {s.security_id: s.delisting_announced for s in SPECS if s.delisting_date}
DELISTED = {s.security_id: s.delisting_date for s in SPECS if s.delisting_date}


def _grid() -> list[date]:
    """Month-ends across the tape, plus a week either side of every lifecycle event."""
    days = {
        d for d in sessions(date(2021, 1, 4), date(2023, 12, 29)) if (d + timedelta(3)).day <= 3
    }
    for spec in SPECS:
        for event in (spec.listing_date, spec.delisting_date, spec.delisting_announced):
            if event is not None:
                days |= {event + timedelta(days=k) for k in range(-7, 8)}
    return sorted(days)


GRID = _grid()


class TestNoLifecycleEndBeforeItsAnnouncement:
    """An end date is news. No as-of view may carry one before it was announced.

    This is the generic check the 2026-09 audit asked for. It would have caught
    the ticker_map rows that, as of 2021-10-01, already showed Northwind's
    2023-03-10 end and Vela's 2022-06-15 end.
    """

    @pytest.mark.parametrize(
        ("dataset", "column"),
        [(Dataset.TICKER_MAP, "end_date"), (Dataset.SECURITIES, "delisting_date")],
    )
    def test_against_the_fixture_announcements(
        self, store: Store, dataset: Dataset, column: str
    ) -> None:
        for as_of in GRID:
            frame = store.as_of(dataset, as_of)
            for security_id in frame.loc[frame[column].notna(), "security_id"]:
                announced = ANNOUNCED.get(security_id)
                assert announced is not None and announced <= as_of, (
                    f"{dataset.value}.{column} for {security_id} visible as of {as_of}, "
                    f"announced {announced}"
                )

    def test_ticker_ends_agree_with_the_security_master(self, store: Store) -> None:
        # Fixture-independent form: on any date, a ticker may carry an end date
        # only for a security whose delisting is already known that day.
        for as_of in GRID:
            tickers = store.as_of(Dataset.TICKER_MAP, as_of)
            securities = store.as_of(Dataset.SECURITIES, as_of)
            ended = set(tickers.loc[tickers["end_date"].notna(), "security_id"])
            known_dead = set(securities.loc[securities["delisting_date"].notna(), "security_id"])
            assert ended <= known_dead, f"as of {as_of}: {sorted(ended - known_dead)}"


class TestDelistingConvention:
    """delisting_date is the first non-trading date, for the tape and the universe alike."""

    def test_each_sessions_bars_belong_to_that_sessions_universe(self, store: Store) -> None:
        # The audit found a bar for Northwind on 2023-03-10 while the universe
        # that day excluded it, so a backtest would miss the delisting return.
        # Scope: bars for session D, as of D. Older bars are the next test.
        for as_of in GRID:
            bars = get_bars(store, as_of, start=as_of, end=as_of, adjust=False)
            universe = set(get_universe(store, as_of)["security_id"])
            orphans = set(bars["security_id"]) - universe
            assert not orphans, f"as of {as_of}: bars for {sorted(orphans)} outside the universe"

    def test_a_delisted_names_history_stays_knowable_outside_the_universe(
        self, store: Store
    ) -> None:
        # Pins the limit of the claim above, as stated in get_universe and
        # docs/point-in-time.md: it covers each day's own session, not history.
        as_of = date(2023, 6, 30)
        northwind = "SEC0010"
        assert northwind not in set(get_universe(store, as_of)["security_id"])
        history = get_bars(store, as_of, security_ids=[northwind], adjust=False)
        assert not history.empty
        assert pd.Timestamp(history["effective_date"].max()).date() < DELISTED[northwind]

    @pytest.mark.parametrize("security_id", sorted(DELISTED))
    def test_last_bar_is_the_session_before_delisting(self, store: Store, security_id: str) -> None:
        bars = get_bars(store, date(2023, 12, 29), security_ids=[security_id], adjust=False)
        last = pd.Timestamp(bars["effective_date"].max()).date()
        delisted = DELISTED[security_id]
        assert last < delisted
        assert last == max(sessions(delisted - timedelta(days=7), delisted - timedelta(days=1)))


#: Runs in a fresh interpreter. Prints digests of the raw fixture records and
#: of as-of query results, plus the zone DuckDB would default to there.
_PROBE = textwrap.dedent(
    """
    import hashlib, json
    from datetime import date
    import duckdb
    from rplat import Store, get_bars, get_fundamentals, get_universe
    from rplat.sources.fixture import FixtureSource
    from rplat.store.schema import PROVENANCE_COLUMNS
    from rplat.types import Dataset

    def digest(text):
        return hashlib.sha256(text.encode()).hexdigest()

    source = FixtureSource()
    records = {ds.value: digest("\\n".join(map(repr, source.records(ds)))) for ds in Dataset}

    def frame_digest(frame):
        keep = [c for c in frame.columns if c not in PROVENANCE_COLUMNS]
        return digest(frame[keep].to_csv(index=False))

    with Store.open(None) as store:
        store.ingest(source)
        as_of = date(2022, 8, 1)  # the restated EPS is filed on exactly this date
        queries = {
            "fundamentals": frame_digest(get_fundamentals(store, as_of)),
            "bars": frame_digest(get_bars(store, as_of, start=date(2022, 7, 1))),
            "universe": frame_digest(get_universe(store, as_of)),
        }
    raw_zone = duckdb.connect().execute("SELECT current_setting('TimeZone')").fetchone()[0]
    print(json.dumps({"records": records, "queries": queries, "duckdb_zone": raw_zone}))
    """
)


@pytest.fixture(scope="module")
def two_processes() -> list[dict[str, object]]:
    """The probe under two hash seeds and two machine time zones."""
    runs = []
    for hash_seed, zone in (("0", "America/New_York"), ("1", "Asia/Tokyo")):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed, "TZ": zone}
        # Our own interpreter running a constant script: no untrusted input.
        out = subprocess.run(  # noqa: S603
            [sys.executable, "-c", _PROBE], env=env, capture_output=True, text=True, check=True
        )
        runs.append(json.loads(out.stdout))
    return runs


def test_fixture_records_are_identical_across_hash_seeds(
    two_processes: list[dict[str, object]],
) -> None:
    # Dividend amounts used to be seeded from hash(security_id), which Python
    # salts per process, so they changed on every run.
    first, second = two_processes
    assert first["records"] == second["records"]


def test_query_results_do_not_depend_on_the_machine_zone(
    two_processes: list[dict[str, object]],
) -> None:
    first, second = two_processes
    if first["duckdb_zone"] == second["duckdb_zone"]:
        pytest.skip("TZ did not change DuckDB's default zone here; nothing to compare")
    assert first["queries"] == second["queries"]
