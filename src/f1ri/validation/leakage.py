"""Point-in-time validation and future-data leakage verification helpers."""
from __future__ import annotations

import pandas as pd

from f1ri.features.pace_examples import build_pace_examples_dataframe
from f1ri.features.pace_features import row_to_pace_request
from f1ri.models.contracts import PaceDistribution, PaceModel, PaceRequest


def validate_forecast_timestamp(request: PaceRequest, forecast: PaceDistribution) -> None:
    if request.as_of_utc.tzinfo is None or forecast.trained_through_utc.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware")
    if forecast.trained_through_utc > request.as_of_utc:
        raise ValueError("Model uses training data later than the prediction cutoff")


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
