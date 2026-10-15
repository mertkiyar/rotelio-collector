"""Persistent flight-only storage and reproducible English-schema exports."""

import csv
import fcntl
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from flight_schema import FIELDS, VERSION

ROOT = Path(__file__).resolve().parent
DATA = (
    Path(os.environ["ROTELIO_DATA_DIR"]).expanduser().resolve()
    if os.environ.get("ROTELIO_DATA_DIR")
    else ROOT / "data"
)
QUERY_FIELDS = "collection_date query_id region_id region_name trip_direction search_origin_id search_destination_id travel_date source_url query_state attempt_count offer_count rejected_offer_count raw_path observed_at error_code error_message".split()
SUMMARY_FIELDS = "collection_date query_id region_id trip_direction search_origin_id search_destination_id travel_date observed_at lead_days eligible_offer_count minimum_price_minor minimum_price_amount median_price_minor currency".split()


def now():
    return datetime.now(timezone.utc).isoformat()


def connect(path=None):
    p = Path(path or DATA / "flights.sqlite3")
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(
        """CREATE TABLE IF NOT EXISTS queries(
      collection_date TEXT NOT NULL,query_id TEXT NOT NULL,region_id TEXT NOT NULL,priority INTEGER NOT NULL,
      query_state TEXT NOT NULL DEFAULT 'pending',attempt_count INTEGER NOT NULL DEFAULT 0,
      offer_count INTEGER NOT NULL DEFAULT 0,rejected_offer_count INTEGER NOT NULL DEFAULT 0,
      payload TEXT NOT NULL,PRIMARY KEY(collection_date,query_id));
      CREATE INDEX IF NOT EXISTS pending_queries ON queries(collection_date,query_state,priority);
      CREATE TABLE IF NOT EXISTS offers(observation_id TEXT PRIMARY KEY,snapshot_id TEXT NOT NULL,
      collection_date TEXT NOT NULL,query_id TEXT NOT NULL,payload TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS offers_day_query ON offers(collection_date,query_id);
      CREATE TABLE IF NOT EXISTS rejected(snapshot_id TEXT NOT NULL,source_offer_id TEXT,reason TEXT NOT NULL,payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,at TEXT NOT NULL,event_type TEXT NOT NULL,message TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS control(key TEXT PRIMARY KEY,value TEXT NOT NULL);"""
    )
    return db


def get_control(db, key):
    r = db.execute("SELECT value FROM control WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def set_control(db, key, value):
    with db:
        if value is None:
            db.execute("DELETE FROM control WHERE key=?", (key,))
        else:
            db.execute("INSERT OR REPLACE INTO control VALUES(?,?)", (key, value))


def event(db, kind, message):
    with db:
        db.execute(
            "INSERT INTO events(at,event_type,message) VALUES(?,?,?)",
            (now(), kind, message),
        )


def save_query(db, job, state, offers=(), rejected=(), **extra):
    payload = {
        **job,
        **extra,
        "query_state": state,
        "offer_count": len(offers),
        "rejected_offer_count": len(rejected),
    }
    with db:
        # Re-validating the same genuine snapshot updates its quarantine without
        # duplicating observations or requiring another source request.
        for sid in {r["snapshot_id"] for r in list(offers) + list(rejected)}:
            db.execute("DELETE FROM rejected WHERE snapshot_id=?", (sid,))
        for offer in offers:
            db.execute(
                "INSERT OR IGNORE INTO offers VALUES(?,?,?,?,?)",
                (
                    offer["observation_id"],
                    offer["snapshot_id"],
                    offer["collection_date"],
                    offer["query_id"],
                    json.dumps(offer, ensure_ascii=False),
                ),
            )
        for bad in rejected:
            db.execute(
                "INSERT INTO rejected VALUES(?,?,?,?)",
                (
                    bad["snapshot_id"],
                    bad.get("source_offer_id"),
                    bad["reason"],
                    json.dumps(bad, ensure_ascii=False),
                ),
            )
        db.execute(
            "UPDATE queries SET query_state=?,offer_count=?,rejected_offer_count=?,payload=? WHERE collection_date=? AND query_id=?",
            (
                state,
                len(offers),
                len(rejected),
                json.dumps(payload, ensure_ascii=False),
                job["collection_date"],
                job["query_id"],
            ),
        )


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_csv(path, fields, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for record in records:
                row = {}
                for k in fields:
                    v = record.get(k)
                    if v is None:
                        v = ""
                    elif isinstance(v, bool):
                        v = str(v).lower()
                    elif isinstance(v, (list, dict)):
                        v = json.dumps(v, ensure_ascii=False)
                    elif isinstance(v, str) and v.lstrip().startswith(
                        ("=", "+", "-", "@")
                    ):
                        v = "'" + v
                    row[k] = v
                w.writerow(row)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def manifest(db, day):
    states = {
        r["query_state"]: r["n"]
        for r in db.execute(
            "SELECT query_state,count(*) n FROM queries WHERE collection_date=? GROUP BY query_state",
            (day,),
        )
    }
    regions = []
    for r in db.execute(
        'SELECT region_id,count(*) query_count,sum(offer_count) offer_count,sum(query_state="ok") successful_query_count FROM queries WHERE collection_date=? GROUP BY region_id',
        (day,),
    ):
        regions.append(dict(r))
    total = sum(states.values())
    rejected_count = db.execute(
        "SELECT coalesce(sum(rejected_offer_count),0) FROM queries WHERE collection_date=?",
        (day,),
    ).fetchone()[0]
    covered_count = db.execute(
        """SELECT count(*) FROM queries q WHERE q.collection_date=?
      AND q.query_state IN ('ok','partial') AND EXISTS(SELECT 1 FROM offers o
      WHERE o.collection_date=q.collection_date AND o.query_id=q.query_id
      AND json_extract(o.payload,'$.analysis_eligible')=1)""",
        (day,),
    ).fetchone()[0]
    return dict(
        schema_version=VERSION,
        collection_date=day,
        generated_at=now(),
        query_count=total,
        query_states=states,
        successful_query_count=states.get("ok", 0),
        pending_query_count=states.get("pending", 0),
        processed_query_count=total
        - states.get("pending", 0)
        - states.get("running", 0),
        offer_count=db.execute(
            "SELECT count(*) FROM offers WHERE collection_date=?", (day,)
        ).fetchone()[0],
        rejected_offer_count=rejected_count,
        covered_query_count=covered_count,
        coverage_complete=bool(total) and covered_count == total,
        validation_clean=rejected_count == 0,
        complete=bool(total)
        and covered_count == total
        and rejected_count == 0
        and set(states) <= {"ok"},
        blocked_reason=get_control(db, "blocked"),
        regions=regions,
    )


def export(db, day, full_history=True):
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / ".export.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        daily = DATA / "daily" / day

        def offers(where="", args=()):
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM offers "
                    + where
                    + " ORDER BY collection_date,query_id,observation_id",
                    args,
                )
            ]

        with db:
            db.execute("BEGIN")
            all_offers = (
                offers() if full_history else offers("WHERE collection_date=?", (day,))
            )
            day_offers = [r for r in all_offers if r["collection_date"] == day]
            if full_history:
                write_csv(DATA / "flights.csv", FIELDS, all_offers)
            write_csv(daily / "flights.csv", FIELDS, day_offers)
            queries = []
            for r in db.execute(
                "SELECT * FROM queries WHERE collection_date=? ORDER BY priority",
                (day,),
            ):
                queries.append(
                    {
                        **json.loads(r["payload"]),
                        **{
                            k: r[k]
                            for k in (
                                "query_state",
                                "attempt_count",
                                "offer_count",
                                "rejected_offer_count",
                            )
                        },
                    }
                )
            write_csv(daily / "queries.csv", QUERY_FIELDS, queries)
            summaries = []
            groups = {}
            for o in all_offers:
                if o["analysis_eligible"]:
                    groups.setdefault(
                        (o["collection_date"], o["query_id"], o["snapshot_id"]), []
                    ).append(o)
            for group in groups.values():
                sample = group[0]
                prices = sorted(o["price_minor"] for o in group)
                n = len(prices)
                median = (
                    prices[n // 2]
                    if n % 2
                    else (prices[n // 2 - 1] + prices[n // 2]) / 2
                )
                summaries.append(
                    {
                        **sample,
                        "travel_date": sample["departure_date"],
                        "eligible_offer_count": n,
                        "minimum_price_minor": prices[0],
                        "minimum_price_amount": f"{prices[0]//100}.{prices[0]%100:02d}",
                        "median_price_minor": median,
                    }
                )
            if full_history:
                write_csv(DATA / "query_prices.csv", SUMMARY_FIELDS, summaries)
            write_csv(
                daily / "query_prices.csv",
                SUMMARY_FIELDS,
                [r for r in summaries if r["collection_date"] == day],
            )
            if full_history:
                rejected = [
                    json.loads(r[0]) for r in db.execute("SELECT payload FROM rejected")
                ]
                write_csv(
                    DATA / "rejected_offers.csv",
                    ["snapshot_id", "query_id", "source_offer_id", "reason"],
                    rejected,
                )
            m = manifest(db, day)
            atomic_text(
                daily / "manifest.json",
                json.dumps(m, ensure_ascii=False, indent=2) + "\n",
            )
            if full_history:
                events = [
                    dict(r)
                    for r in db.execute(
                        "SELECT at,event_type,message FROM events ORDER BY id"
                    )
                ]
                atomic_text(
                    DATA / "events.jsonl",
                    "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in events),
                )
        return m
