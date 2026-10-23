import contextlib
import io
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "transportation"))
import daily
from flight_plan import build_jobs, cycle_state
from flight_recovery import review_loading_stop
from flight_store import connect, get_control, save_query, set_control


class LoadingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "flights.sqlite3"
        self.db = connect(self.path)
        self.addCleanup(lambda: self.db.close())
        self.cfg = daily.load_config()
        self.day = date(2026, 10, 23)
        self.clock = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
        self.job = build_jobs(self.cfg, self.day)[0]
        daily.ensure_plan(self.db, self.cfg, self.day)

    def timeout(self, attempts=1, retry_after=None):
        self.db.execute(
            "UPDATE queries SET attempt_count=? WHERE query_id=?",
            (attempts, self.job["query_id"]),
        )
        save_query(self.db, self.job, "load_timeout", retry_after=(retry_after or self.clock).isoformat())

    def test_loading_timeout_retries_only_after_cooldown_and_never_counts_as_coverage(self):
        self.timeout(retry_after=self.clock + timedelta(minutes=10))
        before = cycle_state(self.db, self.cfg, self.day, at=self.clock)
        self.assertNotIn(self.job["query_id"], before["runnable_ids"])
        self.assertEqual(before["offer_count"], 0)
        self.assertFalse(before["coverage_complete"])
        after = cycle_state(self.db, self.cfg, self.day, at=self.clock + timedelta(minutes=10))
        self.assertIn(self.job["query_id"], after["runnable_ids"])
        daily.ensure_plan(self.db, self.cfg, self.day)
        row = self.db.execute("SELECT query_state,attempt_count FROM queries WHERE query_id=?", (self.job["query_id"],)).fetchone()
        self.assertEqual(tuple(row), ("pending", 1))

    def test_second_timeout_cannot_retry_on_same_or_following_day(self):
        self.timeout(attempts=2)
        for day in (self.day, date(2026, 10, 24)):
            state = cycle_state(self.db, self.cfg, day, at=self.clock + timedelta(days=1))
            self.assertNotIn(self.job["query_id"], state["runnable_ids"])

    def test_cooldown_survives_day_boundary(self):
        self.timeout(retry_after=self.clock + timedelta(days=1))
        following = date(2026, 10, 24)
        self.assertNotIn(self.job["query_id"], cycle_state(self.db, self.cfg, following, at=self.clock)["runnable_ids"])
        daily.ensure_plan(self.db, self.cfg, following)
        state = cycle_state(self.db, self.cfg, following, at=self.clock + timedelta(days=1, minutes=1))
        self.assertIn(self.job["query_id"], state["runnable_ids"])
        self.assertEqual(state["cycle_attempt_count"], 1)

    def test_missing_cooldown_fails_closed(self):
        self.timeout()
        save_query(self.db, self.job, "load_timeout")
        self.assertNotIn(self.job["query_id"], cycle_state(self.db, self.cfg, self.day)["runnable_ids"])

    def legacy_stop(self):
        self.db.execute("UPDATE queries SET attempt_count=1 WHERE query_id=?", (self.job["query_id"],))
        save_query(self.db, self.job, "error", error_code="RuntimeError", error_message="RuntimeError: results_load_unverified")
        set_control(self.db, "blocked", "RuntimeError: results_load_unverified")

    def test_explicit_review_preserves_attempts_and_observations(self):
        self.legacy_stop()
        self.db.execute("INSERT INTO offers VALUES('old','old','2026-10-20','old','{}')")
        self.db.commit()
        review_loading_stop(self.db, self.cfg, self.day, self.job["query_id"])
        self.assertIsNotNone(get_control(self.db, "blocked"))
        review_loading_stop(self.db, self.cfg, self.day, self.job["query_id"], apply=True)
        self.assertIsNone(get_control(self.db, "blocked"))
        state = cycle_state(self.db, self.cfg, self.day)
        self.assertIn(self.job["query_id"], state["runnable_ids"])
        self.assertEqual(state["cycle_attempt_count"], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM offers").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM events WHERE event_type='loading_stop_reviewed'").fetchone()[0], 1)

    def test_review_cannot_clear_access_validation_or_unknown_stops(self):
        self.legacy_stop()
        for reason in ("HTTP_403", "HTTP_429", "human_verification_or_access_denied", "Widespread fare validation failure", "unknown"):
            with self.subTest(reason=reason):
                set_control(self.db, "blocked", reason)
                with self.assertRaises(ValueError):
                    review_loading_stop(self.db, self.cfg, self.day, self.job["query_id"], apply=True)
                self.assertEqual(get_control(self.db, "blocked"), reason)

    def test_review_rejects_wrong_query_expired_date_and_exhausted_retry(self):
        self.legacy_stop()
        for key, day in (("wrong", self.day), (self.job["query_id"], date(2026, 10, 25))):
            with self.assertRaises(ValueError):
                review_loading_stop(self.db, self.cfg, day, key, apply=True)
        self.db.execute("UPDATE queries SET attempt_count=2 WHERE query_id=?", (self.job["query_id"],))
        self.db.commit()
        with self.assertRaises(ValueError):
            review_loading_stop(self.db, self.cfg, self.day, self.job["query_id"], apply=True)
        self.assertIsNotNone(get_control(self.db, "blocked"))

    def test_three_consecutive_loading_timeouts_stop_without_fabricated_prices(self):
        self.db.close()
        cfg = {**self.cfg, "minimum_seconds_between_searches": 0}
        with patch.object(daily, "DATA", Path(self.folder.name)), patch.object(daily, "today", return_value=self.day), patch.object(daily, "driver_start", return_value=MagicMock()), patch.object(daily, "choose_node", return_value="test"), patch.object(daily, "capture", side_effect=daily.ResultsLoadingTimeout("loading")) as source, patch.object(daily, "export", return_value={}), contextlib.redirect_stdout(io.StringIO()), patch.object(daily, "connect", side_effect=lambda: connect(self.path)):
            self.assertEqual(daily.collect(cfg, 5), 2)
        self.assertEqual(source.call_count, 3)
        self.db = connect(self.path)
        self.assertEqual(self.db.execute("SELECT count(*) FROM queries WHERE query_state='load_timeout'").fetchone()[0], 3)
        self.assertEqual(self.db.execute("SELECT count(*) FROM offers").fetchone()[0], 0)
        self.assertIn("Consecutive", get_control(self.db, "blocked"))

    def test_isolated_loading_timeout_continues_but_access_block_stops_immediately(self):
        self.db.close()
        cfg = {**self.cfg, "minimum_seconds_between_searches": 0}
        errors = [daily.ResultsLoadingTimeout("loading"), daily.AccessBlocked("HTTP_429")]
        with patch.object(daily, "DATA", Path(self.folder.name)), patch.object(daily, "today", return_value=self.day), patch.object(daily, "driver_start", return_value=MagicMock()), patch.object(daily, "choose_node", return_value="test"), patch.object(daily, "capture", side_effect=errors) as source, patch.object(daily, "export", return_value={}), contextlib.redirect_stdout(io.StringIO()), patch.object(daily, "connect", side_effect=lambda: connect(self.path)):
            self.assertEqual(daily.collect(cfg, 5), 3)
        self.assertEqual(source.call_count, 2)
        self.db = connect(self.path)
        self.assertEqual(get_control(self.db, "blocked"), "HTTP_429")

    def test_capture_distinguishes_loading_from_unknown_pages_and_access_denial(self):
        for blocked, loading, expected in ((False, True, daily.ResultsLoadingTimeout), (False, False, RuntimeError), (True, True, daily.AccessBlocked)):
            with self.subTest(blocked=blocked, loading=loading):
                page = MagicMock()
                page.goto.return_value.status = 200
                page.evaluate.return_value = dict(ready=False, blocked=blocked, loading=loading, count=0, signature="")
                with patch.object(daily.time, "monotonic", side_effect=[0, 0, 1000]):
                    with self.assertRaises(expected) as error:
                        daily.capture(page, self.job, self.cfg)
                self.assertIs(type(error.exception), expected)
                self.assertEqual(page.goto.call_count, 1)

    def test_http_access_blocks_stop_before_polling(self):
        for code in (401, 403, 429):
            with self.subTest(code=code):
                page = MagicMock()
                page.goto.return_value.status = code
                with self.assertRaises(daily.AccessBlocked):
                    daily.capture(page, self.job, self.cfg)
                page.evaluate.assert_not_called()

    def test_retry_budget_is_not_extended_by_deferred_queries(self):
        self.timeout()
        other = build_jobs(self.cfg, self.day)[1]["query_id"]
        self.db.execute("UPDATE queries SET attempt_count=991 WHERE query_id=?", (other,))
        self.db.commit()
        state = cycle_state(self.db, self.cfg, self.day, at=self.clock)
        self.assertEqual(state["remaining_attempt_budget"], 0)
        self.assertEqual(state["runnable_query_count"], 0)
        self.assertFalse(state["coverage_complete"])

    def test_successful_deferred_retry_is_covered_once(self):
        self.timeout()
        daily.ensure_plan(self.db, self.cfg, self.day)
        self.db.execute("UPDATE queries SET attempt_count=2 WHERE query_id=?", (self.job["query_id"],))
        offer = dict(
            observation_id="retried-observation", snapshot_id="retried-snapshot",
            collection_date=self.day.isoformat(), query_id=self.job["query_id"],
            analysis_eligible=True,
        )
        save_query(self.db, self.job, "ok", offers=[offer])
        state = cycle_state(self.db, self.cfg, self.day)
        self.assertEqual(state["covered_query_count"], 1)
        self.assertEqual(state["offer_count"], 1)
        self.assertEqual(state["cycle_attempt_count"], 2)
        self.assertNotIn(self.job["query_id"], state["runnable_ids"])
