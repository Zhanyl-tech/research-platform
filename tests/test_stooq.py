"""StooqSource, offline: a hand-written sample CSV and a faked urlopen. No network is touched.

Nothing here was captured from Stooq. The one live request made during the
2026-09 audit got a bot-check page, so the parser has not been run against a
real Stooq CSV since. These tests pin the parsing of the documented column
layout, not agreement with the live service.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from datetime import date
from email.message import Message
from types import TracebackType
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from rplat.prices import get_bars
from rplat.sources.stooq import StooqFetchError, StooqSource, stooq_url
from rplat.store.store import Store
from rplat.types import BarRecord, Dataset

#: Written by hand in Stooq's daily column layout (Date,Open,High,Low,Close,Volume).
#: The fourth row is malformed and must be skipped.
SAMPLE_CSV = """Date,Open,High,Low,Close,Volume
2024-01-02,187.15,188.44,183.89,185.64,82488700
2024-01-03,184.22,185.88,183.43,184.25,58414500
2024-01-04,182.15,183.09,180.88,181.91,71983600
2024-01-05,not-a-number,1,1,1,1
"""

#: Modelled on the start of the page one request received during the 2026-09
#: audit. Only its opening (`<!DOCTYPE html>` and "This site requires JavaScript
#: to verify your") was noted then; the rest is filled in by hand.
CHALLENGE_PAGE = (
    "<!DOCTYPE html><html><head><title>stooq</title></head>"
    "<body>This site requires JavaScript to verify your browser.</body></html>"
)


class _Response:
    def __init__(self, body: str, content_type: str) -> None:
        self._body = body.encode()
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


def _serve(monkeypatch: pytest.MonkeyPatch, body: str, content_type: str = "text/csv") -> list[str]:
    """Replace urlopen; return the list the requested URLs are recorded into."""
    seen: list[str] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _Response:
        seen.append(request.full_url)
        return _Response(body, content_type)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


class TestUrl:
    def test_symbol_is_url_encoded_into_the_query(self) -> None:
        parts = urlsplit(stooq_url("aapl.us"))
        assert (parts.scheme, parts.netloc, parts.path) == ("https", "stooq.com", "/q/d/l/")
        assert parse_qs(parts.query) == {"s": ["aapl.us"], "i": ["d"]}

    @pytest.mark.parametrize(
        "symbol",
        [
            "aapl.us&i=w",  # used to add a second `i` and fetch weekly bars
            "aapl.us#",  # used to push `&i=d` into the fragment
            "file:///etc/passwd",
            "aapl us",
            "aapl.us?x=1",
            "",
        ],
    )
    def test_symbols_that_could_reshape_the_url_are_refused(self, symbol: str) -> None:
        with pytest.raises(ValueError, match="invalid Stooq symbol"):
            StooqSource([symbol])
        with pytest.raises(ValueError, match="invalid Stooq symbol"):
            stooq_url(symbol)


class TestSampleResponse:
    def test_parses_bars_and_skips_bad_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _serve(monkeypatch, SAMPLE_CSV)
        source = StooqSource(["aapl.us"])
        bars = list(source.records(Dataset.BARS))
        assert seen == [stooq_url("aapl.us")]
        assert len(bars) == 3
        first = bars[0]
        assert isinstance(first, BarRecord)
        assert first.security_id == "STOOQ:AAPL.US"
        assert first.effective_date == date(2024, 1, 2)
        # The documented assumption (see caveats()): known on the session date.
        assert first.knowledge_date == first.effective_date
        assert first.volume == 82488700

    def test_fetches_once_per_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _serve(monkeypatch, SAMPLE_CSV)
        source = StooqSource(["aapl.us"])
        list(source.records(Dataset.BARS))
        securities = list(source.records(Dataset.SECURITIES))
        assert len(seen) == 1
        assert len(securities) == 1
        assert list(source.records(Dataset.FUNDAMENTALS)) == []

    def test_ingests_into_a_store(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _serve(monkeypatch, SAMPLE_CSV)
        with Store.open(None) as store:
            written = store.ingest(StooqSource(["aapl.us"]))
            assert written == {Dataset.SECURITIES: 1, Dataset.BARS: 3}
            frame = get_bars(store, date(2024, 1, 3), security_ids=["STOOQ:AAPL.US"])
            assert len(frame) == 2  # the 2024-01-04 bar is not known yet

    def test_listing_date_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _serve(monkeypatch, SAMPLE_CSV)
        source = StooqSource(["aapl.us"], listing_date=date(1980, 12, 12))
        (security,) = source.records(Dataset.SECURITIES)
        assert security.listing_date == date(1980, 12, 12)  # type: ignore[attr-defined]


class TestFailures:
    @pytest.mark.parametrize("content_type", ["text/html", "text/plain"])
    def test_html_challenge_page_is_named_as_such(
        self, monkeypatch: pytest.MonkeyPatch, content_type: str
    ) -> None:
        # Detected by header or by body, since a challenge page may be mislabelled.
        _serve(monkeypatch, CHALLENGE_PAGE, content_type)
        with pytest.raises(StooqFetchError, match=r"returned an HTML page.*not CSV"):
            list(StooqSource(["aapl.us"]).records(Dataset.BARS))

    def test_other_non_csv_bodies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _serve(monkeypatch, "No data", "text/plain")
        with pytest.raises(StooqFetchError, match="unexpected response"):
            list(StooqSource(["zzzz.us"]).records(Dataset.BARS))

    def test_network_errors_are_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def unreachable(request: Any, timeout: float) -> _Response:
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(urllib.request, "urlopen", unreachable)
        with pytest.raises(StooqFetchError, match=r"could not fetch aapl\.us"):
            list(StooqSource(["aapl.us"]).records(Dataset.BARS))


def test_caveats_are_stated() -> None:
    caveats = StooqSource.caveats()
    assert any("knowledge_date is assumed" in c for c in caveats)
    assert "2 symbol(s)" in StooqSource(["a.us", "b.us"]).describe()
