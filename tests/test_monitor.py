import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from monitor import IssueAlerts, MARKER, check_pool, main, scheduled_reference_day
from datetime import date, datetime
from zoneinfo import ZoneInfo


class MonitorTests(unittest.TestCase):
    def test_delayed_morning_check_keeps_previous_day_reference(self):
        now = datetime(2026, 10, 17, 12, 25, tzinfo=ZoneInfo("Europe/Istanbul"))
        self.assertEqual(scheduled_reference_day(now, "17 5 * * *"), date(2026, 10, 16))
        self.assertEqual(
            scheduled_reference_day(now.replace(day=20), "17 5 * * *"),
            date(2026, 10, 19),
        )

    def test_delayed_night_check_does_not_assess_new_cycle_before_deadline(self):
        now = datetime(2026, 10, 17, 2, 4, tzinfo=ZoneInfo("Europe/Istanbul"))
        self.assertEqual(
            scheduled_reference_day(now, "57 20 * * *"), date(2026, 10, 16)
        )
        self.assertEqual(
            scheduled_reference_day(now.replace(hour=23, minute=58), "57 20 * * *"),
            date(2026, 10, 17),
        )

    def test_manual_check_before_deadline_reviews_last_due_cycle(self):
        now = datetime(2026, 10, 17, 12, 25, tzinfo=ZoneInfo("Europe/Istanbul"))
        self.assertEqual(scheduled_reference_day(now), date(2026, 10, 16))

    def client(self, existing=None):
        with patch.dict(
            os.environ,
            {
                "GITHUB_REPOSITORY": "example/collector",
                "ALERT_RECIPIENT": "example",
                "GITHUB_TOKEN": "unit-test-placeholder",
            },
        ):
            client = IssueAlerts()
        client.request = Mock()
        client.open_alert = Mock(return_value=existing)
        return client

    def report(self, kind="daily_incomplete", day="2026-10-16"):
        return {
            "healthy": False,
            "kind": kind,
            "collection_date": day,
            "covered_query_count": 959,
            "planned_query_count": 960,
            "cycle_observation_count": 6000,
        }

    def test_missing_day_is_unhealthy_even_when_preflight_exits_successfully(self):
        with patch("monitor.run") as run:

            def preflight(*args, **kwargs):
                print(
                    json.dumps(
                        {
                            "collection_date": "2026-10-16",
                            "needs_collection": True,
                            "planned_query_count": 960,
                            "covered_query_count": 0,
                            "daily_observation_count": 0,
                        }
                    )
                )
                return 0

            run.side_effect = preflight
            report = check_pool()
        self.assertFalse(report["healthy"])
        self.assertEqual(report["kind"], "daily_incomplete")
        self.assertEqual(report["covered_query_count"], 0)

    def test_complete_day_requires_passing_audit(self):
        for audit in (True, False):
            with self.subTest(audit=audit), patch("monitor.run") as run:

                def preflight(*args, **kwargs):
                    print(
                        json.dumps(
                            {
                                "collection_date": "2026-10-16",
                                "coverage_complete": True,
                                "audit_passed": audit,
                                "planned_query_count": 960,
                                "covered_query_count": 960,
                                "daily_observation_count": 6321,
                            }
                        )
                    )
                    return 0

                run.side_effect = preflight
                self.assertEqual(check_pool()["healthy"], audit)

    def test_private_error_details_cannot_leak_into_alert(self):
        with patch("monitor.run", side_effect=RuntimeError("private URL and token")):
            report = check_pool()
        self.assertEqual(report["kind"], "health_check_failed")
        self.assertNotIn("private URL", json.dumps(report))
        body = self.client().body(
            report, "https://github.com/example/collector/actions/runs/1"
        )
        self.assertNotIn("private URL", body)

    def test_unhealthy_day_creates_assigned_mention_once(self):
        client = self.client()
        self.assertEqual(
            client.sync(
                self.report(), "https://github.com/example/collector/actions/runs/1"
            ),
            "created",
        )
        method, path, payload = client.request.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/repos/example/collector/issues")
        self.assertEqual(payload["assignees"], ["example"])
        self.assertIn("@example", payload["body"])
        self.assertIn("959 / 960", payload["body"])
        client.open_alert.return_value = {"number": 1, "body": payload["body"]}
        client.request.reset_mock()
        self.assertEqual(
            client.sync(
                self.report(), "https://github.com/example/collector/actions/runs/2"
            ),
            "already_notified",
        )
        client.request.assert_not_called()

    def test_new_day_renotifies_existing_issue_instead_of_opening_another(self):
        client = self.client(
            {
                "number": 1,
                "body": MARKER + "\n<!-- alert-key:2026-10-15:daily_incomplete -->",
            }
        )
        self.assertEqual(
            client.sync(
                self.report(), "https://github.com/example/collector/actions/runs/2"
            ),
            "updated",
        )
        self.assertEqual(
            client.request.call_args_list[0].args[:2],
            ("POST", "/repos/example/collector/issues/1/comments"),
        )
        self.assertIn("@example", client.request.call_args_list[0].args[2]["body"])
        self.assertEqual(
            client.request.call_args_list[1].args[:2],
            ("PATCH", "/repos/example/collector/issues/1"),
        )

    def test_healthy_day_resolves_existing_alert_without_creating_a_new_one(self):
        client = self.client({"number": 1, "body": MARKER})
        self.assertEqual(
            client.sync(
                {"healthy": True}, "https://github.com/example/collector/actions/runs/2"
            ),
            "resolved",
        )
        self.assertEqual(client.request.call_args.args[2]["state"], "closed")
        client = self.client()
        self.assertEqual(
            client.sync(
                {"healthy": True}, "https://github.com/example/collector/actions/runs/2"
            ),
            "healthy",
        )
        client.request.assert_not_called()

    def test_notification_test_cannot_replace_or_close_a_real_alert(self):
        client = self.client({"number": 1, "body": MARKER})
        client.request.return_value = {"number": 2}
        self.assertEqual(
            client.test_notification(
                "https://github.com/example/collector/actions/runs/2"
            ),
            "test_sent",
        )
        client.open_alert.assert_not_called()
        self.assertNotIn(MARKER, client.request.call_args_list[0].args[2]["body"])
        self.assertEqual(
            client.request.call_args_list[1].args[1],
            "/repos/example/collector/issues/2",
        )

    def test_only_bot_created_monitor_issues_are_managed(self):
        client = self.client()
        del client.open_alert
        client.request.return_value = [
            {"number": 1, "body": MARKER, "user": {"login": "example"}},
            {"number": 2, "body": MARKER, "user": {"login": "github-actions[bot]"}},
        ]
        self.assertEqual(client.open_alert()["number"], 2)

    def test_live_writer_defers_only_its_own_refresh_cycle(self):
        for created, expected in (
            ('2026-10-20T21:42:49Z', True),
            ('2026-10-22T22:00:00Z', False),
            ('2026-10-19T12:00:00Z', False),
        ):
            with self.subTest(created=created):
                client = self.client()
                client.request.side_effect = lambda method, path: {
                    'workflow_runs': (
                        [{'created_at': created}]
                        if 'status=in_progress&' in path else []
                    )
                }
                self.assertEqual(
                    client.collection_in_progress(self.report(day='2026-10-20')),
                    expected,
                )

    def test_no_live_writer_does_not_suppress_missing_coverage(self):
        client = self.client()
        client.request.return_value = {'workflow_runs': []}
        self.assertFalse(client.collection_in_progress(self.report()))

    def test_nightly_check_preserves_existing_alert_while_writer_is_running(self):
        client = Mock(repository='example/collector')
        client.collection_in_progress.return_value = True
        report = self.report()
        with patch('monitor.IssueAlerts', return_value=client), patch(
            'monitor.check_pool', return_value=report
        ), patch('sys.argv', ['monitor.py']), patch.dict(
            os.environ,
            {'GITHUB_EVENT_NAME': 'schedule', 'GITHUB_RUN_ID': '43', 'GITHUB_EVENT_PATH': ''},
        ), patch('builtins.print') as output:
            self.assertEqual(main(), 0)
        client.sync.assert_not_called()
        result = json.loads(output.call_args.args[0])
        self.assertEqual(result['notification'], 'collection_in_progress')
        self.assertFalse(result['healthy'])

    def test_failed_collection_notifies_without_needing_private_vault_access(self):
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            event.write_text(
                json.dumps({"workflow_run": {"id": 42, "conclusion": "failure"}})
            )
            client = Mock(repository="example/collector")
            client.sync.return_value = "created"
            with patch("monitor.IssueAlerts", return_value=client), patch(
                "monitor.check_pool"
            ) as check, patch("sys.argv", ["monitor.py"]), patch.dict(
                os.environ,
                {
                    "GITHUB_EVENT_NAME": "workflow_run",
                    "GITHUB_EVENT_PATH": str(event),
                    "GITHUB_RUN_ID": "43",
                },
            ), patch(
                "builtins.print"
            ):
                self.assertEqual(main(), 2)
            check.assert_not_called()
            report, link = client.sync.call_args.args
            self.assertEqual(report["kind"], "workflow_failed")
            self.assertTrue(link.endswith("/42"))

    def test_delayed_morning_event_does_not_alert_or_claim_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            event.write_text(json.dumps({"schedule": "17 5 * * *"}))
            client = Mock(repository="example/collector")
            late = datetime(2026, 10, 17, 12, 25, tzinfo=ZoneInfo("Europe/Istanbul"))
            with patch("monitor.IssueAlerts", return_value=client), patch(
                "monitor.datetime"
            ) as clock, patch(
                "monitor.check_pool",
                return_value={"healthy": True, "kind": "cycle_not_started"},
            ) as check, patch(
                "sys.argv", ["monitor.py"]
            ), patch.dict(
                os.environ,
                {
                    "GITHUB_EVENT_NAME": "schedule",
                    "GITHUB_EVENT_PATH": str(event),
                    "GITHUB_RUN_ID": "43",
                },
            ), patch(
                "builtins.print"
            ):
                clock.now.return_value = late
                self.assertEqual(main(), 0)
            check.assert_called_once_with(cycle_day=date(2026, 10, 16))
            client.sync.assert_not_called()

    def test_successful_partial_batch_waits_for_deadline_but_nightly_check_alerts(self):
        for event_name, conclusion, notified in (
            ("workflow_run", "success", False),
            ("schedule", None, True),
        ):
            with self.subTest(
                event=event_name
            ), tempfile.TemporaryDirectory() as directory:
                event = Path(directory) / "event.json"
                event.write_text(
                    json.dumps({"workflow_run": {"conclusion": conclusion}})
                )
                client = Mock(repository="example/collector")
                client.sync.return_value = "created"
                client.collection_in_progress.return_value = False
                with patch("monitor.IssueAlerts", return_value=client), patch(
                    "monitor.check_pool", return_value=self.report()
                ), patch("sys.argv", ["monitor.py"]), patch.dict(
                    os.environ,
                    {
                        "GITHUB_EVENT_NAME": event_name,
                        "GITHUB_EVENT_PATH": str(event),
                        "GITHUB_RUN_ID": "43",
                    },
                ), patch(
                    "builtins.print"
                ):
                    main()
                self.assertEqual(client.sync.called, notified)


if __name__ == "__main__":
    unittest.main()
