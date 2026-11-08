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
        following = date(2026, 10, 24)
        # A departure on the following day expires there, so use a later one.
        self.job = next(j for j in build_jobs(self.cfg, self.day) if j["travel_date"] > following.isoformat())
        self.timeout(retry_after=self.clock + timedelta(days=1))
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

    NAVIGATION_STOP = (
        "TimeoutError: Page.goto: Timeout 35000ms exceeded.\nCall log:\n"
        '  - navigating to "https://example.invalid/", waiting until "domcontentloaded"\n'
    )

    def navigation_stop(self):
        self.db.execute("UPDATE queries SET attempt_count=1 WHERE query_id=?", (self.job["query_id"],))
        save_query(self.db, self.job, "error", error_code="TimeoutError", error_message=self.NAVIGATION_STOP)
        set_control(self.db, "blocked", self.NAVIGATION_STOP)

    def test_reviewed_navigation_stop_in_the_active_cycle_allows_one_retry(self):
        self.navigation_stop()
        review_loading_stop(self.db, self.cfg, self.day, self.job["query_id"], apply=True)
        self.assertIsNone(get_control(self.db, "blocked"))
        state = cycle_state(self.db, self.cfg, self.day)
        self.assertIn(self.job["query_id"], state["runnable_ids"])
        self.assertEqual(state["cycle_attempt_count"], 1)

    def test_reviewed_navigation_stop_from_a_past_cycle_clears_without_coverage(self):
        self.navigation_stop()
        self.db.execute("INSERT INTO offers VALUES('old','old','2026-10-20','old','{}')")
        self.db.commit()
        later = date(2026, 11, 8)
        review_loading_stop(self.db, self.cfg, later, self.job["query_id"])
        self.assertEqual(get_control(self.db, "blocked"), self.NAVIGATION_STOP)
        review_loading_stop(self.db, self.cfg, later, self.job["query_id"], apply=True)
        self.assertIsNone(get_control(self.db, "blocked"))
        row = self.db.execute(
            "SELECT query_state,offer_count FROM queries WHERE query_id=? ORDER BY collection_date DESC LIMIT 1",
            (self.job["query_id"],),
        ).fetchone()
        self.assertEqual(tuple(row), ("error", 0))
        self.assertEqual(self.db.execute("SELECT count(*) FROM offers").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM events WHERE event_type='navigation_stop_reviewed'").fetchone()[0], 1)

    def test_navigation_review_requires_the_query_that_saved_the_stop(self):
        self.navigation_stop()
        other = build_jobs(self.cfg, self.day)[1]["query_id"]
        for key in ("missing", other):
            with self.subTest(key=key), self.assertRaises(ValueError):
                review_loading_stop(self.db, self.cfg, self.day, key, apply=True)
        set_control(self.db, "blocked", "TimeoutError: Locator.click: Timeout 8000ms exceeded.")
        with self.assertRaises(ValueError):
            review_loading_stop(self.db, self.cfg, self.day, self.job["query_id"], apply=True)
        self.assertIsNotNone(get_control(self.db, "blocked"))

    DATE_STOP = "ValueError: future_travel_date_required"

    def same_day_stop(self, collection_date=None):
        job = {**self.job, "collection_date": collection_date or self.job["travel_date"]}
        # Reproduce the row an earlier plan created for a departure on the run day.
        self.db.execute(
            "INSERT OR IGNORE INTO queries(collection_date,query_id,region_id,priority,attempt_count,payload) "
            "SELECT ?,query_id,region_id,priority,1,payload FROM queries WHERE query_id=? LIMIT 1",
            (job["collection_date"], job["query_id"]),
        )
        save_query(self.db, job, "error", error_code="ValueError", error_message=self.DATE_STOP)
        set_control(self.db, "blocked", self.DATE_STOP)

    def test_reviewed_same_day_departure_stop_clears_without_a_retry_or_fare(self):
        self.same_day_stop()
        later = date(2026, 10, 24)
        review_loading_stop(self.db, self.cfg, later, self.job["query_id"])
        self.assertEqual(get_control(self.db, "blocked"), self.DATE_STOP)
        review_loading_stop(self.db, self.cfg, later, self.job["query_id"], apply=True)
        self.assertIsNone(get_control(self.db, "blocked"))
        row = self.db.execute(
            "SELECT query_state,offer_count FROM queries WHERE query_id=? ORDER BY collection_date DESC LIMIT 1",
            (self.job["query_id"],),
        ).fetchone()
        self.assertEqual(tuple(row), ("error", 0))
        self.assertNotIn(self.job["query_id"], cycle_state(self.db, self.cfg, later)["runnable_ids"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM events WHERE event_type='date_stop_reviewed'").fetchone()[0], 1)

    def test_same_day_review_rejects_other_queries_and_future_departures(self):
        self.same_day_stop(collection_date="2026-10-23")
        with self.assertRaises(ValueError):
            review_loading_stop(self.db, self.cfg, self.day, self.job["query_id"], apply=True)
        self.same_day_stop()
        other = build_jobs(self.cfg, self.day)[1]["query_id"]
        for key in ("missing", other):
            with self.subTest(key=key), self.assertRaises(ValueError):
                review_loading_stop(self.db, self.cfg, self.day, key, apply=True)
        self.assertEqual(get_control(self.db, "blocked"), self.DATE_STOP)

    MIDNIGHT_STOP = "ValueError: observation_date_mismatch"

    def midnight_stop(self, failed_at):
        self.db.execute("UPDATE queries SET attempt_count=1 WHERE query_id=?", (self.job["query_id"],))
        save_query(self.db, self.job, "error", error_code="ValueError", error_message=self.MIDNIGHT_STOP)
        self.db.execute(
            "INSERT INTO events(at,event_type,message) VALUES(?,?,?)",
            (failed_at, "query_error", self.job["query_id"] + " " + self.MIDNIGHT_STOP),
        )
        set_control(self.db, "blocked", self.MIDNIGHT_STOP)

    def test_reviewed_midnight_stop_allows_one_retry_in_the_active_cycle(self):
        following = date(2026, 10, 24)
        self.job = next(j for j in build_jobs(self.cfg, self.day) if j["travel_date"] > following.isoformat())
        # 21:00:07 UTC on 23 October is 00:00:07 on 24 October in Turkey.
        self.midnight_stop("2026-10-23T21:00:07+00:00")
        review_loading_stop(self.db, self.cfg, following, self.job["query_id"])
        self.assertEqual(get_control(self.db, "blocked"), self.MIDNIGHT_STOP)
        review_loading_stop(self.db, self.cfg, following, self.job["query_id"], apply=True)
        self.assertIsNone(get_control(self.db, "blocked"))
        row = self.db.execute(
            "SELECT query_state,offer_count FROM queries WHERE query_id=? AND collection_date=?",
            (self.job["query_id"], self.day.isoformat()),
        ).fetchone()
        self.assertEqual(tuple(row), ("load_timeout", 0))

    def test_midnight_review_requires_a_search_that_crossed_midnight(self):
        self.midnight_stop("2026-10-23T20:59:00+00:00")
        with self.assertRaises(ValueError):
            review_loading_stop(self.db, self.cfg, self.day, self.job["query_id"], apply=True)
        other = build_jobs(self.cfg, self.day)[1]["query_id"]
        with self.assertRaises(ValueError):
            review_loading_stop(self.db, self.cfg, self.day, other, apply=True)
        self.assertEqual(get_control(self.db, "blocked"), self.MIDNIGHT_STOP)

    def test_no_search_starts_close_to_midnight(self):
        self.db.close()
        cfg = {**self.cfg, "minimum_seconds_between_searches": 0}
        with patch.object(daily, "DATA", Path(self.folder.name)), patch.object(daily, "today", return_value=self.day), patch.object(daily, "seconds_to_midnight", return_value=daily.MIDNIGHT_MARGIN_SECONDS - 1), patch.object(daily, "driver_start", return_value=MagicMock()), patch.object(daily, "choose_node", return_value="test"), patch.object(daily, "capture") as source, patch.object(daily, "export", return_value={}), contextlib.redirect_stdout(io.StringIO()), patch.object(daily, "connect", side_effect=lambda: connect(self.path)):
            self.assertEqual(daily.collect(cfg, 5), 0)
        source.assert_not_called()
        self.db = connect(self.path)
        self.assertIsNone(get_control(self.db, "blocked"))
        self.assertEqual(self.db.execute("SELECT count(*) FROM events WHERE event_type='day_rollover'").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM queries WHERE query_state='running'").fetchone()[0], 0)

    def test_consecutive_timeout_stop_clears_only_for_a_deferred_query(self):
        set_control(self.db, "blocked", "Consecutive supplier loading timeouts require review")
        with self.assertRaises(ValueError):
            review_loading_stop(self.db, self.cfg, self.day, self.job["query_id"], apply=True)
        self.timeout(retry_after=self.clock + timedelta(minutes=10))
        review_loading_stop(self.db, self.cfg, self.day, self.job["query_id"], apply=True)
        self.assertIsNone(get_control(self.db, "blocked"))
        state = cycle_state(self.db, self.cfg, self.day, at=self.clock)
        self.assertNotIn(self.job["query_id"], state["runnable_ids"])

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

    def test_capture_defers_a_page_navigation_timeout_but_not_other_failures(self):
        timeout = type("TimeoutError", (Exception,), {})  # Playwright's own class name
        page = MagicMock()
        page.goto.side_effect = timeout("Page.goto: Timeout 35000ms exceeded.")
        with self.assertRaises(daily.NavigationTimeout) as error:
            daily.capture(page, self.job, self.cfg)
        self.assertIsInstance(error.exception, daily.ResultsLoadingTimeout)
        page.evaluate.assert_not_called()
        page.goto.side_effect = ConnectionError("net::ERR_NAME_NOT_RESOLVED")
        with self.assertRaises(ConnectionError):
            daily.capture(page, self.job, self.cfg)

    def test_navigation_timeout_is_deferred_without_a_price_or_stop(self):
        self.db.close()
        cfg = {**self.cfg, "minimum_seconds_between_searches": 0}
        errors = [daily.NavigationTimeout("slow page"), daily.AccessBlocked("HTTP_429")]
        with patch.object(daily, "DATA", Path(self.folder.name)), patch.object(daily, "today", return_value=self.day), patch.object(daily, "driver_start", return_value=MagicMock()), patch.object(daily, "choose_node", return_value="test"), patch.object(daily, "capture", side_effect=errors) as source, patch.object(daily, "export", return_value={}), contextlib.redirect_stdout(io.StringIO()), patch.object(daily, "connect", side_effect=lambda: connect(self.path)):
            self.assertEqual(daily.collect(cfg, 5), 3)
        self.assertEqual(source.call_count, 2)
        self.db = connect(self.path)
        row = self.db.execute("SELECT query_state,json_extract(payload,'$.error_code'),offer_count FROM queries WHERE query_state='load_timeout'").fetchone()
        self.assertEqual(tuple(row), ("load_timeout", "navigation_timeout", 0))
        self.assertEqual(get_control(self.db, "blocked"), "HTTP_429")

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
