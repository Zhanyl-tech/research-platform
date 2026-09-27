"""The command line, run in-process with click's CliRunner."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Iterable
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pytest
from click.testing import CliRunner

from rplat.cli import main
from rplat.fundamentals import get_fundamentals
from rplat.sources.base import DataSource
from rplat.sources.fixture import FixtureSource
from rplat.store.store import Store
from rplat.types import Dataset
from rplat.universe import get_universe


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(scope="module")
def built_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("cli") / "research.duckdb"
    result = CliRunner().invoke(main, ["ingest", "--db", str(path)])
    assert result.exit_code == 0, result.output
    return path


def _counts(path: Path) -> dict[Dataset, int]:
    with Store.open(path, read_only=True) as store:
        return {dataset: store.row_count(dataset) for dataset in Dataset}


class TestIngest:
    def test_reports_rows_per_dataset(self, built_db: Path) -> None:
        counts = _counts(built_db)
        assert all(n > 0 for n in counts.values())

    def test_existing_store_is_left_alone_without_force(
        self, runner: CliRunner, built_db: Path
    ) -> None:
        before = built_db.stat().st_mtime_ns
        result = runner.invoke(main, ["ingest", "--db", str(built_db)])
        assert result.exit_code == 0
        assert "already exists; pass --force" in result.output
        assert built_db.stat().st_mtime_ns == before

    def test_force_rebuilds_and_leaves_no_staging_files(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        db = tmp_path / "r.duckdb"
        assert runner.invoke(main, ["ingest", "--db", str(db)]).exit_code == 0
        first = _counts(db)
        result = runner.invoke(main, ["ingest", "--db", str(db), "--force"])
        assert result.exit_code == 0, result.output
        assert _counts(db) == first  # rebuilt, not appended to
        assert sorted(p.name for p in tmp_path.iterdir()) == ["r.duckdb"]


class TestForceFailsSafe:
    """--force replaces a file. Every way it can go wrong must leave the old one intact."""

    def test_refuses_to_replace_a_file_that_is_not_a_store(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        precious = tmp_path / "thesis.duckdb"
        precious.write_text("three years of work")
        result = runner.invoke(main, ["ingest", "--db", str(precious), "--force"])
        assert result.exit_code == 1
        assert "not an rplat store. Nothing was changed" in result.output
        assert precious.read_text() == "three years of work"

    def test_refuses_while_a_write_ahead_log_is_pending(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        db = tmp_path / "r.duckdb"
        assert runner.invoke(main, ["ingest", "--db", str(db)]).exit_code == 0
        before = db.read_bytes()
        wal = tmp_path / "r.duckdb.wal"
        wal.write_bytes(b"pending")
        result = runner.invoke(main, ["ingest", "--db", str(db), "--force"])
        assert result.exit_code == 1
        assert "r.duckdb.wal exists" in result.output
        assert db.read_bytes() == before
        assert wal.read_bytes() == b"pending"

    @pytest.mark.parametrize("flags", [[], ["--force"]], ids=["plain", "force"])
    def test_refuses_to_build_beside_an_orphan_write_ahead_log(
        self, runner: CliRunner, tmp_path: Path, flags: list[str]
    ) -> None:
        # No store, but a log left by a crashed writer. Renaming a new store
        # into place would hand DuckDB that log to replay into it.
        db = tmp_path / "r.duckdb"
        wal = tmp_path / "r.duckdb.wal"
        wal.write_bytes(b"orphan")
        result = runner.invoke(main, ["ingest", "--db", str(db), *flags])
        assert result.exit_code == 1
        assert "r.duckdb.wal exists without r.duckdb" in result.output
        assert "Nothing was changed" in result.output
        assert sorted(p.name for p in tmp_path.iterdir()) == ["r.duckdb.wal"]
        assert wal.read_bytes() == b"orphan"

    def test_a_real_orphan_log_never_reaches_the_new_store(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """The review's reproduction, end to end.

        A writer appends a foreign EPS of 99.0 and dies before checkpointing;
        the user deletes the store but not its log. The old rename-based code
        exited 0 and the rebuilt store answered 99.0 instead of the fixture's
        1.5. Now ingest refuses, and following its advice gives a clean store.
        """
        db = tmp_path / "r.duckdb"
        wal = tmp_path / "r.duckdb.wal"
        assert runner.invoke(main, ["ingest", "--db", str(db)]).exit_code == 0
        crash = (
            "import os, sys\n"
            "from datetime import date\n"
            "from rplat import FundamentalRecord, Store, Dataset\n"
            "store = Store.open(sys.argv[1])\n"
            "store.append(Dataset.FUNDAMENTALS, [FundamentalRecord('SEC0001', "
            "date(2022, 6, 30), 'eps_diluted', 99.0, date(2022, 8, 1))], source='x')\n"
            "os._exit(0)  # no close, so no checkpoint: the append lives only in the log\n"
        )
        # Our own interpreter running a constant script: no untrusted input.
        subprocess.run([sys.executable, "-c", crash, str(db)], check=True)  # noqa: S603
        assert wal.exists(), "the crashed writer left no log; the scenario did not happen"
        db.unlink()

        refused = runner.invoke(main, ["ingest", "--db", str(db)])
        assert refused.exit_code == 1
        assert "exists without r.duckdb" in refused.output
        assert not db.exists()

        wal.rename(tmp_path / "aside.wal")  # what the message says to do
        assert runner.invoke(main, ["ingest", "--db", str(db)]).exit_code == 0
        with Store.open(db, read_only=True) as store:
            eps = get_fundamentals(
                store, date(2022, 9, 1), security_ids=["SEC0001"], metrics=["eps_diluted"]
            )
            row = eps[eps["period_end"] == pd.Timestamp(2022, 6, 30)]
            assert list(row["value"]) == [1.5]
            assert set(store.table(Dataset.FUNDAMENTALS)["source"]) == {"fixture"}

    def test_refuses_if_a_log_appears_while_building(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "r.duckdb"
        real_ingest = Store.ingest

        def ingest_while_someone_writes(self: Store, source: DataSource) -> dict[Dataset, int]:
            written = real_ingest(self, source)
            (tmp_path / "r.duckdb.wal").write_bytes(b"late")
            return written

        monkeypatch.setattr(Store, "ingest", ingest_while_someone_writes)
        result = runner.invoke(main, ["ingest", "--db", str(db)])
        assert result.exit_code == 1
        assert "exists without r.duckdb" in result.output
        # The staging file is gone and nothing was renamed into place.
        assert sorted(p.name for p in tmp_path.iterdir()) == ["r.duckdb.wal"]

    def test_refuses_to_replace_an_unrelated_database_with_an_ingests_table(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        # One generic table name used to be enough to pass as a store.
        pipeline = tmp_path / "pipeline.duckdb"
        conn = duckdb.connect(str(pipeline))
        conn.execute("CREATE TABLE ingests (job VARCHAR, rows INT)")
        conn.execute("CREATE TABLE customers (id INT, name VARCHAR)")
        conn.execute("INSERT INTO customers VALUES (1, 'precious')")
        conn.close()
        before = pipeline.read_bytes()
        result = runner.invoke(main, ["ingest", "--db", str(pipeline), "--force"])
        assert result.exit_code == 1
        assert "not an rplat store. Nothing was changed" in result.output
        assert pipeline.read_bytes() == before

    def test_a_store_in_use_is_reported_as_in_use(
        self,
        runner: CliRunner,
        tmp_path: Path,
        hold_open: Callable[[Path], Callable[[], None]],
    ) -> None:
        db = tmp_path / "r.duckdb"
        assert runner.invoke(main, ["ingest", "--db", str(db)]).exit_code == 0
        before = db.read_bytes()
        hold_open(db)
        result = runner.invoke(main, ["ingest", "--db", str(db), "--force"])
        assert result.exit_code == 1
        assert "open for writing" in result.output
        assert "close that process and retry" in result.output
        assert "Nothing was changed" in result.output
        # It used to say "not an rplat store" about a perfectly good store.
        assert "not an rplat store" not in result.output
        assert db.read_bytes() == before

    def test_a_duckdb_file_that_cannot_be_opened_is_not_called_foreign(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        broken = tmp_path / "broken.duckdb"
        broken.write_bytes(b"\0" * 8 + b"DUCK")
        result = runner.invoke(main, ["ingest", "--db", str(broken), "--force"])
        assert result.exit_code == 1
        assert "DuckDB could not open" in result.output
        assert "Nothing was changed" in result.output
        assert broken.read_bytes() == b"\0" * 8 + b"DUCK"

    def test_help_says_it_loads_only_the_fixture(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ingest", "--help"])
        assert result.exit_code == 0
        assert "synthetic fixture" in result.output
        assert "not an upgrade path" in result.output

    def test_a_failed_rebuild_keeps_the_old_store(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "r.duckdb"
        assert runner.invoke(main, ["ingest", "--db", str(db)]).exit_code == 0
        before = _counts(db)

        def vendor_timeout(self: FixtureSource, dataset: Dataset) -> Iterable[object]:
            raise RuntimeError("vendor timeout")

        monkeypatch.setattr(FixtureSource, "records", vendor_timeout)
        result = runner.invoke(main, ["ingest", "--db", str(db), "--force"])
        assert isinstance(result.exception, RuntimeError)
        # The old code unlinked the store first, so this left nothing behind.
        assert _counts(db) == before
        assert sorted(p.name for p in tmp_path.iterdir()) == ["r.duckdb"]


class TestQueryCommands:
    def test_universe(self, runner: CliRunner, built_db: Path) -> None:
        result = runner.invoke(main, ["universe", "--db", str(built_db), "--as-of", "2022-06-30"])
        assert result.exit_code == 0, result.output
        with Store.open(built_db, read_only=True) as store:
            expected = len(get_universe(store, date(2022, 6, 30)))
        assert f"universe as of 2022-06-30: {expected} securities" in result.output
        assert "NWF" in result.output

    def test_universe_include_delisted(self, runner: CliRunner, built_db: Path) -> None:
        args = ["universe", "--db", str(built_db), "--as-of", "2023-06-30"]
        alive = runner.invoke(main, args)
        everything = runner.invoke(main, [*args, "--include-delisted"])
        # A dead name has no ticker in force any more, so look for its id.
        assert "SEC0010" not in alive.output
        assert "SEC0010" in everything.output

    def test_bars_adjusted_and_raw(self, runner: CliRunner, built_db: Path) -> None:
        args = ["bars", "--db", str(built_db), "--as-of", "2023-01-03", "--security", "SEC0002"]
        window = ["--start", "2022-08-01", "--end", "2022-08-01"]
        adjusted = runner.invoke(main, [*args, *window])
        raw = runner.invoke(main, [*args, *window, "--raw"])
        assert adjusted.exit_code == 0 and raw.exit_code == 0
        assert "bars as of 2023-01-03: 1 rows" in adjusted.output
        assert "adjustment_factor" in adjusted.output
        assert "adjustment_factor" not in raw.output

    def test_bars_with_an_injection_string_finds_nothing(
        self, runner: CliRunner, built_db: Path
    ) -> None:
        # `--security` fed straight into the interpolated SQL before the fix.
        result = runner.invoke(
            main,
            [
                "bars",
                "--db",
                str(built_db),
                "--as-of",
                "2022-09-01",
                "--security",
                "SEC0001') OR (security_id = 'SEC0001",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "bars as of 2022-09-01: 0 rows" in result.output

    def test_history_labels_time_zone(self, runner: CliRunner, built_db: Path) -> None:
        result = runner.invoke(
            main,
            [
                "history",
                "--db",
                str(built_db),
                "--security",
                "SEC0001",
                "--period-end",
                "2022-06-30",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "2022-08-01" in result.output and "2022-11-15" in result.output
        assert "+00:00" in result.output  # ingested_at is UTC and says so

    @pytest.mark.parametrize(
        "command",
        [
            ["universe", "--as-of", "2022-06-30"],
            ["bars", "--as-of", "2022-06-30"],
            ["history", "--security", "SEC0001", "--period-end", "2022-06-30"],
        ],
    )
    def test_missing_store_is_a_usage_error_and_creates_nothing(
        self, runner: CliRunner, tmp_path: Path, command: list[str]
    ) -> None:
        db = tmp_path / "typo" / "x.duckdb"
        result = runner.invoke(main, [*command, "--db", str(db)])
        assert result.exit_code == 2
        assert "not found — run: rplat ingest --db" in result.output
        assert not db.parent.exists()

    def test_non_store_file_is_reported(self, runner: CliRunner, tmp_path: Path) -> None:
        import duckdb

        empty = tmp_path / "empty.duckdb"
        duckdb.connect(str(empty)).close()
        result = runner.invoke(main, ["universe", "--db", str(empty), "--as-of", "2022-06-30"])
        assert result.exit_code == 1
        assert "not an rplat store" in result.output


class TestDemo:
    def test_walks_all_five_traps(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["demo"])
        assert result.exit_code == 0, result.output
        for heading in (
            "1. Survivorship bias",
            "2. Ticker recycling",
            "3. Restatement",
            "4. Corporate actions",
            "5. Late-arriving data",
        ):
            assert heading in result.output
        # The same strings CI greps for.
        assert "eps_diluted = 1.5" in result.output
        assert "eps_diluted = 1.2" in result.output
        assert "0 of 5 sessions visible" in result.output
        assert "factor 1.0; announced 2022-08-25, not yet effective" in result.output
        assert "factor 2.0; after the ex-date" in result.output

    def test_has_no_ignored_db_option(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["demo", "--db", "x.duckdb"])
        assert result.exit_code == 2
        assert "No such option" in result.output

    def test_version(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output
