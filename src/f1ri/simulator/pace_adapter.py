"""Read-only integration with the frozen Stage 3 Pace V2 trajectory interface."""
from __future__ import annotations

import hashlib
import json
import pickle
from copy import copy
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import numpy as np

from f1ri.features.stint_examples import DRY_COMPOUNDS
from f1ri.models.contracts import (
    SCENARIO_CONTINUE_STINT,
    SCENARIO_NEW_FRESH,
    SCENARIO_NEW_USED,
    SUPPORT_UNSUPPORTED,
    StintForecastRequest,
    StintTrajectoryForecast,
)
from f1ri.models.stint_forecast import PaceModelV2
from f1ri.models.trajectory_uncertainty import CorrelatedTrajectorySampler
from f1ri.validation.leakage import validate_stint_forecast_timestamp

_MANIFEST = "config/experiments/stage5_s1_d8_manifest.json"


class UnsupportedPaceForecast(ValueError):
    """Pace V2 has insufficient support for this driver's request."""


@dataclass(frozen=True, slots=True)
class PaceBundle:
    model: PaceModelV2
    artifact_sha256: str
    trained_through_utc: datetime
    calibrated_through_utc: datetime
    training_sessions: tuple[int, ...]
    calibration_sessions: tuple[int, ...]
    regulation_era: str

    def validate_cutoff(self, request: StintForecastRequest) -> None:
        if request.as_of_utc.utcoffset() is None:
            raise ValueError("Pace requests require a timezone-aware cutoff")
        if (
            request.session_key in self.training_sessions + self.calibration_sessions
            or request.as_of_utc <= max(self.trained_through_utc, self.calibrated_through_utc)
        ):
            raise ValueError("Pace V2 training and calibration must precede the forecast cutoff")
        if request.regulation_era != self.regulation_era:
            raise ValueError(f"Pace V2 artifact supports {self.regulation_era}")


def load_pace_bundle(project_root: Path, stage3_root: Path) -> PaceBundle:
    """Load only the locally trusted pickle whose digest is pinned by the project."""
    manifest = json.loads((project_root / _MANIFEST).read_text(encoding="utf-8"))
    for name, expected in {
        "config/experiments/stage3_degradation_v1.yaml": manifest["stage3_config_sha256"],
        "data/raw/reference/pirelli_allocations.csv": manifest["compound_mapping_sha256"],
    }.items():
        if hashlib.sha256((project_root / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Frozen Stage 3 project dependency differs: {name}")
    model_path = manifest["model_path"]
    model_bytes = None
    for name, expected in manifest["files_sha256"].items():
        path = stage3_root / name
        if not path.is_file():
            raise FileNotFoundError(f"Frozen Stage 3 bundle is incomplete: {path}")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError(f"Frozen Stage 3 artifact digest differs: {path}")
        if name == model_path:
            model_bytes = content
    if model_bytes is None:
        raise ValueError("Frozen model digest is missing")
    model = pickle.loads(model_bytes)
    trained = datetime.fromisoformat(manifest["trained_through_utc"])
    calibrated = datetime.fromisoformat(manifest["calibrated_through_utc"])
    if (
        not isinstance(model, PaceModelV2) or model.model_version != manifest["model_version"]
        or model.trained_through_utc_ != trained or not model.is_fitted_
        or not model.use_correlated_uncertainty or not model.trajectory_sampler_.is_calibrated_
    ):
        raise ValueError("Saved artifact does not match the fitted, calibrated Pace V2 model")
    return PaceBundle(
        model, manifest["files_sha256"][model_path], trained, calibrated,
        tuple(manifest["train_sessions"]), tuple(manifest["calibration_sessions"]),
        manifest["regulation_era"],
    )


@dataclass(frozen=True, slots=True)
class PaceTrajectoryBatch:
    forecast: StintTrajectoryForecast
    lap_samples_s: np.ndarray
    cumulative_samples_s: np.ndarray
    artifact_sha256: str
    random_stream_id: str
    circuit_seen_in_training: bool


class _SampleCapture:
    """Keep the full draw from the original sampler; the public forecast retains up to 50."""

    def __init__(self, sampler: CorrelatedTrajectorySampler) -> None:
        self.sampler = sampler
        self.samples: dict[str, np.ndarray] | None = None

    def sample_trajectories(self, *args, **kwargs) -> dict[str, np.ndarray]:
        if self.samples is not None:
            raise RuntimeError("Expected one joint draw per Pace V2 request")
        self.samples = self.sampler.sample_trajectories(*args, **kwargs)
        return self.samples


def _trajectory(bundle: PaceBundle, request: StintForecastRequest) -> PaceTrajectoryBatch:
    request = replace(request, retain_trajectory_samples=True)
    model = copy(bundle.model)
    capture = _SampleCapture(model.trajectory_sampler_)
    model.trajectory_sampler_ = capture
    forecast = model.forecast_stint(request)
    validate_stint_forecast_timestamp(request, forecast)
    assert capture.samples is not None
    lap = capture.samples["trajectories"]
    cumulative = capture.samples["cum_trajectories"]
    if (
        lap.shape != (request.n_trajectory_samples, request.horizon_laps)
        or not np.isfinite(lap).all() or (lap <= 0).any()
        or not np.isfinite(cumulative).all()
    ):
        raise ValueError("Invalid Pace V2 trajectory samples")
    lap.setflags(write=False)
    cumulative.setflags(write=False)
    return PaceTrajectoryBatch(
        forecast, lap, cumulative, bundle.artifact_sha256,
        f"d8:{request.session_key}:{request.driver_number}:{request.stint_number}:"
        f"{request.as_of_utc.isoformat()}:{request.random_seed}",
        request.circuit_short_name in bundle.model.prior_baseline_.circuit_medians_,
    )


def forecast_pace(bundle: PaceBundle, request: StintForecastRequest) -> PaceTrajectoryBatch:
    """Produce unchanged Pace V2 draws for one point-in-time continuation request."""
    bundle.validate_cutoff(request)
    if (
        request.scenario_type != SCENARIO_CONTINUE_STINT
        or request.compound_relative not in DRY_COMPOUNDS or request.tyre_age_at_start < 0
        or request.planned_start_lap != request.cutoff_lap_number + 1
        or not 1 <= request.horizon_laps <= 20
        or request.planned_start_lap + request.horizon_laps - 1 > request.scheduled_race_laps
        or request.n_trajectory_samples < 2 or request.random_seed < 0
    ):
        raise ValueError("Invalid S1 continuation request")
    batch = _trajectory(bundle, request)
    if batch.forecast.support_status == SUPPORT_UNSUPPORTED:
        raise UnsupportedPaceForecast("Pace V2 returned an unsupported continuation forecast")
    return batch


def forecast_new_stint(bundle: PaceBundle, request: StintForecastRequest) -> PaceTrajectoryBatch:
    """Use D8's original new-stint path, with an explicit unseen-circuit support gate."""
    bundle.validate_cutoff(request)
    age = request.tyre_age_at_start
    scenario = SCENARIO_NEW_USED if age else SCENARIO_NEW_FRESH
    if (
        request.scenario_type != scenario or request.compound_relative not in DRY_COMPOUNDS
        or age < 0 or request.planned_start_lap <= request.cutoff_lap_number
        or not 1 <= request.horizon_laps <= 20
        or request.planned_start_lap + request.horizon_laps - 1 > request.scheduled_race_laps
        or request.n_trajectory_samples < 2 or request.random_seed < 0
        or request.clean_laps_observed_current_stint or request.median_stint_clean_laps_s is not None
        or request.observed_stint_clean_times_s or request.observed_stint_clean_ages
        or request.observed_stint_clean_progress
    ):
        raise ValueError("Invalid S2 replacement-tire request")
    if (
        request.circuit_short_name not in bundle.model.prior_baseline_.circuit_medians_
        and (request.clean_laps_observed_race <= 0 or request.median_last_3_clean_laps_s is None)
    ):
        raise UnsupportedPaceForecast("Unseen-circuit new stint needs observed clean race pace")
    batch = _trajectory(bundle, request)
    if batch.forecast.support_status == SUPPORT_UNSUPPORTED:
        raise UnsupportedPaceForecast("Pace V2 returned an unsupported replacement-tire forecast")
    return batch


def reference_parity(bundle: PaceBundle, fixture_path: Path) -> list[dict]:
    """Check exact inputs from the Stage 3 archive, including calibration-race fixtures."""
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if fixture["model_sha256"] != bundle.artifact_sha256:
        raise ValueError("Stage 3 fixtures refer to a different model")
    results = []
    for case in fixture["cases"]:
        data = case["request"]
        serialized = json.dumps(data, sort_keys=True, allow_nan=False).encode()
        if hashlib.sha256(serialized).hexdigest() != case["input_sha256"]:
            raise ValueError("Stage 3 reference inputs differ from the frozen fixture")
        fields = {key: tuple(value) if isinstance(value, list) else value
                  for key, value in data.items()}
        fields["as_of_utc"] = datetime.fromisoformat(data["as_of_utc"])
        request = StintForecastRequest(**fields)
        batch = _trajectory(bundle, request)
        laps = batch.forecast.lap_forecasts
        means = [lap.expected_lap_time_s for lap in laps]
        intervals = [[lap.p05_cumulative_time_s, lap.p25_cumulative_time_s,
                      lap.p75_cumulative_time_s, lap.p95_cumulative_time_s] for lap in laps]
        error = max(
            float(np.max(np.abs(np.asarray(means) - case["expected_laps_s"]))),
            float(np.max(np.abs(np.asarray(intervals) - case["cumulative_intervals_s"]))),
            float(np.max(np.abs(batch.lap_samples_s[:3] - case["first_three_trajectories_s"]))),
        )
        if error > 1e-9:
            raise ValueError(f"Stage 3 reference trajectory differs by {error:.3g} s")
        results.append({"session_key": request.session_key, "driver_number": request.driver_number,
                        "cutoff_lap": request.cutoff_lap_number, "max_abs_delta_s": error,
                        "calibration_race": request.session_key in bundle.calibration_sessions,
                        "cumulative_mean_s": np.cumsum(means).tolist()})
    return results
