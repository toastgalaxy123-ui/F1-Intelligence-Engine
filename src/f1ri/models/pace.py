"""High-level race-pace model interface for simulation and backtesting (Pace V1 & Pace V2)."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from f1ri.models.contracts import (
    PaceDistribution,
    PaceModel,
    PaceRequest,
    ProjectedLapForecast,
    StintForecastModel,
    StintForecastRequest,
    StintTrajectoryForecast,
)
from f1ri.models.degradation_linear import AdaptiveTireResetProgressBaseline
from f1ri.models.pace_baselines import (
    CompoundRecentMedianBaseline,
    HistoricalCircuitPriorBaseline,
    RaceProgressDriftBaseline,
    RecentCleanLapMedianBaseline,
)
from f1ri.models.pace_linear import AdditivePaceModel
from f1ri.models.stint_forecast import (
    OpenLoopPaceModelWrapper,
    PaceModelV2,
    build_stage3_experiment_models,
    compare_stint_scenarios,
)


@dataclass
class MedianPaceBaseline:
    """A sanity-check baseline fitted on past observations only.

    Kept for Stage 0 test compatibility; use `HistoricalCircuitPriorBaseline`,
    `CompoundRecentMedianBaseline`, `PaceModelV1`, or `PaceModelV2` for point-in-time forecasting.
    """

    group_medians_: dict[tuple[int, str], float] | None = None
    global_median_: float | None = None

    def fit(self, past: pd.DataFrame) -> MedianPaceBaseline:
        required = {"driver_number", "compound", "observed_lap_time_s"}
        if not required.issubset(past.columns):
            raise ValueError(f"Missing features: {required - set(past.columns)}")
        if past.empty:
            raise ValueError("No training data")
        d = past.dropna(subset=["driver_number", "compound", "observed_lap_time_s"])
        if d.empty:
            raise ValueError("No valid lap times")
        self.global_median_ = float(d["observed_lap_time_s"].median())
        grouped = d.groupby(["driver_number", "compound"])["observed_lap_time_s"]
        self.group_medians_ = {
            (int(a), str(b)): float(v) for (a, b), v in grouped.median().items()
        }
        return self

    def predict_seconds(self, driver_number: int, compound: str) -> float:
        if self.group_medians_ is None or self.global_median_ is None:
            raise RuntimeError("Fit this baseline first")
        return self.group_medians_.get((driver_number, compound), self.global_median_)


# Alias frozen Stage 2 model class explicitly
PaceModelV1 = AdditivePaceModel


def build_experiment_models(ridge_alpha: float = 15.0) -> dict[str, PaceModel]:
    """Instantiate the controlled Stage 2 experiment suite E0 through E8."""
    return {
        "E0_historical_prior": HistoricalCircuitPriorBaseline(
            model_version="E0_historical_circuit_prior_v1"
        ),
        "E1_recent_clean_median": RecentCleanLapMedianBaseline(
            model_version="E1_recent_clean_median_v1"
        ),
        "E2_compound_recent_median": CompoundRecentMedianBaseline(
            model_version="E2_compound_recent_median_v1"
        ),
        "E3_race_progress_drift": RaceProgressDriftBaseline(
            model_version="E3_race_progress_drift_v1"
        ),
        "E4_additive_base": AdditivePaceModel(
            model_version="E4_additive_base_v1",
            ridge_alpha=ridge_alpha,
            include_compound_age=False,
            include_track_temp=False,
            use_online_adaptation=False,
            use_calibrated_intervals=False,
        ),
        "E5_additive_compound_age": AdditivePaceModel(
            model_version="E5_additive_compound_age_v1",
            ridge_alpha=ridge_alpha,
            include_compound_age=True,
            include_track_temp=False,
            use_online_adaptation=False,
            use_calibrated_intervals=False,
        ),
        "E6_additive_temp": AdditivePaceModel(
            model_version="E6_additive_temp_v1",
            ridge_alpha=ridge_alpha,
            include_compound_age=True,
            include_track_temp=True,
            use_online_adaptation=False,
            use_calibrated_intervals=False,
        ),
        "E7_pace_v1_online_uncalibrated": AdditivePaceModel(
            model_version="E7_pace_v1_online_uncalibrated",
            ridge_alpha=ridge_alpha,
            include_compound_age=True,
            include_track_temp=True,
            use_online_adaptation=True,
            use_calibrated_intervals=False,
        ),
        "E8_pace_v1_calibrated": AdditivePaceModel(
            model_version="E8_pace_v1_calibrated",
            ridge_alpha=ridge_alpha,
            include_compound_age=True,
            include_track_temp=True,
            use_online_adaptation=True,
            use_calibrated_intervals=True,
        ),
    }


__all__ = [
    "AdaptiveTireResetProgressBaseline",
    "AdditivePaceModel",
    "CompoundRecentMedianBaseline",
    "HistoricalCircuitPriorBaseline",
    "MedianPaceBaseline",
    "OpenLoopPaceModelWrapper",
    "PaceDistribution",
    "PaceModel",
    "PaceModelV1",
    "PaceModelV2",
    "PaceRequest",
    "ProjectedLapForecast",
    "RaceProgressDriftBaseline",
    "RecentCleanLapMedianBaseline",
    "StintForecastModel",
    "StintForecastRequest",
    "StintTrajectoryForecast",
    "build_experiment_models",
    "build_stage3_experiment_models",
    "compare_stint_scenarios",
]
