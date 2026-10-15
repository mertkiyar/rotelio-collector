"""Offline CSV/SQLite contract audit. Does not collect or modify observations."""

import argparse
import csv
import fcntl
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from daily import load_config, build_jobs, today
from flight_schema import FIELDS, VERSION
from flight_store import DATA


def audit(day):
    with (DATA / "collector.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return {
                "audit_passed": False,
                "audit_deferred": True,
                "reason": "collector_running",
            }
        db = sqlite3.connect(
            "file:" + str((DATA / "flights.sqlite3").resolve()) + "?mode=ro", uri=True
        )
        db.row_factory = sqlite3.Row
        cfg = load_config()
        jobs = {j["query_id"]: j for j in build_jobs(cfg, day)}
        records = [
            json.loads(r[0])
            for r in db.execute(
                "SELECT payload FROM offers ORDER BY collection_date,query_id,observation_id"
            )
        ]
        ids = set()
        errors = []
        for r in records:
            if set(r) != set(FIELDS):
                errors.append("field_set:" + r.get("observation_id", "unknown"))
                continue
            if r["schema_version"] != VERSION:
                errors.append("schema_version")
            if r["observation_id"] in ids:
                errors.append("duplicate_observation")
            ids.add(r["observation_id"])
            if r["collection_date"] < cfg["cohort_anchor_date"]:
                errors.append("legacy_observation")
            if "UNIT TEST" in r["carrier"]:
                errors.append("unit_fixture_in_pool")
            if (
                r["price_minor"] <= 0
                or r["price_amount"]
                != f"{r['price_minor']//100}.{r['price_minor']%100:02d}"
            ):
                errors.append("price_unit_mismatch")
            dep = datetime.fromisoformat(r["departure_at"])
            arr = datetime.fromisoformat(r["arrival_at"])
            obs = datetime.fromisoformat(r["observed_at"])
            if not dep.tzinfo or not arr.tzinfo or not obs.tzinfo:
                errors.append("missing_timezone")
            if dep + timedelta(minutes=r["duration_minutes"]) != arr:
                errors.append("clock_duration_mismatch")
            if not r["departure_time"] or not r["arrival_time"]:
                errors.append("missing_clock")
            if r["collection_date"] == day.isoformat():
                j = jobs.get(r["query_id"])
                if (
                    not j
                    or r["departure_date"] != j["travel_date"]
                    or r["region_id"] != j["region_id"]
                ):
                    errors.append("pilot_query_mismatch")
        expected_all = {r["observation_id"] for r in records}
        expected_day = {
            r["observation_id"]
            for r in records
            if r["collection_date"] == day.isoformat()
        }
        for path, expected in [
            (DATA / "flights.csv", expected_all),
            (DATA / "daily" / day.isoformat() / "flights.csv", expected_day),
        ]:
            with path.open(encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames != FIELDS:
                    errors.append("csv_header_mismatch:" + str(path))
                rows = list(reader)
                if (
                    len(rows) != len(expected)
                    or {r["observation_id"] for r in rows} != expected
                ):
                    errors.append("csv_sqlite_mismatch:" + str(path))
        counts = db.execute(
            "SELECT query_state,count(*) FROM queries WHERE collection_date=? GROUP BY query_state",
            (day.isoformat(),),
        ).fetchall()
        current = [r for r in records if r["collection_date"] == day.isoformat()]
        covered = {r["query_id"] for r in current if r["analysis_eligible"]}
        report = {
            "audit_passed": not errors,
            "collection_date": day.isoformat(),
            "schema_version": VERSION,
            "canonical_field_count": len(FIELDS),
            "offer_count": len(current),
            "region_count": len({r["region_id"] for r in current}),
            "query_count": sum(r[1] for r in counts),
            "covered_query_count": len(covered),
            "coverage_complete": set(jobs) <= covered,
            "query_states": dict(counts),
            "minimum_observed_at": min(
                (r["observed_at"] for r in current), default=None
            ),
            "maximum_observed_at": max(
                (r["observed_at"] for r in current), default=None
            ),
            "errors": errors,
        }
        db.close()
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-coverage", action="store_true")
    args = parser.parse_args()
    result = audit(today())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(
        0
        if result["audit_passed"]
        and (not args.require_coverage or result.get("coverage_complete"))
        else 2
    )
