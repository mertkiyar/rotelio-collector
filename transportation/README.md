# Transportation

This module collects and validates dated flight offers for the Rotelio pilot.
It keeps a persistent observation history and exports consistent CSV files for
cost comparison and future price analysis. Bus collection is outside the scope;
car-cost estimation is a later feature.

## Requirements

Use Python 3.12 on macOS or Linux. The collector uses Unix file locking. Install
the pinned runtime packages from the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r transportation/requirements.txt
```

An installed Google Chrome is used by default. The following local browser check
does not request ticket prices:

```sh
python transportation/daily.py doctor
```

To use Playwright-managed Chromium instead:

```sh
python -m playwright install --with-deps chromium
export FLIGHT_BROWSER_CHANNEL=chromium
python transportation/daily.py doctor
```

## Collection

```sh
python transportation/daily.py collect --limit 1
python transportation/daily.py collect --all
python transportation/daily.py check
python transportation/daily.py export
python transportation/audit.py --require-coverage
```

`--limit 1` is a small source-access check. `--all` processes at most one 240-query batch of remaining cycle work.
Search starts are spaced by at least 30 seconds. Daily and cycle attempt
limits are 992, including a 32-attempt interruption reserve. A 240-query
batch takes at least two hours; a complete refresh takes at least eight hours.

The refresh plan covers every date in a 30-day departure window, in both
directions, and renews every three days from 17 October 2026. Each cycle has
960 queries, executed in batches of at most 240. Completed cycle queries are
not repeated on later batch runs. Recovery observations retain their actual
calendar date. Old rows are preserved. See the root README for hosted times.


## Persistent data

The default storage directory is `transportation/data/`. Use
`ROTELIO_DATA_DIR` to select an existing persistent directory. When moving to a
new checkout or another host, retain the original pool and its dated backups;
do not start with an empty database if the intention is to continue its history.

| Output | Contents |
| --- | --- |
| `flights.sqlite3` | Persistent observations, query states and events |
| `flights.csv` | All retained valid observations |
| `daily/YYYY-MM-DD/flights.csv` | Observations from one collection day |
| `query_prices.csv` | Minimum and median eligible fares per query snapshot |
| `daily/YYYY-MM-DD/queries.csv` | Query outcomes and observation timestamps |
| `daily/YYYY-MM-DD/manifest.json` | Actual coverage and validation counts |
| `rejected_offers.csv` | Invalid source-card identifiers and reasons |
| `raw/YYYY-MM-DD/` | Original captured snapshots |

Daily files update during collection. Full-history exports refresh at run
completion or through `export`. Preserve the database as well as CSV exports:
CSV alone does not retain query progress or the stored access-block state.
Generated datasets are not tracked in Git.

## Validation

The requested route and date, currency, airports, displayed clocks and duration
are checked before export. A displayed arrival day offset is validated. Missing
clocks or inconsistent prices are not filled in. HTTP access blocks and human
verification stop collection; they are not bypassed.

`coverage_complete=true` means each planned query has at least one eligible,
validated offer. `validation_clean=false` means some additional source cards
were rejected. Full query coverage does not claim that every source card was
valid or that all supplier fare bundles were collected.

An `empty_unverified` query is not proof of no flights. A stored block requires
diagnosis before the manual `continue` command is used. Schedulers must not clear
blocks or repeatedly retry access challenges automatically.

## Testing and integration

```sh
python -m unittest discover -s transportation/tests -v
```

Tests use temporary databases and isolated fixtures, including next-day
accumulation, duplicate prevention, date validation and exact price units.
See [DATA_CONTRACT.md](DATA_CONTRACT.md) for all 48 fields and
[INTEGRATION.md](INTEGRATION.md) for date matching and budget calculations.

Collection hosting and scheduling are configured separately. This module has no
automatic execution trigger in the shared repository.
