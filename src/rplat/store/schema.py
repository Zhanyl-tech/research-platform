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
update or delete path, every table carries ``ingest_id``/``ingested_at``
provenance, and a test asserts that re-ingesting a changed value adds a row
instead of replacing one.
"""

from __future__ import annotations

from rplat.types import Dataset

#: DDL for every table. Safe to run repeatedly.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS securities (
    security_id      VARCHAR   NOT NULL,
    name             VARCHAR   NOT NULL,
    listing_date     DATE      NOT NULL,
    delisting_date   DATE,
    delisting_reason VARCHAR,
    knowledge_date   DATE      NOT NULL,
    source           VARCHAR   NOT NULL,
    ingest_id        VARCHAR   NOT NULL,
    ingested_at      TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS ticker_map (
    security_id    VARCHAR   NOT NULL,
    ticker         VARCHAR   NOT NULL,
    start_date     DATE      NOT NULL,
    end_date       DATE,
    knowledge_date DATE      NOT NULL,
    source         VARCHAR   NOT NULL,
    ingest_id      VARCHAR   NOT NULL,
    ingested_at    TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS bars (
    security_id    VARCHAR   NOT NULL,
    effective_date DATE      NOT NULL,
    open           DOUBLE    NOT NULL,
    high           DOUBLE    NOT NULL,
    low            DOUBLE    NOT NULL,
    close          DOUBLE    NOT NULL,
    volume         BIGINT    NOT NULL,
    knowledge_date DATE      NOT NULL,
    source         VARCHAR   NOT NULL,
    ingest_id      VARCHAR   NOT NULL,
    ingested_at    TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS corporate_actions (
    security_id    VARCHAR   NOT NULL,
    effective_date DATE      NOT NULL,
    action_type    VARCHAR   NOT NULL,
    ratio          DOUBLE,
    amount         DOUBLE,
    knowledge_date DATE      NOT NULL,
    source         VARCHAR   NOT NULL,
    ingest_id      VARCHAR   NOT NULL,
    ingested_at    TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS fundamentals (
    security_id    VARCHAR   NOT NULL,
    period_end     DATE      NOT NULL,
    fiscal_period  VARCHAR,
    metric         VARCHAR   NOT NULL,
    value          DOUBLE,
    knowledge_date DATE      NOT NULL,
    source         VARCHAR   NOT NULL,
    ingest_id      VARCHAR   NOT NULL,
    ingested_at    TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS ingests (
    ingest_id   VARCHAR   NOT NULL,
    source      VARCHAR   NOT NULL,
    dataset     VARCHAR   NOT NULL,
    row_count   BIGINT    NOT NULL,
    ingested_at TIMESTAMP NOT NULL
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

#: Provenance columns present on every fact table.
PROVENANCE_COLUMNS = ("source", "ingest_id", "ingested_at")


def as_of_sql(dataset: Dataset, *, extra_where: str = "") -> str:
    """Return SQL selecting the rows knowable on a given as-of date.

    The parameter placeholder is ``$as_of``. For each fact key, this keeps the
    row with the greatest ``knowledge_date`` that does not exceed the as-of
    date; ties break on ``ingested_at`` so a same-day correction still wins over
    the value it corrects.

    This single window is the entire point-in-time guarantee. Filtering
    ``knowledge_date <= $as_of`` alone is not enough — that would return *every*
    historical belief, including superseded ones. Taking the newest surviving
    row per key is what reconstructs the researcher's view on that date.
    """
    keys = ", ".join(FACT_KEYS[dataset])
    where = f"WHERE knowledge_date <= $as_of{f' AND {extra_where}' if extra_where else ''}"
    return f"""
        SELECT * EXCLUDE (_rn)
        FROM (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY {keys}
                       ORDER BY knowledge_date DESC, ingested_at DESC
                   ) AS _rn
            FROM {dataset.value}
            {where}
        )
        WHERE _rn = 1
    """
