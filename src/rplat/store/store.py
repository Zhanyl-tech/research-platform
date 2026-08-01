"""The append-only bitemporal store."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import duckdb
import pandas as pd

from rplat.store.schema import FACT_KEYS, SCHEMA_SQL, as_of_sql
from rplat.types import (
    BarRecord,
    CorporateActionRecord,
    Dataset,
    FundamentalRecord,
    SecurityRecord,
    TickerRecord,
)

#: Column order per table, matching SCHEMA_SQL. Insertion is positional, so this
#: is the one place the order is defined.
_COLUMNS: dict[Dataset, tuple[str, ...]] = {
    Dataset.SECURITIES: (
        "security_id",
        "name",
        "listing_date",
        "delisting_date",
        "delisting_reason",
        "knowledge_date",
    ),
    Dataset.TICKER_MAP: (
        "security_id",
        "ticker",
        "start_date",
        "end_date",
        "knowledge_date",
    ),
    Dataset.BARS: (
        "security_id",
        "effective_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "knowledge_date",
    ),
    Dataset.CORPORATE_ACTIONS: (
        "security_id",
        "effective_date",
        "action_type",
        "ratio",
        "amount",
        "knowledge_date",
    ),
    Dataset.FUNDAMENTALS: (
        "security_id",
        "period_end",
        "fiscal_period",
        "metric",
        "value",
        "knowledge_date",
    ),
}

_RecordT = SecurityRecord | TickerRecord | BarRecord | CorporateActionRecord | FundamentalRecord


class Store:
    """A bitemporal, append-only store over DuckDB.

    There is deliberately no ``update`` or ``delete``. The only way to change
    what the store says about a fact is to append a new belief about it with a
    later ``knowledge_date``; the old belief stays queryable forever. That is
    what lets :meth:`as_of` reconstruct any past view, and it is why a
    backfilled vendor correction cannot silently rewrite a backtest.

    Usable as a context manager::

        with Store.open("research.duckdb") as store:
            store.ingest(FixtureSource())
            universe = store.as_of(Dataset.SECURITIES, date(2022, 1, 3))
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        self._conn.execute(SCHEMA_SQL)

    @classmethod
    def open(cls, path: str | Path | None = None) -> Self:
        """Open a store at ``path``, or in memory when ``path`` is None."""
        target = ":memory:" if path is None else str(path)
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        return cls(duckdb.connect(target))

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

    # ── writing ────────────────────────────────────────────────────────────

    def append(
        self,
        dataset: Dataset,
        records: Iterable[_RecordT],
        *,
        source: str,
        ingest_id: str | None = None,
    ) -> int:
        """Append records. Never updates, never deletes. Returns rows written.

        Records whose fact key already exists are still appended: that is a
        restatement, and keeping both is the point.
        """
        ingest_id = ingest_id or uuid.uuid4().hex
        now = datetime.now(UTC).replace(tzinfo=None)
        columns = _COLUMNS[dataset]

        rows: list[tuple[Any, ...]] = []
        for record in records:
            values = [_field(record, name) for name in columns]
            values.extend([source, ingest_id, now])
            rows.append(tuple(values))

        if rows:
            placeholders = ", ".join("?" * (len(columns) + 3))
            self._conn.executemany(f"INSERT INTO {dataset.value} VALUES ({placeholders})", rows)
        self._conn.execute(
            "INSERT INTO ingests VALUES (?, ?, ?, ?, ?)",
            [ingest_id, source, dataset.value, len(rows), now],
        )
        return len(rows)

    def ingest(self, source: Any) -> dict[Dataset, int]:
        """Pull every dataset a source offers and append it.

        The source only has to implement the datasets it actually has, which is
        what keeps a bars-only vendor and a full fixture behind one interface.
        """
        ingest_id = uuid.uuid4().hex
        written: dict[Dataset, int] = {}
        for dataset in Dataset:
            records = list(source.records(dataset))
            if records:
                written[dataset] = self.append(
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
    ) -> pd.DataFrame:
        """Return the rows a researcher could have seen on ``as_of_date``.

        Restated facts collapse to whichever belief was current then — not the
        one current today.
        """
        extra = ""
        params: dict[str, Any] = {"as_of": as_of_date}
        if security_ids is not None:
            listed = ", ".join(f"'{sid}'" for sid in security_ids)
            extra = f"security_id IN ({listed})" if listed else "1 = 0"
        sql = as_of_sql(dataset, extra_where=extra)
        return self._conn.execute(sql, params).df()

    def revisions(self, dataset: Dataset, **keys: Any) -> pd.DataFrame:
        """Every belief ever held about one fact, oldest first.

        The audit trail behind :meth:`as_of`: this is how you answer "when did
        this number change, and to what".
        """
        unknown = set(keys) - set(FACT_KEYS[dataset])
        if unknown:
            raise ValueError(f"not fact keys for {dataset.value}: {sorted(unknown)}")
        clause = " AND ".join(f"{k} = ${k}" for k in keys) or "1 = 1"
        return self._conn.execute(
            f"SELECT * FROM {dataset.value} WHERE {clause} ORDER BY knowledge_date, ingested_at",
            keys,
        ).df()

    def table(self, dataset: Dataset) -> pd.DataFrame:
        """Raw table contents, every revision, no as-of filtering."""
        return self._conn.execute(f"SELECT * FROM {dataset.value}").df()

    def row_count(self, dataset: Dataset) -> int:
        """Total rows stored for a dataset, across all revisions."""
        result = self._conn.execute(f"SELECT count(*) FROM {dataset.value}").fetchone()
        return int(result[0]) if result else 0

    def sql(self, query: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
        """Escape hatch for ad-hoc analysis against the store."""
        return self._conn.execute(query, params or {}).df()


def _field(record: _RecordT, name: str) -> Any:
    """Read a field, mapping enum values to their string form for storage."""
    value = getattr(record, name)
    return value.value if hasattr(value, "value") else value
