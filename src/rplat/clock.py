"""As-of semantics, down to the time of day.

The store stamps every fact with a calendar *date*, not an instant. That is a
deliberate precision limit: most sources publish a date (a filing date, a
session date), and inventing a time of day for them would be false precision.
But a date is only safe if it has exactly one meaning. Before this module
existed, a ``datetime`` passed as an as-of date was silently accepted, so the
same query leaked intraday on one call and depended on the machine's time zone
on another. The contract below removes both.

The contract
------------
1. **Reference time zone.** Knowledge dates and as-of dates are calendar dates
   in :data:`EXCHANGE_TZ` (America/New_York). US equity sessions and SEC filing
   dates are both assigned in Eastern time, so that is the zone a
   ``knowledge_date`` is read in. A source for another market must convert its
   stamps to this zone's calendar date, or carry its own store.

2. **``as_of = D`` is the view at the end of day D.** The bound is
   *inclusive*: every fact with ``knowledge_date <= D`` is visible. The next day
   is *exclusive*: no fact with ``knowledge_date >= D + 1 day`` is visible.

3. **A fact stamped D may have landed at any instant during D.** A bar is known
   at the close. An SEC filing started by 5:30 p.m. ET is dated that business
   day, and Forms 3, 4 and 5, Form 144 and Schedules 13D/13G get the same date
   up to 10 p.m. ET (17 CFR 232.13(a),
   https://www.law.cornell.edu/cfr/text/17/232.13). So ``as_of = D`` is only
   leak-free for a decision made *after* everything stamped D has arrived. For a
   decision at a real instant, use :func:`as_of_for_decision`. It returns the
   previous calendar day unless you state when your sources are complete.

4. **Dates only.** Every as-of argument must be a :class:`datetime.date`.
   A :class:`datetime.datetime`, and therefore a ``pandas.Timestamp``, is
   rejected with :class:`TypeError`. Accepting one implied a time-of-day
   precision the store does not have. A zoned datetime was also compared through
   DuckDB's session ``TimeZone``, which defaults to the machine's zone
   (https://duckdb.org/docs/current/sql/data_types/timestamp.html), so the
   answer depended on the laptop.

5. **Ranges over the date a fact is about are inclusive at both ends.**
   ``start``/``end`` on :func:`rplat.get_bars` (session dates) and on
   :func:`rplat.get_fundamentals` (period ends) select ``[start, end]``.

6. **Lifecycle ends are exclusive.** ``SecurityRecord.delisting_date`` and
   ``TickerRecord.end_date`` are the first date on which the security no longer
   trades or the symbol no longer applies. ``listing_date`` and ``start_date``
   are inclusive. So a security's last bar is the session before its
   ``delisting_date``, and it is in the universe on every date it has a bar.

The store also pins DuckDB's session ``TimeZone`` to UTC on every connection.
Its date comparisons are DATE against DATE and never cast through a zone, so
the pin is belt and braces for the date logic. It is what makes the
``ingested_at`` provenance column (``TIMESTAMPTZ``) read identically on every
machine.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

#: The zone that ``knowledge_date`` and ``as_of_date`` are calendar dates in.
EXCHANGE_TZ = ZoneInfo("America/New_York")

#: Where the full contract lives; quoted in error messages so they are actionable.
SEMANTICS_DOC = "docs/point-in-time.md#as-of-semantics"


def require_date(value: object, name: str = "as_of_date") -> date:
    """Return ``value`` if it is a plain :class:`datetime.date`, else raise.

    ``datetime`` subclasses ``date``, so ``isinstance(value, date)`` alone
    would wave it through. That is exactly the bug this guards against: a
    09:30 ``datetime`` used to return the bar that only closes at 16:00.
    """
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(
            f"{name} must be a datetime.date, not {type(value).__name__}. "
            "A datetime implies time-of-day precision the store does not have; "
            "convert a decision instant with rplat.clock.as_of_for_decision(). "
            f"See {SEMANTICS_DOC}."
        )
    return value


def as_of_for_decision(decision_time: datetime, *, day_complete_at: time | None = None) -> date:
    """The latest as-of date whose every fact was knowable at ``decision_time``.

    Args:
        decision_time: The instant the decision is made. Must be timezone-aware.
            A naive datetime means "whatever zone this machine is in", which is
            the machine-dependence this module exists to remove.
        day_complete_at: Optional local time (in :data:`EXCHANGE_TZ`) by which
            *you assert* every source you read has finished publishing the facts
            it stamps with today's date. Leave it unset unless you can defend
            it. A bars-only pipeline that trusts close-of-session bars might pass
            ``time(16, 0)``. Anything that reads SEC filings should not pass a
            time earlier than the 10 p.m. ET window in 17 CFR 232.13(a).

    Returns:
        With no ``day_complete_at``: the calendar day *before* the decision's
        local date. A fact stamped today may still land later today, so
        today's stamps are never safe. With ``day_complete_at``: the local date
        itself once the local time has reached it, otherwise the day before.

    The step back is one *calendar* day, not one trading session. Knowledge
    dates are calendar dates, so a Monday 09:30 decision gets Sunday. That
    includes Friday's bars and anything stamped over the weekend.
    """
    if not isinstance(decision_time, datetime):
        raise TypeError(f"decision_time must be a datetime, not {type(decision_time).__name__}")
    if decision_time.tzinfo is None or decision_time.utcoffset() is None:
        raise ValueError(
            "decision_time must be timezone-aware (e.g. tzinfo=ZoneInfo('America/New_York') "
            "or datetime.UTC); a naive datetime would be read in this machine's zone"
        )
    if day_complete_at is not None and day_complete_at.tzinfo is not None:
        raise ValueError(
            "day_complete_at is a wall-clock time in America/New_York; pass it without tzinfo"
        )

    local = decision_time.astimezone(EXCHANGE_TZ)
    today = local.date()
    if day_complete_at is not None and local.time() >= day_complete_at:
        return today
    return today - timedelta(days=1)
