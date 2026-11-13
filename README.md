# ticker-lineage — what a naive ticker join actually costs you

*(Formerly `idgraph` / ticker-truth. The Python package is now `tickerlineage`.)*

A temporal identifier graph over SEC filer data, built to measure one thing: how often
does joining financial data on ticker symbol silently attach the wrong company's history?

**Answer: 0.9% to 1.3% of ticker-quarter observations, over a clean nine-year window.**

Nothing errors. No exception is raised, no row count changes, no test fails. The join
succeeds and the data is wrong.

---

## The headline number, and why it is a range

| Window | Misattributed | Observations | Rate |
|---|---:|---:|---:|
| 2021Q3 – 2026Q3 (strict) | 2,052 | 227,440 | **0.902%** |
| 2020Q3 – 2026Q3 (auto-selected) | 3,612 | 269,867 | **1.338%** |
| 2017Q3 – 2026Q3 (full period) | 15,056 | 367,069 | 4.102% ⚠️ |

The 4.1% figure is inflated and should not be quoted. Two of the 36 snapshots are points
where **SEC changed the ticker file's own composition**, not where the market changed:

- **2019Q4** — 6,259 → 13,548 rows (+117%)
- **2020Q3** — 13,633 → 8,426 rows (−38%)

285 of the 1,481 detected transitions land on those two snapshots. They are file artifacts
wearing the costume of corporate events. `EdgeSet.regime_artifacts()` flags any
snapshot-over-snapshot composition change above 25%, and the analysis reruns inside the
longest clean window.

I report both endpoints of the range because choosing the window is a judgement call.
Collapsing it to one number would be the same failure mode the project exists to measure.

---

## The four failure classes

Built from 33,268 identifier edges across 31,787 tickers and 20,154 CIKs.

### 1. Ticker reuse — 1,400 tickers held by more than one CIK

A ticker is released and reassigned to an unrelated company years later. A ticker-keyed
history splices the two together into one continuous series.

| Ticker | Was | Became | Gap |
|---|---|---|---|
| **S** | Sprint Corporation | SentinelOne, Inc. | 2 quarters |
| **UTX** | United Technologies | Ultratrex Inc. | 19 quarters |
| **BBT** | BB&T Corporation | Beacon Financial Corp | 22 quarters |
| **STI** | SunTrust Banks | Solidion Technology Inc. | 12 quarters |
| **MWG** | Morgan Stanley | Multi Ways Holdings Ltd | 11 quarters |
| **HAWK** | Blackhawk Network Holdings | HawkEye 360, Inc. | 23 quarters |

All verified against the live SEC submissions API. CIK 101830 is now `SPRINT LLC` and
holds no tickers; CIK 1583708 holds `S` on the NYSE and was formerly Sentinel Labs.

Classified into 864 genuine reassignments, 364 holdco restructures, 253 ambiguous.

### 2. Ticker changes, CIK persists — 1,124 companies

Ball Corporation `BLL` → `BALL` in 2022Q3. Same CIK 9389, same business. A ticker-keyed
history breaks in two at the rename and the earlier half looks like a dead company.

### 3. Concurrent multi-ticker CIKs — 5,842

Share classes, preferred series, warrants. Alabama Power alone carries ten tickers under
one CIK. Joining on ticker either double-counts the entity or arbitrarily picks one class.

*This bucket is honestly mixed* — it conflates true share classes with preferreds and
bankruptcy `Q`-suffixed tickers. It is a count of concurrent multi-ticker CIKs, not a
clean share-class count, and I have not separated them.

### 4. Survivorship — reported, not headlined

12,137 CIKs are absent from the final snapshot. **That number is not usable** and the code
says so: absence from the ticker file is confounded by the file's own composition changes.
`survivorship()` returns the count together with the confound and a refusal to present it
bare. Validating it needs per-CIK filing activity, which is the obvious next step.

---

## The best case in the dataset: BLK

BlackRock reorganised in 2024. CIK **1364742** was renamed *BlackRock Finance, Inc.* on
2024-09-26. CIK **2012383**, formerly *BlackRock Funding, Inc.*, took the name and the
ticker on 2024-10-01.

A naive **ticker** join survives this cleanly.
A naive **CIK** join breaks on it.

Both keys fail, in opposite directions, on the same corporate event. Which is the argument
for the temporal edge table rather than for picking a better single key.

---

## What the matching layers actually resolved

Deterministic rules do nearly all the work. That ordering is the thesis, not an
implementation detail: **when an exact identifier exists, inference is the wrong tool.**

1. **Deterministic** — normalised names identical
2. **Normalized** — whole-token prefix relation
3. **Fuzzy** — RapidFuzz `token_set_ratio` ≥ 88
4. **Unresolved** — refuse to classify

There is no LLM layer in this build, deliberately. CIK *is* the exact answer; wrapping
inference around it would undercut the whole argument.

### The case I did not force

Apache Corp. → APA Corp (CIK 6769 → 1841666) *is* a real holdco restructure. Name matching
cannot see it: `APACHE` vs `APA` scores **66.7**, well under the threshold, because it is
an abbreviation rather than a typo.

I left it unresolved. Lowering the threshold until it passes buys one true positive and an
unknown number of false ones — and a false positive here merges two unrelated companies,
which is precisely the failure being measured. The test asserts the honest behaviour and
documents that the correct fix is an external deterministic signal (SEC `formerNames` and
filing continuity), not a looser threshold.

---

## Known limitations

Stated up front because this team validates for a living.

- **Gap collapsing.** The edge builder extends a segment when a ticker reappears under the
  *same* CIK, so delist-then-relist under one CIK reads as continuous. Reuse detection is
  unaffected; intra-period gaps are understated. `presence_drift()` measures the effect per
  snapshot rather than pretending it is zero.
- **Snapshot-resolution dates.** A transition is dated to the first quarterly snapshot that
  shows it, which can lag the corporate event by years. GOOGL/GOOG moves from Google Inc.
  to Alphabet Inc. at 2019Q4 in this data; the actual restructuring was **2015**. The date
  reflects when SEC updated the file, not when the event happened.
- **This is SEC filer data, not a trading universe.** No prices, no volumes, no exchange
  delisting records. The failure classes are real; market impact is inferred.
- **Coverage starts 2017-08-28**, the first Wayback snapshot of the ticker file.
- **`filings.recent` holds only the most recent ~1,000 filings**, so first-filing dates
  taken from it are wrong for high-volume filers (Morgan Stanley reports a 2025 "first"
  filing). Full history needs `filings.files`. Nothing here depends on it.
- **Not Spark.** 33,268 edges over 36 snapshots is a Pandas-and-Postgres problem. Reaching
  for a cluster would have been the wrong call and would have said so about my judgement.

---

## Data sources

All free, no paid vendor, no API key.

| Source | Use |
|---|---|
| `sec.gov/files/company_tickers.json` | current ticker ↔ CIK map |
| Wayback Machine CDX + raw snapshots | 36 quarterly historical maps, 2017Q3–2026Q3 |
| `data.sec.gov/submissions/CIK##########.json` | `formerNames` with date ranges, filing history, verification |

SEC requires a `User-Agent` identifying you and rate limits to 10 req/s. `fetch.py` throttles
to 8/s and 2/s for Wayback. Everything is cached on disk by URL, so runs are resumable and
reruns are free.

---

## Architecture

**One door in.** Every external read goes through `fetch.py`. Rate limiting, the User-Agent
rule, retry policy and caching are enforced in exactly one place, so a fix there is
inherited by every caller.

**Ranges, not points.** `ticker AAPL → CIK 320193` is not a fact. `ticker AAPL → CIK 320193
during [2017-08-28, ...)` is. The Postgres schema carries a generated `daterange` with a
GiST index, and an exclusion constraint makes the contradiction *unrepresentable*:

```sql
ALTER TABLE identifier_edge
    ADD CONSTRAINT no_concurrent_ticker_owners
    EXCLUDE USING gist (id_value WITH =, validity WITH &&)
    WHERE (id_type = 'ticker');
```

Ticker **reuse** is legal — disjoint ranges. Ticker **ambiguity** is not. The database
refuses it rather than the pipeline hoping to catch it.

**Provenance on every edge.** `source`, `confidence` and `resolved_by` are columns, not
comments. An edge that came from fuzzy matching is queryably different from one that came
from an exact identifier.

**Fail loud.** A row that cannot be keyed is logged, a duplicate ticker within one snapshot
raises a warning, and a snapshot that parses to zero rows raises rather than being treated
as an empty quarter.

---

## Layout

```
src/tickerlineage/
  fetch.py       acquisition - the only module that touches the network
  build.py       snapshots -> temporal edges; regime artifact detection
  normalize.py   deterministic name normalisation
  analyze.py     four failure classes + the naive-join diff
  pipeline.py    end-to-end run
  schema.sql     Postgres temporal schema, constraints, analysis views
tests/           75 tests, on real SEC cases; network-free
data/
  findings.json  measured results with caveats attached
  snapshots.csv  36 snapshots with row counts and regime labels
pyproject.toml   package metadata; installs the `ticker-lineage` command
```

## Run it

Requires Python 3.10+.

```bash
pip install -e ".[dev]"          # or: pip install -r requirements.txt
ticker-lineage --out data/ --user-agent "Your Name you@example.com"
python -m pytest -q
```

Without installing, `python -m tickerlineage.pipeline ...` works from the repo root with
`src` on `PYTHONPATH`. The `--user-agent` flag falls back to the `SEC_USER_AGENT`
environment variable; there is no built-in default. `make install | dev | run | test`
wrap the same commands.

If `pip install -e .` complains that editable mode needs a setuptools build, your pip is
too old: `python -m pip install --upgrade pip`.

Full run is ~36 Wayback fetches, a few minutes, resumable. Put your own contact string in
`--user-agent`; SEC blocks requests without one.

## Tests

75 tests, and they sit where the ambiguity is rather than where the code is complex —
normalisation rules and temporal overlap logic. Every fixture is a real pair from the SEC
data.

The suite includes hard negatives that **must not** match (Blackhawk vs BlackRock share
four characters), idempotence checks on the normaliser, boundary cases on
overlapping-vs-touching ranges, and a test asserting that a company genuinely called
"Corp" does not normalise to the empty string and match everything.

Two bugs surfaced on the first run: an EDGAR qualifier regex that missed `INC/ FA` because
of the space, and the Apache/APA case above. One was a code fix, one was the test asserting
something the data does not support.

Fetching and the end-to-end pipeline are tested offline: a fake HTTP session pins the
retry policy (429/5xx are retried, other 4xx fail immediately, nothing is cached until a
response parses), and a fake fetcher drives a full run that checks every output file,
the `regime` column, and non-ASCII company names.

---

## Changelog

**0.2.0**
- Renamed the project to **ticker-lineage**; the package `idgraph` is now `tickerlineage`.
- Added `pyproject.toml`, so the project can be installed with `pip install -e .` and
  provides a `ticker-lineage` command.
- **Fix:** output CSVs are now written as UTF-8. Before, a company name with non-Latin
  characters crashed the pipeline on Windows, where the default encoding is cp1252.
- **Fix:** client errors such as 404 and 403 are no longer retried with backoff. They
  raise `PermanentFetchError` straight away. 429 and 5xx responses are still retried.
- `snapshots.csv` now includes a `regime` column (`stable` or `artifact`).
- Transitions on artifact snapshots are flagged by snapshot index rather than looked up
  by quarter label.
- The pipeline exits with a clear message when there are no snapshots, and `run_all`
  refuses an empty edge set instead of crashing on it.
- 13 new tests, all offline: retry policy, cache, file shapes, and an end-to-end run.
- pytest is capped below 9, because pytest 9.x crashes internally on CPython 3.10.0.
