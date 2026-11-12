from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from f1ri.features.stint_examples import build_stint_examples_dataframe
from f1ri.models.contracts import SCENARIO_CONTINUE_STINT, SCENARIO_NEW_FRESH


def _make_synthetic_lap_ctx() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    t0 = datetime(2025, 5, 1, 13, 0, 0, tzinfo=UTC)
    rows = []
    # Driver 1 completes Stint 1 (laps 1..12 on MEDIUM) and pits at end of lap 12 for Stint 2 (laps 13..25 on HARD)
    for lap in range(1, 26):
        stint_num = 1 if lap <= 12 else 2
        cmpd = "MEDIUM" if stint_num == 1 else "HARD"
        stint_lap = lap if stint_num == 1 else (lap - 12)
        tyre_age = stint_lap - 1
        is_pit_in = lap == 12
        is_pit_out = lap == 13
        is_clean = (lap > 1) and (not is_pit_out) and (not is_pit_in)
        rows.append(
            {
                "session_key": 9987,
                "meeting_key": 1260,
                "season": 2025,
                "regulation_era": "2023_2025",
                "circuit_key": 6,
                "circuit_short_name": "Imola",
                "driver_number": 1,
                "lap_number": lap,
                "stint_number": stint_num,
                "compound": cmpd,
                "tyre_age_at_lap_start": tyre_age,
                "lap_start_utc": t0 + timedelta(seconds=(lap - 1) * 80),
                "lap_end_estimate_utc": t0 + timedelta(seconds=lap * 80),
                "observed_lap_time_s": 78.0 + 0.05 * tyre_age - 0.03 * lap,
                "race_total_laps": 63,
                "track_status": "GREEN",
                "is_pit_out_lap": is_pit_out,
                "is_pit_in_lap": is_pit_in,
                "is_pit_lap": is_pit_in or is_pit_out,
                "is_clean_green_pace_lap": is_clean,
                "exclusion_reason": None if is_clean else "PIT_OR_LAP1",
            }
        )
    drivers_df = pd.DataFrame(
        [{"driver_number": 1, "name_acronym": "VER", "team_name": "Red Bull Racing"}]
    )
    weather_df = pd.DataFrame(
        [{"date": t0, "track_temperature": 35.0, "air_temperature": 22.0, "rainfall": 0}]
    )
    return pd.DataFrame(rows), drivers_df, weather_df


def test_stint_examples_point_in_time_cutoffs_and_censoring():
    lap_ctx, drivers_df, weather_df = _make_synthetic_lap_ctx()
    examples = build_stint_examples_dataframe(
        lap_ctx_df=lap_ctx,
        drivers_df=drivers_df,
        weather_df=weather_df,
        horizons=(1, 3, 5),
        include_new_stint_scenarios=True,
    )
    assert not examples.empty

    # 1. Point-in-time cutoff check: forecast_as_of_utc equals lap_end_estimate_utc of cutoff_lap
    for _, row in examples.iterrows():
        cutoff_lap = int(row["cutoff_lap_number"])
        expected_ts = lap_ctx.loc[
            lap_ctx["lap_number"] == cutoff_lap, "lap_end_estimate_utc"
        ].iloc[0]
        assert pd.Timestamp(row["forecast_as_of_utc"]) == pd.Timestamp(expected_ts)

    # 2. Censoring check around pit stop (Lap 12 is pit-in, Lap 13 is Stint 2)
    # At cutoff_lap=10, horizon=5 spans laps 11..15, crossing into Stint 2 -> FUTURE_PIT_TRANSITION
    c10_h5 = examples[
        (examples["scenario_type"] == SCENARIO_CONTINUE_STINT)
        & (examples["cutoff_lap_number"] == 10)
        & (examples["requested_horizon"] == 5)
    ].iloc[0]
    assert bool(c10_h5["trajectory_is_complete_clean"]) is False
    assert c10_h5["trajectory_censoring_reason"] == "FUTURE_PIT_TRANSITION"
    assert pd.isna(c10_h5["observed_cumulative_time_s"])

    # At cutoff_lap=5, horizon=5 spans laps 6..10 (all clean in Stint 1) -> complete clean!
    c5_h5 = examples[
        (examples["scenario_type"] == SCENARIO_CONTINUE_STINT)
        & (examples["cutoff_lap_number"] == 5)
        & (examples["requested_horizon"] == 5)
    ].iloc[0]
    assert bool(c5_h5["trajectory_is_complete_clean"]) is True
    assert bool(c5_h5["target_is_clean_green"]) is True
    assert c5_h5["trajectory_censoring_reason"] == "COMPLETE_CLEAN"
    assert c5_h5["observed_cumulative_time_s"] > 0

    # 3. New-stint scenario emitted at pit-in cutoff_lap=12 for Stint 2 (HARD)
    new_stints = examples[examples["scenario_type"] == SCENARIO_NEW_FRESH]
    assert not new_stints.empty
    ns_h5 = new_stints[new_stints["requested_horizon"] == 5].iloc[0]
    assert int(ns_h5["cutoff_lap_number"]) == 12
    assert ns_h5["compound_relative"] == "HARD"
    assert int(ns_h5["clean_laps_observed_current_stint"]) == 0
    assert int(ns_h5["clean_laps_observed_race"]) > 0
