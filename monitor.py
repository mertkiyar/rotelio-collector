"""Check daily collection health and maintain a deduplicated GitHub alert."""

import argparse
import contextlib
import io
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from cloud_runner import run

MARKER = "<!-- rotelio-collection-health:v1 -->"
TITLE = "Flight collection needs attention"


def today():
    return datetime.now(ZoneInfo("Europe/Istanbul")).date().isoformat()


def check_pool(cycle_day=None):
    """Use the same private restore, exact-plan check and audit as collection."""
    try:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            result = run(240, check_only=True, cycle_day=cycle_day)
        status = json.loads(captured.getvalue())
        healthy = (
            result == 0
            and (
                status.get("coverage_complete") is True
                or status.get("reason") == "cycle_not_started"
            )
            and status.get("audit_passed") is True
        )
        return {
            "healthy": healthy,
            "kind": "daily_complete" if healthy else "daily_incomplete",
            "collection_date": status.get("cycle_start_date")
            or status["collection_date"],
            "checked_on": status["collection_date"],
            "cycle_start_date": status.get("cycle_start_date"),
            "cycle_observation_count": status.get(
                "cycle_observation_count", status.get("daily_observation_count", 0)
            ),
            "covered_query_count": status.get("covered_query_count", 0),
            "planned_query_count": status["planned_query_count"],
        }
    except Exception:
        # Source errors, signed URLs and token details must not enter an issue.
        return {
            "healthy": False,
            "kind": "health_check_failed",
            "collection_date": today(),
        }


class IssueAlerts:
    def __init__(self):
        self.repository = os.environ.get("GITHUB_REPOSITORY", "")
        self.recipient = os.environ.get("ALERT_RECIPIENT", "")
        self.token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise RuntimeError("GITHUB_REPOSITORY must be owner/repository")
        if not re.fullmatch(r"[A-Za-z0-9-]+", self.recipient):
            raise RuntimeError("ALERT_RECIPIENT must be a GitHub username")
        if not self.token or any(ord(c) < 33 or ord(c) > 126 for c in self.token):
            raise RuntimeError("GitHub notification token is not configured correctly")
        self.base = "/repos/" + self.repository

    def request(self, method, path, payload=None):
        request = urllib.request.Request(
            "https://api.github.com" + path,
            method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Rotelio-health-monitor",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                "Notification API failed: HTTP " + str(error.code)
            ) from None
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError("Notification API connection failed") from None

    def open_alert(self):
        page = 1
        while True:
            issues = self.request(
                "GET", self.base + f"/issues?state=open&per_page=100&page={page}"
            )
            for issue in issues:
                if (
                    "pull_request" not in issue
                    and issue.get("user", {}).get("login") == "github-actions[bot]"
                    and MARKER in (issue.get("body") or "")
                ):
                    return issue
            if len(issues) < 100:
                return None
            page += 1

    def body(self, report, run_url):
        kind = report["kind"]
        messages = {
            "workflow_failed": "The flight collection workflow did not finish successfully.",
            "daily_incomplete": "The saved refresh cycle does not cover all 960 planned queries.",
            "health_check_failed": "The private checkpoint, access or data audit could not be verified.",
            "notification_test": "Notification delivery test. No collection failure is being reported.",
        }
        body = (
            MARKER
            + "\n"
            + f"<!-- alert-key:{report['collection_date']}:{kind} -->\n\n"
            + f"@{self.recipient}\n\n"
            + messages[kind]
            + "\n\n"
            + f"Refresh cycle starting (Turkey): **{report['collection_date']}**.\n\n"
        )
        if "covered_query_count" in report:
            body += (
                f"Covered queries: **{report['covered_query_count']} / {report['planned_query_count']}**. "
                + f"Saved cycle observations: **{report['cycle_observation_count']}**.\n\n"
            )
        body += f"[Open workflow run]({run_url})\n\n"
        if kind != "notification_test":
            body += (
                "Review the run summary and private diagnostics. Existing observations remain stored. "
                "No automatic source-block reset or access-control bypass is performed.\n"
            )
        return body

    def sync(self, report, run_url):
        existing = self.open_alert()
        if report["healthy"]:
            if existing:
                path = self.base + "/issues/" + str(existing["number"])
                self.request(
                    "POST",
                    path + "/comments",
                    {
                        "body": "Refresh-cycle coverage and the private-pool audit passed. "
                        + f"[Verification run]({run_url}). This alert is resolved."
                    },
                )
                self.request(
                    "PATCH", path, {"state": "closed", "state_reason": "completed"}
                )
                return "resolved"
            return "healthy"
        body = self.body(report, run_url)
        if existing:
            key = f"<!-- alert-key:{report['collection_date']}:{report['kind']} -->"
            if key in existing["body"]:
                return "already_notified"
            path = self.base + "/issues/" + str(existing["number"])
            self.request("POST", path + "/comments", {"body": body})
            self.request("PATCH", path, {"body": body})
            return "updated"
        self.request(
            "POST",
            self.base + "/issues",
            {"title": TITLE, "body": body, "assignees": [self.recipient]},
        )
        return "created"

    def test_notification(self, run_url):
        # Keep the test separate so it cannot close or replace a genuine alert.
        report = {"kind": "notification_test", "collection_date": today()}
        issue = self.request(
            "POST",
            self.base + "/issues",
            {
                "title": "Collection notification test",
                "body": self.body(report, run_url).replace(
                    MARKER, "<!-- rotelio-notification-test:v1 -->"
                ),
                "assignees": [self.recipient],
            },
        )
        self.request(
            "PATCH",
            self.base + "/issues/" + str(issue["number"]),
            {"state": "closed", "state_reason": "completed"},
        )
        return "test_sent"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-notification", action="store_true")
    args = parser.parse_args()
    alerts = IssueAlerts()
    run_url = f"https://github.com/{alerts.repository}/actions/runs/{int(os.environ['GITHUB_RUN_ID'])}"
    if args.test_notification:
        print(json.dumps({"notification": alerts.test_notification(run_url)}))
        return 0
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event = json.loads(Path(event_path).read_text()) if event_path else {}
    failed_run = event.get("workflow_run", {})
    if (
        os.environ.get("GITHUB_EVENT_NAME") == "workflow_run"
        and failed_run.get("conclusion") != "success"
    ):
        report = {
            "healthy": False,
            "kind": "workflow_failed",
            "collection_date": today(),
        }
        run_url = f"https://github.com/{alerts.repository}/actions/runs/{int(failed_run['id'])}"
    else:
        now = datetime.now(ZoneInfo("Europe/Istanbul"))
        reference = (
            now.date() - timedelta(days=1)
            if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and now.hour < 9
            else None
        )
        report = check_pool(cycle_day=reference)
        if (
            os.environ.get("GITHUB_EVENT_NAME") == "workflow_run"
            and not report["healthy"]
        ):
            # A successful small manual batch may deliberately leave a partial day.
            print(json.dumps({"notification": "waiting_for_daily_deadline", **report}))
            return 0
    result = alerts.sync(report, run_url)
    print(json.dumps({"notification": result, **report}))
    return 0 if report["healthy"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("Collection monitor failed: " + type(error).__name__)
        raise SystemExit(2)
