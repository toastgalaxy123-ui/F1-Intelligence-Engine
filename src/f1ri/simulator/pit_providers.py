"""Locked Stage 4.1 P1 access for provisional net-time scenarios."""
from __future__ import annotations

import hashlib
import json
import pickle
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from f1ri.models.contracts import PitLossRequest, StintForecastRequest
from f1ri.models.pit_calibration import CalibratedPitLossModel
from f1ri.simulator.pit_execution import PitStopRequest, PitTransitionPlan
from f1ri.simulator.state import TrackState
from f1ri.validation.leakage import validate_pit_forecast_timestamp

P1_ROLE = "SIMPLE_NET_LOSS_FALLBACK_NO_APPROVED_SERVICE_RISK"


@dataclass(frozen=True, slots=True)
class P1PitLossFallback:
    model: CalibratedPitLossModel
    artifact_sha256: str
    selection_sessions: tuple[int, ...]
    selection_data_through: datetime

    def expected_plan(
        self, stop: PitStopRequest, current: StintForecastRequest, *, seed: int = 42,
        samples: int = 2000,
    ) -> PitTransitionPlan:
        if (
            (stop.session_key, stop.driver_number, stop.as_of_utc)
            != (current.session_key, current.driver_number, current.as_of_utc)
            or not current.planned_start_lap <= stop.pit_in_lap < current.scheduled_race_laps
            or current.regulation_era != "2023_2025" or current.season not in (2023, 2024, 2025)
            or not current.circuit_short_name or stop.track_state is not TrackState.GREEN
        ):
            raise ValueError("P1 requires a matching 2023–2025 pre-stop pace scenario")
        if stop.session_key in self.selection_sessions or stop.as_of_utc <= self.selection_data_through:
            raise ValueError("P1 selection data must precede the historical forecast")
        request = PitLossRequest(
            session_key=stop.session_key, driver_number=stop.driver_number,
            circuit_short_name=current.circuit_short_name, circuit_key=current.circuit_key,
            season=current.season, team_id=current.team_id or "UNKNOWN_TEAM",
            as_of_utc=stop.as_of_utc, latest_completed_lap=current.cutoff_lap_number,
            intended_pit_lap=stop.pit_in_lap, old_compound=current.compound_relative,
            old_tyre_age=current.tyre_age_at_start + stop.pit_in_lap - current.planned_start_lap + 1,
            new_compound=stop.replacement.compound,
            new_tyre_initial_age=stop.replacement.initial_age_laps,
            track_status=stop.track_state.value, track_temperature_c=current.track_temperature_c,
            rainfall=False, n_samples=samples, random_seed=seed,
        )
        loss = self.model.predict_pit_loss(request)
        validate_pit_forecast_timestamp(request, loss)
        if (
            loss.support_status == "UNSUPPORTED" or loss.track_status_scenario != "GREEN"
            or not loss.includes_stationary_service or not loss.includes_pit_out_transition
        ):
            raise ValueError("P1 did not provide a supported green-flag net-only forecast")
        return PitTransitionPlan(
            stop, net_loss_s=loss.expected_loss_s, cost_source=P1_ROLE,
            support_status=loss.support_status, model_version=loss.model_version,
            random_stream_id=f"p1-expected:{self.artifact_sha256}:{seed}:{samples}",
        )


def load_locked_p1(project_root: Path, artifact: Path) -> P1PitLossFallback:
    """Read the exact selected artifact, authenticating the bytes before deserializing."""
    report = project_root / "artifacts/reports/stage4_1_stabilization_manifest.json"
    lock = json.loads(report.read_text(encoding="utf-8"))["selection_lock"]
    if lock["selected_model"] != "P1" or lock["selected_role"] != P1_ROLE:
        raise ValueError("The recorded selection is not the provisional P1 fallback")
    if not artifact.is_file():
        raise FileNotFoundError(f"Locked Stage 4.1 P1 model artifact is missing: {artifact}")
    content = artifact.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != lock["model_artifact_sha256"]["P1"]:
        raise ValueError("P1 artifact digest does not match the Stage 4.1 selection")
    config = (project_root / "config/experiments/stage4_1_stabilization.yaml").read_bytes()
    if hashlib.sha256(config).hexdigest() != lock["config_sha256"]:
        raise ValueError("Stage 4.1 experiment config differs from the selection")
    cohort = yaml.safe_load(config)["cohort"]
    selection_sessions = tuple(row["session_key"] for split in ("train", "val")
                               for row in cohort[split])
    selection_data_through = max(datetime.fromisoformat(score["calibrated_through_utc"])
                                 for score in lock["model_scores"].values()
                                 if score["calibrated_through_utc"])
    model = pickle.loads(content)
    if (
        not isinstance(model, CalibratedPitLossModel) or model.base_model.experiment_id != "P1"
        or not model.base_model.is_fitted_ or model.scale != 1
        or model.calibrated_through_utc is not None or model.base_model.components_identified_
    ):
        raise ValueError("Selected artifact is not the frozen simple P1 fallback")
    return P1PitLossFallback(model, digest, selection_sessions, selection_data_through)
