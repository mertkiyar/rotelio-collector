"""Restore the private pool, collect once, and retain a versioned checkpoint."""

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from vault import GitHubVault, VaultError, observation_ids

ROOT = Path(__file__).resolve().parent


def progress(data):
    database = data / "flights.sqlite3"
    day = datetime.now(ZoneInfo("Europe/Istanbul")).date().isoformat()
    try:
        db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            total, completed = db.execute(
                "SELECT count(*), coalesce(sum(query_state IN ('ok','partial')),0) "
                "FROM queries WHERE collection_date=?",
                (day,),
            ).fetchone()
            offers = db.execute(
                "SELECT count(*) FROM offers WHERE collection_date=?",
                (day,),
            ).fetchone()[0]
        finally:
            db.close()
        print(
            json.dumps(
                {
                    "progress": "running",
                    "processed_queries": completed,
                    "planned_queries": total,
                    "daily_observations": offers,
                }
            ),
            flush=True,
        )
    except sqlite3.Error:
        print("Collector is running; waiting for progress metadata.", flush=True)


def collect_process(limit, data, log):
    env = {
        **os.environ,
        "ROTELIO_DATA_DIR": str(data),
        "FLIGHT_BROWSER_CHANNEL": "chromium",
        "PYTHONUNBUFFERED": "1",
    }
    # The collector does not need access to the vault token.
    env.pop("DATA_VAULT_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    with log.open("ab") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "transportation/daily.py"),
                "collect",
                "--limit",
                str(limit),
            ],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 150 * 60
        try:
            while time.monotonic() < deadline:
                try:
                    return process.wait(
                        timeout=min(60, max(1, deadline - time.monotonic()))
                    )
                except subprocess.TimeoutExpired:
                    progress(data)
            raise subprocess.TimeoutExpired(process.args, 150 * 60)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=40)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
            return 130


def plan_covered(db, day, jobs):
    expected = {job["query_id"] for job in jobs}
    rows = db.execute(
        """SELECT q.query_id, q.query_state, EXISTS(
        SELECT 1 FROM offers o WHERE o.collection_date=q.collection_date
        AND o.query_id=q.query_id
        AND json_extract(o.payload,'$.analysis_eligible')=1)
        FROM queries q WHERE q.collection_date=?""",
        (day,),
    ).fetchall()
    return (
        bool(expected)
        and {row[0] for row in rows} == expected
        and all(row[1] in ("ok", "partial") and row[2] for row in rows)
    )


def write_summary(summary):
    # Aggregate counts only. Offer payloads and diagnostic logs stay private.
    print(json.dumps(summary, indent=2))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as output:
            output.write(
                "## Flight collection\n\n```json\n"
                + json.dumps(summary, indent=2)
                + "\n```\n"
            )


def workflow_output(needs_collection):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with Path(path).open("a", encoding="utf-8") as output:
            output.write(f"needs_collection={str(needs_collection).lower()}\n")


def run(limit, check_only=False):
    if not 1 <= limit <= 224:
        raise VaultError("Query limit must be between 1 and 224")
    vault = GitHubVault()
    with tempfile.TemporaryDirectory(prefix="rotelio-cloud-") as directory:
        working = Path(directory)
        data = working / "data"
        data.mkdir()
        database = data / "flights.sqlite3"
        before = vault.restore(database)
        baseline = observation_ids(database)
        if len(baseline) != before["offer_count"]:
            raise VaultError("Restored pool has duplicate identifiers")

        os.environ["ROTELIO_DATA_DIR"] = str(data)
        sys.path.insert(0, str(ROOT / "transportation"))
        from daily import today, load_config, build_jobs
        from flight_store import connect, export, get_control
        from audit import audit

        day = today()
        jobs = build_jobs(load_config(), day)
        db = connect(database)
        try:
            if get_control(db, "blocked"):
                raise VaultError(
                    "Stored collection block requires review; no source requests sent"
                )
            covered = plan_covered(db, day.isoformat(), jobs)
            current = export(db, day.isoformat()) if covered else None
        finally:
            db.close()
        if covered:
            report = audit(day)
            if not report["audit_passed"]:
                raise VaultError(
                    "Stored daily observations failed audit; no source requests sent"
                )
            write_summary(
                {
                    "collection_date": day.isoformat(),
                    "skipped": True,
                    "reason": "daily_plan_already_covered",
                    "total_observation_count": before["offer_count"],
                    "daily_observation_count": current["offer_count"],
                    "covered_query_count": len(jobs),
                    "planned_query_count": len(jobs),
                    "coverage_complete": True,
                    "audit_passed": True,
                    "source_requests_sent": 0,
                    "checkpoint_saved": False,
                }
            )
            workflow_output(False)
            return 0
        if check_only:
            write_summary(
                {
                    "collection_date": day.isoformat(),
                    "needs_collection": True,
                    "total_observation_count": before["offer_count"],
                    "planned_query_count": len(jobs),
                    "source_requests_sent": 0,
                }
            )
            workflow_output(True)
            return 0

        log = data / "collection.log"
        result = collect_process(limit, data, log)
        db = connect(database)
        try:
            current = export(db, day.isoformat())
        finally:
            db.close()
        report = audit(day)
        (data / "audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

        bundle = working / "observations.tar.gz"
        with tarfile.open(bundle, "w:gz") as archive:
            # No raw records are uploaded to public workflow artifacts or logs.
            for name in (
                "flights.csv",
                "query_prices.csv",
                "daily",
                "raw",
                "collection.log",
                "audit.json",
                "events.jsonl",
                "rejected_offers.csv",
            ):
                path = data / name
                if path.exists():
                    archive.add(path, arcname=name)
        published = vault.publish(
            database,
            baseline,
            expected_asset_id=before["asset_id"],
            bundle=bundle,
        )
        summary = {
            "collection_date": day.isoformat(),
            "requested_limit": limit,
            "collection_exit_code": result,
            "previous_observation_count": before["offer_count"],
            "total_observation_count": published["offer_count"],
            "added_observation_count": published["offer_count"] - before["offer_count"],
            "daily_observation_count": current["offer_count"],
            "covered_query_count": current["covered_query_count"],
            "planned_query_count": current["query_count"],
            "coverage_complete": current["coverage_complete"],
            "audit_passed": report["audit_passed"],
            "checkpoint_saved": True,
        }
        write_summary(summary)
        if result or not report["audit_passed"]:
            return result or 2
        if limit == 224 and not current["coverage_complete"]:
            return 2
        if limit < 224 and current["offer_count"] == 0:
            return 2
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check the private pool without source requests",
    )
    args = parser.parse_args()
    try:
        raise SystemExit(run(args.limit, check_only=args.check))
    except VaultError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
    except Exception as error:
        # Do not send source content, private URLs or credentials to public logs.
        print("Cloud collection failed: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(2)
