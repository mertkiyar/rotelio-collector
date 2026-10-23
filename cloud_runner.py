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
from datetime import date, datetime
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


def audit_exports(database, data, day, state):
    from flight_store import connect, export
    from audit import audit

    days = sorted(set(state["observation_dates"] + [day.isoformat()]))
    db = connect(database)
    try:
        for index, value in enumerate(days):
            export(db, value, full_history=index == 0)
    finally:
        db.close()
    reports = [audit(date.fromisoformat(value)) for value in days]
    result = {"audit_passed": all(r["audit_passed"] for r in reports), "days": reports}
    (data / "audit.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def cycle_summary(day, state):
    return {
        "collection_date": day.isoformat(),
        "cycle_start_date": state["cycle_start_date"],
        "next_cycle_date": state["next_cycle_date"],
        "travel_date_start": state.get("travel_date_start"),
        "travel_date_end": state.get("travel_date_end"),
        "cycle_observation_count": state["offer_count"],
        "daily_observation_count": state["daily_observation_count"],
        "covered_query_count": state["covered_query_count"],
        "planned_query_count": state["query_count"],
        "coverage_complete": state["coverage_complete"],
        "expired_query_count": state["expired_query_count"],
        "cycle_attempt_count": state["cycle_attempt_count"],
    }


def run(limit, check_only=False, cycle_day=None):
    if not 1 <= limit <= 240:
        raise VaultError("Batch query limit must be between 1 and 240")
    if cycle_day is not None and not check_only:
        raise VaultError(
            "A different cycle reference is allowed only for read-only checks"
        )
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
        from daily import today, load_config
        from flight_plan import cycle_state, public_cycle_state, expected_query_count
        from flight_store import connect, get_control

        day = today()
        cfg = load_config()
        db = connect(database)
        try:
            blocked = bool(get_control(db, "blocked"))
            if blocked and not check_only:
                raise VaultError(
                    "Stored collection block requires review; no source requests sent"
                )
            # A persistent stop affects the active collector. A delayed monitor
            # checking an older cycle must still report this current incident.
            state = cycle_state(db, cfg, day, plan_day=None if blocked else cycle_day)
        finally:
            db.close()
        summary = {
            **cycle_summary(day, state),
            "total_observation_count": before["offer_count"],
        }
        if blocked:
            write_summary({
                **summary,
                "skipped": True,
                "needs_collection": False,
                "collection_blocked": True,
                "reason": "collection_blocked",
                "source_requests_sent": 0,
                "checkpoint_saved": False,
            })
            workflow_output(False)
            return 0
        if state["cycle_start_date"] is None or state["coverage_complete"]:
            report = audit_exports(database, data, day, state)
            if not report["audit_passed"]:
                raise VaultError(
                    "Stored observations failed audit; no source requests sent"
                )
            write_summary(
                {
                    **summary,
                    "skipped": True,
                    "reason": (
                        "cycle_not_started"
                        if state["cycle_start_date"] is None
                        else "refresh_cycle_already_covered"
                    ),
                    "next_cycle_planned_query_count": expected_query_count(cfg),
                    "audit_passed": True,
                    "source_requests_sent": 0,
                    "checkpoint_saved": False,
                }
            )
            workflow_output(False)
            return 0
        if check_only or not state["runnable_query_count"]:
            write_summary(
                {
                    **summary,
                    "needs_collection": bool(state["runnable_query_count"]),
                    "reason": (
                        "pending_queries"
                        if state["runnable_query_count"]
                        else "incomplete_cycle_requires_review"
                    ),
                    "source_requests_sent": 0,
                }
            )
            workflow_output(bool(state["runnable_query_count"]))
            return 0 if check_only else 2

        log = data / "collection.log"
        result = collect_process(limit, data, log)
        db = connect(database)
        try:
            state = cycle_state(db, cfg, day)
        finally:
            db.close()
        report = audit_exports(database, data, day, state)
        cycle_folder = data / "cycles" / state["cycle_start_date"]
        cycle_folder.mkdir(parents=True, exist_ok=True)
        (cycle_folder / "manifest.json").write_text(
            json.dumps(public_cycle_state(state), indent=2), encoding="utf-8"
        )
        bundle = working / "observations.tar.gz"
        with tarfile.open(bundle, "w:gz") as archive:
            for name in (
                "flights.csv",
                "query_prices.csv",
                "daily",
                "cycles",
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
            database, baseline, expected_asset_id=before["asset_id"], bundle=bundle
        )
        added = published["offer_count"] - before["offer_count"]
        write_summary(
            {
                **cycle_summary(day, state),
                "requested_limit": limit,
                "collection_exit_code": result,
                "previous_observation_count": before["offer_count"],
                "total_observation_count": published["offer_count"],
                "added_observation_count": added,
                "audit_passed": report["audit_passed"],
                "checkpoint_saved": True,
            }
        )
        if result or not report["audit_passed"]:
            return result or 2
        return 0 if added > 0 or state["coverage_complete"] else 2


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
