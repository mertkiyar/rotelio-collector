"""Thirty consecutive departure dates, refreshed in fixed three-day cycles."""

from datetime import date, timedelta


def cycle_start(cfg, day):
    anchor = date.fromisoformat(cfg["refresh_anchor_date"])
    elapsed = (day - anchor).days
    if elapsed < 0:
        return None
    return anchor + timedelta(
        days=elapsed // cfg["refresh_interval_days"] * cfg["refresh_interval_days"]
    )


def travel_dates(cfg, day):
    start = cycle_start(cfg, day)
    return (
        []
        if start is None
        else [start + timedelta(days=n) for n in range(1, cfg["horizon_days"] + 1)]
    )


def expected_query_count(cfg):
    return 2 * len(cfg["origin_ids"]) * len(cfg["destinations"]) * cfg["horizon_days"]


def build_jobs(cfg, day):
    from flight_schema import query_id

    jobs = []
    for travel in travel_dates(cfg, day):
        for direction in ("outbound", "inbound"):
            for destination in cfg["destinations"]:
                for home in cfg["origin_ids"]:
                    origin, dest = (
                        (home, destination["search_id"])
                        if direction == "outbound"
                        else (destination["search_id"], home)
                    )
                    if set(cfg["points"][origin]["airports"]) & set(
                        cfg["points"][dest]["airports"]
                    ):
                        raise ValueError("Overlapping pilot airports")
                    jobs.append(
                        dict(
                            collection_date=day.isoformat(),
                            query_id=query_id(origin, dest, travel.isoformat()),
                            region_id=destination["region_id"],
                            region_name=destination["name"],
                            trip_direction=direction,
                            search_origin_id=origin,
                            search_destination_id=dest,
                            travel_date=travel.isoformat(),
                            source_url=f"https://www.obilet.com/ucuslar/{origin}-{dest}/{travel:%Y%m%d}/1a/economy/all",
                            priority=len(jobs),
                        )
                    )
    if jobs and (
        len(jobs) != expected_query_count(cfg)
        or len({j["query_id"] for j in jobs}) != len(jobs)
    ):
        raise ValueError("Refresh plan contains missing or duplicate queries")
    return jobs


def cycle_state(db, cfg, day, plan_day=None):
    start = cycle_start(cfg, plan_day or day)
    jobs = build_jobs(cfg, plan_day or day)
    expected = {j["query_id"] for j in jobs}
    if start is None:
        return dict(
            cycle_start_date=None,
            next_cycle_date=cfg["refresh_anchor_date"],
            query_count=0,
            covered_query_count=0,
            coverage_complete=False,
            offer_count=0,
            daily_observation_count=db.execute(
                "SELECT count(*) FROM offers WHERE collection_date=?",
                (day.isoformat(),),
            ).fetchone()[0],
            runnable_query_count=0,
            cycle_attempt_count=0,
            expired_query_count=0,
            observation_dates=[],
            covered_ids=set(),
            runnable_ids=set(),
        )
    end = min(day, start + timedelta(days=cfg["refresh_interval_days"] - 1))
    bounds = (start.isoformat(), end.isoformat())
    covered = (
        {
            r[0]
            for r in db.execute(
                """SELECT DISTINCT q.query_id FROM queries q WHERE q.collection_date BETWEEN ? AND ?
        AND q.query_state IN ('ok','partial') AND EXISTS(SELECT 1 FROM offers o
        WHERE o.collection_date=q.collection_date AND o.query_id=q.query_id
        AND json_extract(o.payload,'$.analysis_eligible')=1)""",
                bounds,
            )
        }
        & expected
    )
    current_states = dict(
        db.execute(
            "SELECT query_id,query_state FROM queries WHERE collection_date=?",
            (day.isoformat(),),
        )
    )
    runnable = {
        j["query_id"]
        for j in jobs
        if j["query_id"] not in covered
        and j["travel_date"] >= day.isoformat()
        and current_states.get(j["query_id"], "pending") in ("pending", "running")
    }
    attempts = sum(
        r[1]
        for r in db.execute(
            "SELECT query_id,attempt_count FROM queries WHERE collection_date BETWEEN ? AND ?",
            bounds,
        )
        if r[0] in expected
    )
    daily_attempts = db.execute(
        "SELECT coalesce(sum(attempt_count),0) FROM queries WHERE collection_date=?",
        (day.isoformat(),),
    ).fetchone()[0]
    remaining = max(
        0,
        min(
            cfg["max_queries_per_cycle"] - attempts,
            cfg["max_queries_per_day"] - daily_attempts,
        ),
    )
    counts = [
        (r[0], r[1])
        for r in db.execute(
            "SELECT collection_date,query_id FROM offers WHERE collection_date BETWEEN ? AND ?",
            bounds,
        )
        if r[1] in expected
    ]
    return dict(
        cycle_start_date=start.isoformat(),
        next_cycle_date=(
            start + timedelta(days=cfg["refresh_interval_days"])
        ).isoformat(),
        travel_date_start=(start + timedelta(days=1)).isoformat(),
        travel_date_end=(start + timedelta(days=cfg["horizon_days"])).isoformat(),
        query_count=len(expected),
        covered_query_count=len(covered),
        coverage_complete=bool(expected) and covered == expected,
        offer_count=len(counts),
        daily_observation_count=db.execute(
            "SELECT count(*) FROM offers WHERE collection_date=?", (day.isoformat(),)
        ).fetchone()[0],
        runnable_query_count=min(len(runnable), remaining),
        remaining_attempt_budget=remaining,
        cycle_attempt_count=attempts,
        expired_query_count=sum(
            j["query_id"] not in covered and j["travel_date"] < day.isoformat()
            for j in jobs
        ),
        observation_dates=sorted({r[0] for r in counts}),
        covered_ids=covered,
        runnable_ids=runnable,
    )


def public_cycle_state(state):
    return {k: v for k, v in state.items() if k not in ("covered_ids", "runnable_ids")}
