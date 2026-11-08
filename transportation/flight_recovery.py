"""Explicit review of loading, navigation and same-day date stops; never access or fare blocks."""

import json
from flight_plan import cycle_start
from flight_store import event, get_control, now, save_query, set_control

LEGACY_LOADING_STOP = "RuntimeError: results_load_unverified"
NAVIGATION_STOP_PREFIX = "TimeoutError: Page.goto: Timeout "
CONSECUTIVE_TIMEOUT_STOP = "Consecutive supplier loading timeouts require review"
SAME_DAY_DEPARTURE_STOP = "ValueError: future_travel_date_required"


def review_loading_stop(db, cfg, day, query_id, apply=False):
    stop = get_control(db, "blocked")
    if stop == LEGACY_LOADING_STOP:
        return _review_first_attempt(db, cfg, day, query_id, stop, "RuntimeError", apply)
    if isinstance(stop, str) and stop.startswith(NAVIGATION_STOP_PREFIX):
        return _review_navigation_stop(db, cfg, day, query_id, stop, apply)
    if stop == CONSECUTIVE_TIMEOUT_STOP:
        return _review_consecutive_timeouts(db, cfg, day, query_id, apply)
    if stop == SAME_DAY_DEPARTURE_STOP:
        return _review_same_day_departure(db, query_id, stop, apply)
    raise ValueError("Saved stop is not a reviewable loading, navigation or same-day date stop")


def _latest(db, query_id, start=None, day=None):
    if start is None:
        return db.execute(
            "SELECT * FROM queries WHERE query_id=? ORDER BY collection_date DESC LIMIT 1",
            (query_id,),
        ).fetchone()
    return db.execute(
        "SELECT * FROM queries WHERE query_id=? AND collection_date BETWEEN ? AND ? "
        "ORDER BY collection_date DESC LIMIT 1",
        (query_id, start.isoformat(), day.isoformat()),
    ).fetchone()


def _attempts(db, query_id, start, day):
    return db.execute(
        "SELECT sum(attempt_count) FROM queries WHERE query_id=? "
        "AND collection_date BETWEEN ? AND ?",
        (query_id, start.isoformat(), day.isoformat()),
    ).fetchone()[0]


def _review_first_attempt(db, cfg, day, query_id, stop, error_code, apply):
    start = cycle_start(cfg, day)
    if start is None:
        raise ValueError("No active cycle to retry the reviewed query in")
    row = _latest(db, query_id, start, day)
    if row is None:
        raise ValueError("Reviewed query is not in the active cycle")
    job = json.loads(row["payload"])
    if (
        row["query_state"] != "error"
        or job.get("error_code") != error_code
        or job.get("error_message") != stop
        or job.get("travel_date", "") < day.isoformat()
        or row["offer_count"] != 0
        or _attempts(db, query_id, start, day) != 1
    ):
        raise ValueError("Query does not match the reviewed, unpriced first attempt")
    # Review does not claim coverage, remove observations or reset attempt counts.
    if apply:
        with db:
            save_query(db, job, "load_timeout", retry_after=now())
            set_control(db, "blocked", None)
            event(db, "loading_stop_reviewed", query_id + ": explicitly authorized retry")
    return True


def _review_navigation_stop(db, cfg, day, query_id, stop, apply):
    row = _latest(db, query_id)
    if row is None:
        raise ValueError("Reviewed query is not in the private pool")
    job = json.loads(row["payload"])
    if (
        row["query_state"] != "error"
        or job.get("error_code") != "TimeoutError"
        or job.get("error_message") != stop
        or row["offer_count"] != 0
    ):
        raise ValueError("Query does not match the saved, unpriced navigation timeout")
    start = cycle_start(cfg, day)
    retryable = (
        start is not None
        and row["collection_date"] >= start.isoformat()
        and job.get("travel_date", "") >= day.isoformat()
        and _attempts(db, query_id, start, day) == 1
    )
    if retryable:
        return _review_first_attempt(db, cfg, day, query_id, stop, "TimeoutError", apply)
    # A departed date or an earlier cycle cannot be observed again. The query stays
    # an unpriced error, so it never counts as coverage or becomes a zero fare.
    if apply:
        with db:
            set_control(db, "blocked", None)
            event(db, "navigation_stop_reviewed", query_id + ": stop cleared; query left unpriced")
    return True


def _review_consecutive_timeouts(db, cfg, day, query_id, apply):
    start = cycle_start(cfg, day)
    if start is None:
        raise ValueError("No active cycle for the reviewed timeouts")
    row = _latest(db, query_id, start, day)
    if row is None or row["query_state"] != "load_timeout" or row["offer_count"] != 0:
        raise ValueError("Reviewed query is not a deferred timeout in the active cycle")
    # The deferred queries keep their own cooldown and one-retry limit.
    if apply:
        with db:
            set_control(db, "blocked", None)
            event(db, "timeout_stop_reviewed", query_id + ": consecutive timeout stop cleared")
    return True


def _review_same_day_departure(db, query_id, stop, apply):
    row = _latest(db, query_id)
    if row is None:
        raise ValueError("Reviewed query is not in the private pool")
    job = json.loads(row["payload"])
    if (
        row["query_state"] != "error"
        or job.get("error_code") != "ValueError"
        or job.get("error_message") != stop
        or row["offer_count"] != 0
        or job.get("travel_date", "9999-12-31") > row["collection_date"]
    ):
        raise ValueError("Query does not match the saved, unpriced same-day departure")
    # The departure was not a future date when searched, so it is never retried and
    # stays an unpriced error rather than coverage or a zero fare.
    if apply:
        with db:
            set_control(db, "blocked", None)
            event(db, "date_stop_reviewed", query_id + ": stop cleared; query left unpriced")
    return True
