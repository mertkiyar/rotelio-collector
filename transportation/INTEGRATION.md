# Flight data integration

Use `flights.csv` for individual offers and `query_prices.csv` for snapshot-level
price analysis. The canonical field definitions are in `DATA_CONTRACT.md`.
Original place names and source labels retain their original language.

## Joining project modules

- Join accommodation and places through `region_id`, keeping their own exact
  coordinates. A destination airport is not the hotel location.
- Match outbound and return departure dates to the requested holiday dates.
- Retain `collection_date` and `observed_at`: observations from different days
  are separate price snapshots, not extra components of one holiday budget.
- Allow time between flight arrival, hotel arrival and scheduled visits.
- Select compatible return airports and a return departure after the outbound
  arrival. IST and SAW must not be treated as the same physical airport.

## Budget calculation

Use `analysis_eligible=true` and integer `price_minor` values for calculations.
For selected offers from the same observation day:

```text
flight_total_minor = (outbound.price_minor + inbound.price_minor) * adults
flight_total_try = flight_total_minor / 100
```

This is a group estimate from single-adult quotes. Bag availability, transfer
fees and checkout prices are not verified. Missing charges remain unknown.
`price_scope=per_person_one_way` expresses both passenger and journey scope.

Accommodation must separately declare its currency, price scope, meal plan,
room count, guest count and stay length. A per-room-per-night rate must be
converted before it is added to flight totals.

Places must separately declare holiday categories, estimated visit duration,
entry-fee scope and currency. An unknown admission fee must not become zero;
zero means confirmed free entry. Missing opening hours remain unknown.

## Price analysis

Split training and evaluation chronologically. Do not allow later observations
of the same departure to enter the training data used for an earlier forecast.
Compare an initial model against the last observed price. Minimum query fares
may come from different carriers or schedules on different observation days;
`schedule_id` is a heuristic schedule match, not a verified flight number.

## Refresh windows

A refresh cycle covers every departure date from cycle start + 1 through
cycle start + 30, in both directions. Cycles repeat every three days. CSV
`collection_date` and `observed_at` always describe actual capture time, even
when a delayed batch finishes on a later day. Do not relabel a whole cycle as
one simultaneous snapshot. Use the latest available matching-date offers and
show their age. The first and last stored dates bound supported fare lookup;
a 14-night stay starting near the last date can require a return outside it.

Daily manifests describe actual-day rows. The private bundle also contains
`cycles/YYYY-MM-DD/manifest.json` for full-cycle coverage. Earlier historical
offers are preserved but are not fresh observations for a later cycle.
