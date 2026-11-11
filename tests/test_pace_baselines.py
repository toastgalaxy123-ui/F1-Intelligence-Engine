from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from f1ri.models.contracts import PaceRequest
from f1ri.models.pace_baselines import (
    CompoundRecentMedianBaseline,
    HistoricalCircuitPriorBaseline,
    RaceProgressDriftBaseline,
    RecentCleanLapMedianBaseline,
)

T0 = datetime(2024, 9, 1, 13, 0, 0, tzinfo=UTC)


def _synthetic_train_df() -> pd.DataFrame:
    rows = []
    for lap in range(2, 25):
        rows.append(
            {
                "session_key": 9590,
                "driver_number": 1,
                "target_lap_number": lap,
                "horizon": 1,
                "forecast_as_of_utc": T0 + timedelta(seconds=lap * 82),
                "season": 2024,
                "regulation_era": "2023_2025",
                "circuit_short_name": "Monza",
                "team_id": "RED_BULL",
                "driver_id": "VER",
                "race_progress": lap / 53.0,
                "compound": "MEDIUM",
                "tyre_age_at_lap_start": lap,
                "clean_laps_observed_current_stint": max(lap - 2, 0),
                "clean_laps_observed_race": max(lap - 2, 0),
                "last_clean_lap_age": 1,
                "last_clean_lap_time_s": 82.5 - 0.02 * lap,
                "median_last_3_clean_laps_s": 82.5 - 0.02 * lap,
                "median_stint_clean_laps_s": 82.5 - 0.02 * lap,
                "last_known_track_temp_c": 38.0,
                "observed_target_lap_time_s": 82.48 - 0.02 * lap,
                "target_is_clean_green": True,
            }
        )
    return pd.DataFrame(rows)


def test_baselines_cold_start_and_unseen_circuit_fallback():
    df = _synthetic_train_df()
    e0 = HistoricalCircuitPriorBaseline().fit(df)
    e1 = RecentCleanLapMedianBaseline().fit(df)
    e2 = CompoundRecentMedianBaseline().fit(df)
    e3 = RaceProgressDriftBaseline().fit(df)

    # Unseen circuit & unseen driver cold-start request
    req_unseen = PaceRequest(
        session_key=9999,
        driver_number=99,
        lap_number=2,
        compound="HARD",
        tyre_age_at_lap_start=1,
        track_temperature_c=30.0,
        as_of_utc=T0 + timedelta(days=30),
        circuit_short_name="UnseenTrack",
        driver_id="NEW",
        team_id="NEW_TEAM",
    )
    d0 = e0.predict(req_unseen)
    assert d0.confidence_level == "COLD_START"
    assert d0.std_seconds >= 0.90
    assert d0.p05_seconds < d0.p25_seconds < d0.median_seconds < d0.p75_seconds < d0.p95_seconds

    # Warm request with stint clean laps uses current stint median
    req_warm = PaceRequest(
        session_key=9912,
        driver_number=1,
        lap_number=15,
        compound="HARD",
        tyre_age_at_lap_start=6,
        track_temperature_c=35.0,
        as_of_utc=T0 + timedelta(days=30),
        circuit_short_name="Monza",
        driver_id="VER",
        team_id="RED_BULL",
        median_last_3_clean_laps_s=83.100,
        median_stint_clean_laps_s=81.900,
        clean_laps_observed_current_stint=4,
        clean_laps_observed_race=12,
        last_clean_lap_age=1,
    )
    assert e1.predict(req_warm).expected_seconds == pytest.approx(83.100)
    assert e2.predict(req_warm).expected_seconds == pytest.approx(81.900)
    assert e3.predict(req_warm).expected_seconds < 81.900
