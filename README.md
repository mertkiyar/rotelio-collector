# Rotelio flight collector

Daily flight observations for the Rotelio holiday-planning pilot. The workflow
runs on a standard GitHub-hosted Linux runner, independently of a personal
computer. This repository contains code and isolated test fixtures only.
Collected offers, CSV exports, raw snapshots and diagnostic logs are retained in
a separate private repository.

## Collection scope

Istanbul (IST and SAW) and Ankara (ESB) connect to eight pilot regions:
Antalya, Izmir/Cesme, Bodrum, Fethiye, Trabzon, Nevsehir/Cappadocia, Gaziantep
and Erzurum. Every refresh covers both directions for **all 30 consecutive
future departure dates**: 2 origins × 8 destinations × 2 directions × 30 dates =
960 searches. The 48-field offer format is unchanged. Historical observations
remain in the same cumulative pool.

Refresh cycles begin on **17 October 2026** and repeat every three calendar days
(17, 20, 23 October, and so on). Python computes the anchored interval across
month boundaries; cron day-of-month shortcuts are not used. Each cycle fixes
its departure window to the following 30 dates. The first window is 18 October
through 16 November. This is data sampling and does not restrict UI date choices.

Scheduled checks run at 09:17, 12:17, 15:17, 18:17 and 21:17 Turkey time.
Each scheduled workflow can run four 240-query batches serially, so an active
refresh does not need four separate cron events to arrive. Each batch restores
the last committed private checkpoint and publishes its own version afterward.
Manual dispatch still runs one batch at the selected limit. A failed batch
stops the remaining jobs, preserving the stored access-block state.
Search starts remain at least 30 seconds apart, so 960 searches take at least
eight hours in total, with additional loading time. Actual GitHub start times
can be delayed or dropped. Runs share a single writer concurrency group.

A completed cycle causes subsequent invocations to exit before installing a
browser, sending source requests or uploading another checkpoint. Checks still
run on intervening days so delayed work can resume. Fresh cycles re-observe all
30 dates, including overlap with the preceding window. Up to 32 extra attempts
are reserved for interrupted work; both daily and cycle limits are 992 attempts.
Source-access and validation blocks are never automatically reset.

A saved stop is reported by preflight as `collection_blocked=true` and
`needs_collection=false`. Scheduled jobs then skip browser setup and source
requests without generating repeated collector failures. This is a paused,
incomplete cycle, not successful coverage. Direct collection still refuses to
run until the saved stop has been reviewed. The monitor keeps the incident open
and reports the active cycle even when a delayed check targeted an older one.

Coverage is measured over the complete cycle, using genuine observation dates
from its three calendar days. A resumed batch retains its actual capture date;
it is never backdated to the first day. Missed departure dates and empty or
blocked results remain missing, not zero-priced offers. They prevent full-cycle
coverage. Earlier seven-date observations remain valid history but cannot count
as coverage of a new cycle.

## Collection alerts

A separate monitor workflow runs after collection completes and checks the
private pool again at 23:57 Turkey time and 08:17 the following morning. Failed or cancelled collection
runs raise an immediate alert. The late checks detect a missing or incomplete
refresh cycle even when the collection workflow never started. They never collect
flights or change the private checkpoint.

A late check defers its final coverage decision while a collector for the same
three-day cycle is running or queued. It does not mark the partial pool complete
or close an existing alert. The writer's completion and the next scheduled check
reassess saved coverage. Work for a newer cycle cannot defer an older alert.

Alerts use an issue assigned to and mentioning the repository owner. The issue
contains aggregate coverage counts and a run link, without flight records or
private diagnostic details. Repeated checks of the same unresolved cycle do not
post another notification, even when a follow-up check changes its diagnosis.
A new cycle updates the existing issue; a verified complete cycle closes it.
A delayed healthy check for an older cycle cannot close a newer incident.

Delivering or deduplicating an alert is a successful monitor execution, while its
JSON report still says `healthy=false` and the issue remains open. Notification
API failures still fail the job. This avoids an additional failed-Actions
notification for every repeated health alert.

GitHub inbox and email delivery depend on the owner's notification preferences.
See [SETUP.md](SETUP.md#notification-delivery). The monitor has repository-local
Issues write permission; it uses the existing private-vault secret only for the
read-only health check. Actions read permission identifies active collection
runs. No additional credential or paid service is required.
Both workflows depend on GitHub Actions: an outage or disabled scheduling that
prevents every monitor run also prevents these alerts. An independent external
dead-man monitor would be needed to cover that failure mode.

## Private storage

Before each run the collector restores the latest private SQLite checkpoint.
It verifies the SHA-256 checksum, database integrity and observation count.
Collection adds new observations without removing earlier days. The final
checkpoint must retain every previously stored observation identifier.

Versioned checkpoints and dated CSV/raw bundles are uploaded as release assets
in the private data repository. Earlier assets are not deleted. The release
metadata identifies the latest committed checkpoint; uploading an asset alone
does not replace it. A stale writer is rejected. A missing or corrupt checkpoint
stops collection instead of creating an empty replacement pool.

The daily workflow preserves partial results and the stored source-block state
when the collector exits normally with an error. A hard runner termination can
lose uncheckpointed observations from that run; the last committed pool and
earlier backups remain available. No workflow artifact or cache is used as the
primary database.

## Setup

See [SETUP.md](SETUP.md) for the private repository and access-token steps.
The required repository settings are:

| Setting | Type | Purpose |
| --- | --- | --- |
| `DATA_VAULT_TOKEN` | Actions secret | Fine-grained token limited to the private data repository, Contents read/write |
| `DATA_VAULT_REPOSITORY` | Actions variable | Private storage repository in `owner/repository` format |
| `COLLECTION_ENABLED` | Actions variable | `true` enables the refresh schedule after a successful hosted access check |

The collection job is skipped if this code repository is private. Scheduled
collection remains disabled until `COLLECTION_ENABLED=true`. Manual runs can
perform the initial one-query check while the daily schedule is disabled.
All workflow invocations share one concurrency group; a new run does not cancel
an existing writer.

## Cost boundary

Only the standard `ubuntu-latest` GitHub-hosted runner is used. Standard hosted
execution in public repositories is free under GitHub's current billing rules.
The workflow does not upload Actions artifacts, create dependency caches or use
larger runners. Making this repository private causes the collection job to be
skipped rather than consume private-repository minutes.

Storage uses private release assets rather than Actions artifact storage. GitHub
documents up to 1,000 assets per release and a maximum of 2 GiB per asset; this
pilot retains two assets per run that performs collection. These service limits and pricing
can change and should be reviewed if the deployment is extended.

## Data quality and privacy

The fixed 48-field observation format is documented in
[DATA_CONTRACT.md](transportation/DATA_CONTRACT.md). Quotes are one-adult,
economy, one-way amounts in TRY. They are not verified checkout prices.
Inconsistent source cards are quarantined; unknown charges are not made zero.
HTTP access blocks and human verification stop collection and are not bypassed.

The public log contains aggregate progress only. Offer payloads and collector
diagnostics remain private. The vault token is supplied only to the steps that
need it and is removed from the flight-collector subprocess environment.

Full query coverage is distinct from all source cards passing validation.
The final audit checks the CSV/SQLite match, fields, prices, clocks and query
coverage. A hosted access check is required: successful access from a local
computer does not establish that the source accepts a cloud runner.

## Tests

```sh
python -m unittest discover -s transportation/tests -v
python -m unittest discover -s tests -v
```

Tests are offline, use temporary databases and never enter the live pool.
Dependencies for browser execution are pinned in
`transportation/requirements.txt`.

## References

- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [Scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [Release storage limits](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
- [Playwright on CI](https://playwright.dev/python/docs/ci)
