"""Collect and validate daily flight offers for the Rotelio pilot."""

import argparse
import fcntl
import json
import os
import shutil
import signal
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from flight_schema import TZ, FIELDS, VERSION, normalize, query_id
from flight_store import (
    ROOT,
    DATA,
    connect,
    event,
    export,
    get_control,
    set_control,
    save_query,
    now,
    manifest,
)


class AccessBlocked(RuntimeError):
    pass


class UnverifiedEmpty(RuntimeError):
    pass


def today():
    return datetime.now(TZ).date()


def load_config():
    cfg = json.loads((ROOT / "pilot_config.json").read_text(encoding="utf-8"))
    if len(cfg["origin_ids"]) != 2 or len(cfg["destinations"]) != 8:
        raise ValueError("Expected approved 2-origin / 8-destination pilot")
    if len(cfg["cohort_offsets"]) != 7 or len(set(cfg["cohort_offsets"])) != 7:
        raise ValueError("Seven unique cohort dates required")
    for k, p in cfg["points"].items():
        if not p.get("airports") or not p.get("name"):
            raise ValueError("Invalid point " + k)
    return cfg


def cohort_dates(cfg, day):
    anchor = date.fromisoformat(cfg["cohort_anchor_date"])
    if day < anchor:
        raise ValueError("Cannot backfill observations before the pilot start date")
    values = [anchor + timedelta(days=n) for n in cfg["cohort_offsets"]]
    # Simulate replacements from the fixed anchor, so newly added dates also
    # remain the same on later collection days rather than sliding every day.
    cursor = anchor
    while cursor <= day:
        values = [
            d
            for d in values
            if cursor < d <= cursor + timedelta(days=cfg["horizon_days"] - 1)
        ]
        while len(values) < 7:
            candidate = cursor + timedelta(days=cfg["horizon_days"] - 1)
            while candidate in values:
                candidate -= timedelta(days=1)
            values.append(candidate)
        cursor += timedelta(days=1)
    return sorted(values)


def build_jobs(cfg, day):
    jobs = []
    for travel in cohort_dates(cfg, day):
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
                    key = query_id(origin, dest, travel.isoformat())
                    jobs.append(
                        dict(
                            collection_date=day.isoformat(),
                            query_id=key,
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
    if len(jobs) != 224 or len({j["query_id"] for j in jobs}) != 224:
        raise ValueError("Pilot plan must have 224 unique queries")
    return jobs


def ensure_plan(db, cfg, day):
    jobs = build_jobs(cfg, day)
    with db:
        db.execute(
            "UPDATE queries SET query_state='missed' WHERE collection_date<? AND query_state IN ('pending','running')",
            (day.isoformat(),),
        )
        for j in jobs:
            db.execute(
                "INSERT OR IGNORE INTO queries(collection_date,query_id,region_id,priority,payload) VALUES(?,?,?,?,?)",
                (
                    j["collection_date"],
                    j["query_id"],
                    j["region_id"],
                    j["priority"],
                    json.dumps(j, ensure_ascii=False),
                ),
            )
    return jobs


def choose_node():
    configured = os.environ.get("PLAYWRIGHT_NODEJS_PATH")
    if configured and not Path(configured).is_file():
        raise RuntimeError("PLAYWRIGHT_NODEJS_PATH does not point to a file")
    node = configured or shutil.which("node")
    if node:
        os.environ["PLAYWRIGHT_NODEJS_PATH"] = node
    return node or "bundled"


def browser_options(timeout):
    channel = os.environ.get("FLIGHT_BROWSER_CHANNEL", "chrome")
    if channel not in ("chrome", "chromium"):
        raise ValueError("FLIGHT_BROWSER_CHANNEL must be chrome or chromium")
    return {"channel": channel, "headless": True, "timeout": timeout}


def driver_start():
    choose_node()

    def expired(*_):
        raise TimeoutError("Local driver startup exceeded 30 seconds")

    old = signal.signal(signal.SIGALRM, expired)
    signal.alarm(30)
    try:
        from playwright.sync_api import sync_playwright

        return sync_playwright().start()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def validation_requires_stop(offers, rejected, raw_count):
    # Inconsistent connecting durations are quarantined, without stopping
    # the collection of structurally valid offers from other routes.
    structural = [
        r for r in rejected if r["reason"] != "arrival_clock_duration_mismatch"
    ]
    return not offers or len(structural) > max(5, raw_count // 5)


def capture(page, job, cfg):
    response = page.goto(
        job["source_url"], wait_until="domcontentloaded", timeout=35000
    )
    if response is None:
        raise RuntimeError("navigation_no_response")
    if response.status in (401, 403, 429):
        raise AccessBlocked("HTTP_" + str(response.status))
    if response.status >= 400:
        raise RuntimeError("HTTP_" + str(response.status))
    last = None
    stable = 0
    start = time.monotonic()
    last_signal = None
    while time.monotonic() - start < cfg["results_timeout_seconds"]:
        s = page.evaluate(
            """() => {
          const rows=Array.from(document.querySelectorAll('#outbound-journeys > [id^="journey-"]'))
            .filter(e=>/^journey-[A-Z]{3}-[A-Z]{3}-\\d{8}-/.test(e.id)&&e.getClientRects().length&&e.querySelector('.amount-integer'));
          return {ready:!!document.getElementById('departure-input')&&!document.getElementById('departure-input').disabled,
           blocked:/verify you are human|access denied|unusual traffic|erişiminiz engellendi|captcha doğrulama/i.test(document.body.innerText),
           count:rows.length,signature:rows.map(e=>e.id+':'+e.querySelector('.amount-integer').textContent).join('|')};}"""
        )
        last_signal = s
        if s["blocked"]:
            raise AccessBlocked("human_verification_or_access_denied")
        if s["ready"] and s["count"] and s["signature"] == last:
            stable += 1
        else:
            stable = 0
        if stable >= cfg["stable_result_seconds"]:
            break
        last = s["signature"]
        page.wait_for_timeout(1000)
    else:
        if last_signal and last_signal["ready"] and not last_signal["count"]:
            raise UnverifiedEmpty("No visible fare cards; not verified as zero flights")
        raise RuntimeError("results_load_unverified")
    button = page.locator("#currency-modal-btn")
    if button.inner_text().strip() != "TRY":
        button.click(timeout=8000)
        page.locator('a[data-action="set-currency"][data-code="TRY"]').first.click(
            timeout=12000
        )
        page.wait_for_function(
            "document.getElementById('currency-modal-btn')?.textContent.trim()==='TRY'",
            timeout=15000,
        )
        page.wait_for_timeout(3000)
    prior = None
    stable = 0
    for _ in range(12):
        page.mouse.wheel(0, 1800)
        page.wait_for_timeout(750)
        count = page.locator('#outbound-journeys > [id^="journey-"]').count()
        stable = stable + 1 if count == prior else 0
        if stable >= 2:
            break
        prior = count
    else:
        raise RuntimeError("card_list_not_stable")
    raw = page.evaluate(
        "(" + (ROOT / "extract_flights_visible.js").read_text(encoding="utf-8") + ")()"
    )
    raw["capture_method"] = "playwright_rendered_dom"
    raw["collection_meta"] = {
        "scope": "loaded_default_fare_cards",
        "hidden_fare_bundles_collected": False,
        "supplier_inventory_complete": False,
    }
    return raw


def collect(cfg, limit):
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / "collector.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Collector already running.", flush=True)
            return 4
        db = connect()
        day = today()
        ensure_plan(db, cfg, day)
        p = browser = context = None
        result = 0
        try:
            if get_control(db, "blocked"):
                print("Collection blocked:", get_control(db, "blocked"), flush=True)
                return 3
            with db:
                db.execute(
                    "UPDATE queries SET query_state='pending' WHERE collection_date=? AND query_state='running'",
                    (day.isoformat(),),
                )
            used = db.execute(
                "SELECT coalesce(sum(attempt_count),0) FROM queries WHERE collection_date=?",
                (day.isoformat(),),
            ).fetchone()[0]
            remaining = max(0, cfg["max_queries_per_day"] - used)
            pending = db.execute(
                "SELECT * FROM queries WHERE collection_date=? AND query_state='pending' ORDER BY priority LIMIT ?",
                (day.isoformat(), min(limit, remaining)),
            ).fetchall()
            if not pending:
                print(
                    json.dumps(manifest(db, day.isoformat()), ensure_ascii=False),
                    flush=True,
                )
                return 0
            event(
                db,
                "collection_started",
                f"{len(pending)} queries; node={choose_node()}",
            )
            print("Starting local browser; planned queries:", len(pending), flush=True)
            p = driver_start()
            browser = p.chromium.launch(**browser_options(25000))
            context = browser.new_context(locale="tr-TR", timezone_id="Europe/Istanbul")
            page = context.new_page()
            page.set_default_timeout(15000)
            for item in pending:
                if today() != day:
                    event(
                        db,
                        "day_rollover",
                        "Stopped before writing a new day into the earlier collection.",
                    )
                    break
                last = get_control(db, "last_query_at")
                if last:
                    wait = (
                        cfg["minimum_seconds_between_searches"]
                        - (
                            datetime.now(timezone.utc) - datetime.fromisoformat(last)
                        ).total_seconds()
                    )
                    if wait > 0:
                        time.sleep(wait)
                if today() != day:
                    break
                job = json.loads(item["payload"])
                job["attempt_count"] = item["attempt_count"] + 1
                with db:
                    db.execute(
                        "UPDATE queries SET query_state='running',attempt_count=attempt_count+1 WHERE collection_date=? AND query_id=?",
                        (day.isoformat(), job["query_id"]),
                    )
                set_control(db, "last_query_at", now())
                print(
                    job["region_id"],
                    job["trip_direction"],
                    job["search_origin_id"],
                    "->",
                    job["search_destination_id"],
                    job["travel_date"],
                    flush=True,
                )
                folder = DATA / "raw" / day.isoformat()
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / (
                    job["query_id"]
                    + "_"
                    + datetime.now(timezone.utc).strftime("%H%M%S%f")
                    + ".json"
                )
                try:
                    raw = capture(page, job, cfg)
                    path.write_text(
                        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    sid, offers, rejected = normalize(raw, job, cfg)
                    state = (
                        "ok"
                        if offers and not rejected
                        else "partial" if offers else "error"
                    )
                    save_query(
                        db,
                        job,
                        state,
                        offers,
                        rejected,
                        raw_path=str(path.relative_to(DATA)),
                        observed_at=raw["collected_at"],
                    )
                    event(
                        db,
                        "query_finished",
                        json.dumps(
                            {
                                "query_id": job["query_id"],
                                "region_id": job["region_id"],
                                "query_state": state,
                                "offer_count": len(offers),
                                "rejected_offer_count": len(rejected),
                            }
                        ),
                    )
                    print(
                        "Saved",
                        len(offers),
                        "offers;",
                        len(rejected),
                        "rejected",
                        flush=True,
                    )
                    # An isolated source inconsistency is quarantined. Widespread parse failure stops collection.
                    if validation_requires_stop(offers, rejected, len(raw["rows"])):
                        set_control(
                            db,
                            "blocked",
                            "Widespread fare validation failure: " + job["query_id"],
                        )
                        result = 2
                        break
                except AccessBlocked as error:
                    save_query(
                        db,
                        job,
                        "blocked",
                        error_code="source_access_blocked",
                        error_message=str(error),
                    )
                    set_control(db, "blocked", str(error))
                    event(db, "source_access_blocked", str(error))
                    result = 3
                    break
                except UnverifiedEmpty as error:
                    save_query(
                        db,
                        job,
                        "empty_unverified",
                        error_code="empty_unverified",
                        error_message=str(error),
                    )
                    event(db, "query_empty_unverified", job["query_id"])
                    print("Empty result not verified; no price written.", flush=True)
                except Exception as error:
                    message = type(error).__name__ + ": " + str(error)[:1500]
                    save_query(
                        db,
                        job,
                        "error",
                        error_code=type(error).__name__,
                        error_message=message,
                    )
                    event(db, "query_error", job["query_id"] + " " + message)
                    print(message, flush=True)
                    try:
                        page.screenshot(path=str(path.with_suffix(".png")))
                    except Exception:
                        pass
                    # Do not repeatedly hit a broken page/parser during one run.
                    set_control(db, "blocked", message)
                    result = 2
                    break
                finally:
                    export(db, day.isoformat(), full_history=False)
        except KeyboardInterrupt:
            result = 130
            event(
                db,
                "collection_interrupted",
                "Manual interruption; captured observations preserved.",
            )
        except Exception as error:
            message = type(error).__name__ + ": " + str(error)[:1500]
            event(db, "local_runtime_error", message)
            set_control(db, "blocked", message)
            print(message, flush=True)
            result = 2
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            if p:
                try:
                    p.stop()
                except Exception:
                    pass
            event(db, "collection_finished", "exit_code=" + str(result))
            m = export(db, day.isoformat())
            print(json.dumps(m, ensure_ascii=False), flush=True)
            db.close()
        return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sp = ap.add_subparsers(dest="command", required=True)
    cp = sp.add_parser("collect")
    cp.add_argument("--limit", type=int, default=32)
    cp.add_argument("--all", action="store_true")
    sp.add_parser("check")
    sp.add_parser("export")
    sp.add_parser("doctor")
    sp.add_parser(
        "continue", help="Manual only, after diagnosing the stored block reason."
    )
    args = ap.parse_args()
    cfg = load_config()
    if args.command == "doctor":
        p = driver_start()
        try:
            b = p.chromium.launch(**browser_options(20000))
            page = b.new_page()
            page.set_content("<title>Rotelio</title><p>ready</p>")
            assert page.locator("p").inner_text() == "ready"
            b.close()
            print("Local browser check passed.", flush=True)
        finally:
            p.stop()
        return 0
    if args.command == "collect":
        limit = cfg["max_queries_per_day"] if args.all else args.limit
        if not 1 <= limit <= cfg["max_queries_per_day"]:
            raise ValueError("Invalid query limit")
        return collect(cfg, limit)
    db = connect()
    try:
        ensure_plan(db, cfg, today())
        if args.command == "continue":
            set_control(db, "blocked", None)
            event(db, "manual_continue", "Manual continuation after diagnosis.")
        print(
            json.dumps(export(db, today().isoformat()), ensure_ascii=False, indent=2),
            flush=True,
        )
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as e:
        print(type(e).__name__ + ": " + str(e), file=sys.stderr)
        raise SystemExit(2)
