"""Explicit review of the legacy supplier-loading stop; never access blocks."""

import json
from flight_plan import cycle_start
from flight_store import event, get_control, now, save_query, set_control


def review_loading_stop(db, cfg, day, query_id, apply=False):
    expected = "RuntimeError: results_load_unverified"
    start = cycle_start(cfg, day)
    if start is None or get_control(db, "blocked") != expected:
        raise ValueError("Saved stop is not the reviewed legacy loading timeout")
    row = db.execute(
        "SELECT * FROM queries WHERE query_id=? AND collection_date BETWEEN ? AND ? "
        "ORDER BY collection_date DESC LIMIT 1",
        (query_id, start.isoformat(), day.isoformat()),
    ).fetchone()
    if row is None:
        raise ValueError("Reviewed query is not in the active cycle")
    job = json.loads(row["payload"])
    attempts = db.execute(
        "SELECT sum(attempt_count) FROM queries WHERE query_id=? "
        "AND collection_date BETWEEN ? AND ?",
        (query_id, start.isoformat(), day.isoformat()),
    ).fetchone()[0]
    if (
        row["query_state"] != "error"
        or job.get("error_code") != "RuntimeError"
        or job.get("error_message") != expected
        or job.get("travel_date", "") < day.isoformat()
        or row["offer_count"] != 0
        or attempts != 1
    ):
        raise ValueError("Query does not match the reviewed, unpriced first attempt")
    # Review does not claim coverage, remove observations or reset attempt counts.
    if apply:
        with db:
            save_query(db, job, "load_timeout", retry_after=now())
            set_control(db, "blocked", None)
            event(db, "loading_stop_reviewed", query_id + ": explicitly authorized retry")
    return True
