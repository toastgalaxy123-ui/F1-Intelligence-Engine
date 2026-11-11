# Data contracts, conventions and labeling risks

## Canonical conventions

| Concept | Standard |
|---|---|
| Event/time | All UTC, timezone-aware |
| Lap/stint time | seconds (`*_s`) |
| Temperature | °C (`*_c`) |
| Distance | metres (`*_m`) |
| Track speed | km/h only if indicated (`*_kmh`) |
| Angles | degrees |
| Lap number | integer starting at 1 within session |
| Compound | OpenF1 uppercase categorical `SOFT`, `MEDIUM`, `HARD`, etc. |
| Unknown values | SQL NULL or explicit `UNKNOWN`, never fabricated zeros |
| Driver key | `(session_key, driver_number)` |

## SILVER (observations only)

`silver.sessions`: session_key, meeting_key, year, circuit_key,
circuit_short_name, country_name, session start/end timestamps, race type.

`silver.laps`: session_key, meeting_key, driver_number, lap_number,
date_start TIMESTAMPTZ, lap_duration DOUBLE, 3 sector durations, is_pit_out_lap.

`silver.stints`: session_key, meeting_key, driver_number, stint_number,
lap_start, lap_end, compound, tyre_age_at_start.

`silver.pit`: session_key, meeting_key, driver_number, lap_number,
date, lane_duration, stop_duration.

`silver.weather`: weather timestamps and temperature, rainfall, humidity.

`silver.race_control`: timestamp, event category, flag, scope, raw message.

`silver.session_result`, `silver.starting_grid`, `silver.position`,
`silver.intervals`: raw observed metadata, typed where safe.
`silver.intervals` keeps gap/interval raw as strings because lapped values
cannot be interpreted as numeric seconds.

## GOLD (explicit derived features)

`gold.lap_observations` includes:
- `season`, `circuit_key`, `circuit_short_name`, `regulation_era` from silver.sessions.
- `observed_lap_time_s`: actual time from OpenF1.
- `tyre_age_at_lap_start`: `tyre_age_at_start + lap_number - lap_start`.
- `pit_event_lap`: OpenF1 says pit lane passed on this lap.
- `pit_out_lap`: OpenF1 flag.
- `latest_observed_track_temp_c`: weather reading at or before lap start.
- `track_status = 'UNCLASSIFIED'`: status parser not yet built.
- `confirmed_green_lap = FALSE`: do not treat it as green.
- `provisional_pace_candidate`: obvious first/pit-in/out laps excluded but
  safety cars, VSC, yellow flags and traffic are NOT excluded.

**DO NOT** train a publishable pace/degradation model treating provisional
candidates as verified clean-air green-flag laps.

## Necessary future gold tables

`gold.track_status_intervals`: session_key, start_utc, end_utc, status,
source_rule, confidence. Build a STATE MACHINE from race-control events.
Include ambiguity state and manually inspect every unusual GP.

`gold.lap_context`: session_key, driver_number, lap_number,
start/end approx timestamp, safety status, pit flags, gaps, traffic exposure,
weather, tire age, fuel-load proxy, elapsed seconds, masking reasons.

`gold.race_state_snapshots`: session_key, as_of_utc, completed_laps,
positions, gaps, tires, retirements, weather status and source freshness.

`gold.training_examples`: target event time, feature cutoff UTC, feature
provenance, target available_at time, split IDs and exclusion rationale.

**Observed vs estimated vs simulated:**
`observed_` is from source; `estimated_` is inferred; `predicted_` is output
from trained model; `simulated_` is sampled within race simulator.

## Not identifiable directly from public timing data

- True fuel mass (use race-progress proxy and carefully disclose ambiguity).
- Driver-controlled energy deployment (especially 2026).
- Exact tire condition/grip/temperature for every car.
- True clean-air potential from laps behind traffic.
- Counterfactual outcome of a strategy not driven in reality.

Any model relying on these must make assumptions and quantify uncertainty.
