from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from f1ri.features.degradation_features import (
    HistoricalSupportTable,
    basis_late_cliff,
    basis_warm_up,
    row_to_stint_forecast_request,
)
from f1ri.models.contracts import (
    SUPPORT_EXTRAPOLATED,
    SUPPORT_LIMITED,
    SUPPORT_SUPPORTED,
    SUPPORT_UNSUPPORTED,
)


def test_used_tire_set_age_progression_and_basis_functions():
    # Used tire set starting at age 4: a_k = a_start + (k - 1)
    a_start = 4
    horizons = [1, 2, 3, 5, 10]
    ages = [a_start + (k - 1) for k in horizons]
    assert ages == [4, 5, 6, 8, 13]

    # Warm-up basis decays monotonically with tire age
    w = basis_warm_up(np.array([0.0, 1.0, 2.0, 5.0]))
    assert w[0] == 1.0
    assert np.all(np.diff(w) < 0.0)

    # Late-stint hinge quadratic basis is 0 up to knot_age and strictly positive after
    c = basis_late_cliff(np.array([5.0, 15.0, 20.0, 25.0, 30.0]), knot_age=20.0)
    assert list(c[:3]) == [0.0, 0.0, 0.0]
    assert c[4] > c[3] > 0.0


def test_historical_support_classification():
    rows = []
    # Build 6 stints on Sakhir MEDIUM reaching age 22
    for s in range(1, 7):
        for age in range(1, 23):
            rows.append(
                {
                    "session_key": 9472,
                    "stint_id": f"9472_1_{s}",
                    "regulation_era": "2023_2025",
                    "circuit_short_name": "Sakhir",
                    "compound_relative": "MEDIUM",
                    "compound_absolute": "C2",
                    "tyre_age_at_lap_start": age,
                    "is_clean_green_pace_lap": True,
                    "observed_lap_time_s": 95.0,
                }
            )
    # Build 2 stints on Sakhir SOFT reaching age 14 (limited circuit support < 3 stints)
    for s in range(1, 3):
        for age in range(1, 15):
            rows.append(
                {
                    "session_key": 9472,
                    "stint_id": f"9472_1_soft_{s}",
                    "regulation_era": "2023_2025",
                    "circuit_short_name": "Sakhir",
                    "compound_relative": "SOFT",
                    "compound_absolute": "C3",
                    "tyre_age_at_lap_start": age,
                    "is_clean_green_pace_lap": True,
                    "observed_lap_time_s": 94.2,
                }
            )
    df = pd.DataFrame(rows)
    table = HistoricalSupportTable().fit(df)

    # Sakhir MEDIUM at age 15 -> SUPPORTED (>=3 stints, within p90+2)
    status_med, warn_med = table.classify_support(
        regulation_era="2023_2025",
        circuit_short_name="Sakhir",
        compound_relative="MEDIUM",
        compound_absolute="C2",
        projected_tyre_age=15,
    )
    assert status_med == SUPPORT_SUPPORTED
    assert warn_med is None

    # Sakhir MEDIUM at age 28 -> EXTRAPOLATED (exceeds circuit max age 22 + 3)
    status_ext, warn_ext = table.classify_support(
        regulation_era="2023_2025",
        circuit_short_name="Sakhir",
        compound_relative="MEDIUM",
        compound_absolute="C2",
        projected_tyre_age=28,
    )
    assert status_ext == SUPPORT_EXTRAPOLATED
    assert warn_ext is not None

    # Sakhir SOFT at age 10 -> LIMITED_SUPPORT (only 2 stints at circuit)
    status_lim, _ = table.classify_support(
        regulation_era="2023_2025",
        circuit_short_name="Sakhir",
        compound_relative="SOFT",
        compound_absolute="C3",
        projected_tyre_age=10,
    )
    assert status_lim == SUPPORT_LIMITED

    # Implausible tire age or non-dry compound -> UNSUPPORTED
    status_unsup, _ = table.classify_support(
        regulation_era="2023_2025",
        circuit_short_name="Sakhir",
        compound_relative="INTERMEDIATE",
        compound_absolute=None,
        projected_tyre_age=5,
    )
    assert status_unsup == SUPPORT_UNSUPPORTED


def test_row_to_stint_forecast_request_parses_clean_stint_history():
    row = {
        "session_key": 9912,
        "driver_number": 16,
        "cutoff_lap_number": 25,
        "planned_start_lap": 26,
        "requested_horizon": 5,
        "forecast_as_of_utc": datetime(2025, 9, 7, 13, 40, 0, tzinfo=UTC),
        "scenario_type": "CONTINUE_CURRENT_STINT",
        "compound_relative": "HARD",
        "compound_absolute": "C3",
        "tyre_age_at_forecast_start": 10,
        "stint_number": 2,
        "scheduled_race_laps": 53,
        "last_known_track_temp_c": 41.0,
        "last_known_air_temp_c": 27.0,
        "circuit_short_name": "Monza",
        "driver_id": "LEC",
        "team_id": "FERRARI",
        "season": 2025,
        "regulation_era": "2023_2025",
        "clean_laps_observed_current_stint": 3,
        "clean_laps_observed_race": 18,
        "observed_stint_clean_ages_json": "[7, 8, 9]",
        "observed_stint_clean_times_json": "[82.4, 82.35, 82.38]",
        "observed_stint_clean_progress_json": "[0.415, 0.434, 0.453]",
    }
    req = row_to_stint_forecast_request(row)
    assert req.session_key == 9912
    assert req.observed_stint_clean_ages == (7, 8, 9)
    assert req.observed_stint_clean_times_s == (82.4, 82.35, 82.38)
    assert req.observed_stint_clean_progress == (0.415, 0.434, 0.453)
