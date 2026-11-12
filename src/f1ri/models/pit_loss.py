"""Unified prospective pit-loss models and chronological P0–P10 ablations.

Lane duration already includes stationary service. Net loss and lane time are
parallel outputs, never additive costs. Labels are uncertain counterfactuals.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from f1ri.models.contracts import PitLossDistribution, PitLossRequest, PitSharedScenario
from f1ri.models.pit_neutralization import PitNeutralizationModel
from f1ri.models.pit_service import StationaryServiceModel
from f1ri.models.pit_transit import PooledCircuitModel
from f1ri.models.pit_transition import PitTransitionModel
from f1ri.models.pit_uncertainty import sample_shared_pit_scenario, summarize_samples


def observable_training_events(frame: pd.DataFrame, cutoff: datetime) -> pd.DataFrame:
    if cutoff.tzinfo is None:
        raise ValueError("Training cutoff must be timezone-aware")
    if frame.empty:
        return frame.copy()
    if "observation_available_utc" not in frame:
        raise ValueError("Training events require observation availability timestamps")
    dates = pd.to_datetime(frame.observation_available_utc, utc=True)
    selected = frame[dates.notna() & (dates <= cutoff)].copy()
    if selected.pit_event_id.duplicated().any():
        raise ValueError("Duplicate training pit events")
    return selected.sort_values(["observation_available_utc", "pit_event_id"]).reset_index(drop=True)


@dataclass
class PitLossModelV1:
    experiment_id: str = "P8"
    circuit_shrinkage: float = 12.0
    team_shrinkage: float = 20.0
    model_version: str = "PitLossV1"
    reference_version: str = "past-race-centered-ridge-reference-v1"
    service_: StationaryServiceModel = field(default_factory=StationaryServiceModel)
    route_: PooledCircuitModel = field(default_factory=PooledCircuitModel)
    lane_: PooledCircuitModel = field(default_factory=PooledCircuitModel)
    transition_: PitTransitionModel = field(default_factory=PitTransitionModel)
    neutralization_: PitNeutralizationModel = field(default_factory=PitNeutralizationModel)
    trained_through_utc_: datetime | None = None
    training_event_ids_: tuple[str, ...] = ()
    residual_pairs_: np.ndarray = field(default_factory=lambda: np.empty((0, 2)))
    measurement_std_: float = 0.0
    calibration_scale_: float = 1.0
    calibration_events_: int = 0
    is_fitted_: bool = False

    def fit(self, events: pd.DataFrame, *, training_cutoff_utc: datetime,
            neutralization_controls: pd.DataFrame | None = None) -> PitLossModelV1:
        self.is_fitted_ = False
        self.calibration_scale_, self.calibration_events_ = 1.0, 0
        frame = observable_training_events(events, training_cutoff_utc)
        routine = frame[frame.pit_event_type == "ROUTINE_TIRE_CHANGE"].copy()
        direct = routine[routine.eligible_direct_lane.fillna(False)].copy()
        green = routine[routine.eligible_primary_label.fillna(False)
                        & routine.transition_loss_s.notna()].copy()
        if green.empty or direct.empty:
            raise ValueError("PitLossV1 requires measured lane durations and reconstructed green labels")
        idx = self.experiment_id
        if idx not in {f"P{i}" for i in range(11)} | {f"N{i}" for i in range(4)}:
            raise ValueError(f"Unknown pit experiment: {idx}")
        use_components = idx not in {"P0", "P1", "P3"}
        use_teams = idx not in {"P0", "P1", "P2", "P3", "P4"}
        tail = idx not in {"P0", "P1", "P2", "P3", "P4", "P5"}
        self.service_ = StationaryServiceModel(
            team_shrinkage=self.team_shrinkage if use_teams else 1e12, allow_tail=tail,
        ).fit(direct)
        self.components_identified_ = use_components and self.service_.available
        selected = green.copy()
        selected["_service"] = [self.service_.expected(str(t)) or 0 for t in selected.team_id]
        measured = pd.to_numeric(selected.stop_duration_s, errors="coerce")
        # A latent expectation aids fitting; observed service stays NULL and is
        # never scored as if imputed values were measurements.
        selected["_service_for_fit"] = measured.fillna(selected._service) if self.components_identified_ else 0.0
        selected["_route_target"] = selected.transition_loss_s-selected._service_for_fit
        direct["_moving_lane_target"] = pd.to_numeric(direct.lane_duration_s)
        if self.components_identified_:
            direct["_moving_lane_target"] -= pd.to_numeric(direct.stop_duration_s).fillna(pd.Series(
                [self.service_.expected(str(t)) or 0 for t in direct.team_id], index=direct.index))
        shrink = 1e12 if idx == "P0" else (0.0 if idx in {"P1", "P2"} else self.circuit_shrinkage)
        self.route_ = PooledCircuitModel(shrinkage=shrink).fit(selected, "_route_target", weighted=idx not in {"P0", "P1", "P2"})
        self.lane_ = PooledCircuitModel(shrinkage=shrink).fit(direct, "_moving_lane_target")
        route_means = np.array([self.route_.predict(str(c), int(s)) for c, s in zip(selected.circuit_short_name, selected.season)])
        residual = selected.transition_loss_s.to_numpy(float)-route_means-selected._service_for_fit.to_numpy(float)
        self.transition_ = PitTransitionModel()
        if idx in {"P7", "P8", "P9", "P10", "N0", "N1", "N2", "N3"}:
            self.transition_.fit(selected, residual)
            residual -= np.array([self.transition_.predict(str(c)) for c in selected.new_compound])
        lane_means = np.array([self.lane_.predict(str(c), int(s)) for c, s in zip(selected.circuit_short_name, selected.season)])
        observed_moving = selected.lane_duration_s.to_numpy(float)-selected._service_for_fit.to_numpy(float)
        # When service is modeled separately, estimate joint residual variation
        # from measured-service rows. Otherwise missing-service residuals already
        # contain service noise and sampling service again would duplicate it.
        residual_mask = measured.notna().to_numpy() if self.components_identified_ else np.ones(len(selected), dtype=bool)
        self.residual_pairs_ = np.column_stack((residual, observed_moving-lane_means))[residual_mask]
        if not len(self.residual_pairs_):
            raise ValueError("Component model requires at least one labeled measured-service event")
        label_std = selected.loc[residual_mask, "label_std_s"].fillna(3).to_numpy(float)
        self.measurement_std_ = float(np.sqrt(np.median(label_std**2)))
        residual_variance = float(np.var(residual[residual_mask]))
        self.intrinsic_fraction_ = float(np.sqrt(max(residual_variance-np.mean(label_std**2), .25)
                                                 / max(residual_variance, .25)))
        self.trained_through_utc_ = pd.to_datetime(frame.observation_available_utc, utc=True).max().to_pydatetime()
        self.training_event_ids_ = tuple(frame.pit_event_id.astype(str))
        controls = neutralization_controls if neutralization_controls is not None else pd.DataFrame()
        if not controls.empty:
            controls = controls[pd.to_datetime(controls.observation_available_utc, utc=True) <= training_cutoff_utc]
        self.neutralization_ = PitNeutralizationModel().fit(controls)
        self.is_fitted_ = True
        return self

    def calibrate(self, validation: pd.DataFrame) -> PitLossModelV1:
        """Validation-only interval scale. Never fit on final test residuals."""
        if validation.empty:
            return self
        if self.trained_through_utc_ is None:
            raise ValueError("Fit before calibration")
        if (pd.to_datetime(validation.forecast_as_of_utc, utc=True) <= self.trained_through_utc_).any():
            raise ValueError("Calibration must follow the training window")
        from f1ri.features.pit_context import request_from_json
        ratios = []
        for row in validation.to_dict("records"):
            if pd.isna(row.get("target_transition_loss_s")) or not row.get("eligible_primary_label"):
                continue
            forecast = self.predict_pit_loss(request_from_json(row["request_json"]))
            width = max((forecast.p95_loss_s-forecast.p05_loss_s)/2, .1)
            ratios.append(abs(row["target_transition_loss_s"]-forecast.median_loss_s)/width)
        if len(ratios) >= 10:
            self.calibration_scale_ = max(1.0, float(np.quantile(ratios, .9)))
            self.calibration_events_ = len(ratios)
            self.trained_through_utc_ = pd.to_datetime(validation.observation_available_utc, utc=True).max().to_pydatetime()
        return self

    def predict_pit_loss(
        self, request: PitLossRequest, shared_scenario: PitSharedScenario | None = None,
    ) -> PitLossDistribution:
        if not self.is_fitted_ or self.trained_through_utc_ is None:
            raise ValueError("PitLossV1 is not fitted")
        if self.trained_through_utc_ > request.as_of_utc:
            raise ValueError("Model training/calibration extends beyond forecast cutoff")
        rng = np.random.default_rng(request.random_seed)
        n, circuit = request.n_samples, request.circuit_short_name
        shared = shared_scenario or sample_shared_pit_scenario(circuit, n, request.random_seed+7919)
        if (shared.circuit_short_name != circuit or len(shared.route_standard_normal) != n
                or len(shared.pace_reference_standard_normal) != n):
            raise ValueError("Shared pit scenario must match circuit and sample count")
        count = self.route_.counts_.get(circuit, 0)
        support = "SUPPORTED" if count >= 20 else ("LIMITED_SUPPORT" if count else "EXTRAPOLATED")
        assumptions = ["Net penalty uses equivalent old/new normal racing laps; includes pit-out effects",
                       "Lane duration includes stationary time and is not an additional penalty",
                       "Component attribution is statistical, not individually measured physical phases"]
        if request.layout_version is None or request.pit_speed_limit_kph is None:
            assumptions.append("Unverified pit layout/speed limit; circuit historical behavior only")
        if request.season >= 2026 and 1 not in self.route_.era_offsets_:
            support = "EXTRAPOLATED"
            assumptions.append("Uncalibrated regulation-era transfer")
        if request.stop_type != "ROUTINE_TIRE_CHANGE" or request.pit_lane_open is False or request.race_control_constraints:
            support = "UNSUPPORTED"
            assumptions.append("Exceptional service, closed lane or race-control constraints require a separate model/legality check")
        service = self.service_.sample(request.team_id, n, rng) if self.components_identified_ else None
        specified = request.observed_service_duration_s
        if specified is None:
            specified = request.hypothetical_service_duration_s
        if specified is not None:
            if self.components_identified_:
                service = np.full(n, specified)
                assumptions.append("Explicit service scenario assumes unchanged route boundaries")
            else:
                support = "UNSUPPORTED"
                assumptions.append("Cannot condition service separately without measured service training data")
        service_values = service if service is not None else np.zeros(n)
        draws = self.residual_pairs_[rng.integers(0, len(self.residual_pairs_), size=n)]
        route = self.route_.predict(circuit, request.season) + np.array(shared.route_standard_normal)*self.route_.parameter_std(circuit)
        route = route + self.calibration_scale_*self.intrinsic_fraction_*draws[:, 0]
        transition_mean = self.transition_.predict(request.new_compound)
        transition = np.full(n, transition_mean)
        reference_error = np.array(shared.pace_reference_standard_normal)*self.measurement_std_*self.calibration_scale_
        adjustment, adjustment_std, neutral_support, neutral_assumptions = self.neutralization_.adjustment(request)
        assumptions.extend(neutral_assumptions)
        if neutral_support == "UNSUPPORTED":
            support = "UNSUPPORTED"
        neutral = adjustment+rng.normal(0, adjustment_std, n)
        penalty = request.known_penalty_s
        # Counterfactual loss = observed transition - normal reference. A higher
        # normal-pace reference reduces the inferred penalty, giving a NEGATIVE
        # shared reference contribution. Matching pace samples contain +error.
        losses = route+transition+service_values-reference_error+neutral+penalty
        moving = (self.lane_.predict(circuit, request.season) + draws[:, 1]
                  + np.array(shared.route_standard_normal)*self.lane_.parameter_std(circuit))
        lanes = np.maximum(moving+service_values+penalty, service_values+penalty)
        if support == "UNSUPPORTED" and request.track_status == "GREEN":
            extra = rng.normal(0, 10, n)
            losses += extra
            route += extra
        delay_p = self.service_.delay_probability(request.team_id) if self.service_.available else None
        if specified is not None and self.components_identified_:
            delay_p = float(specified > self.service_.slow_stop_threshold_s)
        retain = request.retain_samples
        return PitLossDistribution(
            **summarize_samples(losses), expected_lane_duration_s=float(np.mean(lanes)),
            expected_stationary_duration_s=float(np.mean(service_values)+penalty) if service is not None else None,
            expected_route_loss_s=float(np.mean(route+neutral)),
            expected_transition_loss_s=float(transition_mean) if self.transition_.identified_ else None,
            delay_probability=delay_p, track_status_scenario="MIXED" if request.future_status_scenario else request.track_status,
            support_status=support, confidence_level="LOW" if support != "SUPPORTED" else "NORMAL",
            n_circuit_events=count, model_version=f"{self.model_version}:{self.experiment_id}",
            pace_reference_version=self.reference_version, trained_through_utc=self.trained_through_utc_,
            forecast_as_of_utc=request.as_of_utc, assumptions=tuple(assumptions),
            exclusions=("Traffic and rejoin positions", "Safety Car queue simulation", "Future neutralization hazards",
                        "Reference uncertainty enters pit with negative sign and matching pace with positive sign"),
            sampled_losses_s=tuple(losses) if retain else None,
            sampled_lane_durations_s=tuple(lanes) if retain else None,
            sampled_stationary_durations_s=tuple(service_values+penalty) if retain and service is not None else None,
            sampled_route_losses_s=tuple(route+neutral) if retain else None,
            sampled_transition_losses_s=tuple(transition) if retain else None,
            sampled_reference_errors_s=tuple(reference_error) if retain else None,
            remaining_loss_s=float(np.mean(losses)-request.observed_service_duration_s)
            if request.observed_service_duration_s is not None else None,
        )


def build_stage4_experiment_models(**kwargs) -> dict[str, PitLossModelV1]:
    return {name: PitLossModelV1(experiment_id=name, **kwargs)
            for name in [*(f"P{i}" for i in range(11)), *(f"N{i}" for i in range(4))]}
