"""A real, free, no-credentials price source — proof the interface swaps.

Stooq serves daily bars as CSV over HTTPS with no API key, which makes it the
honest choice for demonstrating that :class:`~rplat.sources.base.DataSource`
is a real seam and not a single-implementation abstraction.

Whether it serves them *to you* is another matter. During the 2026-09 audit, a
single request from one machine got an HTML "this site requires JavaScript to
verify" page instead of CSV. That is one observation from one IP; it may not
reproduce elsewhere and has not been re-checked since. :class:`StooqSource`
reports that case as its own error rather than a generic parse failure. The
parsing path is tested offline in ``tests/test_stooq.py`` against a hand-written
CSV in Stooq's column layout (Date,Open,High,Low,Close,Volume). No Stooq
response was captured, and the parser has not been run against a live one
since the audit.

It is also a good illustration of the interface's *point*, because it cannot
supply most of what the platform wants. There is no restatement history, no
delisting metadata, and no filing dates. So this source implements bars and a
minimal security record, returns nothing for the rest, and — importantly —
stamps ``knowledge_date`` equal to the session date, which is a claim about the
data that is only approximately true.

That approximation is documented rather than hidden: see :meth:`StooqSource.caveats`.
Never used by ``make demo``, which must run offline and deterministically.
"""

from __future__ import annotations

import csv
import io
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from datetime import date, datetime

from rplat.sources.base import DataSource
from rplat.types import BarRecord, Dataset, SecurityRecord

STOOQ_BASE_URL = "https://stooq.com/q/d/l/"
DEFAULT_TIMEOUT = 20.0

#: What a symbol may contain. ``&``, ``#``, ``?``, ``=``, ``/`` and spaces are
#: out: a symbol like ``aapl.us&i=w`` used to add its own query parameter
#: (silently fetching weekly bars), and ``aapl.us#`` pushed ``&i=d`` into the
#: URL fragment.
SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


class StooqFetchError(RuntimeError):
    """Raised when Stooq cannot be reached or returns something unusable."""


class StooqSource(DataSource):
    """Daily bars from Stooq for a fixed list of symbols.

    Args:
        symbols: Stooq symbols, e.g. ``["aapl.us", "msft.us"]``.
        listing_date: Date to record as the securities' listing date. Stooq does
            not publish one; the earliest bar is used when omitted.
        timeout: Per-request timeout in seconds.
    """

    name = "stooq"

    def __init__(
        self,
        symbols: list[str],
        *,
        listing_date: date | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        bad = [symbol for symbol in symbols if not SYMBOL_PATTERN.fullmatch(symbol)]
        if bad:
            raise ValueError(
                f"invalid Stooq symbol(s) {bad!r}: letters, digits, '.', '_' and '-' only"
            )
        self.symbols = symbols
        self.listing_date = listing_date
        self.timeout = timeout
        self._cache: dict[str, list[BarRecord]] = {}

    def describe(self) -> str:
        return f"Stooq daily bars for {len(self.symbols)} symbol(s), no credentials required"

    @staticmethod
    def caveats() -> list[str]:
        """What this source cannot honestly promise.

        Worth reading before using it for anything but a smoke test — each of
        these is a place where the point-in-time guarantee degrades to an
        assumption.
        """
        return [
            "knowledge_date is assumed equal to the session date. Stooq does not "
            "publish when a bar was made available, so a late-corrected bar is "
            "indistinguishable from one that was right the first time.",
            "Prices are already split-adjusted by the vendor, using every split "
            "up to today. That is precisely the look-ahead this platform avoids "
            "with raw prices plus as-of factors, and it cannot be undone without "
            "a corporate-actions feed.",
            "No delisted symbols, so a universe built from Stooq alone is "
            "survivorship-biased by construction.",
            "No fundamentals and therefore no restatement history.",
        ]

    def records(self, dataset: Dataset) -> Iterable[object]:
        match dataset:
            case Dataset.BARS:
                return [bar for symbol in self.symbols for bar in self._bars(symbol)]
            case Dataset.SECURITIES:
                return self._securities()
            case _:
                return ()

    def _securities(self) -> list[SecurityRecord]:
        out: list[SecurityRecord] = []
        for symbol in self.symbols:
            bars = self._bars(symbol)
            if not bars:
                continue
            first = self.listing_date or min(bar.effective_date for bar in bars)
            out.append(
                SecurityRecord(
                    security_id=_security_id(symbol),
                    name=symbol.upper(),
                    listing_date=first,
                    knowledge_date=first,
                )
            )
        return out

    def _bars(self, symbol: str) -> list[BarRecord]:
        if symbol in self._cache:
            return self._cache[symbol]

        url = stooq_url(symbol)
        try:
            # S310 is about opening file:// or custom schemes. The scheme here
            # is fixed by STOOQ_BASE_URL and the symbol can only reach the
            # query string, URL-encoded, after SYMBOL_PATTERN has vetted it.
            request = urllib.request.Request(url, headers={"User-Agent": "rplat/0.1"})  # noqa: S310
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                content_type = response.headers.get_content_type()
                payload = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError) as exc:
            raise StooqFetchError(f"could not fetch {symbol} from Stooq: {exc}") from exc

        head = payload.lstrip()[:200].lower()
        if content_type == "text/html" or head.startswith(("<!doctype html", "<html")):
            raise StooqFetchError(
                f"Stooq returned an HTML page for {symbol}, not CSV (content-type "
                f"{content_type!r}). This has been seen as a JavaScript bot-verification "
                "challenge; StooqSource cannot pass one. Try later or from another network."
            )
        if not head.startswith("date"):
            raise StooqFetchError(
                f"unexpected response for {symbol} (rate limited or unknown symbol): "
                f"{payload[:80]!r}"
            )

        security_id = _security_id(symbol)
        bars: list[BarRecord] = []
        for row in csv.DictReader(io.StringIO(payload)):
            try:
                session = datetime.strptime(row["Date"], "%Y-%m-%d").date()
                bars.append(
                    BarRecord(
                        security_id=security_id,
                        effective_date=session,
                        open=float(row["Open"]),
                        high=float(row["High"]),
                        low=float(row["Low"]),
                        close=float(row["Close"]),
                        volume=int(float(row.get("Volume") or 0)),
                        # See caveats(): an assumption, not a published fact.
                        knowledge_date=session,
                    )
                )
            except (KeyError, ValueError):
                continue

        self._cache[symbol] = bars
        return bars


def stooq_url(symbol: str) -> str:
    """The daily-bars CSV URL for one symbol, with the symbol URL-encoded."""
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError(f"invalid Stooq symbol {symbol!r}")
    return f"{STOOQ_BASE_URL}?{urllib.parse.urlencode({'s': symbol, 'i': 'd'})}"


def _security_id(symbol: str) -> str:
    """Stable synthetic id for a Stooq symbol."""
    return f"STOOQ:{symbol.upper()}"
