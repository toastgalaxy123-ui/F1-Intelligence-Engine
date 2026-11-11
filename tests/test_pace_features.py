from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from f1ri.features.pace_examples import build_pace_examples_dataframe
from f1ri.features.pace_features import (
    canonical_driver_id,
    canonical_team_id,
    compute_recent_clean_stats,
)

T0 = datetime(2025, 10, 5, 12, 0, 0, tzinfo=UTC)


def _ts(s: float) -> datetime:
    return T0 + timedelta(seconds=s)


def test_driver_and_team_identity_normalization():
    # Same driver across different car numbers resolves to canonical acronym
    assert canonical_driver_id("BEA", 38) == "BEA"
    assert canonical_driver_id("BEA", 87) == "BEA"
    assert canonical_driver_id(None, 44) == "DRV_44"

    # Team rebrands resolve to canonical team_id
    assert canonical_team_id("AlphaTauri") == "RACING_BULLS"
    assert canonical_team_id("RB") == "RACING_BULLS"
    assert canonical_team_id("Racing Bulls") == "RACING_BULLS"
    assert canonical_team_id("Alfa Romeo") == "SAUBER"
    assert canonical_team_id("Kick Sauber") == "SAUBER"


def test_recent_clean_lap_median_uses_only_completed_past_laps():
    prior_clean = pd.DataFrame(
        {
            "lap_number": [2, 3, 4],
            "stint_number": [1, 1, 1],
            "observed_lap_time_s": [97.120, 96.980, 97.050],
        }
    )
    stats = compute_recent_clean_stats(prior_clean, current_stint=1, target_lap_number=5)
    assert stats["last_clean_lap_time_s"] == pytest.approx(97.050)
    assert stats["median_last_3_clean_laps_s"] == pytest.approx(97.050)
    assert stats["median_stint_clean_laps_s"] == pytest.approx(97.050)
    assert stats["clean_laps_observed_current_stint"] == 3
    assert stats["last_clean_lap_age"] == 1


def test_pit_transition_and_missing_target_and_weather_handling():
    lap_ctx = pd.DataFrame(
        [
            {
                "session_key": 100,
                "meeting_key": 10,
                "driver_number": 63,
                "season": 2025,
                "circuit_key": 1,
                "circuit_short_name": "Singapore",
                "regulation_era": "2023_2025",
                "lap_number": 2,
                "race_total_laps": 62,
                "lap_start_utc": _ts(100),
                "lap_end_estimate_utc": _ts(196),
                "observed_lap_time_s": 96.0,
                "stint_number": 1,
                "compound": "MEDIUM",
                "tyre_age_at_lap_start": 4,
                "is_pit_in_lap": False,
                "is_pit_out_lap": False,
                "is_pit_lap": False,
                "is_clean_green_pace_lap": True,
                "exclusion_reason": None,
            },
            # Lap 3: clean lap at end of which we issue h=1 (lap 4) and h=3 (lap 6) forecasts
            {
                "session_key": 100,
                "meeting_key": 10,
                "driver_number": 63,
                "season": 2025,
                "circuit_key": 1,
                "circuit_short_name": "Singapore",
                "regulation_era": "2023_2025",
                "lap_number": 3,
                "race_total_laps": 62,
                "lap_start_utc": _ts(196),
                "lap_end_estimate_utc": _ts(293),
                "observed_lap_time_s": 97.0,
                "stint_number": 1,
                "compound": "MEDIUM",
                "tyre_age_at_lap_start": 5,
                "is_pit_in_lap": False,
                "is_pit_out_lap": False,
                "is_pit_lap": False,
                "is_clean_green_pace_lap": True,
                "exclusion_reason": None,
            },
            # Lap 4: pit-in lap (stint 1 ends)
            {
                "session_key": 100,
                "meeting_key": 10,
                "driver_number": 63,
                "season": 2025,
                "circuit_key": 1,
                "circuit_short_name": "Singapore",
                "regulation_era": "2023_2025",
                "lap_number": 4,
                "race_total_laps": 62,
                "lap_start_utc": _ts(293),
                "lap_end_estimate_utc": _ts(395),
                "observed_lap_time_s": 102.0,
                "stint_number": 1,
                "compound": "MEDIUM",
                "tyre_age_at_lap_start": 6,
                "is_pit_in_lap": True,
                "is_pit_out_lap": False,
                "is_pit_lap": True,
                "is_clean_green_pace_lap": False,
                "exclusion_reason": "PIT_IN",
            },
            # Lap 5: pit-out lap on HARD (stint 2)
            {
                "session_key": 100,
                "meeting_key": 10,
                "driver_number": 63,
                "season": 2025,
                "circuit_key": 1,
                "circuit_short_name": "Singapore",
                "regulation_era": "2023_2025",
                "lap_number": 5,
                "race_total_laps": 62,
                "lap_start_utc": _ts(395),
                "lap_end_estimate_utc": _ts(507),
                "observed_lap_time_s": 112.0,
                "stint_number": 2,
                "compound": "HARD",
                "tyre_age_at_lap_start": 0,
                "is_pit_in_lap": False,
                "is_pit_out_lap": True,
                "is_pit_lap": True,
                "is_clean_green_pace_lap": False,
                "exclusion_reason": "PIT_OUT",
            },
            # Lap 6: clean lap on stint 2 (crossed a pit stop relative to cutoff_lap=3!)
            {
                "session_key": 100,
                "meeting_key": 10,
                "driver_number": 63,
                "season": 2025,
                "circuit_key": 1,
                "circuit_short_name": "Singapore",
                "regulation_era": "2023_2025",
                "lap_number": 6,
                "race_total_laps": 62,
                "lap_start_utc": _ts(507),
                "lap_end_estimate_utc": _ts(602),
                "observed_lap_time_s": 95.0,
                "stint_number": 2,
                "compound": "HARD",
                "tyre_age_at_lap_start": 1,
                "is_pit_in_lap": False,
                "is_pit_out_lap": False,
                "is_pit_lap": False,
                "is_clean_green_pace_lap": True,
                "exclusion_reason": None,
            },
            # Lap 7: incomplete DNF lap (observed_lap_time_s is None)
            {
                "session_key": 100,
                "meeting_key": 10,
                "driver_number": 63,
                "season": 2025,
                "circuit_key": 1,
                "circuit_short_name": "Singapore",
                "regulation_era": "2023_2025",
                "lap_number": 7,
                "race_total_laps": 62,
                "lap_start_utc": _ts(602),
                "lap_end_estimate_utc": None,
                "observed_lap_time_s": None,
                "stint_number": 2,
                "compound": "HARD",
                "tyre_age_at_lap_start": 2,
                "is_pit_in_lap": False,
                "is_pit_out_lap": False,
                "is_pit_lap": False,
                "is_clean_green_pace_lap": False,
                "exclusion_reason": "INCOMPLETE_OR_INVALID_LAP",
            },
        ]
    )
    drivers_df = pd.DataFrame(
        [{"driver_number": 63, "name_acronym": "RUS", "team_name": "Mercedes"}]
    )
    # Weather reading only after lap 2 ends (at t=250s), so at cutoff_lap=2 weather is missing (None)
    weather_df = pd.DataFrame(
        [{"date": _ts(250), "track_temperature": 38.2, "air_temperature": 30.1, "rainfall": False}]
    )

    ex = build_pace_examples_dataframe(lap_ctx, drivers_df, weather_df, horizons=(1, 3))

    # At cutoff_lap=2, target_lap=3: starting tire age (3) is preserved -> expected tyre_age = 5
    c2_h1 = ex[(ex["cutoff_lap_number"] == 2) & (ex["horizon"] == 1)].iloc[0]
    assert c2_h1["tyre_age_at_lap_start"] == 5
    assert c2_h1["tyre_age_at_stint_start"] == 4
    assert pd.isna(c2_h1["last_known_track_temp_c"])
    assert bool(c2_h1["target_is_clean_green"]) is True

    # At cutoff_lap=3, horizon=3 -> target_lap=6 is on stint 2 (crossed pit stop on lap 4),
    # so target_is_clean_green MUST be False with STINT_CHANGED_BEFORE_TARGET!
    c3_h3 = ex[(ex["cutoff_lap_number"] == 3) & (ex["horizon"] == 3)].iloc[0]
    assert bool(c3_h3["target_is_clean_green"]) is False
    assert c3_h3["target_exclusion_reason"] == "STINT_CHANGED_BEFORE_TARGET"
    assert c3_h3["last_known_track_temp_c"] == pytest.approx(38.2)

    # At cutoff_lap=6, horizon=1 -> target_lap=7 is incomplete (observed_lap_time_s is None),
    # so target_is_clean_green MUST be False!
    c6_h1 = ex[(ex["cutoff_lap_number"] == 6) & (ex["horizon"] == 1)].iloc[0]
    assert bool(c6_h1["target_is_clean_green"]) is False
    assert pd.isna(c6_h1["observed_target_lap_time_s"])
