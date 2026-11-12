from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from f1ri.features.degradation_features import row_to_stint_forecast_request
from f1ri.features.stint_examples import build_stint_examples_dataframe
from f1ri.ingestion.compound_allocations import (
    default_allocations_csv_path,
    load_and_validate_compound_allocations,
    resolve_compound_allocation,
)
from f1ri.models.contracts import SCENARIO_CONTINUE_STINT
from f1ri.models.degradation_hierarchical import HierarchicalTireDegradationModel
from f1ri.models.stint_forecast import PaceModelV2
from f1ri.validation.leakage import (
    assert_stint_leakage_invariants,
    validate_stint_forecast_timestamp,
)


def _build_stint_leakage_fixtures() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    t0 = datetime(2025, 9, 7, 13, 0, 0, tzinfo=UTC)
    rows = []
    for lap in range(1, 31):
        tyre_age = lap - 1
        is_clean = lap > 1
        rows.append(
            {
                "session_key": 9912,
                "meeting_key": 1267,
                "season": 2025,
                "regulation_era": "2023_2025",
                "circuit_key": 39,
                "circuit_short_name": "Monza",
                "driver_number": 16,
                "lap_number": lap,
                "stint_number": 1,
                "compound": "MEDIUM",
                "tyre_age_at_lap_start": tyre_age,
                "lap_start_utc": t0 + timedelta(seconds=(lap - 1) * 82),
                "lap_end_estimate_utc": t0 + timedelta(seconds=lap * 82),
                "observed_lap_time_s": 82.0 + 0.03 * tyre_age - 0.025 * lap,
                "race_total_laps": 53,
                "track_status": "GREEN",
                "is_pit_out_lap": False,
                "is_pit_in_lap": False,
                "is_pit_lap": False,
                "is_clean_green_pace_lap": is_clean,
                "exclusion_reason": None if is_clean else "LAP_1",
            }
        )
    drivers_df = pd.DataFrame(
        [{"driver_number": 16, "name_acronym": "LEC", "team_name": "Ferrari"}]
    )
    weather_df = pd.DataFrame(
        [
            {"date": t0 + timedelta(seconds=k * 160), "track_temperature": 38.0, "air_temperature": 26.0, "rainfall": 0}
            for k in range(20)
        ]
    )
    return pd.DataFrame(rows), drivers_df, weather_df


def _fit_simple_v2(lap_ctx: pd.DataFrame, drivers_df: pd.DataFrame, weather_df: pd.DataFrame) -> PaceModelV2:
    # Fit on an earlier 2024 session so trained_through_utc < 2025 test forecast_as_of_utc
    train_laps = lap_ctx.copy()
    train_laps["session_key"] = 9590
    train_laps["season"] = 2024
    t_2024 = datetime(2024, 9, 1, 13, 0, 0, tzinfo=UTC)
    train_laps["lap_start_utc"] = [
        t_2024 + timedelta(seconds=(int(lnum) - 1) * 82) for lnum in train_laps["lap_number"]
    ]
    train_laps["lap_end_estimate_utc"] = [
        t_2024 + timedelta(seconds=int(lnum) * 82) for lnum in train_laps["lap_number"]
    ]
    w_2024 = weather_df.copy()
    w_2024["date"] = [t_2024 + timedelta(seconds=k * 160) for k in range(len(w_2024))]
    ex_train = build_stint_examples_dataframe(
        lap_ctx_df=train_laps,
        drivers_df=drivers_df,
        weather_df=w_2024,
        horizons=(1,),
    )
    return PaceModelV2(degradation_model=HierarchicalTireDegradationModel()).fit(ex_train)


def test_stint_leakage_tests_a_b_c_d_e():
    lap_ctx, drivers_df, weather_df = _build_stint_leakage_fixtures()
    model = _fit_simple_v2(lap_ctx, drivers_df, weather_df)

    # Execute Tests A, B, C, and D via assert_stint_leakage_invariants at cutoff_lap=12
    assert_stint_leakage_invariants(
        lap_ctx_df=lap_ctx,
        drivers_df=drivers_df,
        weather_df=weather_df,
        cutoff_lap=12,
        stint_model=model,
    )

    # Explicit verification of Test E:
    # 1. Timestamp check raises ValueError if model was trained on data after request.as_of_utc
    ex_test = build_stint_examples_dataframe(
        lap_ctx_df=lap_ctx,
        drivers_df=drivers_df,
        weather_df=weather_df,
        horizons=(5,),
    )
    row = ex_test[
        (ex_test["cutoff_lap_number"] == 12)
        & (ex_test["scenario_type"] == SCENARIO_CONTINUE_STINT)
    ].iloc[0]
    req = row_to_stint_forecast_request(row)
    fc = model.forecast_stint(req)
    validate_stint_forecast_timestamp(req, fc)

    # Mutate request timestamp to precede model.trained_through_utc -> must raise ValueError
    req_leaked = row_to_stint_forecast_request(
        {**row.to_dict(), "forecast_as_of_utc": datetime(2023, 1, 1, 0, 0, 0, tzinfo=UTC)}
    )
    with pytest.raises(ValueError, match="later than the prediction cutoff"):
        validate_stint_forecast_timestamp(req_leaked, fc)

    # 2. Tyre allocation publication-time check blocks future-dated allocation rows
    root = Path(__file__).resolve().parents[1]
    alloc_df = load_and_validate_compound_allocations(default_allocations_csv_path(root))
    blocked = resolve_compound_allocation(
        alloc_df, 1229, "MEDIUM", datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC)
    )
    assert blocked["compound_absolute"] is None
    assert blocked["compound_mapping_source"] == "FUTURE_PUBLICATION_BLOCKED"
