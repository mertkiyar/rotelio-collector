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

from cloud_runner import ROOT, collect_process, plan_covered, run

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
    def test_complete_plan_requires_every_expected_query_and_eligible_offer(self):
        with tempfile.TemporaryDirectory() as directory:
            db = daily_database(Path(directory) / "pool.sqlite3")
            jobs = [{"query_id": "first"}, {"query_id": "second"}]
            self.assertTrue(plan_covered(db, "2026-10-16", jobs))
            self.assertFalse(plan_covered(db, "2026-10-17", jobs))
            self.assertFalse(
                plan_covered(db, "2026-10-16", jobs + [{"query_id": "third"}])
            )
            self.assertFalse(plan_covered(db, "2026-10-16", [{"query_id": "first"}]))
            db.execute(
                "UPDATE queries SET query_state='running' WHERE query_id='second'"
            )
            self.assertFalse(plan_covered(db, "2026-10-16", jobs))
            db.execute("UPDATE queries SET query_state='partial'")
            db.execute(
                "UPDATE offers SET payload='{"
                + '"analysis_eligible":false'
                + "}' WHERE query_id='second'"
            )
            self.assertFalse(plan_covered(db, "2026-10-16", jobs))
            db.close()

    def test_completed_day_skips_source_and_checkpoint_in_check_and_run(self):
        for check_only in (True, False):
            with self.subTest(
                check_only=check_only
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                original = root / "original.sqlite3"
                daily_database(original).close()
                vault = Mock()

                def restore(destination):
                    shutil.copyfile(original, destination)
                    return {"offer_count": 2, "asset_id": 7}

                vault.restore.side_effect = restore
                with contextlib.ExitStack() as stack:
                    stack.enter_context(
                        patch("cloud_runner.GitHubVault", return_value=vault)
                    )
                    source = stack.enter_context(patch("cloud_runner.collect_process"))
                    stack.enter_context(
                        patch("daily.today", return_value=date(2026, 10, 16))
                    )
                    stack.enter_context(
                        patch(
                            "daily.build_jobs",
                            return_value=[
                                {"query_id": "first"},
                                {"query_id": "second"},
                            ],
                        )
                    )
                    stack.enter_context(
                        patch("flight_store.export", return_value={"offer_count": 2})
                    )
                    stack.enter_context(
                        patch("audit.audit", return_value={"audit_passed": True})
                    )
                    stack.enter_context(
                        patch.dict(
                            os.environ,
                            {
                                "GITHUB_OUTPUT": str(root / "outputs"),
                                "GITHUB_STEP_SUMMARY": str(root / "summary"),
                            },
                        )
                    )
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    self.assertEqual(run(224, check_only=check_only), 0)
                    source.assert_not_called()
                    vault.publish.assert_not_called()
                self.assertEqual(
                    (root / "outputs").read_text(), "needs_collection=false\n"
                )
                self.assertIn(
                    '"source_requests_sent": 0', (root / "summary").read_text()
                )

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
                self.assertEqual(run(224, check_only=True), 0)
                source.assert_not_called()
                vault.publish.assert_not_called()
            self.assertEqual((root / "outputs").read_text(), "needs_collection=true\n")

    def test_stored_block_stops_preflight_without_exposing_reason(self):
        from vault import VaultError

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
            ) as source, self.assertRaises(VaultError) as error:
                run(224, check_only=True)
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
