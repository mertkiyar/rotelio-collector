"""Rotelio flight observations, schema 2.0. Network-free validation."""

import hashlib
import json
import re
from datetime import datetime, time, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Istanbul")
VERSION = "2.0"
FIELDS = """schema_version observation_id snapshot_id query_id collection_date observed_at
region_id region_name origin_city destination_city trip_direction search_origin_id search_destination_id
source source_url source_offer_id capture_method carrier origin_airport_code destination_airport_code
origin_airport_name destination_airport_name departure_date departure_time departure_at
arrival_date arrival_time arrival_at arrival_date_basis duration_minutes lead_days departure_weekday
departure_hour is_weekend is_direct transfer_description currency price_minor price_amount price_scope
search_adults cabin_class checked_baggage_display cabin_baggage_display available
analysis_eligible checkout_price_verified schedule_id""".split()
MONTHS = {
    m: i
    for i, m in enumerate(
        "Ocak Şubat Mart Nisan Mayıs Haziran Temmuz Ağustos Eylül Ekim Kasım Aralık".split(),
        1,
    )
}


def identifier(value):
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def query_id(origin, destination, travel_date):
    return identifier(
        "|".join([VERSION, origin, destination, travel_date, "1", "economy", "TRY"])
    )


def normalize(raw, job, config):
    search = raw["search"]
    parsed = urlparse(raw["source_url"])
    match = re.fullmatch(
        r"/(?:en/)?ucuslar/(\d+_\d+)-(\d+_\d+)/(\d{8})/1a/economy/all/?", parsed.path
    )
    expected = (
        job["search_origin_id"],
        job["search_destination_id"],
        job["travel_date"].replace("-", ""),
    )
    if (
        parsed.hostname not in ("www.obilet.com", "obilet.com")
        or not match
        or match.groups() != expected
    ):
        raise ValueError("source_route_mismatch")
    if raw.get("source") != "obilet" or raw.get("transport_mode") != "flight":
        raise ValueError("flight_source_required")
    if (search["origin_id"], search["destination_id"], search["requested_date"]) != (
        expected[0],
        expected[1],
        job["travel_date"],
    ):
        raise ValueError("search_route_mismatch")
    if (
        search.get("currency_display") != "TRY"
        or type(search.get("adult_count")) is not int
        or search["adult_count"] != 1
        or search.get("price_scope") != "per_person"
    ):
        raise ValueError("search_currency_or_passenger_mismatch")
    observed = datetime.fromisoformat(raw["collected_at"].replace("Z", "+00:00"))
    if (
        observed.tzinfo is None
        or observed.astimezone(TZ).date().isoformat() != job["collection_date"]
    ):
        raise ValueError("observation_date_mismatch")
    date_label = re.match(r"^(\d{1,2})\s+(\w+)", search.get("displayed_date", ""))
    departure_day = datetime.strptime(expected[2], "%Y%m%d").date()
    if not date_label or (int(date_label[1]), MONTHS.get(date_label[2])) != (
        departure_day.day,
        departure_day.month,
    ):
        raise ValueError("displayed_date_mismatch")
    if departure_day <= observed.astimezone(TZ).date():
        raise ValueError("future_travel_date_required")
    if not raw.get("rows"):
        raise ValueError("empty_snapshot_not_a_zero_fare")
    sid = identifier(
        json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    origin = config["points"][job["search_origin_id"]]
    dest = config["points"][job["search_destination_id"]]
    valid = []
    invalid = []
    seen = set()
    for row in raw["rows"]:
        try:
            for field in (
                "source_offer_id",
                "carrier",
                "origin_terminal",
                "destination_terminal",
                "departure_text",
                "arrival_text",
            ):
                if not isinstance(row.get(field), str) or not row[field].strip():
                    raise ValueError("missing_" + field)
            airport_id = re.match(
                r"^([A-Z]{3})-([A-Z]{3})-(\d{8})-", row["source_offer_id"]
            )
            if not airport_id or airport_id.groups() != (
                row.get("origin_airport_code"),
                row.get("destination_airport_code"),
                expected[2],
            ):
                raise ValueError("offer_airport_or_date_mismatch")
            if row.get("departure_date_from_card") != job["travel_date"]:
                raise ValueError("offer_date_mismatch")
            if (
                row["origin_airport_code"] not in origin["airports"]
                or row["destination_airport_code"] not in dest["airports"]
            ):
                raise ValueError("airport_outside_pilot_route")
            if (
                type(row.get("direct")) is not bool
                or type(row.get("available_marker")) is not bool
            ):
                raise ValueError("availability_or_direct_flag_missing")
            arrival_label = re.fullmatch(
                r"(\d{2}:\d{2})(?:\s*\+(\d+))?", row["arrival_text"]
            )
            if (
                not re.fullmatch(r"\d{2}:\d{2}", row["departure_text"])
                or not arrival_label
            ):
                raise ValueError("invalid_clock")
            duration = re.fullmatch(
                r"(?:(\d+)s)?\s*(?:(\d+)dk)?", row.get("duration_text") or ""
            )
            if not duration or not any(duration.groups()):
                raise ValueError("invalid_duration")
            minutes = int(duration[1] or 0) * 60 + int(duration[2] or 0)
            if not 0 < minutes < 2880:
                raise ValueError("duration_out_of_range")
            departure = datetime.combine(
                departure_day, time.fromisoformat(row["departure_text"]), TZ
            )
            arrival = departure + timedelta(minutes=minutes)
            if arrival.strftime("%H:%M") != arrival_label[1]:
                raise ValueError("arrival_clock_duration_mismatch")
            if arrival_label[2] is not None and (
                arrival.date() - departure_day
            ).days != int(arrival_label[2]):
                raise ValueError("arrival_day_offset_mismatch")
            integer = row.get("price_integer")
            fraction = row.get("price_decimal") or ",00"
            if (
                row.get("price_currency") not in ("TL", "TRY", "₺")
                or not isinstance(integer, str)
                or not re.fullmatch(r"(?:\d{1,3}(?:\.\d{3})+|\d+)", integer.strip())
                or not re.fullmatch(r",\d{2}", fraction)
            ):
                raise ValueError("invalid_price_or_currency")
            amount = int(integer.replace(".", "")) * 100 + int(fraction[1:])
            if amount <= 0:
                raise ValueError("positive_price_required")
            if row["source_offer_id"] in seen:
                raise ValueError("duplicate_offer_in_snapshot")
            seen.add(row["source_offer_id"])
            schedule = identifier(
                "|".join(
                    [
                        row["carrier"],
                        row["origin_airport_code"],
                        row["destination_airport_code"],
                        departure.isoformat(),
                        arrival.isoformat(),
                    ]
                )
            )
            record = dict(
                schema_version=VERSION,
                observation_id=identifier(sid + "|" + row["source_offer_id"]),
                snapshot_id=sid,
                query_id=job["query_id"],
                collection_date=job["collection_date"],
                observed_at=observed.isoformat(),
                region_id=job["region_id"],
                region_name=job["region_name"],
                origin_city=origin["name"],
                destination_city=dest["name"],
                trip_direction=job["trip_direction"],
                search_origin_id=job["search_origin_id"],
                search_destination_id=job["search_destination_id"],
                source="obilet",
                source_url=raw["source_url"],
                source_offer_id=row["source_offer_id"],
                capture_method="playwright_rendered_dom",
                carrier=row["carrier"],
                origin_airport_code=row["origin_airport_code"],
                destination_airport_code=row["destination_airport_code"],
                origin_airport_name=row["origin_terminal"],
                destination_airport_name=row["destination_terminal"],
                departure_date=job["travel_date"],
                departure_time=row["departure_text"],
                departure_at=departure.isoformat(),
                arrival_date=arrival.date().isoformat(),
                arrival_time=arrival_label[1],
                arrival_at=arrival.isoformat(),
                arrival_date_basis=(
                    "displayed_clock_and_day_offset"
                    if arrival_label[2] is not None
                    else "displayed_clock_date_inferred_from_duration"
                ),
                duration_minutes=minutes,
                lead_days=(departure_day - observed.astimezone(TZ).date()).days,
                departure_weekday=departure_day.weekday(),
                departure_hour=departure.hour,
                is_weekend=departure_day.weekday() >= 5,
                is_direct=row["direct"],
                transfer_description=row.get("transfer_text"),
                currency="TRY",
                price_minor=amount,
                price_amount=f"{amount//100}.{amount%100:02d}",
                price_scope="per_person_one_way",
                search_adults=1,
                cabin_class="economy",
                checked_baggage_display=row.get("checked_baggage_display"),
                cabin_baggage_display=row.get("cabin_baggage_display"),
                available=row["available_marker"],
                analysis_eligible=row["available_marker"] and departure > observed,
                checkout_price_verified=False,
                schedule_id=schedule,
            )
            assert set(record) == set(FIELDS)
            valid.append(record)
        except (ValueError, TypeError, KeyError) as error:
            invalid.append(
                {
                    "snapshot_id": sid,
                    "query_id": job["query_id"],
                    "source_offer_id": row.get("source_offer_id"),
                    "reason": str(error),
                    "raw_row": row,
                }
            )
    return sid, valid, invalid
