# Changelog

## Unreleased — 2026-09-26: fixes from the 2026-09 audit

An external audit found look-ahead leaks in the data layer, a SQL injection,
and metadata that claimed more than the code does. This entry lists the changes
made in response: behaviour, public API, dependencies and tooling. The package
version is still 0.1.0, although the on-disk format changed (see the first
Breaking entry), so this entry names store formats by schema version and
commit rather than by package version.

### Breaking

- **Store schema version 2.** New `ingest_seq` and `row_ordinal` columns,
  `ingested_at` is now `TIMESTAMPTZ` (UTC), and a `store_meta` table records the
  version. A schema-1 store (written by rplat before these fixes, commit
  `396572a`) is refused with `StoreSchemaError`. There is no migration.
  `rplat ingest --force --db <path>` rebuilds the *synthetic fixture* store and
  loads nothing else, so it is the right fix only for a store that held
  fixture data. A store holding anything else (a `StooqSource` ingest, your own
  appends) must be re-ingested from its original sources into a new file. The
  error message checks the store's `source` columns and says which case
  applies; it recommends `--force` only when every row came from the fixture.
- **As-of arguments must be `datetime.date`.** A `datetime` or
  `pandas.Timestamp` passed as an as-of date, a `start`/`end`, or a date key now
  raises `TypeError` at every public entry point.
- **Split adjustment starts at the ex-date, not the announcement.** The test
  that pinned the old behaviour was rewritten. See Fixed.
- **`Store.sql` runs exactly one `SELECT`.** Anything else raises `StoreError`.
- **`rplat demo --db` is gone.** It was accepted and ignored.
- **Fixture data changed.** The two delisted names lose their bar on the
  delisting date, their ticker rows split into two, and dividend amounts
  changed (they were different in every process before). The traps and the
  demo's key numbers are unchanged.

### Fixed

- **SQL injection through `security_ids`** (also reachable from
  `rplat bars --security`). Ids were interpolated into the SQL, and the extra
  clause was appended without parentheses. `SEC0001') OR (security_id =
  'SEC0001` returned the restated EPS 1.2 as of 2022-09-01. Every filter value
  is now a bound parameter, filter columns are allow-listed to fact keys, and
  the extra clause is parenthesised. The global ruff `S608` ignore, whose
  justification this contradicted, is replaced by per-line waivers that name
  where each identifier comes from. Tests show the payload returns no rows, an
  id containing `'` round-trips, and a bad column name is refused.
- **Price levels halved before a split happened.** Splits are now applied only
  when both announced and effective by the as-of date. Reproduced against the
  old code in this session: as of 2022-09-16 the latest Beacon close came back
  as 395.50 when 791.00 traded. Announced-but-not-effective splits are still
  returned by `get_corporate_actions`. The resulting invariant (the latest
  visible bar has `adjustment_factor == 1`) holds only while the ex-date
  session's own bar is visible. If that bar arrives late, the latest visible
  bar is a pre-split session in post-split terms until it lands. The docs say
  so, and a test pins the case.
- **Fixture ticker map leaked delistings years early.** Ticker end dates were on
  the row knowable from the listing date. They are now a second row, stamped
  with the announcement date, just as the security master already did it. A
  grid test checks every date for an end date visible before its announcement.
- **As-of time of day was undefined.** It is now a written contract
  (`rplat/clock.py`, `docs/point-in-time.md#as-of-semantics`): New York
  calendar dates, `as_of = D` is the end of day D, inclusive, and ranges are
  inclusive. New `as_of_for_decision()` maps a timezone-aware instant to a
  leak-free as-of date. DuckDB's session `TimeZone` is pinned to UTC. A test
  runs the same queries under two machine time zones and compares digests.
- **Delisting date meant two different things.** It is now exclusive
  everywhere: the tape stops the session before it, and a test asserts every
  bar for session D that is knowable as of D belongs to D's universe. (Older
  bars of a delisted name stay knowable after it leaves the universe; a test
  pins that too.)
- **Same-key ties resolved arbitrarily.** Ties now break on `ingest_seq`, then
  `row_ordinal`; `ingested_at` is provenance only. Tested within a batch, across
  batches with a frozen clock, and with a clock stepping backwards.
- **Partial ingests were permanent.** `ingest()` pulls every dataset first, then
  writes them all in one transaction. `append()` is atomic too.
- **`Store.sql` could rewrite history** with `UPDATE`. It now refuses
  everything but a single `SELECT`. The docs now say plainly that the file
  itself can still be edited with other tools.
- **A NULL split ratio crashed `get_bars`** for every security in the call.
  Rejected at append; `_apply_split_factors` raises a named error if one
  arrives another way.
- **Impossible records were accepted.** Appends reject stamps earlier than the
  fact (a bar before its session, a filing before its period ends), broken
  OHLC, non-positive prices, splits without a positive ratio, and end dates not
  after start dates. The whole batch is refused and nothing is written.
- **Fixture dividends differed per process** (seeded from salted `hash()`).
  Now seeded from the spec index. A test compares SHA-256 digests of every
  dataset across two `PYTHONHASHSEED` values. The docstring no longer claims
  byte-identical output across machines; NumPy's NEP 19 does not promise that
  across versions.
- **Stooq URL.** The https check could never fire, and symbols like
  `aapl.us&i=w` injected query parameters. Symbols are now validated, the URL
  is built with `urlencode`, and an HTML bot-check page is reported by name.
- **CLI query commands on a missing path** created an empty store and reported
  zero rows. They now open read-only and exit with a usage error.
- **`rplat ingest --force` deleted the old store before rebuilding.** It now
  builds beside it and renames it into place only on success. Because a
  renamed file inherits any `<db>.wal` already beside it, and DuckDB replays
  that log into the next open, `ingest` refuses while such a log exists,
  whether or not the store itself does, and checks again just before the
  rename. (An intermediate version of this change checked only when the store
  existed. With a crashed writer's log left beside a deleted store, the
  rebuilt store answered 99.0 instead of the fixture's 1.5. That was
  reproduced here, and a test replays the scenario.) `--force` refuses to
  replace a file unless its tables are exactly an rplat store's (one table
  named `ingests` used to be enough, so an unrelated database could be
  replaced), and a store another process has open is reported as in use
  rather than as "not an rplat store". `rplat ingest --help` now says it loads
  the synthetic fixture only. Each case is tested.
- cli.py said the demo walks "four" traps; it walks five.

### Performance

Measured on 2026-09-26 with `bench/ingest.py` and `bench/asof.py` (default
arguments), before = commit `396572a`, one machine, before and after runs
interleaved because other jobs were loading it. Figures are the range of
per-invocation medians. Full details and caveats are in the README under
"Measured". An earlier pass reported a "before" append rate of 4,367 rows/s
and an unchanged full view; re-running interleaved did not reproduce either,
so both are replaced.

- `Store.append`: Arrow columns and one `INSERT … SELECT` with an explicit
  column list replace `executemany`. 100,000 bars: 5,764–7,115 → 335,183–363,433
  rows/s.
- `get_bars` and `get_fundamentals` push key-column filters into SQL ahead of
  the as-of window. One session of 2,000 names from a 5,000,000-row table:
  0.671–0.839 s → 0.004–0.005 s.
- The full as-of view got slower: 0.656–0.773 s → 0.710–0.919 s for 2,502,000
  rows, slower in all six interleaved pairs by 1% to 19%. Probably the
  tie-break columns and `TIMESTAMPTZ`; not profiled.

### Added

- `Store.open(path, read_only=True)` for concurrent readers. A test holds eight
  reader processes open at once and checks that a writer is refused meanwhile.
- `get_fundamentals(start=, end=)` over `period_end`; `Store.as_of(key_in=,
  key_between=)`; `Store.is_store_file()`; `StoreError`, `StoreSchemaError`,
  `StoreBusyError`, `RecordValidationError`. `rplat` also exports
  `as_of_for_decision` and `EXCHANGE_TZ` (the `America/New_York` zone the
  as-of contract uses), and `rplat.store` exports `SCHEMA_VERSION`.
- Runtime dependency `tzdata` on Windows only (`sys_platform == 'win32'`):
  `rplat.clock` uses `zoneinfo`, which reads the OS time-zone database, and
  Windows does not ship one.
- Tests. Before (commit `396572a`): 39 tests, 66% line coverage of `rplat`.
  After: 231 tests, 98%. Both were measured on 2026-09-26 with
  `pytest --cov=rplat`, the "before" figure by running that commit's own tests
  against its own code. The new
  tests include a hypothesis oracle comparing `as_of` with a brute-force
  reference, grid invariants over all five datasets, CLI tests, and offline
  Stooq tests. Each fix above was checked by putting the bug back into a copy
  of the code and confirming a test fails.
- `bench/ingest.py`, `bench/asof.py`, `make bench`.
- CI: `permissions: contents: read`, Python 3.13 in the matrix, a coverage gate
  (90%), a job installing the lowest direct dependency versions, and a job
  that builds the wheel and runs the demo from it outside the checkout.
  `src/rplat/py.typed` ships the type hints.
- Makefile: uses uv when present, otherwise refuses a Python older than 3.11
  with a message instead of a resolver error.

### Changed (tooling)

- Dev extras: `pandas-stubs` now has a floor (`>=2.1`), and `hypothesis>=6.100`
  is new.
- mypy also checks `bench/`, and its missing-imports override now covers
  `pyarrow.*` as well as `duckdb.*`.
- ruff: the global `S608` ignore is gone (see Fixed); `bench/*` may use
  `assert` and non-cryptographic random (`S101`, `S311`).
- Coverage settings (`source = ["rplat"]`, missing lines shown) live in
  `pyproject.toml`.
- `.hypothesis/` is git-ignored and removed by `make clean`.

### Changed (wording)

- Package description, and the README's Stooq and CI claims, now describe what
  exists: a Phase-1 point-in-time data layer. Lineage, leakage detection
  beyond the append-time check, evaluation and scale-out are still to come.
  The Stooq tests use a hand-written CSV in Stooq's column layout, and the
  README and docstrings now say that instead of "recorded".
- Phase 3's leakage detector and calendar-misalignment check are described as
  planned (docs/point-in-time.md, `rplat.sources.base`,
  `rplat.sources.fixture`). They were written in the present tense.
- The README's CLI example said the 2023 universe had "two names gone, one
  arrived" relative to 2022. It is Northwind gone and Zenith (the new `ZZZ`)
  arrived; Vela had already delisted before both dates.

### Verified locally, not yet in CI

- The lowest-direct install (Python 3.11 with duckdb 1.0.0, pandas 2.1.0,
  numpy 1.26.0, pyarrow 15.0.0, click 8.1.0, via
  `uv pip install --resolution lowest-direct -e ".[dev]"`) passed all 231
  tests here on macOS arm64. The store-file check relies on DuckDB's file
  header and its lock-conflict message; both were checked by hand under
  duckdb 1.0.0 and 1.5.5. Windows is untested.
- The wheel built with `uv build`, installed into a clean venv, and ran
  `rplat demo` from outside the repo, with the four strings CI greps for.
- The workflow passes `actionlint` with shellcheck.
- The new CI jobs and the 3.13 entry have not run on GitHub Actions yet.
