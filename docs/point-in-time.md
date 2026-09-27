# Point-in-time semantics

Why every fact carries two dates, and what breaks without them.

---

## The failure this prevents

A researcher tests a signal over 2015–2023. It looks good. It goes live and the
edge is gone.

The usual explanations — overfitting, crowding, regime change — get reached for
first because they are the interesting ones. But very often nothing is wrong
with the model at all. The problem is that the **database is a current-state
database**, and a current-state database answers a question nobody asked:

> *What do we believe about the past, right now?*

The question research actually needs is:

> *What would we have believed on 3 September 2022?*

Those are different questions, and the gap between them is not noise. It is
systematically biased in the direction of making a backtest look better, because
almost every correction that happens to financial data — restatements, delisting
flags, split adjustments, backfills — replaces a messier past with a tidier one.

The gap does not raise an exception. It produces a clean, plausible, well-behaved
series and a Sharpe ratio that does not survive contact with reality.

## Two dates, not one

Every record in this store carries both:

| | also called | meaning |
|---|---|---|
| `effective_date` | valid time | the date the fact is **about** |
| `knowledge_date` | transaction time, decision time | the date the fact first became **knowable** |

Keeping both is called a *bitemporal* model, and it is the whole mechanism.
Everything else in this document is a consequence.

```
                    effective_date  ───────────────────────────►
                    (what it is about)

  knowledge_date
  (when we learned it)
        │
        ▼

   filed 2022-08-01   ●  eps = 1.50   ← for period ending 2022-06-30
   filed 2022-11-15   ●  eps = 1.20   ← same period, restated

   as of 2022-09-01 the answer is 1.50
   as of 2022-12-01 the answer is 1.20
   both are correct
```

Two rows. Same fact. Different beliefs, held at different times. The store keeps
both forever and a query picks between them:

```sql
SELECT * FROM (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY security_id, period_end, metric
        ORDER BY knowledge_date DESC, ingest_seq DESC, row_ordinal DESC
    ) AS rn
    FROM fundamentals
    WHERE knowledge_date <= $as_of
) WHERE rn = 1
```

Two things in that query matter and are easy to get wrong.

**`WHERE knowledge_date <= $as_of` alone is not enough.** That returns *every*
belief ever held up to that date, superseded ones included — so a single fact
comes back as several rows and any downstream join silently multiplies.

**`ROW_NUMBER` alone is not enough either.** Without the `WHERE`, it picks the
newest belief that exists *today*, which is exactly the look-ahead we are trying
to eliminate.

You need both. The window reconstructs a single coherent view; the filter makes
it the view from a particular day.

**Ties are broken by order, never by clock.** Two beliefs about one fact with
the same `knowledge_date` resolve to the later append (`ingest_seq`, drawn from
a DuckDB sequence) and, within one append, the later record (`row_ordinal`).
The first version ordered by `ingested_at`. Every row in a batch shared that
timestamp, and across batches it trusted two wall-clock readings to increase.
The 2026-09 audit appended 3,000 copies of one value followed by a correction
in a single batch, and the correction lost 200 runs out of 200. Re-running that
probe against the old code while fixing it gave the same 200 out of 200.

**Filters go before the window only on fact-key columns.** `get_bars(start=,
end=)` pushes `effective_date` into the `WHERE`, and `security_ids` and
`metrics` go the same way. That is safe because each of those columns is part
of the partition key, so the filter removes whole partitions and cannot change
which revision wins in one that survives. A filter on `value`, say, could drop
the newest revision and resurrect a superseded one, so `Store.as_of` refuses
it. Every value is bound as a parameter. An id used to be interpolated into
the SQL, and `SEC0001') OR (security_id = 'SEC0001` returned the restated 1.2
as of 2022-09-01.

## As-of semantics

A `knowledge_date` is a calendar date with no time of day. That is deliberate:
most sources publish a date, and inventing a time for it would be false
precision. But the date must mean exactly one thing, or the same query leaks on
one call and not the next. Before this was pinned down, a `datetime` at 09:30
was silently accepted and returned a bar that only closes at 16:00. A zoned
`datetime` was compared through DuckDB's session `TimeZone`, which defaults to
the machine's zone
([DuckDB docs](https://duckdb.org/docs/current/sql/data_types/timestamp.html)),
so the answer depended on the laptop. The contract, implemented in
[`rplat/clock.py`](../src/rplat/clock.py) and pinned by `tests/test_clock.py`:

1. **Reference zone.** Knowledge and as-of dates are calendar dates in
   **America/New_York**. US sessions and SEC filing dates are both assigned
   in Eastern time. A source for another market converts its stamps to this
   zone's calendar date.
2. **`as_of = D` is the view at the end of day D.** Inclusive: every fact with
   `knowledge_date <= D` is visible. Exclusive of the next day: nothing stamped
   `D + 1` or later.
3. **Dates only.** Every as-of argument, and every `start`/`end`, must be a
   `datetime.date`. A `datetime`, and so a `pandas.Timestamp`, raises
   `TypeError` at every public entry point. The store also pins DuckDB's
   session `TimeZone` to UTC, which is why `ingested_at` (a `TIMESTAMPTZ`)
   reads the same on every machine.
4. **Ranges over the date a fact is about are inclusive at both ends**:
   `get_bars(start=, end=)` over sessions, `get_fundamentals(start=, end=)`
   over period ends.
5. **Lifecycle ends are exclusive.** `delisting_date` and a ticker's `end_date`
   are the first date the name no longer trades or the symbol no longer
   applies. `listing_date` and `start_date` are inclusive. A delisted name's
   last bar is the session before its `delisting_date`, and on that session it
   is still in the universe. That session's return is the delisting return a
   survivorship-aware backtest must capture.

**A fact stamped D may land at any instant during D.** A bar is known at the
16:00 close. An SEC filing started by 5:30 p.m. ET is deemed filed that
business day, and Forms 3, 4, 5, Form 144 and Schedules 13D/13G get the same
treatment up to 10 p.m. ET
([17 CFR 232.13(a)](https://www.law.cornell.edu/cfr/text/17/232.13)). So
`as_of = D` is leak-free only for a decision made after everything stamped D
has arrived. For a decision at a real instant, convert it:

```python
as_of_for_decision(datetime(2022, 8, 1, 9, 30, tzinfo=ZoneInfo("America/New_York")))
# -> 2022-07-31
```

The input must be timezone-aware; a naive `datetime` raises. By default the
result is the calendar day before the decision's New York date, because
today's stamps are never safe. The step is a calendar day, not a trading
session: a Monday-open decision gets Sunday, which includes Friday's bars. If
you can defend a time by which every source you read has finished publishing
for the day, pass it as `day_complete_at`. For example, `time(16, 0)` suits a
bars-only pipeline that trusts close-of-session bars; anything reading SEC
filings should not go earlier than 22:00. That value is your assertion, not
something the store can check.

What this does not do: store a time of day. Capturing EDGAR acceptance
timestamps or session close instants in a `knowledge_ts TIMESTAMPTZ` column
would let a 09:30 decision see an 08:00 filing from the same day. That is
future work; it needs sources that actually publish those instants.

## The four ways this bites

### 1. Restatements

A company files Q2 earnings in August and revises them in November. Today's
database holds only the revision.

- **What the backtest does:** reads the November number when simulating August.
- **Why it flatters:** restatements correlate with trouble. Knowing the
  corrected figure early means knowing which companies were misreporting, which
  is close to knowing which ones were about to fall.
- **Here:** `get_fundamentals(store, date(2022, 9, 1))` returns 1.50.
  `get_fundamentals(store, date(2022, 12, 1))` returns 1.20.
  `rplat history --security SEC0001 --period-end 2022-06-30` shows both.

### 2. Survivorship

Delisted companies get dropped from — or flagged in — the security master. A
universe built by filtering today's master to `delisting_date IS NULL` contains
only firms that made it.

- **What the backtest does:** never buys anything that goes to zero.
- **Why it flatters:** it is a strategy with hindsight about bankruptcy, which
  is worth a great deal and is not available.
- **Here:** delisting is a *second* `securities` row with its own
  `knowledge_date`. As of any date before the announcement, the resolved row has
  no delisting date at all, so `get_universe` **cannot** filter the name out.
  Survivorship is not avoided by remembering to include dead names; it is
  structurally impossible to introduce, *provided* every table that carries an
  end date follows the same rule. The fixture's `ticker_map` once did not. It
  put each ticker's end date on the row knowable from the listing date, so as
  of 2021-10-01 it already "knew" Northwind's 2023 bankruptcy. Ticker ends are
  now second rows too, and `tests/test_fixture.py` checks every dataset on a
  grid of dates for an end date visible before its announcement.

There is a subtlety worth naming: between the announcement and the delisting the
company still trades. Dropping it on the announcement date would be its own,
opposite look-ahead. The universe keeps it until it actually stops trading —
`tests/test_universe.py` pins each of those boundaries. The price tape agrees
with the universe: the last bar is the session before `delisting_date`, and a
test asserts that every bar for session D that is knowable as of D belongs to a
name in D's universe. That is a statement about each day's own session only.
A delisted name's older bars stay knowable after it leaves the universe, which
is what lets a backtest still price its history.

### 3. Corporate actions

Vendor "adjusted close" columns are adjusted with every split up to *today*.

- **What the backtest does:** uses a 2019 price that has been divided by a
  factor announced in 2022.
- **Why it flatters:** it is subtle rather than dramatic. The series has no
  visible defect — it is smooth and continuous, which is precisely why nobody
  checks it.
- **Here:** the store holds **raw** prices only, plus a corporate-actions table
  with the announcement in `knowledge_date`. `get_bars(..., adjust=True)` builds
  the factor from splits that are both announced *and* effective on the as-of
  date, and returns the factor it used, so the adjustment is auditable rather
  than baked in.

Knowing about a split and applying it are different things. From the
announcement, the split is knowable, and `get_corporate_actions` returns it,
ex-date and ratio included. It is applied to prices only from the ex-date,
because before then there is no discontinuity to remove. An earlier version
applied it from the announcement and documented that as intended. Returns were
unaffected, but every price *level* was wrong for the weeks in between. As of
2022-09-16, three days before Beacon's 2-for-1, the latest close was reported at
395.50 when 791.00 traded, which corrupts market cap, P/E and any price filter.
The invariant is now tested on a grid of dates: the latest visible bar has
`adjustment_factor == 1`, *provided the ex-date session's own bar is visible*.
That holds throughout the fixture but not in general. If the ex-date bar
arrives late, the split still applies from the ex-date, so until that bar lands
the latest visible bar is a pre-split session shown in post-split terms (a
100.00 close reported as 50.00, factor 2.0), which is not a price that traded.
`tests/test_prices.py` pins that case. Anything that reads a current price
level from the latest bar should check its `adjustment_factor`.

### 4. Late arrival and identity

Two smaller traps that behave the same way:

**Backfills.** A vendor delivers a week of missing bars several days late. As of
the session date those rows did not exist, and a pipeline that ran that week
correctly saw nothing. Storing them with `knowledge_date` = session date makes
history look complete and hands a live simulation data it did not have.

**Ticker recycling.** Symbols get reassigned to unrelated companies. Keyed on
the ticker, two firms become one series with a violent discontinuity at the
seam — which reads as a return, not as an error. Identity here is
`security_id`; tickers are resolved per as-of date through a table with its own
validity windows.

## The assumption this cannot check for you

The store guarantees that a query returns the belief held on a date. It cannot
guarantee that `knowledge_date` was *set honestly* in the first place.

Two kinds of bad stamp need separating, because the store can catch one and
not the other.

**Impossible stamps are caught.** A bar whose `knowledge_date` is before its
session, or a quarter "filed" before it ended, cannot be true. `Store.append`
rejects the whole batch with a `RecordValidationError` that lists the offending
rows, and writes nothing. The same check refuses OHLC bars with `high < low` or
non-positive prices, splits without a finite positive ratio, and end dates that
do not follow their start dates. It is cheap, and it is the first real piece of
leakage detection here.

**Plausible-but-wrong stamps are not.** A source that stamps `knowledge_date`
with the effective date, because the real publication timestamp was not
captured, produces a store that is internally consistent and quietly wrong.
Nothing downstream can detect this, because from the inside it looks identical
to data that genuinely was available immediately.

This is why `StooqSource.caveats()` exists and says so out loud: Stooq publishes
no availability timestamps, so its `knowledge_date` is an assumption, not a
fact. And it is why the reference dataset is synthetic — real free data has no
restatement history, so you cannot demonstrate correct restatement handling with
it.

Phase 3's planned leakage detector will attack this from the other side.
Rather than trusting the ingest, it will check whether any *feature* value at
time T depends on inputs whose `knowledge_date` is after T, and it will ship
with a deliberately leaky fixture to prove it fires. Neither exists yet. Today
the append-time check above is the only leakage detection in the code.

## What this costs

Being honest about the trade, since the design is not free:

- **Storage grows with revisions, not facts.** Fine for daily equity data;
  a consideration for tick data, where a rolling retention policy on superseded
  revisions would become necessary.
- **Every query needs an as-of date.** There is no "just give me the data" call,
  by design. It is friction, and it is the point.
- **The window function is more expensive than a plain scan.** Partitioning by
  fact key over an append-only table is the operation the whole store is tuned
  for, which is a large part of why the engine underneath is columnar. Asking
  for one session pushes the range into SQL ahead of the window and stays cheap.
  Asking for the full view of millions of facts does not. The README's
  "Measured" section has the numbers, the command and the machine.
- **Sources must supply real availability dates.** Many do not. That is a
  sourcing problem this design surfaces rather than solves — which is better
  than a design that hides it.

## Further reading

- Snodgrass, *Developing Time-Oriented Database Applications in SQL* — the
  standard treatment of bitemporal modelling.
- Marcos López de Prado, *Advances in Financial Machine Learning*, ch. 7 — why
  temporal separation and embargoes matter in evaluation. Phase 5 builds on it.
- SEC Financial Statement Data Sets — a real corpus with genuine filing dates
  and restatements, and the natural production source behind this interface.
