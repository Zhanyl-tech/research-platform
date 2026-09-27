"""DuckDB schema and the as-of resolution SQL.

Why DuckDB. The central operation here is "for each key, the newest row whose
knowledge_date is on or before D" — a windowed scan over an append-only,
column-heavy table. That is an analytical query, so an analytical engine is the
right shape: DuckDB runs it in-process with no server to operate, reads and
writes Parquet natively, and keeps the whole store a single file a researcher
can copy. SQLite would serve the same API but scans row-wise, and a warehouse
would mean infrastructure the demo promises not to need.

Append-only is a *convention enforced by the writer*, not a database
constraint — DuckDB has no such mode. :class:`rplat.store.Store` exposes no
update or delete path, its :meth:`~rplat.store.Store.sql` escape hatch runs a
single SELECT only, every table carries provenance columns, and a test asserts
that re-ingesting a changed value adds a row instead of replacing one. None of
that stops someone with the file and the ``duckdb`` CLI from editing it.

Provenance columns, on every fact table:

``source``
    The :attr:`~rplat.sources.base.DataSource.name` that wrote the row.
``ingest_id``
    One id per :meth:`~rplat.store.Store.ingest` (or per direct ``append``).
``ingested_at``
    Wall-clock time of the write, ``TIMESTAMPTZ`` in UTC. **Provenance only.**
    Wall clocks can repeat or step backwards (a coarse clock, an NTP
    correction), so nothing orders by it.
``ingest_seq``
    A value from a DuckDB sequence, drawn once per ``append``. It only ever
    increases within a store, so a later append always outranks an earlier one.
``row_ordinal``
    The record's position within its ``append`` batch. Among records sharing
    a fact key, a ``knowledge_date`` and an ``ingest_seq``, the later record in
    the batch is the later belief.
"""

from __future__ import annotations

from rplat.types import Dataset

#: Bumped whenever a table's columns change. A store written under another
#: version is refused rather than silently mis-read; see ``Store.open``.
#: 1 = rplat before the 2026-09 audit fixes (commit 396572a), no version table.
#: 2 = ingest_seq/row_ordinal, TIMESTAMPTZ. The code writing either one
#: reports package version 0.1.0, so messages name the schema, never the
#: package version.
SCHEMA_VERSION = 2

#: The tables schema 1 created. Schema 2 has the same six plus ``store_meta``.
SCHEMA_1_TABLES = frozenset(
    {"securities", "ticker_map", "bars", "corporate_actions", "fundamentals", "ingests"}
)

#: The tables :data:`SCHEMA_SQL` creates. ``tests/test_store.py`` asserts the
#: two agree, so a new table cannot be added to the DDL without updating this.
STORE_TABLES = SCHEMA_1_TABLES | {"store_meta"}

#: DDL for every table. Safe to run repeatedly.
SCHEMA_SQL = """
CREATE SEQUENCE IF NOT EXISTS ingest_seq START 1;

CREATE TABLE IF NOT EXISTS store_meta (
    key   VARCHAR NOT NULL PRIMARY KEY,
    value VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS securities (
    security_id      VARCHAR     NOT NULL,
    name             VARCHAR     NOT NULL,
    listing_date     DATE        NOT NULL,
    delisting_date   DATE,
    delisting_reason VARCHAR,
    knowledge_date   DATE        NOT NULL,
    source           VARCHAR     NOT NULL,
    ingest_id        VARCHAR     NOT NULL,
    ingested_at      TIMESTAMPTZ NOT NULL,
    ingest_seq       BIGINT      NOT NULL,
    row_ordinal      BIGINT      NOT NULL
);

CREATE TABLE IF NOT EXISTS ticker_map (
    security_id    VARCHAR     NOT NULL,
    ticker         VARCHAR     NOT NULL,
    start_date     DATE        NOT NULL,
    end_date       DATE,
    knowledge_date DATE        NOT NULL,
    source         VARCHAR     NOT NULL,
    ingest_id      VARCHAR     NOT NULL,
    ingested_at    TIMESTAMPTZ NOT NULL,
    ingest_seq     BIGINT      NOT NULL,
    row_ordinal    BIGINT      NOT NULL
);

CREATE TABLE IF NOT EXISTS bars (
    security_id    VARCHAR     NOT NULL,
    effective_date DATE        NOT NULL,
    open           DOUBLE      NOT NULL,
    high           DOUBLE      NOT NULL,
    low            DOUBLE      NOT NULL,
    close          DOUBLE      NOT NULL,
    volume         BIGINT      NOT NULL,
    knowledge_date DATE        NOT NULL,
    source         VARCHAR     NOT NULL,
    ingest_id      VARCHAR     NOT NULL,
    ingested_at    TIMESTAMPTZ NOT NULL,
    ingest_seq     BIGINT      NOT NULL,
    row_ordinal    BIGINT      NOT NULL
);

CREATE TABLE IF NOT EXISTS corporate_actions (
    security_id    VARCHAR     NOT NULL,
    effective_date DATE        NOT NULL,
    action_type    VARCHAR     NOT NULL,
    ratio          DOUBLE,
    amount         DOUBLE,
    knowledge_date DATE        NOT NULL,
    source         VARCHAR     NOT NULL,
    ingest_id      VARCHAR     NOT NULL,
    ingested_at    TIMESTAMPTZ NOT NULL,
    ingest_seq     BIGINT      NOT NULL,
    row_ordinal    BIGINT      NOT NULL
);

CREATE TABLE IF NOT EXISTS fundamentals (
    security_id    VARCHAR     NOT NULL,
    period_end     DATE        NOT NULL,
    fiscal_period  VARCHAR,
    metric         VARCHAR     NOT NULL,
    value          DOUBLE,
    knowledge_date DATE        NOT NULL,
    source         VARCHAR     NOT NULL,
    ingest_id      VARCHAR     NOT NULL,
    ingested_at    TIMESTAMPTZ NOT NULL,
    ingest_seq     BIGINT      NOT NULL,
    row_ordinal    BIGINT      NOT NULL
);

CREATE TABLE IF NOT EXISTS ingests (
    ingest_id   VARCHAR     NOT NULL,
    ingest_seq  BIGINT      NOT NULL,
    source      VARCHAR     NOT NULL,
    dataset     VARCHAR     NOT NULL,
    row_count   BIGINT      NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL
);
"""

#: The columns that identify one *fact* in each table. Two rows sharing these
#: values are two beliefs about the same fact at different times — a
#: restatement — and as-of resolution picks between them.
FACT_KEYS: dict[Dataset, tuple[str, ...]] = {
    Dataset.SECURITIES: ("security_id",),
    Dataset.TICKER_MAP: ("security_id", "ticker", "start_date"),
    Dataset.BARS: ("security_id", "effective_date"),
    Dataset.CORPORATE_ACTIONS: ("security_id", "effective_date", "action_type"),
    Dataset.FUNDAMENTALS: ("security_id", "period_end", "metric"),
}

#: Fact-key columns of type DATE. Range filters are allowed only on these.
DATE_KEY_COLUMNS = frozenset({"effective_date", "period_end", "start_date"})

#: Provenance columns present on every fact table.
PROVENANCE_COLUMNS = ("source", "ingest_id", "ingested_at", "ingest_seq", "row_ordinal")


def as_of_sql(dataset: Dataset, *, extra_where: str = "") -> str:
    """Return SQL selecting the rows knowable on a given as-of date.

    The parameter placeholder is ``$as_of``. For each fact key, this keeps the
    row with the greatest ``knowledge_date`` that does not exceed the as-of
    date. Ties break on ``ingest_seq`` (later append wins) and then
    ``row_ordinal`` (later record in the batch wins). A same-day correction
    therefore beats the value it corrects without trusting a wall clock.

    This single window is the entire point-in-time guarantee. Filtering
    ``knowledge_date <= $as_of`` alone is not enough — that would return *every*
    historical belief, including superseded ones. Taking the newest surviving
    row per key is what reconstructs the researcher's view on that date.

    ``extra_where`` must reference **fact-key columns only**, with every value
    bound as a parameter; :meth:`rplat.store.Store.as_of` enforces both. A
    filter on a key column removes whole partitions, so it cannot change which
    revision wins inside a partition that survives. A filter on a non-key
    column (``value``, say) could drop the newest revision and resurrect a
    superseded one, which is why it is not allowed. It is wrapped in
    parentheses so an ``OR`` inside it can never widen the result past the
    ``knowledge_date`` filter.
    """
    keys = ", ".join(FACT_KEYS[dataset])
    where = "WHERE knowledge_date <= $as_of"
    if extra_where:
        where += f" AND ({extra_where})"
    # Identifiers come from the Dataset enum and FACT_KEYS; extra_where is built
    # by Store.as_of from allow-listed key columns and $-placeholders only.
    return f"""
        SELECT * EXCLUDE (_rn)
        FROM (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY {keys}
                       ORDER BY knowledge_date DESC, ingest_seq DESC, row_ordinal DESC
                   ) AS _rn
            FROM {dataset.value}
            {where}
        )
        WHERE _rn = 1
    """  # noqa: S608
