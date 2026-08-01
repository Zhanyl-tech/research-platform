"""Command line interface.

``rplat demo`` is the important one: it builds the store from the fixture and
then walks the four point-in-time traps, showing the same query returning
different — and correct — answers on different as-of dates.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import click
import pandas as pd

from rplat import __version__
from rplat.fundamentals import get_fundamentals, restatement_history
from rplat.prices import get_bars
from rplat.sources.fixture import (
    LATE_ARRIVED,
    LATE_FIRST,
    LATE_LAST,
    LATE_SECURITY,
    ORIGINAL_FILING,
    RESTATED_METRIC,
    RESTATED_PERIOD_END,
    RESTATED_SECURITY,
    RESTATEMENT_FILING,
    SPLIT_EX_DATE,
    SPLIT_SECURITY,
    FixtureSource,
)
from rplat.store.store import Store
from rplat.types import Dataset
from rplat.universe import get_universe, resolve_ticker

DEFAULT_DB = Path("data/research.duckdb")


#: click parses to datetime; commands narrow with _as_date.
DATE = click.DateTime(formats=["%Y-%m-%d"])


def _as_date(value: datetime | None) -> date | None:
    return value.date() if value is not None else None


def _show(frame: pd.DataFrame, *, limit: int = 20) -> None:
    if frame.empty:
        click.echo("    (no rows)")
        return
    with pd.option_context("display.width", 160, "display.max_columns", 24):
        text = frame.head(limit).to_string(index=False)
    click.echo("\n".join(f"    {line}" for line in text.splitlines()))
    if len(frame) > limit:
        click.echo(f"    … {len(frame) - limit} more rows")


def _rule(title: str) -> None:
    click.echo()
    click.secho(f"── {title} ", fg="cyan", bold=True, nl=False)
    click.secho("─" * max(0, 74 - len(title)), fg="cyan")


@click.group()
@click.version_option(__version__)
def main() -> None:
    """rplat — point-in-time research data."""


@main.command()
@click.option("--db", type=click.Path(path_type=Path), default=DEFAULT_DB, show_default=True)
@click.option("--force", is_flag=True, help="Rebuild even if the store already exists.")
def ingest(db: Path, force: bool) -> None:
    """Load the deterministic fixture dataset into a store."""
    if db.exists() and not force:
        click.echo(f"{db} already exists; pass --force to rebuild")
        return
    if db.exists():
        db.unlink()

    source = FixtureSource()
    click.echo(f"source: {source.describe()}")
    with Store.open(db) as store:
        written = store.ingest(source)
    for dataset, count in written.items():
        click.echo(f"  {dataset.value:<20} {count:>7,} rows")
    click.echo(f"wrote {db}")


@main.command()
@click.option("--db", type=click.Path(path_type=Path), default=DEFAULT_DB, show_default=True)
@click.option("--as-of", type=DATE, required=True)
@click.option("--include-delisted", is_flag=True)
def universe(db: Path, as_of: datetime, include_delisted: bool) -> None:
    """Show the universe knowable on a date."""
    when = as_of.date()
    with Store.open(db) as store:
        frame = get_universe(store, when, include_delisted=include_delisted)
    click.echo(f"universe as of {when}: {len(frame)} securities")
    _show(frame, limit=50)


@main.command()
@click.option("--db", type=click.Path(path_type=Path), default=DEFAULT_DB, show_default=True)
@click.option("--as-of", type=DATE, required=True)
@click.option("--security", "security_id", default=None)
@click.option("--start", type=DATE, default=None)
@click.option("--end", type=DATE, default=None)
@click.option("--raw", is_flag=True, help="Skip split adjustment.")
def bars(
    db: Path,
    as_of: datetime,
    security_id: str | None,
    start: datetime | None,
    end: datetime | None,
    raw: bool,
) -> None:
    """Show bars knowable on a date."""
    when = as_of.date()
    with Store.open(db) as store:
        frame = get_bars(
            store,
            when,
            security_ids=[security_id] if security_id else None,
            start=_as_date(start),
            end=_as_date(end),
            adjust=not raw,
        )
    click.echo(f"bars as of {when}: {len(frame)} rows")
    _show(frame)


@main.command()
@click.option("--db", type=click.Path(path_type=Path), default=DEFAULT_DB, show_default=True)
@click.option("--security", "security_id", required=True)
@click.option("--period-end", type=DATE, required=True)
@click.option("--metric", default="eps_diluted", show_default=True)
def history(db: Path, security_id: str, period_end: datetime, metric: str) -> None:
    """Show every value ever reported for one fundamental fact."""
    period = period_end.date()
    with Store.open(db) as store:
        frame = restatement_history(store, security_id, period, metric)
    click.echo(f"revisions of {security_id} {metric} for period ending {period}:")
    _show(frame.reindex(columns=["knowledge_date", "value", "source", "ingested_at"]))


@main.command()
@click.option("--db", type=click.Path(path_type=Path), default=DEFAULT_DB, show_default=True)
def demo(db: Path) -> None:
    """Build the store and walk every point-in-time trap it defends against."""
    source = FixtureSource()
    click.secho("\nBuilding the store from the deterministic fixture", bold=True)
    click.echo(f"  {source.describe()}")

    store = Store.open(None)  # in-memory: the demo never needs a file
    written = store.ingest(source)
    total = sum(written.values())
    click.echo(f"  {total:,} rows across {len(written)} datasets, no network, no credentials")
    if db:
        pass  # the --db option is accepted for symmetry; the demo stays in memory

    # ── 1. survivorship ────────────────────────────────────────────────────
    _rule("1. Survivorship bias")
    click.echo("  Northwind Freight goes bankrupt on 2023-03-10, announced 2023-02-24.")
    click.echo("  A universe built from today's security master would never show it.\n")
    for as_of in (date(2022, 6, 30), date(2023, 6, 30)):
        frame = get_universe(store, as_of)
        present = "NWF" in set(frame["ticker"].dropna())
        mark = click.style("present", fg="green") if present else click.style("absent", fg="yellow")
        click.echo(f"  as of {as_of}: {len(frame)} securities, Northwind {mark}")
    click.echo("\n  It is in the 2022 universe because on that date nobody knew it would fail.")

    # ── 2. ticker recycling ────────────────────────────────────────────────
    _rule("2. Ticker recycling")
    click.echo("  ZZZ is Vela Mining until it is acquired, then Zenith Robotics from 2023.\n")
    for as_of in (date(2021, 10, 1), date(2023, 6, 1)):
        sec = resolve_ticker(store, "ZZZ", as_of)
        names = store.as_of(Dataset.SECURITIES, as_of)
        matches = names.loc[names["security_id"] == sec, "name"]
        who = str(matches.iloc[0]) if not matches.empty else "nobody"
        click.echo(f"  as of {as_of}: ZZZ -> {sec or '—'}  ({who})")
    click.echo("\n  Keying a series on the ticker splices two unrelated companies together.")

    # ── 3. restatement ─────────────────────────────────────────────────────
    _rule("3. Restatement")
    click.echo(
        f"  Ardent files Q2-2022 diluted EPS on {ORIGINAL_FILING}, "
        f"then restates it on {RESTATEMENT_FILING}.\n"
    )
    for as_of in (date(2022, 9, 1), date(2022, 12, 1)):
        frame = get_fundamentals(
            store, as_of, security_ids=[RESTATED_SECURITY], metrics=[RESTATED_METRIC]
        )
        row = frame[frame["period_end"] == pd.Timestamp(RESTATED_PERIOD_END)]
        value = row.iloc[0]["value"] if not row.empty else float("nan")
        click.echo(f"  as of {as_of}: eps_diluted = {value}")
    click.echo("\n  Today's database shows only 1.20. A backtest run in September traded on 1.50.")

    # ── 4. split adjustment ────────────────────────────────────────────────
    _rule("4. Corporate actions")
    click.echo(
        f"  Beacon splits 2-for-1 on {SPLIT_EX_DATE}. A vendor 'adjusted close' downloaded\n"
        "  today has that split baked into prices from years earlier.\n"
    )
    session = date(2022, 8, 1)
    for as_of in (date(2022, 8, 20), date(2023, 1, 3)):
        frame = get_bars(store, as_of, security_ids=[SPLIT_SECURITY], start=session, end=session)
        if frame.empty:
            continue
        bar = frame.iloc[0]
        click.echo(
            f"  as of {as_of}: close on {session} = {bar['close']:.2f} "
            f"(factor {bar['adjustment_factor']:.1f})"
        )
    click.echo("\n  Same session, two legitimate answers. Only the as-of date resolves it.")

    # ── 5. late arrival ────────────────────────────────────────────────────
    _rule("5. Late-arriving data")
    click.echo(f"  Cirrus bars for {LATE_FIRST}..{LATE_LAST} were backfilled on {LATE_ARRIVED}.\n")
    for as_of in (LATE_LAST, LATE_ARRIVED):
        frame = get_bars(
            store, as_of, security_ids=[LATE_SECURITY], start=LATE_FIRST, end=LATE_LAST
        )
        click.echo(f"  as of {as_of}: {len(frame)} of 5 sessions visible")
    click.echo("\n  A pipeline running that week correctly saw nothing.")

    _rule("What this buys")
    click.echo(
        "  Every number above is reproducible from an as-of date alone. That is the\n"
        "  precondition for the rest of the platform: feature lineage (phase 2) and\n"
        "  the leakage detector (phase 3) are only meaningful if the data layer can\n"
        "  say what was knowable when.\n"
    )
    store.close()


if __name__ == "__main__":
    sys.exit(main())
