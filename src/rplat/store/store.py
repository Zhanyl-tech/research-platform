"""The append-only bitemporal store."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self, cast

import duckdb
import pandas as pd
import pyarrow as pa

from rplat.clock import require_date
from rplat.sources.base import DataSource
from rplat.sources.fixture import FixtureSource
from rplat.store.schema import (
    DATE_KEY_COLUMNS,
    FACT_KEYS,
    SCHEMA_1_TABLES,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    STORE_TABLES,
    as_of_sql,
)
from rplat.store.validate import Record, validate_records
from rplat.types import Dataset

#: Column names and Arrow types per table, in DDL order. Inserts name their
#: columns explicitly (``INSERT INTO t (a, b) SELECT a, b ...``), so a column
#: added to the DDL can never be silently filled from the wrong field.
#: ``tests/test_store.py`` asserts these match the DDL.
_COLUMNS: Final[dict[Dataset, tuple[tuple[str, pa.DataType], ...]]] = {
    Dataset.SECURITIES: (
        ("security_id", pa.string()),
        ("name", pa.string()),
        ("listing_date", pa.date32()),
        ("delisting_date", pa.date32()),
        ("delisting_reason", pa.string()),
        ("knowledge_date", pa.date32()),
    ),
    Dataset.TICKER_MAP: (
        ("security_id", pa.string()),
        ("ticker", pa.string()),
        ("start_date", pa.date32()),
        ("end_date", pa.date32()),
        ("knowledge_date", pa.date32()),
    ),
    Dataset.BARS: (
        ("security_id", pa.string()),
        ("effective_date", pa.date32()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.int64()),
        ("knowledge_date", pa.date32()),
    ),
    Dataset.CORPORATE_ACTIONS: (
        ("security_id", pa.string()),
        ("effective_date", pa.date32()),
        ("action_type", pa.string()),
        ("ratio", pa.float64()),
        ("amount", pa.float64()),
        ("knowledge_date", pa.date32()),
    ),
    Dataset.FUNDAMENTALS: (
        ("security_id", pa.string()),
        ("period_end", pa.date32()),
        ("fiscal_period", pa.string()),
        ("metric", pa.string()),
        ("value", pa.float64()),
        ("knowledge_date", pa.date32()),
    ),
}

#: Name the Arrow batch is registered under for the duration of one insert.
_BATCH_VIEW: Final = "_rplat_append_batch"

#: DuckDB checks these bytes, which follow an 8-byte checksum, before it opens
#: a file (``MainHeader::MAGIC_BYTE_OFFSET`` in
#: https://github.com/duckdb/duckdb/blob/v1.0.0/src/include/duckdb/storage/storage_info.hpp).
#: Checking them first gives a definite "not a database" without parsing
#: DuckDB's error text. Files written by DuckDB 1.0.0 and 1.5.5 both match.
_DUCKDB_MAGIC: Final = b"DUCK"
_DUCKDB_MAGIC_OFFSET: Final = 8

#: Tables whose ``source`` column records who wrote each row, in both schemas.
_SOURCE_TABLES: Final = (*(dataset.value for dataset in Dataset), "ingests")


class StoreError(RuntimeError):
    """The store cannot do what was asked, for a reason other than bad records."""


class StoreSchemaError(StoreError):
    """The file is not an rplat store, or was written under another schema version."""


class StoreBusyError(StoreError):
    """Another process holds the file open for writing, so it cannot be inspected."""


def _utcnow() -> datetime:
    """Wall-clock time for ``ingested_at``. A function so tests can freeze it."""
    return datetime.now(UTC)


class Store:
    """A bitemporal, append-only store over DuckDB.

    There is deliberately no ``update`` or ``delete``. The only way to change
    what the store says about a fact is to append a new belief about it with a
    later ``knowledge_date``; the old belief stays queryable forever. That is
    what lets :meth:`as_of` reconstruct any past view, and it is why a
    backfilled vendor correction cannot silently rewrite a backtest. This is a
    property of the API. The DuckDB file itself can still be edited by anyone
    who opens it with another tool.

    Usable as a context manager::

        with Store.open("research.duckdb") as store:
            store.ingest(FixtureSource())
            universe = store.as_of(Dataset.SECURITIES, date(2022, 1, 3))

    Open with ``read_only=True`` to query a file another process may also be
    reading. DuckDB lets several processes read one file only in read-only
    mode, and none of them can write while they do
    (https://duckdb.org/docs/current/connect/concurrency.html).
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection, *, read_only: bool = False) -> None:
        self._conn = conn
        self._read_only = read_only
        # DuckDB's session TimeZone defaults to the machine's zone, and casts
        # between zoned and unzoned values use it. Pin it so nothing this store
        # returns depends on the laptop. See rplat.clock.
        self._conn.execute("SET TimeZone = 'UTC'")
        if read_only:
            self._check_schema()
        else:
            self._init_schema()

    @classmethod
    def open(cls, path: str | Path | None = None, *, read_only: bool = False) -> Self:
        """Open a store at ``path``, or in memory when ``path`` is None.

        Args:
            path: Database file. Created (with its parent directory) when
                missing, unless ``read_only``.
            read_only: Open without write access and without running DDL. The
                file must already be an rplat store; a missing file raises
                :class:`FileNotFoundError` instead of creating an empty store
                that would answer every query with zero rows.
        """
        if path is None:
            if read_only:
                raise ValueError("an in-memory store opened read-only would always be empty")
            return cls(duckdb.connect(":memory:"))

        target = Path(path)
        if read_only:
            if not target.exists():
                raise FileNotFoundError(f"no store at {target}")
            conn = duckdb.connect(str(target), read_only=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            conn = duckdb.connect(str(target))
        try:
            return cls(conn, read_only=read_only)
        except BaseException:
            conn.close()
            raise

    @staticmethod
    def is_store_file(path: str | Path) -> bool:
        """Whether ``path`` is an rplat store, of any schema version, and nothing else.

        Used before anything destructive (``rplat ingest --force``) so a
        mistyped path to some other file is refused rather than replaced. It
        therefore errs towards "no". A file qualifies only when:

        * it is a DuckDB database (checked from its header, before opening);
        * its tables are exactly schema 1's six, or those six plus
          ``store_meta`` holding a ``schema_version`` row;
        * it holds no other table, in any schema, and no view.

        One familiar table name is not enough. This used to accept any
        database with a table called ``ingests``, and ``--force`` then replaced
        an unrelated database, every other table in it included.

        Returns:
            False for a missing path, a file that is not a DuckDB database, and
            a DuckDB database that fails the rules above.

        Raises:
            StoreBusyError: another process has the file open for writing, so
                DuckDB will not let it be opened to look. The answer is
                unknown, which is not the same as "no".
            StoreError: the file cannot be read, or has a DuckDB header but
                DuckDB cannot open it (for example, one written by a newer
                DuckDB). Also unknown.
        """
        target = Path(path)
        if not target.is_file() or not _has_duckdb_header(target):
            return False
        try:
            conn = duckdb.connect(str(target), read_only=True)
        except duckdb.Error as exc:
            if _is_lock_conflict(exc):
                raise StoreBusyError(
                    f"another process has {target} open for writing, so it cannot be "
                    f"inspected; close that process and retry ({exc})"
                ) from exc
            raise StoreError(
                f"{target} is a DuckDB file that DuckDB could not open: {exc}"
            ) from exc
        try:
            return _holds_only_store_tables(conn)
        finally:
            conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    @property
    def read_only(self) -> bool:
        """True when opened with ``read_only=True``."""
        return self._read_only

    # ── schema ─────────────────────────────────────────────────────────────

    def _has_table(self, name: str) -> bool:
        found = self._conn.execute(
            "SELECT count(*) FROM duckdb_tables() "
            "WHERE database_name = current_database() AND schema_name = 'main' "
            "AND table_name = ?",
            [name],
        ).fetchone()
        return bool(found and found[0])

    def _schema_version(self) -> int | None:
        if not self._has_table("store_meta"):
            return None
        found = self._conn.execute(
            "SELECT value FROM store_meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(found[0]) if found else None

    def _schema_problem(self, version: int | None) -> str | None:
        # Code writing schema 1 and code writing schema 2 both report package
        # version 0.1.0, so the message names the schema and the commit, never
        # "rplat 0.1.0".
        if version is None and self._has_table("ingests"):
            return (
                "this store uses schema 1 (rplat before the 2026-09 audit fixes, "
                f"commit 396572a), whose tie-break columns differ from schema {SCHEMA_VERSION}. "
                + self._rebuild_advice()
            )
        if version is not None and version > SCHEMA_VERSION:
            return (
                f"this store has schema version {version}, written by a newer rplat; "
                f"this rplat reads version {SCHEMA_VERSION}. Open it with the rplat "
                "that wrote it."
            )
        if version is not None and version != SCHEMA_VERSION:
            return (
                f"this store has schema version {version}; this rplat reads version "
                f"{SCHEMA_VERSION}. " + self._rebuild_advice()
            )
        return None

    def _row_sources(self) -> set[str] | None:
        """Every ``source`` recorded in the store, or None if they cannot be read."""
        present = [name for name in _SOURCE_TABLES if self._has_table(name)]
        if not present:
            return set()
        # Table names come from the Dataset enum plus "ingests"; no caller input.
        union = " UNION ".join(
            f"SELECT DISTINCT source FROM {name}"  # noqa: S608
            for name in present
        )
        try:
            rows = self._conn.execute(union).fetchall()
        except duckdb.Error:
            return None
        return {str(row[0]) for row in rows}

    def _rebuild_advice(self) -> str:
        """What to do about a store in another schema, without destroying data.

        ``rplat ingest --force`` rebuilds from the synthetic fixture and nothing
        else. Recommending it for a store holding anything else (a StooqSource
        ingest, a researcher's own appends) would replace that data with the
        fixture. So it is recommended only when every row came from the
        fixture, which makes the rebuild lossless.
        """
        sources = self._row_sources()
        if sources is not None and sources <= {FixtureSource.name}:
            return (
                "Every row in it came from the fixture, so rebuilding it loses nothing: "
                "`rplat ingest --force --db <path>`."
            )
        held = (
            "from sources other than the fixture "
            f"({', '.join(sorted(sources - {FixtureSource.name}))})"
            if sources is not None
            else "whose sources could not be read"
        )
        return (
            f"It holds data {held}. There is no migration: re-ingest from the original "
            "sources into a new file with this rplat. Do not run `rplat ingest --force` "
            "on it; that replaces the store with the synthetic fixture."
        )

    def _init_schema(self) -> None:
        version = self._schema_version()
        problem = self._schema_problem(version)
        if problem:
            raise StoreSchemaError(problem)
        self._conn.execute(SCHEMA_SQL)
        if version is None:
            self._conn.execute(
                "INSERT INTO store_meta VALUES ('schema_version', ?)", [str(SCHEMA_VERSION)]
            )

    def _check_schema(self) -> None:
        version = self._schema_version()
        problem = self._schema_problem(version)
        if problem:
            raise StoreSchemaError(problem)
        if version is None:
            raise StoreSchemaError("not an rplat store: it has no store_meta table")

    # ── writing ────────────────────────────────────────────────────────────

    def _require_writable(self) -> None:
        if self._read_only:
            raise StoreError("this store was opened read_only=True; reopen it writable to append")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """All-or-nothing. A failure anywhere inside rolls every write back.

        There is no delete path, so a half-written ingest could never be
        undone through the API. Atomicity is what makes that acceptable.
        """
        self._conn.begin()
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        self._conn.commit()

    def append(
        self,
        dataset: Dataset,
        records: Iterable[Record],
        *,
        source: str,
        ingest_id: str | None = None,
    ) -> int:
        """Append records atomically. Never updates, never deletes. Returns rows written.

        Records whose fact key already exists are still appended: that is a
        restatement, and keeping both is the point. Within one batch, a later
        record outranks an earlier one with the same key and ``knowledge_date``.

        Raises:
            RecordValidationError: some record is impossible (known before it
                happened, ``high < low``, a split with no ratio, …). Nothing
                from the batch is written.
        """
        self._require_writable()
        batch = list(records)
        with self._transaction():
            return self._append(dataset, batch, source=source, ingest_id=ingest_id)

    def _append(
        self,
        dataset: Dataset,
        batch: Sequence[object],
        *,
        source: str,
        ingest_id: str | None,
    ) -> int:
        """Validate and write one batch. Caller owns the transaction."""
        validate_records(dataset, batch)
        ingest_id = ingest_id or uuid.uuid4().hex
        now = _utcnow()
        seq_row = self._conn.execute("SELECT nextval('ingest_seq')").fetchone()
        if seq_row is None:  # nextval always returns a row; this keeps mypy honest
            raise StoreError("could not draw from the ingest_seq sequence")
        seq = int(seq_row[0])

        if batch:
            # validate_records has checked every element's class.
            records = cast("Sequence[Record]", batch)
            table = _to_arrow(
                dataset, records, source=source, ingest_id=ingest_id, now=now, seq=seq
            )
            columns = ", ".join(table.column_names)
            self._conn.register(_BATCH_VIEW, table)
            try:
                # Table name from the Dataset enum, columns from _COLUMNS.
                self._conn.execute(
                    f"INSERT INTO {dataset.value} ({columns}) SELECT {columns} FROM {_BATCH_VIEW}"  # noqa: S608
                )
            finally:
                self._conn.unregister(_BATCH_VIEW)

        self._conn.execute(
            "INSERT INTO ingests (ingest_id, ingest_seq, source, dataset, row_count, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [ingest_id, seq, source, dataset.value, len(batch), now],
        )
        return len(batch)

    def ingest(self, source: DataSource) -> dict[Dataset, int]:
        """Pull every dataset a source offers and append it, all or nothing.

        The source only has to implement the datasets it actually has, which is
        what keeps a bars-only vendor and a full fixture behind one interface.

        Every dataset is pulled *before* anything is written, and the writes
        share one transaction. A source that times out on its third dataset,
        or a batch that fails validation, leaves the store exactly as it was.
        """
        self._require_writable()
        pulled = {dataset: list(source.records(dataset)) for dataset in Dataset}
        ingest_id = uuid.uuid4().hex
        written: dict[Dataset, int] = {}
        with self._transaction():
            for dataset, records in pulled.items():
                if records:
                    written[dataset] = self._append(
                        dataset, records, source=source.name, ingest_id=ingest_id
                    )
        return written

    # ── reading ────────────────────────────────────────────────────────────

    def as_of(
        self,
        dataset: Dataset,
        as_of_date: date,
        *,
        security_ids: Sequence[str] | None = None,
        key_in: Mapping[str, Sequence[str]] | None = None,
        key_between: Mapping[str, tuple[date | None, date | None]] | None = None,
    ) -> pd.DataFrame:
        """Return the rows a researcher could have seen by the end of ``as_of_date``.

        Restated facts collapse to whichever belief was current then — not the
        one current today. See :mod:`rplat.clock` for exactly what "as of D"
        means.

        Args:
            dataset: Table to resolve.
            as_of_date: A :class:`datetime.date`. A ``datetime`` raises
                :class:`TypeError`.
            security_ids: Keep only these securities. Shorthand for
                ``key_in={"security_id": ...}``.
            key_in: ``{column: allowed values}`` for string fact-key columns.
            key_between: ``{column: (low, high)}`` for date fact-key columns,
                inclusive at both ends, either end optional.

        Filters are pushed into SQL *before* the as-of window, with every value
        bound as a parameter. Only fact-key columns are accepted. A key filter
        removes whole partitions and so cannot change which revision wins,
        while a filter on any other column could (see :func:`as_of_sql`).
        """
        require_date(as_of_date)
        keys = FACT_KEYS[dataset]
        params: dict[str, Any] = {"as_of": as_of_date}
        conditions: list[str] = []

        in_filters: dict[str, Sequence[str]] = dict(key_in or {})
        if security_ids is not None:
            if "security_id" in in_filters:
                raise ValueError("pass security_ids or key_in['security_id'], not both")
            in_filters["security_id"] = security_ids

        for index, (column, values) in enumerate(in_filters.items()):
            if column not in keys or column in DATE_KEY_COLUMNS:
                raise ValueError(
                    f"key_in accepts string fact-key columns of {dataset.value} "
                    f"{sorted(set(keys) - DATE_KEY_COLUMNS)}, not {column!r}"
                )
            wanted = _string_list(values, column)
            if not wanted:
                conditions.append("FALSE")
                continue
            name = f"in_{index}"
            params[name] = wanted
            conditions.append(f"list_contains(${name}, {column})")

        for index, (column, bounds) in enumerate((key_between or {}).items()):
            if column not in keys or column not in DATE_KEY_COLUMNS:
                raise ValueError(
                    f"key_between accepts date fact-key columns of {dataset.value} "
                    f"{sorted(set(keys) & DATE_KEY_COLUMNS)}, not {column!r}"
                )
            low, high = bounds
            if low is not None:
                params[f"lo_{index}"] = require_date(low, f"{column} lower bound")
                conditions.append(f"{column} >= $lo_{index}")
            if high is not None:
                params[f"hi_{index}"] = require_date(high, f"{column} upper bound")
                conditions.append(f"{column} <= $hi_{index}")

        sql = as_of_sql(dataset, extra_where=" AND ".join(conditions))
        return self._conn.execute(sql, params).df()

    def revisions(self, dataset: Dataset, **keys: Any) -> pd.DataFrame:
        """Every belief ever held about one fact, oldest first.

        The audit trail behind :meth:`as_of`: this is how you answer "when did
        this number change, and to what".
        """
        unknown = set(keys) - set(FACT_KEYS[dataset])
        if unknown:
            raise ValueError(f"not fact keys for {dataset.value}: {sorted(unknown)}")
        for name, value in keys.items():
            if name in DATE_KEY_COLUMNS:
                require_date(value, name)
        # Column names are allow-listed against FACT_KEYS above; values are bound.
        clause = " AND ".join(f"{k} = ${k}" for k in keys) or "TRUE"
        return self._conn.execute(
            f"SELECT * FROM {dataset.value} WHERE {clause} "  # noqa: S608
            "ORDER BY knowledge_date, ingest_seq, row_ordinal",
            keys,
        ).df()

    def table(self, dataset: Dataset) -> pd.DataFrame:
        """Raw table contents, every revision, no as-of filtering."""
        # Table name from the Dataset enum.
        return self._conn.execute(f"SELECT * FROM {dataset.value}").df()  # noqa: S608

    def row_count(self, dataset: Dataset) -> int:
        """Total rows stored for a dataset, across all revisions."""
        # Table name from the Dataset enum.
        result = self._conn.execute(f"SELECT count(*) FROM {dataset.value}").fetchone()  # noqa: S608
        return int(result[0]) if result else 0

    def sql(self, query: str, params: Mapping[str, Any] | None = None) -> pd.DataFrame:
        """Run one read-only SELECT for ad-hoc analysis.

        Anything else is refused: UPDATE, DELETE, DROP, INSERT, COPY, SET,
        ATTACH, EXPLAIN (``EXPLAIN ANALYZE`` executes its statement), and
        multi-statement strings. An ``UPDATE`` through this method used to
        rewrite history in place, contradicting the append-only contract.
        Bind values with ``params`` rather than formatting them into ``query``.
        """
        statements = self._conn.extract_statements(query)
        kinds = [statement.type.name for statement in statements]
        if kinds != ["SELECT"]:
            raise StoreError(
                f"Store.sql runs exactly one SELECT statement; got {kinds or 'nothing'}. "
                "The store is append-only: use Store.append to add a new belief."
            )
        return self._conn.execute(query, dict(params or {})).df()


def _has_duckdb_header(path: Path) -> bool:
    """Whether ``path`` starts like a DuckDB database file."""
    try:
        with path.open("rb") as handle:
            head = handle.read(_DUCKDB_MAGIC_OFFSET + len(_DUCKDB_MAGIC))
    except OSError as exc:
        # Unreadable is not "not a store"; say what actually went wrong.
        raise StoreError(f"cannot read {path}: {exc}") from exc
    return head[_DUCKDB_MAGIC_OFFSET:] == _DUCKDB_MAGIC


def _is_lock_conflict(exc: duckdb.Error) -> bool:
    """Whether DuckDB refused to open a file because another process holds its lock.

    Matched on the message because DuckDB raises a plain IOException for this
    and for other I/O failures. The wording was checked with DuckDB 1.0.0 and
    1.5.5 on macOS. On Windows, where DuckDB locks files differently, it is
    unverified. A miss only makes the error less specific: the caller still
    raises, with DuckDB's own message, instead of answering "not a store".
    """
    return isinstance(exc, duckdb.IOException) and "Could not set lock on file" in str(exc)


def _holds_only_store_tables(conn: duckdb.DuckDBPyConnection) -> bool:
    """The table-set rules of :meth:`Store.is_store_file`, on an open connection."""
    tables = conn.execute(
        "SELECT schema_name, table_name FROM duckdb_tables() "
        "WHERE database_name = current_database()"
    ).fetchall()
    views = conn.execute(
        "SELECT count(*) FROM duckdb_views() "
        "WHERE database_name = current_database() AND NOT internal"
    ).fetchone()
    if views and views[0]:
        return False
    if any(schema != "main" for schema, _ in tables):
        return False
    names = {str(name) for _, name in tables}
    if names == SCHEMA_1_TABLES:
        return True
    if names != STORE_TABLES:
        return False
    try:
        found = conn.execute(
            "SELECT count(*) FROM store_meta WHERE key = 'schema_version'"
        ).fetchone()
    except duckdb.Error:  # a store_meta with other columns is not ours
        return False
    return bool(found and found[0])


def _string_list(values: Sequence[str], column: str) -> list[str]:
    """Validate a key filter's values: a sequence of str, never a bare str."""
    if isinstance(values, str | bytes):
        raise TypeError(
            f"{column} filter must be a sequence of str, not a single {type(values).__name__}"
        )
    out = list(values)
    for value in out:
        if not isinstance(value, str):
            raise TypeError(f"{column} filter values must be str, got {type(value).__name__}")
    return out


def _to_arrow(
    dataset: Dataset,
    batch: Sequence[Record],
    *,
    source: str,
    ingest_id: str,
    now: datetime,
    seq: int,
) -> pa.Table:
    """One Arrow column per field. Columnar, so DuckDB inserts it in one pass."""
    arrays: list[pa.Array] = []
    names: list[str] = []
    for name, arrow_type in _COLUMNS[dataset]:
        values = [_field(record, name) for record in batch]
        arrays.append(pa.array(values, type=arrow_type))
        names.append(name)
    n = len(batch)
    provenance: tuple[tuple[str, pa.Array], ...] = (
        ("source", pa.array([source] * n, type=pa.string())),
        ("ingest_id", pa.array([ingest_id] * n, type=pa.string())),
        ("ingested_at", pa.array([now] * n, type=pa.timestamp("us", tz="UTC"))),
        ("ingest_seq", pa.array([seq] * n, type=pa.int64())),
        ("row_ordinal", pa.array(range(n), type=pa.int64())),
    )
    for name, array in provenance:
        names.append(name)
        arrays.append(array)
    return pa.Table.from_arrays(arrays, names=names)


def _field(record: Record, name: str) -> Any:
    """Read a field, mapping enum values to their string form for storage."""
    value = getattr(record, name)
    return value.value if hasattr(value, "value") else value
