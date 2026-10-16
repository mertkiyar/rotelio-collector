"""Unit fixtures below are synthetic and never imported into the data pool."""

import copy
import csv
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daily import (
    load_config,
    build_jobs,
    travel_dates,
    ensure_plan,
    validation_requires_stop,
    browser_options,
    choose_node,
)
from flight_schema import normalize, FIELDS
from flight_store import connect, save_query, write_csv, manifest, export


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.day = date(2026, 10, 15)
        self.cfg["refresh_anchor_date"] = self.day.isoformat()
        self.job = next(
            j
            for j in build_jobs(self.cfg, self.day)
            if j["travel_date"] == "2026-10-22"
            and j["search_origin_id"] == "250_0"
            and j["region_id"] == "antalya"
            and j["trip_direction"] == "outbound"
        )
        self.raw = {
            "source": "obilet",
            "transport_mode": "flight",
            "source_url": self.job["source_url"],
            "collected_at": "2026-10-15T18:00:00+00:00",
            "search": {
                "origin_id": self.job["search_origin_id"],
                "destination_id": self.job["search_destination_id"],
                "requested_date": "2026-10-22",
                "displayed_date": "22 Ekim Perşembe",
                "currency_display": "TRY",
                "adult_count": 1,
                "price_scope": "per_person",
            },
            "rows": [
                {
                    "source_offer_id": "IST-AYT-20261022-UNIT-TEST",
                    "carrier": "UNIT TEST ONLY",
                    "origin_terminal": "Test origin",
                    "destination_terminal": "Test destination",
                    "origin_airport_code": "IST",
                    "destination_airport_code": "AYT",
                    "departure_date_from_card": "2026-10-22",
                    "departure_text": "23:20",
                    "arrival_text": "00:30",
                    "duration_text": "1s 10dk",
                    "price_integer": "1.234",
                    "price_decimal": ",56",
                    "price_currency": "TL",
                    "direct": True,
                    "available_marker": True,
                }
            ],
        }

    def test_complete_approved_scope(self):
        jobs = build_jobs(self.cfg, self.day)
        self.assertEqual(len(jobs), 960)
        self.assertEqual(len({x["query_id"] for x in jobs}), 960)
        self.assertEqual(len({x["region_id"] for x in jobs}), 8)
        for region in self.cfg["destinations"]:
            subset = [x for x in jobs if x["region_id"] == region["region_id"]]
            self.assertEqual(len(subset), 120)
            self.assertEqual(
                {x["trip_direction"] for x in subset}, {"outbound", "inbound"}
            )

    def test_all_thirty_dates_and_both_directions_are_present(self):
        from datetime import timedelta

        expected = {self.day + timedelta(days=i) for i in range(1, 31)}
        self.assertEqual(set(travel_dates(self.cfg, self.day)), expected)
        for value in expected:
            jobs = [
                j
                for j in build_jobs(self.cfg, self.day)
                if j["travel_date"] == value.isoformat()
            ]
            self.assertEqual(len(jobs), 32)
            self.assertEqual(sum(j["trip_direction"] == "outbound" for j in jobs), 16)
            self.assertEqual(sum(j["trip_direction"] == "inbound" for j in jobs), 16)

    def test_cycle_stays_fixed_for_three_days_and_moves_across_months(self):
        from datetime import timedelta

        for offset in (0, 15, 18, 45):
            start = self.day + timedelta(days=offset)
            self.assertEqual(
                travel_dates(self.cfg, start),
                travel_dates(self.cfg, start + timedelta(days=2)),
            )
            self.assertNotEqual(
                travel_dates(self.cfg, start),
                travel_dates(self.cfg, start + timedelta(days=3)),
            )
        self.assertEqual(travel_dates(self.cfg, date(2026, 10, 14)), [])

    def test_exact_price_and_overnight_clock(self):
        _, rows, bad = normalize(self.raw, self.job, self.cfg)
        self.assertFalse(bad)
        self.assertEqual(rows[0]["price_minor"], 123456)
        self.assertEqual(rows[0]["price_amount"], "1234.56")
        self.assertEqual(rows[0]["arrival_at"], "2026-10-23T00:30:00+03:00")
        self.assertEqual(set(rows[0]), set(FIELDS))
        self.assertIsNone(rows[0]["cabin_baggage_display"])

    def test_currency_or_day_mismatch_stops_snapshot(self):
        for field, value in [
            ("currency_display", "EUR"),
            ("displayed_date", "23 Ekim Cuma"),
        ]:
            raw = copy.deepcopy(self.raw)
            raw["search"][field] = value
            with self.assertRaises(ValueError):
                normalize(raw, self.job, self.cfg)

    def test_invalid_arrival_is_quarantined(self):
        self.raw["rows"][0]["arrival_text"] = "00:31"
        _, rows, bad = normalize(self.raw, self.job, self.cfg)
        self.assertFalse(rows)
        self.assertEqual(bad[0]["reason"], "arrival_clock_duration_mismatch")

    def test_explicit_next_day_marker(self):
        self.raw["rows"][0]["arrival_text"] = "00:30 +1"
        _, rows, bad = normalize(self.raw, self.job, self.cfg)
        self.assertFalse(bad)
        self.assertEqual(rows[0]["arrival_time"], "00:30")
        self.assertEqual(rows[0]["arrival_date"], "2026-10-23")
        self.assertEqual(
            rows[0]["arrival_date_basis"], "displayed_clock_and_day_offset"
        )
        self.raw["rows"][0]["arrival_text"] = "00:30 +2"
        _, rows, bad = normalize(self.raw, self.job, self.cfg)
        self.assertFalse(rows)
        self.assertEqual(bad[0]["reason"], "arrival_day_offset_mismatch")

    def test_airport_outside_region_is_not_imported(self):
        r = self.raw["rows"][0]
        r["source_offer_id"] = r["source_offer_id"].replace("AYT", "GZP")
        r["destination_airport_code"] = "GZP"
        _, rows, bad = normalize(self.raw, self.job, self.cfg)
        self.assertFalse(rows)
        self.assertEqual(bad[0]["reason"], "airport_outside_pilot_route")

    def test_missing_clock_is_not_filled(self):
        self.raw["rows"][0]["departure_text"] = None
        _, rows, bad = normalize(self.raw, self.job, self.cfg)
        self.assertFalse(rows)
        self.assertTrue(bad)

    def test_empty_snapshot_is_not_zero_fare(self):
        self.raw["rows"] = []
        with self.assertRaises(ValueError):
            normalize(self.raw, self.job, self.cfg)

    def test_bad_source_duration_cannot_pollute_or_stall_valid_routes(self):
        timing = [{"reason": "arrival_clock_duration_mismatch"}] * 8
        self.assertFalse(validation_requires_stop([{"valid": True}] * 31, timing, 39))
        self.assertTrue(validation_requires_stop([], timing, 8))
        self.assertTrue(
            validation_requires_stop(
                [{"valid": True}], [{"reason": "invalid_price_or_currency"}] * 9, 10
            )
        )

    def test_persistent_replay_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            db = connect(Path(d) / "test.sqlite3")
            ensure_plan(db, self.cfg, self.day)
            _, rows, bad = normalize(self.raw, self.job, self.cfg)
            for _ in range(2):
                save_query(db, self.job, "ok", rows, bad)
            self.assertEqual(db.execute("SELECT count(*) FROM offers").fetchone()[0], 1)
            self.assertEqual(manifest(db, "2026-10-15")["query_count"], 960)
            db.close()

    def test_csv_schema_and_formula_handling(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "test.csv"
            write_csv(
                p,
                FIELDS,
                [
                    {
                        "carrier": "=TEST",
                        "price_minor": 123456,
                        "price_amount": "1234.56",
                        "available": True,
                    }
                ],
            )
            with p.open(encoding="utf-8-sig", newline="") as f:
                r = csv.DictReader(f)
                self.assertEqual(r.fieldnames, FIELDS)
                row = next(r)
            self.assertEqual(row["carrier"], "'=TEST")
            self.assertEqual(row["available"], "true")
            self.assertEqual(row["price_amount"], "1234.56")

    def test_daily_export_preserves_full_history_until_final_refresh(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            db = connect(base / "test.sqlite3")
            ensure_plan(db, self.cfg, self.day)
            _, rows, bad = normalize(self.raw, self.job, self.cfg)
            save_query(db, self.job, "ok", rows, bad)
            with patch("flight_store.DATA", base):
                export(db, "2026-10-15")
                new = copy.deepcopy(rows[0])
                new["observation_id"] = "second-unit-observation"
                new["snapshot_id"] = "second-unit-snapshot"
                save_query(db, self.job, "ok", [new], [])
                export(db, "2026-10-15", full_history=False)
                with (base / "flights.csv").open(encoding="utf-8-sig", newline="") as f:
                    self.assertEqual(len(list(csv.DictReader(f))), 1)
                with (base / "daily/2026-10-15/flights.csv").open(
                    encoding="utf-8-sig", newline=""
                ) as f:
                    self.assertEqual(len(list(csv.DictReader(f))), 2)
                export(db, "2026-10-15")
                with (base / "flights.csv").open(encoding="utf-8-sig", newline="") as f:
                    self.assertEqual(len(list(csv.DictReader(f))), 2)
            db.close()

    def test_browser_channel_configuration(self):
        with patch.dict(os.environ, {"FLIGHT_BROWSER_CHANNEL": "chromium"}):
            self.assertEqual(browser_options(25000)["channel"], "chromium")
        with patch.dict(os.environ, {"FLIGHT_BROWSER_CHANNEL": "unsupported"}):
            with self.assertRaises(ValueError):
                browser_options(25000)

    def test_bundled_node_requires_no_local_path(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "daily.shutil.which", return_value=None
        ):
            self.assertEqual(choose_node(), "bundled")
            self.assertNotIn("PLAYWRIGHT_NODEJS_PATH", os.environ)

    def test_next_day_collection_keeps_previous_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            db = connect(base / "test.sqlite3")
            ensure_plan(db, self.cfg, self.day)
            _, first, bad = normalize(self.raw, self.job, self.cfg)
            save_query(db, self.job, "ok", first, bad)
            next_day = date(2026, 10, 16)
            ensure_plan(db, self.cfg, next_day)
            job = next(
                j
                for j in build_jobs(self.cfg, next_day)
                if j["query_id"] == self.job["query_id"]
            )
            raw = copy.deepcopy(self.raw)
            raw["collected_at"] = "2026-10-16T18:00:00+00:00"
            _, second, bad = normalize(raw, job, self.cfg)
            save_query(db, job, "ok", second, bad)
            with patch("flight_store.DATA", base):
                export(db, next_day.isoformat())
                with (base / "flights.csv").open(encoding="utf-8-sig", newline="") as f:
                    records = list(csv.DictReader(f))
                self.assertEqual(len(records), 2)
                self.assertEqual(
                    {r["collection_date"] for r in records},
                    {"2026-10-15", "2026-10-16"},
                )
                with (base / "daily/2026-10-16/flights.csv").open(
                    encoding="utf-8-sig", newline=""
                ) as f:
                    self.assertEqual(len(list(csv.DictReader(f))), 1)
            db.close()


if __name__ == "__main__":
    unittest.main()
