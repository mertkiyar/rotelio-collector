import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "transportation"))
from daily import load_config, ensure_plan
from flight_plan import build_jobs, cycle_start, cycle_state
from flight_store import connect


def fill_queries(db, jobs, day):
    for job in jobs:
        payload = {**job, "collection_date": day.isoformat()}
        db.execute(
            "INSERT OR REPLACE INTO queries(collection_date,query_id,region_id,priority,query_state,attempt_count,offer_count,payload) VALUES(?,?,?,?,'ok',1,1,?)",
            (
                day.isoformat(),
                job["query_id"],
                job["region_id"],
                job["priority"],
                json.dumps(payload),
            ),
        )
        identifier = day.isoformat() + job["query_id"]
        db.execute(
            "INSERT OR IGNORE INTO offers VALUES(?,?,?,?,?)",
            (
                identifier,
                identifier,
                day.isoformat(),
                job["query_id"],
                '{"analysis_eligible":true}',
            ),
        )
    db.commit()


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.day = date(2026, 10, 17)
        self.jobs = build_jobs(self.cfg, self.day)

    def test_four_batches_cover_exactly_960_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect(Path(directory) / "pool.sqlite3")
            for part in range(4):
                self.assertEqual(
                    cycle_state(db, self.cfg, self.day)["covered_query_count"],
                    part * 240,
                )
                fill_queries(db, self.jobs[part * 240 : (part + 1) * 240], self.day)
            state = cycle_state(db, self.cfg, self.day)
            self.assertTrue(state["coverage_complete"])
            self.assertEqual(state["covered_query_count"], 960)
            self.assertEqual(state["cycle_attempt_count"], 960)
            self.assertEqual(ensure_plan(db, self.cfg, self.day), [])
            db.close()

    def test_resume_next_day_preserves_observation_dates_and_skips_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect(Path(directory) / "pool.sqlite3")
            fill_queries(db, self.jobs[:240], self.day)
            old_ids = {r[0] for r in db.execute("SELECT observation_id FROM offers")}
            following = date(2026, 10, 18)
            needed = ensure_plan(db, self.cfg, following)
            self.assertEqual(len(needed), 720)
            self.assertFalse(
                {j["query_id"] for j in needed}
                & {j["query_id"] for j in self.jobs[:240]}
            )
            fill_queries(db, needed, following)
            self.assertTrue(cycle_state(db, self.cfg, following)["coverage_complete"])
            self.assertEqual(
                {
                    r[0]
                    for r in db.execute("SELECT DISTINCT collection_date FROM offers")
                },
                {"2026-10-17", "2026-10-18"},
            )
            self.assertTrue(
                old_ids
                <= {r[0] for r in db.execute("SELECT observation_id FROM offers")}
            )
            next_cycle = cycle_state(db, self.cfg, date(2026, 10, 20))
            self.assertEqual(next_cycle["covered_query_count"], 0)
            self.assertFalse(next_cycle["coverage_complete"])
            db.close()

    def test_expired_missing_dates_are_not_zero_or_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect(Path(directory) / "pool.sqlite3")
            state = cycle_state(db, self.cfg, date(2026, 10, 19))
            self.assertEqual(state["expired_query_count"], 32)
            self.assertFalse(state["coverage_complete"])
            self.assertEqual(state["offer_count"], 0)
            db.close()

    def test_budget_exhaustion_does_not_imply_full_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect(Path(directory) / "pool.sqlite3")
            fill_queries(db, self.jobs, self.day)
            key = self.jobs[0]["query_id"]
            db.execute("DELETE FROM offers WHERE query_id=?", (key,))
            db.execute(
                "UPDATE queries SET query_state='empty_unverified' WHERE query_id=?",
                (key,),
            )
            db.execute("UPDATE queries SET attempt_count=33 WHERE query_id=?", (key,))
            db.commit()
            state = cycle_state(db, self.cfg, self.day)
            self.assertEqual(state["covered_query_count"], 959)
            self.assertEqual(state["runnable_query_count"], 0)
            self.assertFalse(state["coverage_complete"])
            db.close()

    def test_interval_is_stable_across_month_boundaries(self):
        self.assertEqual(cycle_start(self.cfg, date(2026, 10, 31)), date(2026, 10, 29))
        self.assertEqual(cycle_start(self.cfg, date(2026, 11, 1)), date(2026, 11, 1))
        self.assertEqual(cycle_start(self.cfg, date(2026, 11, 3)), date(2026, 11, 1))
