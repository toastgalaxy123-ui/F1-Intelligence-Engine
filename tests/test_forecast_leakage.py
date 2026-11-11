from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from f1ri.models.pace_linear import AdditivePaceModel
from f1ri.validation.leakage import (
    assert_examples_point_in_time_valid,
    assert_truncation_invariance,
)

T0 = datetime(2025, 10, 5, 12, 0, 0, tzinfo=UTC)


def _ts(s: float) -> datetime:
    return T0 + timedelta(seconds=s)


def test_truncation_invariance_at_lap_25():
    """Verify that deleting or corrupting laps 26+ produces identical features and predictions at lap 25."""
    laps = []
    for drv in (63, 1):
        for lap in range(1, 35):
            laps.append(
                {
                    "session_key": 9896,
                    "meeting_key": 1270,
                    "driver_number": drv,
                    "season": 2025,
                    "circuit_key": 61,
                    "circuit_short_name": "Singapore",
                    "regulation_era": "2023_2025",
                    "lap_number": lap,
                    "race_total_laps": 62,
                    "lap_start_utc": _ts((lap - 1) * 97.0),
                    "lap_end_estimate_utc": _ts(lap * 97.0),
                    "observed_lap_time_s": 97.0 - 0.02 * lap + (0.2 if drv == 1 else 0.0),
                    "stint_number": 1 if lap <= 20 else 2,
                    "compound": "MEDIUM" if lap <= 20 else "HARD",
                    "tyre_age_at_lap_start": (lap - 1) if lap <= 20 else (lap - 21),
                    "is_pit_in_lap": lap == 20,
                    "is_pit_out_lap": lap == 21,
                    "is_pit_lap": lap in (20, 21),
                    "is_clean_green_pace_lap": lap > 1 and lap not in (20, 21),
                    "exclusion_reason": None if (lap > 1 and lap not in (20, 21)) else "PIT",
                }
            )
    lap_ctx_df = pd.DataFrame(laps)
    drivers_df = pd.DataFrame(
        [
            {"driver_number": 63, "name_acronym": "RUS", "team_name": "Mercedes"},
            {"driver_number": 1, "name_acronym": "VER", "team_name": "Red Bull Racing"},
        ]
    )
    weather_df = pd.DataFrame(
        [
            {"date": _ts(w * 300), "track_temperature": 37.0 - 0.1 * w, "air_temperature": 29.5, "rainfall": False}
            for w in range(15)
        ]
    )

    # Fit a model on an earlier season (2024)
    prior_lap_ctx = lap_ctx_df.copy()
    prior_lap_ctx["session_key"] = 9606
    prior_lap_ctx["season"] = 2024
    prior_lap_ctx["lap_start_utc"] = prior_lap_ctx["lap_start_utc"] - timedelta(days=365)
    prior_lap_ctx["lap_end_estimate_utc"] = prior_lap_ctx["lap_end_estimate_utc"] - timedelta(days=365)
    prior_weather = weather_df.copy()
    prior_weather["date"] = prior_weather["date"] - timedelta(days=365)

    from f1ri.features.pace_examples import build_pace_examples_dataframe

    train_ex = build_pace_examples_dataframe(prior_lap_ctx, drivers_df, prior_weather)
    assert_examples_point_in_time_valid(train_ex)
    model = AdditivePaceModel().fit(train_ex)

    assert_truncation_invariance(
        lap_ctx_df=lap_ctx_df,
        drivers_df=drivers_df,
        weather_df=weather_df,
        cutoff_lap=25,
        model=model,
    )
