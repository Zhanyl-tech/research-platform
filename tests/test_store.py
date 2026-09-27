"""Store mechanics: parameter binding, tie-breaks, atomicity, read-only access."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import pytest

import rplat.store.store as store_module
from rplat.fundamentals import get_fundamentals
from rplat.prices import get_bars
from rplat.sources.base import DataSource
from rplat.sources.fixture import RESTATED_METRIC, RESTATED_PERIOD_END, RESTATED_SECURITY
from rplat.store.schema import FACT_KEYS, PROVENANCE_COLUMNS, STORE_TABLES, as_of_sql
from rplat.store.store import _COLUMNS, Store, StoreBusyError, StoreError, StoreSchemaError
from rplat.store.validate import RecordValidationError
from rplat.types import BarRecord, Dataset, FundamentalRecord, SecurityRecord

#: The ``hold_open`` fixture: start a writer on a path, get back its release.
HoldOpen = Callable[[Path], Callable[[], None]]

#: The payload from the 2026-09 audit. Interpolated into SQL, it closed the
#: IN list and OR-ed in a second clause that the knowledge_date filter did not
#: cover, returning the restated EPS (1.2) as of 2022-09-01.
INJECTION = "SEC0001') OR (security_id = 'SEC0001"


def _fact(value: float, *, filed: date = date(2024, 5, 1)) -> FundamentalRecord:
    return FundamentalRecord(
        security_id="TEST",
        period_end=date(2024, 3, 31),
        metric="eps_diluted",
        value=value,
        knowledge_date=filed,
    )


def _value(store: Store, as_of: date) -> float:
    frame = get_fundamentals(store, as_of, security_ids=["TEST"])
    assert len(frame) == 1
    return float(frame.iloc[0]["value"])


class TestParameterBinding:
    """Caller-supplied values are data, never SQL."""

    @pytest.mark.parametrize("dataset", list(Dataset))
    def test_injection_string_matches_nothing(self, store: Store, dataset: Dataset) -> None:
        # Bound as a parameter, the payload is just an id nobody has.
        assert store.as_of(dataset, date(2022, 9, 1), security_ids=[INJECTION]).empty

    def test_injection_cannot_widen_past_the_as_of_date(self, store: Store) -> None:
        as_of = date(2022, 9, 1)
        frame = get_fundamentals(
            store, as_of, security_ids=[RESTATED_SECURITY, INJECTION], metrics=[RESTATED_METRIC]
        )
        assert (frame["knowledge_date"] <= pd.Timestamp(as_of)).all()
        row = frame[frame["period_end"] == pd.Timestamp(RESTATED_PERIOD_END)]
        # 1.5 is the original filing. The injection used to surface 1.2, the
        # restatement filed on 2022-11-15.
        assert list(row["value"]) == [1.5]

    def test_injection_through_other_key_filters_is_inert(self, store: Store) -> None:
        frame = get_fundamentals(store, date(2022, 9, 1), metrics=[INJECTION])
        assert frame.empty

    def test_quote_bearing_id_round_trips(self) -> None:
        # Used to raise a DuckDB ParserException.
        with Store.open(None) as store:
            store.append(
                Dataset.SECURITIES,
                [SecurityRecord("O'REILLY", "O'Reilly", date(2020, 1, 2), date(2020, 1, 2))],
                source="t",
            )
            frame = store.as_of(Dataset.SECURITIES, date(2021, 1, 1), security_ids=["O'REILLY"])
            assert list(frame["security_id"]) == ["O'REILLY"]

    def test_filter_column_names_are_allow_listed(self, store: Store) -> None:
        with pytest.raises(ValueError, match="key_in accepts"):
            store.as_of(Dataset.BARS, date(2022, 9, 1), key_in={"1=1) OR (security_id": ["x"]})
        with pytest.raises(ValueError, match="key_in accepts"):
            # A real column, but not a fact key: filtering on it before the
            # window could resurrect a superseded revision.
            store.as_of(Dataset.FUNDAMENTALS, date(2022, 9, 1), key_in={"source": ["fixture"]})
        with pytest.raises(ValueError, match="key_between accepts"):
            store.as_of(Dataset.BARS, date(2022, 9, 1), key_between={"close": (None, None)})

    def test_a_bare_string_is_not_a_list_of_ids(self, store: Store) -> None:
        # "SEC0001" iterated would be seven one-character ids.
        with pytest.raises(TypeError, match="not a single str"):
            store.as_of(Dataset.BARS, date(2022, 9, 1), security_ids="SEC0001")

    def test_empty_id_list_returns_nothing(self, store: Store) -> None:
        assert store.as_of(Dataset.BARS, date(2022, 9, 1), security_ids=[]).empty

    def test_extra_where_is_parenthesised(self) -> None:
        sql = as_of_sql(Dataset.BARS, extra_where="a = 1 OR b = 2")
        assert "WHERE knowledge_date <= $as_of AND (a = 1 OR b = 2)" in sql


class TestPushdownDoesNotChangeTheAnswer:
    """Key filters in SQL must select exactly what filtering afterwards would."""

    def test_bars_range_matches_post_filtering(self, store: Store) -> None:
        as_of, start, end = date(2023, 6, 1), date(2022, 9, 12), date(2022, 9, 23)
        pushed = store.as_of(Dataset.BARS, as_of, key_between={"effective_date": (start, end)})
        everything = store.as_of(Dataset.BARS, as_of)
        mask = everything["effective_date"].between(pd.Timestamp(start), pd.Timestamp(end))
        columns = ["security_id", "effective_date", "close", "knowledge_date"]
        left = pushed[columns].sort_values(columns[:2]).reset_index(drop=True)
        right = everything.loc[mask, columns].sort_values(columns[:2]).reset_index(drop=True)
        pd.testing.assert_frame_equal(left, right)

    def test_restated_fact_still_resolves_under_a_period_filter(self, store: Store) -> None:
        for as_of, expected in ((date(2022, 9, 1), 1.5), (date(2022, 12, 1), 1.2)):
            frame = get_fundamentals(
                store,
                as_of,
                security_ids=[RESTATED_SECURITY],
                metrics=[RESTATED_METRIC],
                start=RESTATED_PERIOD_END,
                end=RESTATED_PERIOD_END,
            )
            assert list(frame["value"]) == [expected]


class TestTieBreak:
    """Same fact, same knowledge_date: the later belief wins, by order, not by clock."""

    def test_later_record_in_one_batch_wins(self) -> None:
        # The audit appended 3000 x 1.0 then 9.9 in one batch; 9.9 won 0 of
        # 200 runs because every row shared ingested_at and ties fell to DuckDB.
        with Store.open(None) as store:
            store.append(Dataset.FUNDAMENTALS, [_fact(1.0)] * 500 + [_fact(9.9)], source="t")
            assert _value(store, date(2024, 6, 1)) == 9.9

    def test_it_is_position_not_value(self) -> None:
        with Store.open(None) as store:
            store.append(Dataset.FUNDAMENTALS, [_fact(9.9)] + [_fact(1.0)] * 500, source="t")
            assert _value(store, date(2024, 6, 1)) == 1.0

    def test_later_append_wins_with_the_clock_frozen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        frozen = datetime(2026, 1, 1, tzinfo=UTC)
        monkeypatch.setattr(store_module, "_utcnow", lambda: frozen)
        with Store.open(None) as store:
            store.append(Dataset.FUNDAMENTALS, [_fact(1.0)], source="t")
            store.append(Dataset.FUNDAMENTALS, [_fact(9.9)], source="t")
            assert _value(store, date(2024, 6, 1)) == 9.9
            store.append(Dataset.FUNDAMENTALS, [_fact(1.0)], source="t")
            assert _value(store, date(2024, 6, 1)) == 1.0
            # Proof the clock really was frozen, so ingested_at decided nothing.
            assert store.table(Dataset.FUNDAMENTALS)["ingested_at"].nunique() == 1

    def test_clock_stepping_backwards_does_not_reorder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ticks = iter(datetime(2026, 1, 1, tzinfo=UTC) - timedelta(hours=h) for h in range(10))
        monkeypatch.setattr(store_module, "_utcnow", lambda: next(ticks))
        with Store.open(None) as store:
            store.append(Dataset.FUNDAMENTALS, [_fact(1.0)], source="t")
            store.append(Dataset.FUNDAMENTALS, [_fact(9.9)], source="t")
            assert _value(store, date(2024, 6, 1)) == 9.9

    def test_revisions_list_in_resolution_order(self) -> None:
        with Store.open(None) as store:
            store.append(Dataset.FUNDAMENTALS, [_fact(1.0), _fact(2.0)], source="t")
            store.append(Dataset.FUNDAMENTALS, [_fact(3.0, filed=date(2024, 4, 30))], source="t")
            history = store.revisions(
                Dataset.FUNDAMENTALS, security_id="TEST", period_end=date(2024, 3, 31)
            )
            # Oldest belief first: earlier knowledge_date, then append order.
            assert list(history["value"]) == [3.0, 1.0, 2.0]


class _FlakySource(DataSource):
    """Securities fine, bars time out: the audit's partial-ingest reproduction."""

    name = "flaky"

    def describe(self) -> str:
        return "fails on bars"

    def records(self, dataset: Dataset) -> Iterable[object]:
        if dataset is Dataset.SECURITIES:
            return [SecurityRecord("F", "F", date(2020, 1, 2), date(2020, 1, 2))]
        if dataset is Dataset.BARS:
            raise RuntimeError("vendor timeout")
        return ()


class _ImpossibleBarSource(DataSource):
    """Pulls fine; the second dataset written fails validation."""

    name = "impossible"

    def describe(self) -> str:
        return "one bar known before its session"

    def records(self, dataset: Dataset) -> Iterable[object]:
        if dataset is Dataset.SECURITIES:
            return [SecurityRecord("X", "X", date(2020, 1, 2), date(2020, 1, 2))]
        if dataset is Dataset.BARS:
            return [BarRecord("X", date(2022, 8, 5), 10, 11, 9, 10, 100, date(2022, 8, 1))]
        return ()


def _all_counts(store: Store) -> dict[str, int]:
    counts = {dataset.value: store.row_count(dataset) for dataset in Dataset}
    counts["ingests"] = len(store.sql("SELECT * FROM ingests"))
    return counts


class TestAtomicity:
    """A failed write leaves the store exactly as it was; there is no delete to clean up."""

    @pytest.mark.parametrize(
        ("source", "error"),
        [(_FlakySource(), RuntimeError), (_ImpossibleBarSource(), RecordValidationError)],
    )
    def test_failed_ingest_writes_nothing(self, source: DataSource, error: type[Exception]) -> None:
        with Store.open(None) as store:
            store.append(Dataset.FUNDAMENTALS, [_fact(1.0)], source="t")
            before = _all_counts(store)
            with pytest.raises(error):
                store.ingest(source)
            assert _all_counts(store) == before

    def test_failed_append_writes_nothing(self) -> None:
        good = [
            BarRecord("X", date(2022, 8, d), 10, 11, 9, 10, 100, date(2022, 8, d)) for d in (1, 2)
        ]
        bad = BarRecord("X", date(2022, 8, 3), 10, 9, 11, 10, 100, date(2022, 8, 3))  # high < low
        with Store.open(None) as store:
            with pytest.raises(RecordValidationError, match="1 of 3"):
                store.append(Dataset.BARS, [*good, bad], source="t")
            assert _all_counts(store)["bars"] == 0
            assert _all_counts(store)["ingests"] == 0


class TestReadOnlySql:
    """Store.sql cannot rewrite history; it used to run UPDATE happily."""

    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE fundamentals SET value = 42",
            "DELETE FROM fundamentals",
            "DROP TABLE fundamentals",
            "INSERT INTO ingests SELECT * FROM ingests",
            "SET TimeZone = 'Asia/Tokyo'",
            "SELECT 1; UPDATE fundamentals SET value = 42",
            "EXPLAIN ANALYZE UPDATE fundamentals SET value = 42",
            "COPY fundamentals TO 'leak.csv'",
            "ATTACH ':memory:' AS other",
            "CHECKPOINT",
            "",
        ],
    )
    def test_anything_but_one_select_is_refused(
        self, statement: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If the guard ever regresses, COPY/ATTACH must land in a temp dir,
        # not the checkout.
        monkeypatch.chdir(tmp_path)
        with Store.open(None) as store:
            store.append(Dataset.FUNDAMENTALS, [_fact(1.0)], source="t")
            with pytest.raises(StoreError, match="exactly one SELECT"):
                store.sql(statement)
            assert list(store.table(Dataset.FUNDAMENTALS)["value"]) == [1.0]
            tz = store.sql("SELECT current_setting('TimeZone') AS tz")
            assert tz.iloc[0]["tz"] == "UTC"

    def test_select_with_bound_params_works(self) -> None:
        with Store.open(None) as store:
            store.append(Dataset.FUNDAMENTALS, [_fact(1.0)], source="t")
            frame = store.sql(
                "SELECT value FROM fundamentals WHERE metric = $m", {"m": "eps_diluted"}
            )
            assert list(frame["value"]) == [1.0]


class TestSchema:
    """Provenance is explicit, and stores from another schema version are refused."""

    def test_arrow_columns_match_the_ddl(self) -> None:
        with Store.open(None) as store:
            for dataset, columns in _COLUMNS.items():
                table = store.sql(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = $t ORDER BY ordinal_position",
                    {"t": dataset.value},
                )
                expected = [name for name, _ in columns] + list(PROVENANCE_COLUMNS)
                assert list(table["column_name"]) == expected
                assert set(FACT_KEYS[dataset]) <= set(expected)

    def test_session_time_zone_is_pinned_to_utc(self, store_file: Path) -> None:
        with Store.open(store_file, read_only=True) as store:
            tz = store.sql("SELECT current_setting('TimeZone') AS tz")
            assert tz.iloc[0]["tz"] == "UTC"
            stamped = store.table(Dataset.SECURITIES)["ingested_at"]
            assert str(stamped.dt.tz) == "UTC"

    def test_legacy_fixture_store_is_refused_with_a_lossless_rebuild(self, tmp_path: Path) -> None:
        path = _schema_1_store(tmp_path / "legacy.duckdb", source="fixture")
        for read_only in (False, True):
            with pytest.raises(StoreSchemaError) as caught:
                Store.open(path, read_only=read_only)
            message = str(caught.value)
            assert "schema 1 (rplat before the 2026-09 audit fixes, commit 396572a)" in message
            assert "loses nothing: `rplat ingest --force --db <path>`" in message
            # Code writing schema 1 and schema 2 both report 0.1.0; naming the
            # package version made "written by 0.1.0" contradict `rplat --version`.
            assert "0.1.0" not in message
        assert Store.is_store_file(path)

    def test_legacy_store_with_other_data_is_not_told_to_rebuild(self, tmp_path: Path) -> None:
        # `rplat ingest --force` loads only the fixture. Recommending it here
        # used to replace a user's Stooq data with synthetic data.
        path = _schema_1_store(tmp_path / "legacy.duckdb", source="stooq")
        # Fixture rows alongside do not make the rebuild safe, and are not
        # named as the problem.
        _duckdb_file(
            path,
            "INSERT INTO securities VALUES ('S1', 'One', DATE '2024-01-02', NULL, NULL, "
            "DATE '2024-01-02', 'fixture', 'i0', TIMESTAMP '2024-01-02 00:00:00')",
        )
        with pytest.raises(StoreSchemaError) as caught:
            Store.open(path, read_only=True)
        message = str(caught.value)
        assert "sources other than the fixture (stooq)" in message
        assert "There is no migration" in message
        assert "Do not run `rplat ingest --force`" in message
        assert "loses nothing" not in message

    def test_newer_schema_version_is_refused_without_rebuild_advice(self, tmp_path: Path) -> None:
        path = tmp_path / "future.duckdb"
        Store.open(path).close()
        conn = duckdb.connect(str(path))
        conn.execute("UPDATE store_meta SET value = '99' WHERE key = 'schema_version'")
        conn.close()
        with pytest.raises(StoreSchemaError, match="schema version 99") as caught:
            Store.open(path)
        assert "written by a newer rplat" in str(caught.value)
        assert "ingest --force" not in str(caught.value)

    def test_older_recorded_version_gets_the_rebuild_advice(self, tmp_path: Path) -> None:
        path = tmp_path / "older.duckdb"
        with Store.open(path) as built:
            built.append(Dataset.FUNDAMENTALS, [_fact(1.0)], source="fixture")
        conn = duckdb.connect(str(path))
        conn.execute("UPDATE store_meta SET value = '1' WHERE key = 'schema_version'")
        conn.close()
        with pytest.raises(StoreSchemaError, match=r"schema version 1;.*loses nothing"):
            Store.open(path, read_only=True)


#: Schema 1's DDL, as commit 396572a wrote it: no store_meta, no tie-break columns.
_SCHEMA_1_SQL = """
CREATE TABLE securities (security_id VARCHAR NOT NULL, name VARCHAR NOT NULL,
    listing_date DATE NOT NULL, delisting_date DATE, delisting_reason VARCHAR,
    knowledge_date DATE NOT NULL, source VARCHAR NOT NULL, ingest_id VARCHAR NOT NULL,
    ingested_at TIMESTAMP NOT NULL);
CREATE TABLE ticker_map (security_id VARCHAR NOT NULL, ticker VARCHAR NOT NULL,
    start_date DATE NOT NULL, end_date DATE, knowledge_date DATE NOT NULL,
    source VARCHAR NOT NULL, ingest_id VARCHAR NOT NULL, ingested_at TIMESTAMP NOT NULL);
CREATE TABLE bars (security_id VARCHAR NOT NULL, effective_date DATE NOT NULL,
    open DOUBLE NOT NULL, high DOUBLE NOT NULL, low DOUBLE NOT NULL, close DOUBLE NOT NULL,
    volume BIGINT NOT NULL, knowledge_date DATE NOT NULL, source VARCHAR NOT NULL,
    ingest_id VARCHAR NOT NULL, ingested_at TIMESTAMP NOT NULL);
CREATE TABLE corporate_actions (security_id VARCHAR NOT NULL, effective_date DATE NOT NULL,
    action_type VARCHAR NOT NULL, ratio DOUBLE, amount DOUBLE, knowledge_date DATE NOT NULL,
    source VARCHAR NOT NULL, ingest_id VARCHAR NOT NULL, ingested_at TIMESTAMP NOT NULL);
CREATE TABLE fundamentals (security_id VARCHAR NOT NULL, period_end DATE NOT NULL,
    fiscal_period VARCHAR, metric VARCHAR NOT NULL, value DOUBLE, knowledge_date DATE NOT NULL,
    source VARCHAR NOT NULL, ingest_id VARCHAR NOT NULL, ingested_at TIMESTAMP NOT NULL);
CREATE TABLE ingests (ingest_id VARCHAR NOT NULL, source VARCHAR NOT NULL,
    dataset VARCHAR NOT NULL, row_count BIGINT NOT NULL, ingested_at TIMESTAMP NOT NULL);
"""


def _schema_1_store(path: Path, *, source: str) -> Path:
    """A store as rplat wrote it before the audit fixes, with one bar from ``source``."""
    conn = duckdb.connect(str(path))
    conn.execute(_SCHEMA_1_SQL)
    conn.execute(
        "INSERT INTO bars VALUES ('S1', DATE '2024-01-02', 10, 11, 9, 10.5, 100, "
        "DATE '2024-01-02', $source, 'i1', TIMESTAMP '2024-01-03 00:00:00')",
        {"source": source},
    )
    conn.execute(
        "INSERT INTO ingests VALUES ('i1', $source, 'bars', 1, TIMESTAMP '2024-01-03 00:00:00')",
        {"source": source},
    )
    conn.close()
    return path


def _duckdb_file(path: Path, *statements: str) -> Path:
    conn = duckdb.connect(str(path))
    for statement in statements:
        conn.execute(statement)
    conn.close()
    return path


class TestIsStoreFile:
    """The guard in front of `rplat ingest --force`. It must err towards "no"."""

    def test_store_tables_match_the_ddl(self) -> None:
        with Store.open(None) as store:
            tables = store.sql(
                "SELECT table_name FROM duckdb_tables() WHERE database_name = current_database()"
            )
        assert set(tables["table_name"]) == STORE_TABLES

    def test_real_stores_of_both_schemas_are_recognised(
        self, store_file: Path, tmp_path: Path
    ) -> None:
        assert Store.is_store_file(store_file)
        assert Store.is_store_file(_schema_1_store(tmp_path / "v1.duckdb", source="fixture"))

    def test_non_databases_are_not_stores(self, tmp_path: Path) -> None:
        text = tmp_path / "notes.txt"
        text.write_text("not a database")
        zero = tmp_path / "zero.duckdb"
        zero.write_bytes(b"")
        empty = _duckdb_file(tmp_path / "empty.duckdb")
        assert not Store.is_store_file(text)
        assert not Store.is_store_file(zero)
        assert not Store.is_store_file(empty)
        assert not Store.is_store_file(tmp_path / "missing.duckdb")
        assert not Store.is_store_file(tmp_path)  # a directory
        with pytest.raises(StoreSchemaError, match="not an rplat store"):
            Store.open(empty, read_only=True)

    def test_one_familiar_table_name_is_not_enough(self, tmp_path: Path) -> None:
        # The review's probe: this file passed, and `ingest --force` replaced it.
        pipeline = _duckdb_file(
            tmp_path / "pipeline.duckdb",
            "CREATE TABLE ingests (job VARCHAR, rows INT)",
            "CREATE TABLE customers (id INT, name VARCHAR)",
            "INSERT INTO customers VALUES (1, 'precious')",
        )
        assert not Store.is_store_file(pipeline)
        assert not Store.is_store_file(
            _duckdb_file(tmp_path / "meta.duckdb", "CREATE TABLE store_meta (k VARCHAR)")
        )

    @pytest.mark.parametrize(
        "extra",
        [
            "CREATE TABLE customers (id INT)",
            "CREATE VIEW recent AS SELECT * FROM bars",
            "CREATE SCHEMA crm; CREATE TABLE crm.customers (id INT)",
            "DROP TABLE ticker_map",
            "DELETE FROM store_meta",
        ],
        ids=["extra-table", "view", "other-schema", "missing-table", "no-version-row"],
    )
    def test_anything_beyond_or_short_of_a_store_is_refused(
        self, tmp_path: Path, extra: str
    ) -> None:
        path = tmp_path / "almost.duckdb"
        Store.open(path).close()
        assert Store.is_store_file(path)
        _duckdb_file(path, extra)
        assert not Store.is_store_file(path)

    def test_a_duckdb_file_duckdb_cannot_open_is_unknown_not_no(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.duckdb"
        broken.write_bytes(b"\0" * 8 + b"DUCK")  # the header, and nothing after it
        with pytest.raises(StoreError, match="DuckDB could not open"):
            Store.is_store_file(broken)

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions as non-root"
    )
    def test_an_unreadable_file_is_unknown_not_no(self, tmp_path: Path) -> None:
        locked = tmp_path / "locked.duckdb"
        locked.write_bytes(b"x" * 16)
        locked.chmod(0)
        try:
            with pytest.raises(StoreError, match="cannot read"):
                Store.is_store_file(locked)
        finally:
            locked.chmod(0o600)

    def test_a_store_held_by_a_writer_is_busy_not_foreign(
        self, tmp_path: Path, hold_open: HoldOpen
    ) -> None:
        path = tmp_path / "held.duckdb"
        Store.open(path).close()
        release = hold_open(path)
        with pytest.raises(StoreBusyError, match="open for writing") as caught:
            Store.is_store_file(path)
        assert "not an rplat store" not in str(caught.value)
        release()
        assert Store.is_store_file(path)  # the same file, once released


class TestReadOnlyOpen:
    """Read-only is what lets several processes share one store."""

    def test_missing_file_raises_and_creates_nothing(self, tmp_path: Path) -> None:
        target = tmp_path / "nowhere" / "x.duckdb"
        with pytest.raises(FileNotFoundError):
            Store.open(target, read_only=True)
        assert not target.parent.exists()

    def test_in_memory_read_only_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="always be empty"):
            Store.open(None, read_only=True)

    def test_writes_are_refused(self, store_file: Path) -> None:
        with Store.open(store_file, read_only=True) as store:
            assert store.read_only
            with pytest.raises(StoreError, match="read_only"):
                store.append(Dataset.FUNDAMENTALS, [_fact(1.0)], source="t")
            with pytest.raises(StoreError, match="read_only"):
                store.ingest(_FlakySource())

    def test_queries_work(self, store_file: Path) -> None:
        with Store.open(store_file, read_only=True) as store:
            bars = get_bars(store, date(2023, 1, 3), start=date(2022, 8, 1), end=date(2022, 8, 1))
            assert len(bars) > 0


#: A reader process. It opens the store read-only, announces itself, waits
#: until every reader has announced (so all hold the file at once), queries,
#: then waits for the parent's release before closing.
_READER = textwrap.dedent(
    """
    import sys, time
    from datetime import date
    from pathlib import Path
    from rplat import Store, get_universe

    db, gate, n = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
    with Store.open(db, read_only=True) as store:
        (gate / f"open-{sys.argv[4]}").touch()
        deadline = time.monotonic() + 90
        while len(list(gate.glob("open-*"))) < n:
            if time.monotonic() > deadline:
                sys.exit("timed out waiting for the other readers")
            time.sleep(0.05)
        print(len(get_universe(store, date(2022, 6, 30))), flush=True)
        while not (gate / "release").exists():
            if time.monotonic() > deadline:
                sys.exit("timed out waiting for release")
            time.sleep(0.05)
    """
)


def test_eight_processes_read_one_store_at_once(store_file: Path, tmp_path: Path) -> None:
    """The Phase-6 shape (N array tasks, one store) needs concurrent readers.

    Before read-only opens existed, a second process could not open the store
    at all: DuckDB held a write lock for the first. This also pins the other
    half of DuckDB's documented rule: while readers hold the file, no process
    can open it for writing.
    """
    readers = 8
    # Our own interpreter running a constant script: no untrusted input.
    procs = [
        subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", _READER, str(store_file), str(tmp_path), str(readers), str(i)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(readers)
    ]
    try:
        deadline = datetime.now(UTC) + timedelta(seconds=90)
        while len(list(tmp_path.glob("open-*"))) < readers:
            assert all(p.poll() is None for p in procs), [p.communicate() for p in procs]
            assert datetime.now(UTC) < deadline, "readers never all opened the store"
            time.sleep(0.1)
        # All eight hold a read-only handle right now.
        with pytest.raises(duckdb.IOException):
            Store.open(store_file)
    finally:
        (tmp_path / "release").touch()
        results = [p.communicate(timeout=120) for p in procs]

    assert [p.returncode for p in procs] == [0] * readers, results
    counts = {out.strip() for out, _ in results}
    assert len(counts) == 1 and counts.pop().isdigit()
