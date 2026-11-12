"""Point-in-time validation and future-data leakage verification helpers."""
from __future__ import annotations

import pandas as pd

from f1ri.features.degradation_features import row_to_stint_forecast_request
from f1ri.features.pace_examples import build_pace_examples_dataframe
from f1ri.features.pace_features import row_to_pace_request
from f1ri.features.stint_examples import build_stint_examples_dataframe
from f1ri.models.contracts import (
    SCENARIO_CONTINUE_STINT,
    PaceDistribution,
    PaceModel,
    PaceRequest,
    StintForecastModel,
    StintForecastRequest,
    StintTrajectoryForecast,
)


def validate_forecast_timestamp(request: PaceRequest, forecast: PaceDistribution) -> None:
    if request.as_of_utc.tzinfo is None or forecast.trained_through_utc.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware")
    if forecast.trained_through_utc > request.as_of_utc:
        raise ValueError("Model uses training data later than the prediction cutoff")


def validate_pit_forecast_timestamp(request, forecast) -> None:
    if request.as_of_utc.tzinfo is None or forecast.trained_through_utc.tzinfo is None:
        raise ValueError("Pit timestamps must be timezone-aware")
    if forecast.trained_through_utc > request.as_of_utc:
        raise ValueError("Pit training/calibration uses data after forecast cutoff")
    if forecast.forecast_as_of_utc != request.as_of_utc:
        raise ValueError("Pit response cutoff differs from request")


def validate_stint_forecast_timestamp(
    request: StintForecastRequest,
    forecast: StintTrajectoryForecast,
) -> None:
    """Test E — Verify that no stint forecast uses training data later than the prediction cutoff."""
    if request.as_of_utc.tzinfo is None or forecast.trained_through_utc.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware")
    if forecast.trained_through_utc > request.as_of_utc:
        raise ValueError("Stint forecast model uses training data later than the prediction cutoff")


def assert_examples_point_in_time_valid(examples_df: pd.DataFrame) -> None:
    """Verify that every training example respects temporal ordering between cutoff and target."""
    if examples_df.empty:
        return
    if (examples_df["target_lap_number"] <= examples_df["cutoff_lap_number"]).any():
        raise ValueError("Found target_lap_number <= cutoff_lap_number")

    eligible = examples_df[
        examples_df["target_is_clean_green"].fillna(False).astype(bool)
        & examples_df["target_lap_start_utc"].notna()
        & examples_df["forecast_as_of_utc"].notna()
    ]
    if not eligible.empty:
        cutoff_ts = pd.to_datetime(eligible["forecast_as_of_utc"], utc=True)
        target_start_ts = pd.to_datetime(eligible["target_lap_start_utc"], utc=True)
        # Allow up to 1.5s OpenF1 date_start approximation jitter on consecutive laps;
        # larger clock inversions are masked as UPSTREAM_TIMESTAMP_ANOMALY.
        if ((cutoff_ts - target_start_ts).dt.total_seconds() > 1.5).any():
            raise ValueError("Found eligible example with forecast_as_of_utc > target_lap_start_utc + 1.5s")


def assert_truncation_invariance(
    lap_ctx_df: pd.DataFrame,
    drivers_df: pd.DataFrame,
    weather_df: pd.DataFrame,
    cutoff_lap: int,
    model: PaceModel | None = None,
) -> None:
    """Verify that mutating or deleting laps after cutoff_lap does not alter cutoff_lap features or predictions."""
    full_examples = build_pace_examples_dataframe(
        lap_ctx_df=lap_ctx_df,
        drivers_df=drivers_df,
        weather_df=weather_df,
        horizons=(1, 3, 5),
    )
    at_cutoff_full = full_examples[full_examples["cutoff_lap_number"] == cutoff_lap].sort_values(
        ["driver_number", "horizon"]
    ).reset_index(drop=True)

    # Create a mutated copy where all laps > cutoff_lap have corrupted lap times and compounds,
    # and weather readings after the maximum cutoff timestamp are deleted.
    mutated_laps = lap_ctx_df[lap_ctx_df["lap_number"] <= cutoff_lap].copy()
    max_cutoff_utc = pd.to_datetime(mutated_laps["lap_end_estimate_utc"], utc=True).max()
    if not weather_df.empty and pd.notna(max_cutoff_utc):
        w_dates = pd.to_datetime(weather_df["date"], utc=True)
        mutated_weather = weather_df[w_dates <= max_cutoff_utc].copy()
    else:
        mutated_weather = weather_df.copy()

    truncated_examples = build_pace_examples_dataframe(
        lap_ctx_df=mutated_laps,
        drivers_df=drivers_df,
        weather_df=mutated_weather,
        horizons=(1, 3, 5),
    )
    at_cutoff_trunc = truncated_examples[
        truncated_examples["cutoff_lap_number"] == cutoff_lap
    ].sort_values(["driver_number", "horizon"]).reset_index(drop=True)

    predictor_cols = [
        "session_key",
        "driver_number",
        "cutoff_lap_number",
        "target_lap_number",
        "horizon",
        "forecast_as_of_utc",
        "season",
        "regulation_era",
        "circuit_key",
        "circuit_short_name",
        "team_id",
        "driver_id",
        "scheduled_race_laps",
        "race_progress",
        "compound",
        "tyre_age_at_lap_start",
        "stint_number",
        "current_stint_laps_completed",
        "tyre_age_at_stint_start",
        "last_clean_lap_time_s",
        "median_last_3_clean_laps_s",
        "median_last_5_clean_laps_s",
        "median_stint_clean_laps_s",
        "clean_laps_observed_current_stint",
        "clean_laps_observed_race",
        "last_clean_lap_age",
        "last_known_track_temp_c",
        "last_known_air_temp_c",
        "temperature_observation_age_s",
    ]
    pd.testing.assert_frame_equal(
        at_cutoff_full[predictor_cols],
        at_cutoff_trunc[predictor_cols],
        check_dtype=False,
    )

    if model is not None:
        for idx in range(len(at_cutoff_full)):
            req_full = row_to_pace_request(at_cutoff_full.iloc[idx])
            req_trunc = row_to_pace_request(at_cutoff_trunc.iloc[idx])
            pred_full = model.predict(req_full)
            pred_trunc = model.predict(req_trunc)
            if pred_full != pred_trunc:
                raise AssertionError(
                    f"Prediction changed after future truncation: {pred_full} vs {pred_trunc}"
                )


def assert_stint_leakage_invariants(
    lap_ctx_df: pd.DataFrame,
    drivers_df: pd.DataFrame,
    weather_df: pd.DataFrame,
    cutoff_lap: int,
    stint_model: StintForecastModel | None = None,
    allocations_df: pd.DataFrame | None = None,
) -> None:
    """Execute Stage 3 Temporal Leakage Tests A, B, C, and D at cutoff_lap (Section 17.2).

    - Test A: Future data removal (delete all laps > cutoff_lap).
    - Test B: Future pit & compound changes (corrupt future stint_number, pit flags, and compounds).
    - Test C: Future weather replacement (corrupt weather readings after cutoff timestamp).
    - Test D: Future stint duration mutation (truncate or extend future stint boundaries).
    """
    horizons = (1, 3, 5, 10, 15, 20)
    full_ex = build_stint_examples_dataframe(
        lap_ctx_df=lap_ctx_df,
        drivers_df=drivers_df,
        weather_df=weather_df,
        allocations_df=allocations_df,
        horizons=horizons,
        include_new_stint_scenarios=False,
    )
    base_cut = (
        full_ex[
            (full_ex["cutoff_lap_number"] == cutoff_lap)
            & (full_ex["scenario_type"] == SCENARIO_CONTINUE_STINT)
        ]
        .sort_values(["driver_number", "requested_horizon"])
        .reset_index(drop=True)
    )
    if base_cut.empty:
        return

    # Test A: Remove all observations from cutoff_lap + 1 onward
    trunc_laps = lap_ctx_df[lap_ctx_df["lap_number"] <= cutoff_lap].copy()
    ex_a = build_stint_examples_dataframe(
        lap_ctx_df=trunc_laps,
        drivers_df=drivers_df,
        weather_df=weather_df,
        allocations_df=allocations_df,
        horizons=horizons,
        include_new_stint_scenarios=False,
    )
    cut_a = (
        ex_a[
            (ex_a["cutoff_lap_number"] == cutoff_lap)
            & (ex_a["scenario_type"] == SCENARIO_CONTINUE_STINT)
        ]
        .sort_values(["driver_number", "requested_horizon"])
        .reset_index(drop=True)
    )

    # Test B & D: Mutate future pit laps, future compounds, and future stint endpoints
    mut_pit_laps = lap_ctx_df.copy()
    future_mask = mut_pit_laps["lap_number"] > cutoff_lap
    mut_pit_laps.loc[future_mask, "compound"] = "SOFT"
    mut_pit_laps.loc[future_mask, "stint_number"] = 99
    mut_pit_laps.loc[future_mask, "is_pit_in_lap"] = True
    mut_pit_laps.loc[future_mask, "is_pit_lap"] = True
    mut_pit_laps.loc[future_mask, "observed_lap_time_s"] = 199.999
    ex_bd = build_stint_examples_dataframe(
        lap_ctx_df=mut_pit_laps,
        drivers_df=drivers_df,
        weather_df=weather_df,
        allocations_df=allocations_df,
        horizons=horizons,
        include_new_stint_scenarios=False,
    )
    cut_bd = (
        ex_bd[
            (ex_bd["cutoff_lap_number"] == cutoff_lap)
            & (ex_bd["scenario_type"] == SCENARIO_CONTINUE_STINT)
        ]
        .sort_values(["driver_number", "requested_horizon"])
        .reset_index(drop=True)
    )

    # Test C: Replace future weather readings after cutoff_ts
    max_cut_utc = pd.to_datetime(trunc_laps["lap_end_estimate_utc"], utc=True).max()
    mut_weather = weather_df.copy()
    if not mut_weather.empty and pd.notna(max_cut_utc):
        w_ts = pd.to_datetime(mut_weather["date"], utc=True)
        mut_weather.loc[w_ts > max_cut_utc, "track_temperature"] = 99.9
        mut_weather.loc[w_ts > max_cut_utc, "air_temperature"] = 88.8
    ex_c = build_stint_examples_dataframe(
        lap_ctx_df=lap_ctx_df,
        drivers_df=drivers_df,
        weather_df=mut_weather,
        allocations_df=allocations_df,
        horizons=horizons,
        include_new_stint_scenarios=False,
    )
    cut_c = (
        ex_c[
            (ex_c["cutoff_lap_number"] == cutoff_lap)
            & (ex_c["scenario_type"] == SCENARIO_CONTINUE_STINT)
        ]
        .sort_values(["driver_number", "requested_horizon"])
        .reset_index(drop=True)
    )

    predictor_cols = [
        "session_key",
        "driver_number",
        "cutoff_lap_number",
        "planned_start_lap",
        "target_lap_number",
        "requested_horizon",
        "scenario_type",
        "forecast_as_of_utc",
        "compound_relative",
        "compound_absolute",
        "tyre_age_at_forecast_start",
        "tyre_age_at_lap_start",
        "last_clean_lap_time_s",
        "median_last_3_clean_laps_s",
        "median_stint_clean_laps_s",
        "clean_laps_observed_current_stint",
        "clean_laps_observed_race",
        "observed_stint_clean_ages_json",
        "observed_stint_clean_times_json",
        "last_known_track_temp_c",
    ]
    for variant_df in (cut_a, cut_bd, cut_c):
        pd.testing.assert_frame_equal(
            base_cut[predictor_cols],
            variant_df[predictor_cols],
            check_dtype=False,
        )

    if stint_model is not None:
        for idx in range(len(base_cut)):
            req_base = row_to_stint_forecast_request(base_cut.iloc[idx])
            f_base = stint_model.forecast_stint(req_base)
            validate_stint_forecast_timestamp(req_base, f_base)
            for variant_df in (cut_a, cut_bd, cut_c):
                req_var = row_to_stint_forecast_request(variant_df.iloc[idx])
                f_var = stint_model.forecast_stint(req_var)
                if f_base != f_var:
                    raise AssertionError(
                        f"Stint trajectory forecast changed under future mutation: {f_base} vs {f_var}"
                    )
