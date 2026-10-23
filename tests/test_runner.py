import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from cloud_runner import ROOT, collect_process, run

sys.path.insert(0, str(ROOT / "transportation"))


def daily_database(path):
    from flight_store import connect

    db = connect(path)
    for query, state in (("first", "ok"), ("second", "partial")):
        db.execute(
            "INSERT INTO queries(collection_date,query_id,region_id,priority,query_state,payload) "
            "VALUES('2026-10-16',?,'test',0,?,'{}')",
            (query, state),
        )
        db.execute(
            "INSERT INTO offers VALUES(?,?,'2026-10-16',?,?)",
            (query, query, query, json.dumps({"analysis_eligible": True})),
        )
    db.commit()
    return db


class RunnerTests(unittest.TestCase):
    def test_reviewed_loading_preflight_does_not_publish_a_reset_or_collect(self):
        from daily import ensure_plan, load_config
        from flight_plan import build_jobs
        from flight_store import connect, get_control, save_query, set_control
        from test_cycle import fill_queries

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.sqlite3"
            day = date(2026, 10, 23)
            cfg = load_config()
            db = connect(original)
            jobs = build_jobs(cfg, day)
            # Only the reviewed timeout remains. Preflight must expose its retry
            # without resetting the durable source checkpoint.
            fill_queries(db, jobs[1:], day)
            ensure_plan(db, cfg, day)
            db.execute("UPDATE queries SET attempt_count=1 WHERE query_id=?", (jobs[0]["query_id"],))
            save_query(db, jobs[0], "error", error_code="RuntimeError", error_message="RuntimeError: results_load_unverified")
            set_control(db, "blocked", "RuntimeError: results_load_unverified")
            db.close()
            vault = Mock()

            def restore(destination):
                shutil.copyfile(original, destination)
                return {"offer_count": 959, "asset_id": 7}

            vault.restore.side_effect = restore
            with patch("cloud_runner.GitHubVault", return_value=vault), patch("cloud_runner.collect_process") as source, patch("daily.today", return_value=day), patch.dict(os.environ, {"GITHUB_OUTPUT": str(root / "outputs")}), contextlib.redirect_stdout(io.StringIO()) as captured:
                self.assertEqual(run(1, check_only=True, reviewed_loading_query=jobs[0]["query_id"]), 0)
            self.assertTrue(json.loads(captured.getvalue())["needs_collection"])
            self.assertEqual((root / "outputs").read_text(), "needs_collection=true\n")
            source.assert_not_called()
            vault.publish.assert_not_called()
            db = connect(original)
            self.assertEqual(get_control(db, "blocked"), "RuntimeError: results_load_unverified")
            self.assertEqual(db.execute("SELECT count(*) FROM offers").fetchone()[0], 959)
            db.close()

    def test_completed_cycle_skips_source_and_checkpoint_on_following_day(self):
        from daily import load_config
        from flight_plan import build_jobs
        from flight_store import connect
        from test_cycle import fill_queries

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.sqlite3"
            db = connect(original)
            jobs = build_jobs(load_config(), date(2026, 10, 17))
            fill_queries(db, jobs, date(2026, 10, 17))
            db.close()
            vault = Mock()

            def restore(destination):
                shutil.copyfile(original, destination)
                return {"offer_count": 960, "asset_id": 7}

            vault.restore.side_effect = restore
            with patch("cloud_runner.GitHubVault", return_value=vault), patch(
                "cloud_runner.collect_process"
            ) as source, patch("daily.today", return_value=date(2026, 10, 18)), patch(
                "cloud_runner.audit_exports", return_value={"audit_passed": True}
            ), patch.dict(
                os.environ,
                {
                    "GITHUB_OUTPUT": str(root / "outputs"),
                    "GITHUB_STEP_SUMMARY": str(root / "summary"),
                },
            ), contextlib.redirect_stdout(
                io.StringIO()
            ):
                self.assertEqual(run(240, check_only=True), 0)
                source.assert_not_called()
                vault.publish.assert_not_called()
            self.assertEqual((root / "outputs").read_text(), "needs_collection=false\n")

    def test_preflight_reports_missing_day_without_source_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.sqlite3"
            daily_database(original).close()
            vault = Mock()

            def restore(destination):
                shutil.copyfile(original, destination)
                return {"offer_count": 2, "asset_id": 7}

            vault.restore.side_effect = restore
            with patch("cloud_runner.GitHubVault", return_value=vault), patch(
                "cloud_runner.collect_process"
            ) as source, patch(
                "daily.today", return_value=date(2026, 10, 17)
            ), patch.dict(
                os.environ,
                {
                    "GITHUB_OUTPUT": str(root / "outputs"),
                    "GITHUB_STEP_SUMMARY": str(root / "summary"),
                },
            ), contextlib.redirect_stdout(
                io.StringIO()
            ):
                self.assertEqual(run(240, check_only=True), 0)
                source.assert_not_called()
                vault.publish.assert_not_called()
            self.assertEqual((root / "outputs").read_text(), "needs_collection=true\n")

    def test_partial_batch_checkpoints_without_claiming_full_cycle(self):
        from daily import load_config
        from flight_plan import build_jobs
        from flight_store import connect
        from test_cycle import fill_queries

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.sqlite3"
            daily_database(original).close()
            vault = Mock()

            def restore(destination):
                shutil.copyfile(original, destination)
                return {"offer_count": 2, "asset_id": 7}

            vault.restore.side_effect = restore
            vault.publish.return_value = {"offer_count": 3}

            def collect(limit, data, log):
                db = connect(data / "flights.sqlite3")
                fill_queries(
                    db,
                    build_jobs(load_config(), date(2026, 10, 17))[:1],
                    date(2026, 10, 17),
                )
                db.close()
                return 0

            with patch("cloud_runner.GitHubVault", return_value=vault), patch(
                "cloud_runner.collect_process", side_effect=collect
            ), patch("daily.today", return_value=date(2026, 10, 17)), patch(
                "cloud_runner.audit_exports", return_value={"audit_passed": True}
            ), patch.dict(
                os.environ, {"GITHUB_STEP_SUMMARY": str(root / "summary")}
            ), contextlib.redirect_stdout(
                io.StringIO()
            ) as captured:
                self.assertEqual(run(240), 0)
            summary = json.loads(captured.getvalue())
            self.assertEqual(summary["covered_query_count"], 1)
            self.assertEqual(summary["planned_query_count"], 960)
            self.assertFalse(summary["coverage_complete"])
            self.assertTrue(summary["checkpoint_saved"])
            self.assertEqual(summary["added_observation_count"], 1)
            vault.publish.assert_called_once()

    def test_stored_block_preflight_skips_without_requests_or_false_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.sqlite3"
            db = daily_database(original)
            db.execute("INSERT INTO control VALUES('blocked','private source details')")
            db.commit()
            db.close()
            vault = Mock()

            def restore(destination):
                shutil.copyfile(original, destination)
                return {"offer_count": 2, "asset_id": 7}

            vault.restore.side_effect = restore
            with patch("cloud_runner.GitHubVault", return_value=vault), patch(
                "cloud_runner.collect_process"
            ) as source, patch("daily.today", return_value=date(2026, 10, 23)), patch.dict(
                os.environ, {"GITHUB_OUTPUT": str(root / "outputs")}
            ), contextlib.redirect_stdout(io.StringIO()) as captured:
                self.assertEqual(run(240, check_only=True, cycle_day=date(2026, 10, 20)), 0)
            summary = json.loads(captured.getvalue())
            self.assertEqual(summary["reason"], "collection_blocked")
            self.assertTrue(summary["collection_blocked"])
            self.assertFalse(summary["coverage_complete"])
            self.assertFalse(summary["needs_collection"])
            self.assertEqual(summary["source_requests_sent"], 0)
            self.assertEqual(summary["cycle_start_date"], "2026-10-23")
            self.assertEqual(summary["total_observation_count"], 2)
            self.assertNotIn("private source details", captured.getvalue())
            self.assertEqual((root / "outputs").read_text(), "needs_collection=false\n")
            source.assert_not_called()
            vault.publish.assert_not_called()
            from flight_store import connect, get_control
            db = connect(original)
            self.assertEqual(get_control(db, "blocked"), "private source details")
            db.close()

    def test_stored_block_still_refuses_direct_collection(self):
        from vault import VaultError

        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original.sqlite3"
            db = daily_database(original)
            db.execute("INSERT INTO control VALUES('blocked','private source details')")
            db.commit()
            db.close()
            vault = Mock()
            def restore(destination):
                shutil.copyfile(original, destination)
                return {"offer_count": 2, "asset_id": 7}

            vault.restore.side_effect = restore
            with patch("cloud_runner.GitHubVault", return_value=vault), patch(
                "cloud_runner.collect_process"
            ) as source, self.assertRaises(VaultError) as error:
                run(240)
            self.assertNotIn("private source details", str(error.exception))
            source.assert_not_called()
            vault.publish.assert_not_called()

    def test_source_collector_does_not_receive_repository_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            process = Mock()
            process.wait.return_value = 0
            with patch.dict(
                os.environ,
                {
                    "DATA_VAULT_TOKEN": "unit-test-vault-placeholder",
                    "GITHUB_TOKEN": "unit-test-checkout-placeholder",
                },
            ), patch("cloud_runner.subprocess.Popen", return_value=process) as start:
                self.assertEqual(collect_process(1, data, data / "collection.log"), 0)
                child = start.call_args.kwargs["env"]
            self.assertNotIn("DATA_VAULT_TOKEN", child)
            self.assertNotIn("GITHUB_TOKEN", child)
            self.assertEqual(child["ROTELIO_DATA_DIR"], str(data))
            self.assertEqual(child["FLIGHT_BROWSER_CHANNEL"], "chromium")


if __name__ == "__main__":
    unittest.main()
