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
- `track_status`: synchronized from `gold.lap_context` (`GREEN`, `YELLOW`,
  `DOUBLE_YELLOW`, `VSC`, `SC`, `RED`, `UNKNOWN`).
- `confirmed_green_lap`: `TRUE` iff the entire lap window overlapped only
  `GREEN` intervals in `gold.track_status_intervals`.
- `provisional_pace_candidate`: obvious first/pit-in/out laps excluded but
  safety cars, VSC, yellow flags and traffic are NOT excluded.

**DO NOT** train a publishable pace/degradation model treating provisional
candidates as verified clean-air green-flag laps; use `gold.lap_context`
where `is_clean_green_pace_lap = TRUE`.

`gold.track_status_intervals` (Stage 1):
- `session_key`, `start_utc`, `end_utc`, `status` (`GREEN`, `YELLOW`,
  `DOUBLE_YELLOW`, `VSC`, `SC`, `RED`, `UNKNOWN`), `source_rule`, `confidence`.
- Built via a two-layer state machine (`src/f1ri/features/lap_context.py`)
  separating track-wide neutralization (`RED`, `SC`, `VSC`) from sector-level
  yellow/double-yellow flags so sector clears during VSC/SC never restore `GREEN`.
- Defaults to `UNKNOWN` (`confidence = 'LOW'`) when race-control events are
  missing or uninitialized.

`gold.lap_context` (Stage 1):
- Keys & metadata: `session_key`, `meeting_key`, `driver_number`, `season`,
  `circuit_key`, `circuit_short_name`, `regulation_era`, `lap_number`,
  `race_total_laps`.
- Timing & stint: `lap_start_utc`, `lap_end_estimate_utc`, `elapsed_race_time_s`,
  `observed_lap_time_s`, `sector_1_s`, `sector_2_s`, `sector_3_s`,
  `stint_number`, `compound`, `tyre_age_at_lap_start`, `fuel_load_proxy`.
- Weather & as-of position/intervals: `track_temperature_c`, `air_temperature_c`,
  `rainfall`, `position_at_lap_start`, `gap_to_leader_at_start_raw`,
  `gap_to_leader_at_start_s` (`NULL` when lapped, e.g. `'+1 LAP'`),
  `gap_ahead_at_start_raw`, `gap_ahead_at_start_s` (`NULL` for P1 or non-numeric).
- Physical track traffic & blue flags: `track_gap_ahead_at_start_s`,
  `track_gap_ahead_at_end_s` (control-line physical proximity including lapped
  cars), `had_in_lap_on_track_pass`, `is_blue_flagged_lap`,
  `is_clean_air_at_start`, `is_clean_air`.
- Pit & track-status masking: `is_pit_in_lap`, `is_pit_out_lap`, `is_pit_lap`,
  `stint_pit_mismatch`, `track_status`, `confirmed_green_lap`,
  `non_green_overlap_s`, `is_clean_green_pace_lap`, `exclusion_reason`
  (pipe-delimited reasons or `NULL` when usable clean green-flag pace lap).

`gold.pace_training_examples` (Stage 2):
- One row per continued-stint forecast opportunity `(session_key, driver_number, cutoff_lap_number, horizon)`.
- Prediction metadata: `target_lap_number`, `forecast_as_of_utc` (completion UTC of `cutoff_lap_number`), `target_lap_start_utc`, `target_lap_end_utc`, `modeling_eligibility` (`ELIGIBLE_CLEAN_GREEN` or `INELIGIBLE_TARGET`).
- Context & normalized identities: `season`, `regulation_era`, `circuit_key`, `circuit_short_name`, `team_id`, `driver_id`, `scheduled_race_laps`, `race_progress`.
- Tire & recent clean-pace predictors (strictly as of `forecast_as_of_utc`): `compound`, `tyre_age_at_lap_start`, `stint_number`, `current_stint_laps_completed`, `tyre_age_at_stint_start`, `actual_compound_code`, `compound_mapping_source`, `compound_mapping_confidence`, `last_clean_lap_time_s`, `median_last_3_clean_laps_s`, `median_last_5_clean_laps_s`, `median_stint_clean_laps_s`, `clean_laps_observed_current_stint`, `clean_laps_observed_race`, `last_clean_lap_age`.
- Point-in-time weather: `last_known_track_temp_c`, `last_known_air_temp_c`, `last_known_rainfall`, `temperature_observation_age_s`.
- Target fields (never used as predictors): `observed_target_lap_time_s`, `target_is_clean_green`, `target_exclusion_reason`.

## Necessary future gold tables

`gold.race_state_snapshots`: session_key, as_of_utc, completed_laps,
positions, gaps, tires, retirements, weather status and source freshness.

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
