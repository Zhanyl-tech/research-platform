"""As-of read cost on a large bars table: one session for every name.

Usage::

    .venv/bin/python bench/asof.py                      # 5,000,000 rows, 2,000 names
    .venv/bin/python bench/asof.py --revisions 3        # every bar restated twice

The table is bulk-loaded inside DuckDB from ``range()``. That bypasses
``Store.append`` and its validation, which is acceptable only because the
rows are synthetic and well formed by construction. The script then times:

* ``get_bars(store, d, start=d, end=d)``: the query the 2026-09 audit timed.
  A research job asks for one session and should not pay for the whole
  history.
* ``store.as_of(Dataset.BARS, d)``: the full as-of view, which does have to
  resolve every fact.

It runs against older rplat versions too (it adapts to the table's columns),
so the same command gives a before/after comparison.
"""

from __future__ import annotations

import argparse
import os
import platform
import statistics
import time
from collections.abc import Callable
from datetime import date, timedelta

import duckdb
import pandas as pd

import rplat
from rplat import Store, get_bars
from rplat.types import Dataset

START = date(2000, 1, 3)


def environment() -> str:
    # Kept in step with bench/ingest.py by hand; each script runs standalone.
    return (
        f"python {platform.python_version()} | rplat {rplat.__version__} | "
        f"duckdb {duckdb.__version__} | pandas {pd.__version__} | "
        f"{platform.system()} {platform.release()} {platform.machine()} | "
        f"{os.cpu_count()} logical CPUs"
    )


def load(store: Store, rows: int, names: int, revisions: int) -> None:
    """``rows`` distinct bars, each stored ``revisions`` times with later knowledge dates."""
    conn = store._conn  # bench-only bulk load; see module docstring
    columns = {
        row[0]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'bars'"
        ).fetchall()
    }
    has_seq = "ingest_seq" in columns  # schema 2 onwards
    for revision in range(revisions):
        seq_cols = ", ingest_seq, row_ordinal" if has_seq else ""
        seq_vals = f", {revision + 1}, i" if has_seq else ""
        conn.execute(
            f"""
            INSERT INTO bars (security_id, effective_date, open, high, low, close, volume,
                              knowledge_date, source, ingest_id, ingested_at{seq_cols})
            SELECT 'S' || lpad((i % {names})::VARCHAR, 5, '0'),
                   DATE '{START}' + (i // {names})::INT,
                   10, 11, 9, 10.5 + {revision}, 1000,
                   DATE '{START}' + (i // {names})::INT + {revision},
                   'bench', 'r{revision}', now(){seq_vals}
            FROM range({rows}) r(i)
            """  # noqa: S608 -- integers from argparse, no strings
        )


def timed(label: str, repeat: int, call: Callable[[], pd.DataFrame]) -> None:
    runs = []
    rows = 0
    for _ in range(repeat):
        started = time.perf_counter()
        rows = len(call())
        runs.append(time.perf_counter() - started)
    detail = ", ".join(f"{r:.3f}" for r in runs)
    print(f"{label}: {rows:,} rows returned; median {statistics.median(runs):.3f} s ({detail})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", type=int, default=5_000_000)
    parser.add_argument("--names", type=int, default=2_000)
    parser.add_argument("--revisions", type=int, default=1)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()

    print(environment())
    with Store.open(None) as store:
        started = time.perf_counter()
        load(store, args.rows, args.names, args.revisions)
        stored = store.row_count(Dataset.BARS)
        print(f"loaded {stored:,} stored rows in {time.perf_counter() - started:.1f} s")

        session = START + timedelta(days=(args.rows // args.names) // 2)
        timed(
            f"get_bars one session ({session}), all names",
            args.repeat,
            lambda: get_bars(store, session, start=session, end=session, adjust=False),
        )
        timed(
            f"store.as_of(BARS, {session}), full view",
            args.repeat,
            lambda: store.as_of(Dataset.BARS, session),
        )


if __name__ == "__main__":
    main()
