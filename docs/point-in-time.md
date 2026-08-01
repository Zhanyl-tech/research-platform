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
        ORDER BY knowledge_date DESC, ingested_at DESC
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
  structurally impossible to introduce.

There is a subtlety worth naming: between the announcement and the delisting the
company still trades. Dropping it on the announcement date would be its own,
opposite look-ahead. The universe keeps it until it actually stops trading —
`tests/test_universe.py` pins each of those boundaries.

### 3. Corporate actions

Vendor "adjusted close" columns are adjusted with every split up to *today*.

- **What the backtest does:** uses a 2019 price that has been divided by a
  factor announced in 2022.
- **Why it flatters:** it is subtle rather than dramatic. The series has no
  visible defect — it is smooth and continuous, which is precisely why nobody
  checks it.
- **Here:** the store holds **raw** prices only, plus a corporate-actions table
  with the announcement in `knowledge_date`. `get_bars(..., adjust=True)` builds
  the factor from actions knowable on the as-of date and returns the factor it
  used, so the adjustment is auditable rather than baked in.

Note that adjustment becomes valid at the **announcement**, not the ex-date. A
researcher on the day after a split is announced legitimately knows it is
coming. Waiting for the ex-date would understate what was knowable — an error in
the other direction, and a real one.

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

A source that stamps `knowledge_date` with the effective date — because the real
publication timestamp was not captured — produces a store that is internally
consistent and quietly wrong. Nothing downstream can detect this, because from
the inside it looks identical to data that genuinely was available immediately.

This is why `StooqSource.caveats()` exists and says so out loud: Stooq publishes
no availability timestamps, so its `knowledge_date` is an assumption, not a
fact. And it is why the reference dataset is synthetic — real free data has no
restatement history, so you cannot demonstrate correct restatement handling with
it.

Phase 3's leakage detector attacks this from the other side: rather than
trusting the ingest, it checks whether any *feature* value at time T depends on
inputs whose `knowledge_date` is after T, with a deliberately leaky fixture to
prove the detector fires.

## What this costs

Being honest about the trade, since the design is not free:

- **Storage grows with revisions, not facts.** Fine for daily equity data;
  a consideration for tick data, where a rolling retention policy on superseded
  revisions would become necessary.
- **Every query needs an as-of date.** There is no "just give me the data" call,
  by design. It is friction, and it is the point.
- **The window function is more expensive than a plain scan.** Partitioning by
  fact key over an append-only table is the operation the whole store is tuned
  for, which is a large part of why the engine underneath is columnar.
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
