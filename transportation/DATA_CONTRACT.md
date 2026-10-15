# Flight observation contract - version 2.0

`data/flights.csv` and every daily `flights.csv` have the exact same column names,
order, units and types. Definitions apply to JSON payloads in the SQLite pool too.
Place names, airline names and source descriptions retain their original language.

## Identity and provenance

- `schema_version`: `2.0`.
- `observation_id`: unique snapshot/offer identifier; replaying the same observation does not duplicate it.
- `snapshot_id`: SHA-256 prefix of the original captured JSON.
- `query_id`: route, departure date, one adult, economy, TRY and schema version.
- `collection_date`: observation calendar day in Europe/Istanbul, `YYYY-MM-DD`.
- `observed_at`: actual source observation timestamp with UTC offset.
- `region_id`, `region_name`: approved destination region, preserved on both outbound and inbound queries.
- `origin_city`, `destination_city`: flight search locations; airport codes provide actual endpoints.
- `trip_direction`: `outbound` or `inbound` relative to the pilot destination.
- `search_origin_id`, `search_destination_id`: source search point IDs.
- `source`, `source_url`, `source_offer_id`, `capture_method`: reproducible provenance.

## Schedule

- `carrier`: displayed carrier; connecting offers may include a combined label.
- `origin_airport_code`, `destination_airport_code`: three-letter airport codes.
- `origin_airport_name`, `destination_airport_name`: displayed source names.
- `departure_date`, `departure_time`, `departure_at`: date, HH:MM and timezone-aware ISO timestamp.
- `arrival_date`, `arrival_time`, `arrival_at`: displayed clock and date inferred from displayed duration.
- `arrival_date_basis`: `displayed_clock_and_day_offset` when the source shows
  an explicit `+1`/`+2`; otherwise `displayed_clock_date_inferred_from_duration`.
- `duration_minutes`: positive integer; arrival clock must agree with departure plus duration.
- `lead_days`: departure calendar date minus collection calendar date.
- `departure_weekday`: Monday=0, Sunday=6; `departure_hour`: 0-23.
- `is_weekend`, `is_direct`: booleans.
- `transfer_description`: displayed transfer text, nullable.
- `schedule_id`: heuristic carrier/airport/schedule match; not a verified flight number.

## Fare and availability

- `currency`: `TRY`.
- `price_minor`: positive integer kuruş; use this for arithmetic.
- `price_amount`: two-decimal TRY amount; `123456` minor units is `1234.56`.
- `price_scope`: `per_person_one_way`; `search_adults`: 1; `cabin_class`: `economy`.
- `checked_baggage_display`, `cabin_baggage_display`: source text or null; null does not mean free baggage.
- `available`: source card availability marker, not checkout confirmation.
- `analysis_eligible`: displayed available and departure still in the future; structural checks already passed.
- `checkout_price_verified`: false; the collector does not reserve or purchase.

## Regional joins

| region_id | Destination | Airport |
|---|---|---|
| antalya | Antalya / Kaleiçi-Konyaaltı | AYT |
| izmir_cesme | İzmir / Çeşme-Alaçatı | ADB |
| mugla_bodrum | Muğla / Bodrum | BJV |
| mugla_fethiye | Muğla / Fethiye-Ölüdeniz | DLM |
| trabzon | Trabzon / centre-Maçka | TZX |
| nevsehir | Nevşehir / Göreme-Uçhisar-Ürgüp | NAV |
| gaziantep | Gaziantep / centre | GZT |
| erzurum | Erzurum / Palandöken | ERZ |

Hotels and places can use `region_id` but must preserve their own exact coordinates.
An airport-city pair is not the distance or cost of the airport-to-hotel transfer.

## Separate exports

`queries.csv` is the query audit table, not a fare table. Its fixed fields include
`query_state`, `attempt_count`, `offer_count`, `rejected_offer_count`, `observed_at`,
`error_code` and `error_message`. Unknown/empty results never become zero-priced offers.
States: pending, running, ok, partial, empty_unverified, error, blocked, missed.

`query_prices.csv` is a derived training candidate: one eligible snapshot per query
observation, with minimum/median prices and eligible offer count. Minimum price
may represent different carriers/times on different observation days. Use
`schedule_id` or carrier/schedule filters for a same-flight comparison.

Queries on the same travel date are independent one-way offers. A holiday roundtrip
must choose a later return date and compatible airports. A group total from a
one-adult quote is an estimate unless that passenger count is searched separately.

CSV files are UTF-8 BOM, comma-delimited. Null is blank, booleans are `true/false`.
Formula-like source strings receive a leading apostrophe only in CSV display;
the original strings remain in raw JSON and SQLite.
