"""Universe construction — the survivorship-safe answer to "what existed then"."""

from __future__ import annotations

from datetime import date

import pandas as pd

from rplat.store.store import Store
from rplat.types import Dataset

UNIVERSE_COLUMNS = [
    "security_id",
    "ticker",
    "name",
    "listing_date",
    "delisting_date",
    "delisting_reason",
]


def get_universe(store: Store, as_of_date: date, *, include_delisted: bool = False) -> pd.DataFrame:
    """Return exactly the securities a researcher could have traded on a date.

    Survivorship bias is not avoided here by remembering to include dead names.
    It is avoided structurally, by resolving the ``securities`` table *as of the
    query date*: a company that goes bankrupt in 2023 has two rows, and the one
    visible in 2021 has no ``delisting_date`` at all. The filter below therefore
    cannot exclude it, because on that date nobody knew it was going to die.

    A pipeline that instead reads today's security master and filters
    ``delisting_date IS NULL`` builds a universe of survivors, and every
    backtest on it is measuring the returns of companies selected for not going
    bankrupt.

    Ticker is resolved as of the same date, so ``ZZZ`` maps to whichever company
    held it then — not whichever holds it now.

    Boundaries: ``listing_date`` is inclusive and ``delisting_date`` is
    exclusive (the first date the name no longer trades). So a name is in the
    universe on its last trading session, and every bar *for session D* that
    is knowable as of D belongs to a name in D's universe. The fixture tape and
    a test enforce that. It does not extend to older bars: as of 2023-06-30,
    Northwind's 2021-2023 history is knowable, but Northwind is no longer in
    the universe.

    Args:
        store: The bitemporal store to read.
        as_of_date: The date to reconstruct, a :class:`datetime.date` (see
            :mod:`rplat.clock`).
        include_delisted: When True, also return names already delisted by
            ``as_of_date``. Useful for building a survivorship *test*, and for
            attributing returns to names that have since died.

    Returns:
        One row per security, with the ticker in force on ``as_of_date``.
    """
    securities = store.as_of(Dataset.SECURITIES, as_of_date)
    if securities.empty:
        return pd.DataFrame(columns=UNIVERSE_COLUMNS)

    listed = securities["listing_date"] <= pd.Timestamp(as_of_date)
    if include_delisted:
        alive = listed
    else:
        # NaT compares False, which is what we want: a security with no known
        # delisting date is still alive. Strict ">" because delisting_date is
        # the first non-trading date (exclusive end; see SecurityRecord).
        not_yet_dead = securities["delisting_date"].isna() | (
            securities["delisting_date"] > pd.Timestamp(as_of_date)
        )
        alive = listed & not_yet_dead
    securities = securities.loc[alive]

    tickers = store.as_of(Dataset.TICKER_MAP, as_of_date)
    if not tickers.empty:
        in_force = (tickers["start_date"] <= pd.Timestamp(as_of_date)) & (
            tickers["end_date"].isna() | (tickers["end_date"] > pd.Timestamp(as_of_date))
        )
        tickers = tickers.loc[in_force, ["security_id", "ticker"]]
    else:
        tickers = pd.DataFrame(columns=["security_id", "ticker"])

    merged = securities.merge(tickers, on="security_id", how="left")
    return (
        merged.reindex(columns=UNIVERSE_COLUMNS).sort_values("security_id").reset_index(drop=True)
    )


def resolve_ticker(store: Store, ticker: str, as_of_date: date) -> str | None:
    """Map a ticker to the security that held it on a date, or None.

    Tickers are recycled. Keying a time series on the symbol splices unrelated
    companies together at the seam, and the resulting series looks like a
    violent gap rather than an error — which is why identity lives on
    ``security_id`` and symbols are resolved per date.
    """
    tickers = store.as_of(Dataset.TICKER_MAP, as_of_date)
    if tickers.empty:
        return None
    stamp = pd.Timestamp(as_of_date)
    match = tickers[
        (tickers["ticker"] == ticker)
        & (tickers["start_date"] <= stamp)
        & (tickers["end_date"].isna() | (tickers["end_date"] > stamp))
    ]
    if match.empty:
        return None
    return str(match.iloc[0]["security_id"])
