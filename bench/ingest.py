"""Append throughput: how fast ``Store.append`` writes bars into an in-memory store.

Usage::

    .venv/bin/python bench/ingest.py                 # 100,000 bars, 3 repeats
    .venv/bin/python bench/ingest.py --rows 1000000

Prints the interpreter, library versions and platform first, so a number
quoted from this script always travels with the machine it came from. Record
construction is excluded from the timing; validation and the insert are not.
The script only uses ``Store.open`` and ``Store.append``, so it also runs
against older rplat versions for a before/after comparison.
"""

from __future__ import annotations

import argparse
import os
import platform
import statistics
import time
from datetime import date, timedelta

import duckdb
import pandas as pd

import rplat
from rplat import Store
from rplat.types import BarRecord, Dataset


def environment() -> str:
    return (
        f"python {platform.python_version()} | rplat {rplat.__version__} | "
        f"duckdb {duckdb.__version__} | pandas {pd.__version__} | "
        f"{platform.system()} {platform.release()} {platform.machine()} | "
        f"{os.cpu_count()} logical CPUs"
    )


def make_bars(rows: int, names: int) -> list[BarRecord]:
    """``rows`` well-formed bars: ``names`` securities over consecutive days."""
    start = date(2000, 1, 3)
    out = []
    for i in range(rows):
        day = start + timedelta(days=i // names)
        out.append(BarRecord(f"S{i % names:05d}", day, 10.0, 11.0, 9.0, 10.5, 1000, day))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--names", type=int, default=500)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()

    print(environment())
    records = make_bars(args.rows, args.names)
    rates = []
    for attempt in range(1, args.repeat + 1):
        with Store.open(None) as store:
            started = time.perf_counter()
            store.append(Dataset.BARS, records, source="bench")
            elapsed = time.perf_counter() - started
            if store.row_count(Dataset.BARS) != args.rows:
                raise SystemExit("row count mismatch")
        rates.append(args.rows / elapsed)
        print(f"run {attempt}: append {args.rows:,} bars in {elapsed:.2f} s")
    print(f"median {statistics.median(rates):,.0f} rows/s over {args.repeat} runs")


if __name__ == "__main__":
    main()
