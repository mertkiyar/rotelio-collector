# Hosted collection setup

## Repositories

Use two repositories owned by the same account:

- `rotelio-collector`: public code and the collection workflow.
- `rotelio-flight-data`: private checkpoints and collected datasets.

Do not change the visibility of the data repository. The client checks that it
is private before reading or publishing data. Public code must not contain real
sample observations, raw snapshots, access tokens or local database files.

## Initial migration

An existing SQLite pool is copied through SQLite's backup API. The bootstrap
process compresses and uploads the consistent copy to the private release,
then downloads and restores it. Every observation identifier must match the
original. The original local pool remains unchanged.

The bootstrap bundle includes dated CSV exports and events. Older raw page
snapshots remain in the original local backup by default; use `--include-raw`
to include them during initial migration. This avoids delaying the database
transfer when old raw files have been offloaded to cloud-synced local storage.
The database contains all validated observations and quarantined source rows
regardless of this option. New hosted runs include their own raw snapshots.

Bootstrap is a one-time operation. It refuses to replace an already initialized
checkpoint. A vault release tagged `flight-pool` must exist with the JSON body
`{"format_version": 1, "status": "uninitialized"}` before bootstrap.

```sh
python bootstrap.py --data-directory /absolute/path/to/existing/data --minimum-observations EXPECTED_COUNT
```

This command needs `DATA_VAULT_REPOSITORY` and `DATA_VAULT_TOKEN` in its local
environment. Never place credentials in command arguments, tracked files or
chat messages.

## Fine-grained access token

1. Open GitHub Settings, Developer settings, Personal access tokens,
   Fine-grained tokens. Create a token named `Rotelio data vault`.
2. Select the account that owns the private data repository as Resource owner.
3. Set an expiry that covers the project period, for example 90 days.
4. Under Repository access, choose Only select repositories and select only
   `rotelio-flight-data`.
5. Under Repository permissions, grant Contents: Read and write. Metadata read
   access is implicit. No Actions, administration or other repository access is
   needed. Do not choose access to all repositories.
6. Generate the token, then open `rotelio-collector`, Settings, Secrets and
   variables, Actions, New repository secret.
7. Use the secret name `DATA_VAULT_TOKEN` and paste the token into the Secret
   field. Add the secret. Do not paste it into an issue, commit or message.

Repository secret values cannot subsequently be read back. When a token expires,
replace this secret with a new token with the same restricted permissions.

## Repository variables

Under Settings, Secrets and variables, Actions, Variables, configure:

```text
DATA_VAULT_REPOSITORY = OWNER/rotelio-flight-data
COLLECTION_ENABLED = false
```

Keep scheduling disabled until the initial hosted run succeeds.

## First hosted run

1. Open Actions in the public code repository.
2. Select Collect flight observations, then Run workflow.
3. Select `main` and query limit `1`.
4. Confirm that private vault access, tests, browser installation, collection,
   checkpoint upload and final audit succeed.
5. Check that `previous_observation_count` matches the existing pool and
   `added_observation_count` is positive. This check is deliberately not full
   coverage. Review the private bundle if source access or parsing fails.

If the source returns 403/429 or human verification, do not repeatedly rerun it
or clear the stored block automatically. Hosting is not considered operational
until a genuine cloud query has succeeded.

## Daily operation

After the access check, set `COLLECTION_ENABLED=true`. The daily schedule runs at
06:17 UTC (09:17 Turkey time), with a recovery check at 15:17 UTC (18:17 Turkey
time). Both use the same retained pool and the current Turkey calendar date.
For the rest of the current day's plan, manually run query limit `224`.
Completed queries on that observation day are skipped.

The coverage check restores and validates the private pool before installing
the browser. When the exact daily plan is already covered and the audit passes,
`skipped=true` and `reason=daily_plan_already_covered` appear in the summary.
Browser installation, source collection and checkpoint upload are skipped.
An incomplete morning run can leave pending queries for the evening check;
previous observations remain intact. Source-access and validation blocks are
never cleared automatically. An incomplete plan still fails the full-run check
instead of being labelled complete.

Stop the old local scheduler before enabling regular cloud collection. Two
independent pool copies must not both act as the current writer. Keep the local
database as a dated backup, not an independently growing live database.

Review the Actions summary for counts and coverage. Raw prices are not printed
to public logs. In the private repository, Releases, Flight observation pool:

- `pool-*.sqlite3.gz` files are full cumulative database checkpoints.
- `observations-*.tar.gz` files from hosted runs contain the combined CSV,
  that run's dated CSV and raw records.
- The release description points to the latest committed checkpoint.

Older assets remain available. Download the latest checkpoint and use SQLite
exports when a combined CSV of every observation day is needed. Retain an
occasional independent downloaded backup as well.

## Scheduling and costs

The laptop can be asleep or off after hosted scheduling has been verified and
enabled. No local browser session is needed by the hosted workflow. Scheduled
Actions can be delayed or dropped and require the workflow on the default
branch. Confirm an actual run with event `schedule` in the Actions history;
a successful manual dispatch alone does not verify automatic scheduling.

GitHub can disable a public repository's scheduled workflows after 60 days of
repository inactivity. Review the Actions page if development stops for that
long and re-enable the workflow when needed. Replace the vault token before its
configured expiry. These are service lifecycle requirements, not laptop uptime
requirements.

The job uses only a standard public-repository Linux runner. It is skipped if
the code repository becomes private. No Actions artifacts, paid runner types or
cache uploads are configured. The private data repository has no workflow.
