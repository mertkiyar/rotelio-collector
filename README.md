# Rotelio flight collector

Daily flight observations for the Rotelio holiday-planning pilot. The workflow
runs on a standard GitHub-hosted Linux runner, independently of a personal
computer. This repository contains code and isolated test fixtures only.
Collected offers, CSV exports, raw snapshots and diagnostic logs are retained in
a separate private repository.

## Collection scope

Istanbul (IST and SAW) and Ankara (ESB) connect to eight pilot regions:
Antalya, Izmir/Cesme, Bodrum, Fethiye, Trabzon, Nevsehir/Cappadocia, Gaziantep
and Erzurum. Both directions across seven selected future departure dates
produce 224 daily searches. Search starts remain at least 30 seconds apart.
This is a sampled date plan, not a scan of every date or route in Turkey.

The daily schedule is 06:17 UTC, or 09:17 in Turkey. GitHub can delay scheduled
runs. Manual runs support a one-query access check, a 32-query batch or the full
224-query plan. A full day generally takes about two hours.

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
| `COLLECTION_ENABLED` | Actions variable | `true` enables the daily schedule after a successful hosted access check |

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
pilot retains two assets per collection run. These service limits and pricing
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
