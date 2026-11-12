"""Generate complete future stint trajectories and integrate Pace V2 (Sections 9 & 10)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from f1ri.features.degradation_features import row_to_stint_forecast_request
from f1ri.models.contracts import (
    SCENARIO_CONTINUE_STINT,
    SCENARIO_NEW_FRESH,
    SCENARIO_NEW_USED,
    SUPPORT_EXTRAPOLATED,
    SUPPORT_LIMITED,
    SUPPORT_SUPPORTED,
    SUPPORT_UNSUPPORTED,
    PaceDistribution,
    PaceModel,
    PaceRequest,
    ProjectedLapForecast,
    StintForecastRequest,
    StintTrajectoryForecast,
)
from f1ri.models.degradation import PaceDecomposition, TireDegradationModel
from f1ri.models.degradation_hierarchical import HierarchicalTireDegradationModel
from f1ri.models.degradation_linear import (
    COMPOUNDS,
    AdaptiveTireResetProgressBaseline,
    LinearTireDegradationModel,
)
from f1ri.models.degradation_nonlinear import NonlinearTireDegradationModel
from f1ri.models.pace_baselines import (
    CompoundRecentMedianBaseline,
    HistoricalCircuitPriorBaseline,
)
from f1ri.models.pace_linear import AdditivePaceModel
from f1ri.models.stint_state import BayesianStintStateEstimator, StintLatentStatePosterior
from f1ri.models.trajectory_uncertainty import (
    CorrelatedTrajectorySampler,
    estimate_within_stint_autocorrelation,
)
from f1ri.simulator.contracts import HypotheticalStintScenario

SUPPORT_SEVERITY = {
    SUPPORT_SUPPORTED: 0,
    SUPPORT_LIMITED: 1,
    SUPPORT_EXTRAPOLATED: 2,
    SUPPORT_UNSUPPORTED: 3,
}


def _worst_support_status(statuses: list[str]) -> str:
    if not statuses:
        return SUPPORT_SUPPORTED
    return max(statuses, key=lambda s: SUPPORT_SEVERITY.get(s, 0))


@dataclass
class OpenLoopPaceModelWrapper:
    """Wraps a Stage 2 PaceModel (e.g. frozen Pace V1 or E2 baseline) as an open-loop StintForecastModel.

    Predicts all future laps k = 1..H from the single cutoff `as_of_utc` without observing
    any future laps inside the forecast horizon.
    """

    base_pace_model: PaceModel
    model_version: str = "D0_frozen_pace_v1_open_loop"
    degradation_model_version: str = "stage2_embedded_linear"
    sampler_: CorrelatedTrajectorySampler = field(default_factory=CorrelatedTrajectorySampler)
    independent_uncertainty: bool = True

    def fit(
        self,
        train_df: pd.DataFrame,
        calibration_df: pd.DataFrame | None = None,
    ) -> OpenLoopPaceModelWrapper:
        sub = train_df.copy()
        if "horizon" not in sub.columns and "requested_horizon" in sub.columns:
            sub["horizon"] = sub["requested_horizon"]
        if "compound" not in sub.columns and "compound_relative" in sub.columns:
            sub["compound"] = sub["compound_relative"]

        if isinstance(self.base_pace_model, AdditivePaceModel):
            cal = None
            if calibration_df is not None and not calibration_df.empty:
                cal = calibration_df.copy()
                if "horizon" not in cal.columns and "requested_horizon" in cal.columns:
                    cal["horizon"] = cal["requested_horizon"]
                if "compound" not in cal.columns and "compound_relative" in cal.columns:
                    cal["compound"] = cal["compound_relative"]
            self.base_pace_model.fit(sub, calibration_df=cal)
        elif hasattr(self.base_pace_model, "fit"):
            self.base_pace_model.fit(sub)
        return self

    @property
    def trained_through_utc_(self) -> datetime:
        return getattr(
            self.base_pace_model,
            "trained_through_utc_",
            datetime(2025, 1, 1, tzinfo=UTC),
        )

    def forecast_stint(self, request: StintForecastRequest) -> StintTrajectoryForecast:
        h_laps = max(int(request.horizon_laps), 1)
        sched_laps = max(int(request.scheduled_race_laps or 58), 1)
        is_new_stint = request.scenario_type in (SCENARIO_NEW_FRESH, SCENARIO_NEW_USED)

        expected_times = np.zeros(h_laps, dtype=float)
        proj_ages = np.zeros(h_laps, dtype=int)
        prog_vals = np.zeros(h_laps, dtype=float)
        support_statuses: list[str] = []

        for k in range(1, h_laps + 1):
            t_lap = int(request.planned_start_lap + k - 1)
            t_age = int(request.tyre_age_at_start + k - 1)
            t_prog = float(t_lap) / float(sched_laps)
            step_horizon = int(t_lap - request.cutoff_lap_number)

            # Construct open-loop PaceRequest strictly using cutoff history
            last_clean_age_k = (
                int((request.last_clean_lap_age or 1) + (k - 1))
                if request.last_clean_lap_age is not None
                else step_horizon
            )
            p_req = PaceRequest(
                session_key=request.session_key,
                driver_number=request.driver_number,
                lap_number=t_lap,
                compound=request.compound_relative,
                tyre_age_at_lap_start=t_age,
                track_temperature_c=request.track_temperature_c,
                as_of_utc=request.as_of_utc,
                horizon=max(step_horizon, 1),
                season=request.season,
                regulation_era=request.regulation_era,
                circuit_key=request.circuit_key,
                circuit_short_name=request.circuit_short_name,
                driver_id=request.driver_id,
                team_id=request.team_id,
                race_progress=t_prog,
                stint_number=request.stint_number,
                current_stint_laps_completed=(
                    0 if is_new_stint else request.clean_laps_observed_current_stint
                ),
                tyre_age_at_stint_start=request.tyre_age_at_start,
                last_clean_lap_time_s=request.last_clean_lap_time_s,
                median_last_3_clean_laps_s=request.median_last_3_clean_laps_s,
                median_last_5_clean_laps_s=request.median_last_5_clean_laps_s,
                median_stint_clean_laps_s=(
                    None if is_new_stint else request.median_stint_clean_laps_s
                ),
                clean_laps_observed_current_stint=(
                    0 if is_new_stint else request.clean_laps_observed_current_stint
                ),
                clean_laps_observed_race=request.clean_laps_observed_race,
                last_clean_lap_age=last_clean_age_k,
                air_temperature_c=request.air_temperature_c,
                actual_compound_code=request.compound_absolute,
            )
            dist = self.base_pace_model.predict(p_req)
            expected_times[k - 1] = dist.expected_seconds
            proj_ages[k - 1] = t_age
            prog_vals[k - 1] = t_prog
            support_statuses.append(
                SUPPORT_EXTRAPOLATED if t_age > 32 else SUPPORT_SUPPORTED
            )

        dummy_post = StintLatentStatePosterior(
            offset_mean_s=0.0,
            slope_dev_mean_s_per_lap=0.0,
            offset_std_s=0.30,
            slope_dev_std_s_per_lap=0.015,
            cov_offset_slope=0.0,
            reference_tyre_age=float(request.tyre_age_at_start),
            n_stint_clean_laps=(
                0 if is_new_stint else int(request.clean_laps_observed_current_stint)
            ),
        )
        mc = self.sampler_.sample_trajectories(
            expected_lap_times_s=expected_times,
            projected_tyre_ages=proj_ages,
            support_statuses=support_statuses,
            stint_posterior=dummy_post,
            clean_laps_observed_race=int(request.clean_laps_observed_race),
            clean_laps_observed_current_stint=(
                0 if is_new_stint else int(request.clean_laps_observed_current_stint)
            ),
            n_samples=int(request.n_trajectory_samples),
            random_seed=int(request.random_seed),
            independent_laps_ablation=self.independent_uncertainty,
        )

        lap_forecasts: list[ProjectedLapForecast] = []
        for idx in range(h_laps):
            lap_forecasts.append(
                ProjectedLapForecast(
                    step_index=idx + 1,
                    target_lap_number=int(request.planned_start_lap + idx),
                    projected_tyre_age=int(proj_ages[idx]),
                    race_progress=float(prog_vals[idx]),
                    expected_lap_time_s=float(expected_times[idx]),
                    median_lap_time_s=float(mc["lap_median"][idx]),
                    std_lap_time_s=float(mc["lap_std"][idx]),
                    p25_lap_time_s=float(mc["lap_p25"][idx]),
                    p75_lap_time_s=float(mc["lap_p75"][idx]),
                    p05_lap_time_s=float(mc["lap_p05"][idx]),
                    p95_lap_time_s=float(mc["lap_p95"][idx]),
                    expected_cumulative_time_s=float(mc["cum_expected"][idx]),
                    std_cumulative_time_s=float(mc["cum_std"][idx]),
                    p25_cumulative_time_s=float(mc["cum_p25"][idx]),
                    p75_cumulative_time_s=float(mc["cum_p75"][idx]),
                    p05_cumulative_time_s=float(mc["cum_p05"][idx]),
                    p95_cumulative_time_s=float(mc["cum_p95"][idx]),
                    base_pace_contribution_s=float(expected_times[idx]),
                    race_progression_contribution_s=0.0,
                    tire_degradation_contribution_s=0.0,
                    support_status=support_statuses[idx],
                    extrapolation_warning=None,
                )
            )

        sampled_tuples = (
            tuple(tuple(float(x) for x in row) for row in mc["trajectories"][:50])
            if request.retain_trajectory_samples
            else None
        )

        return StintTrajectoryForecast(
            session_key=request.session_key,
            driver_number=request.driver_number,
            cutoff_lap_number=request.cutoff_lap_number,
            planned_start_lap=request.planned_start_lap,
            horizon_laps=h_laps,
            scenario_type=request.scenario_type,
            compound_relative=request.compound_relative,
            compound_absolute=request.compound_absolute,
            tyre_age_at_start=request.tyre_age_at_start,
            as_of_utc=request.as_of_utc,
            lap_forecasts=tuple(lap_forecasts),
            expected_total_time_s=float(mc["cum_expected"][-1]),
            median_total_time_s=float(mc["cum_median"][-1]),
            std_total_time_s=float(mc["cum_std"][-1]),
            p25_total_time_s=float(mc["cum_p25"][-1]),
            p75_total_time_s=float(mc["cum_p75"][-1]),
            p05_total_time_s=float(mc["cum_p05"][-1]),
            p95_total_time_s=float(mc["cum_p95"][-1]),
            support_status=_worst_support_status(support_statuses),
            extrapolation_warning=None,
            model_version=self.model_version,
            degradation_model_version=self.degradation_model_version,
            trained_through_utc=self.trained_through_utc_,
            random_seed=request.random_seed,
            future_weather_scenario=request.future_weather_scenario,
            sampled_lap_trajectories_s=sampled_tuples,
        )

    def predict(self, request: PaceRequest) -> PaceDistribution:
        return self.base_pace_model.predict(request)


@dataclass
class PaceModelV2:
    """Integrated Stage 3 Pace & Stint Trajectory Model (`PaceV2`).

    Unifies:
    - Component A: Underlying driver/car pace (prior + qualifying/grid adaptation + cross-stint race anchor)
    - Component B: Race progression F_r(l) and environmental conditions W(x)
    - Component C: Tire compound offset C_{r,c}, degradation curve D_{r,c}(a), and Bayesian stint-state u_s(a)

    Guarantees that tire degradation enters the prediction exactly once (zero double-counting).
    Supports both `PaceModel.predict(PaceRequest)` and `StintForecastModel.forecast_stint(StintForecastRequest)`.
    """

    degradation_model: TireDegradationModel = field(
        default_factory=HierarchicalTireDegradationModel
    )
    model_version: str = "PaceV2_D8_correlated"
    use_weekend_qualifying_prior: bool = True
    use_stint_offset_adaptation: bool = True
    use_dynamic_stint_slope: bool = True
    use_correlated_uncertainty: bool = True

    prior_baseline_: HistoricalCircuitPriorBaseline = field(
        default_factory=HistoricalCircuitPriorBaseline
    )
    stint_state_estimator_: BayesianStintStateEstimator = field(
        default_factory=BayesianStintStateEstimator
    )
    trajectory_sampler_: CorrelatedTrajectorySampler = field(
        default_factory=CorrelatedTrajectorySampler
    )
    # Qualifying-to-race pace ratio per circuit for cold-start (0A) improvement
    circuit_qual_to_race_offsets_: dict[str, float] = field(default_factory=dict)
    global_qual_to_race_offset_: float = 5.2
    grid_position_slope_s_: float = 0.065
    mean_train_age_: float = 14.0
    mean_train_prog_: float = 0.50
    trained_through_utc_: datetime = field(default_factory=lambda: datetime(1970, 1, 1, tzinfo=UTC))
    is_fitted_: bool = False

    def fit(
        self,
        train_df: pd.DataFrame,
        calibration_df: pd.DataFrame | None = None,
    ) -> PaceModelV2:
        sub = train_df.copy()
        if "horizon" not in sub.columns and "requested_horizon" in sub.columns:
            sub["horizon"] = sub["requested_horizon"]
        if "compound" not in sub.columns and "compound_relative" in sub.columns:
            sub["compound"] = sub["compound_relative"]

        self.prior_baseline_ = HistoricalCircuitPriorBaseline(shrinkage_k=5.0).fit(sub)
        self.degradation_model.fit(sub)
        self.trained_through_utc_ = self.degradation_model.trained_through_utc_

        clean_h1 = sub[
            sub["target_is_clean_green"].fillna(False).astype(bool)
            & sub["observed_target_lap_time_s"].notna()
            & (sub["horizon"] == 1)
        ].copy()
        if not clean_h1.empty:
            self.mean_train_age_ = float(clean_h1["tyre_age_at_lap_start"].median())
            self.mean_train_prog_ = float(clean_h1["race_progress"].median())

            # Learn qualifying-to-race base pace offset per circuit for 0A cold-start improvement
            if "qualifying_lap_time_s" in clean_h1.columns:
                q_sub = clean_h1[clean_h1["qualifying_lap_time_s"].notna()].copy()
                if not q_sub.empty:
                    # Subtract structural progress & tire effects to isolate underlying base pace
                    struct_vals = np.array(
                        [
                            self._structural_offset(
                                compound_relative=str(r["compound"]),
                                compound_absolute=(
                                    str(r["compound_absolute"])
                                    if pd.notna(r.get("compound_absolute"))
                                    else None
                                ),
                                tyre_age=float(r["tyre_age_at_lap_start"] or 0),
                                race_progress=float(r["race_progress"] or 0.5),
                                track_temp_c=(
                                    float(r["last_known_track_temp_c"])
                                    if pd.notna(r.get("last_known_track_temp_c"))
                                    else None
                                ),
                                circuit_short_name=str(r.get("circuit_short_name") or ""),
                                regulation_era=str(r.get("regulation_era") or "2023_2025"),
                            )
                            for _, r in q_sub.iterrows()
                        ],
                        dtype=float,
                    )
                    q_sub["implied_base_minus_qual"] = (
                        q_sub["observed_target_lap_time_s"].to_numpy(dtype=float)
                        - struct_vals
                        - q_sub["qualifying_lap_time_s"].to_numpy(dtype=float)
                    )
                    self.global_qual_to_race_offset_ = float(
                        q_sub["implied_base_minus_qual"].median()
                    )
                    self.circuit_qual_to_race_offsets_ = {}
                    for circ, grp in q_sub.groupby("circuit_short_name"):
                        if pd.notna(circ) and len(grp) >= 15:
                            n_races = int(grp["session_key"].nunique())
                            w_q = n_races / (n_races + 1.5)
                            self.circuit_qual_to_race_offsets_[str(circ)] = float(
                                w_q * float(grp["implied_base_minus_qual"].median())
                                + (1.0 - w_q) * self.global_qual_to_race_offset_
                            )

        self.stint_state_estimator_ = BayesianStintStateEstimator(
            enable_slope_update=self.use_dynamic_stint_slope
        )
        self.is_fitted_ = True

        # Calibrate trajectory uncertainty sampler on validation (or training) h=1 residuals
        cal_df = (
            calibration_df
            if calibration_df is not None and not calibration_df.empty
            else clean_h1
        )
        if cal_df is not None and not cal_df.empty:
            cal_h1 = cal_df[
                cal_df["target_is_clean_green"].fillna(False).astype(bool)
                & cal_df["observed_target_lap_time_s"].notna()
                & (
                    cal_df.get("requested_horizon", cal_df.get("horizon", 1)) == 1
                )
            ].copy()
            if not cal_h1.empty:
                preds = np.array(
                    [
                        self.decompose_single_lap(row_to_stint_forecast_request(r)).total_expected_lap_time_s
                        for _, r in cal_h1.iterrows()
                    ],
                    dtype=float,
                )
                cal_h1["residual_s"] = (
                    cal_h1["observed_target_lap_time_s"].to_numpy(dtype=float) - preds
                )
                acf = estimate_within_stint_autocorrelation(cal_h1, "residual_s", max_lag=3)
                self.trajectory_sampler_.fit_calibration(
                    cal_h1["residual_s"].to_numpy(dtype=float),
                    calib_ar1_rho=acf.get(1, 0.28),
                )

        return self

    def _structural_offset(
        self,
        *,
        compound_relative: str,
        compound_absolute: str | None,
        tyre_age: float,
        race_progress: float,
        track_temp_c: float | None,
        circuit_short_name: str | None,
        regulation_era: str,
    ) -> float:
        prog_s, _ = self.degradation_model.race_progress_effect(
            race_progress=race_progress,
            track_temperature_c=track_temp_c,
            circuit_short_name=circuit_short_name,
            regulation_era=regulation_era,
        )
        tire_eval = self.degradation_model.evaluate_tire(
            compound_relative=compound_relative,
            tyre_age=tyre_age,
            compound_absolute=compound_absolute,
            circuit_short_name=circuit_short_name,
            regulation_era=regulation_era,
        )
        return float(prog_s + tire_eval.total_tire_effect_s)

    def _prior_base_pace_anchor(self, request: StintForecastRequest) -> float:
        """Compute Component A historical/pre-race base pace anchor mu_{d,r}^{(0)} at (prog=0, MEDIUM, age=0)."""
        p_req = PaceRequest(
            session_key=request.session_key,
            driver_number=request.driver_number,
            lap_number=request.planned_start_lap,
            compound=request.compound_relative,
            tyre_age_at_lap_start=request.tyre_age_at_start,
            track_temperature_c=request.track_temperature_c,
            as_of_utc=request.as_of_utc,
            season=request.season,
            regulation_era=request.regulation_era,
            circuit_key=request.circuit_key,
            circuit_short_name=request.circuit_short_name,
            driver_id=request.driver_id,
            team_id=request.team_id,
            race_progress=self.mean_train_prog_,
        )
        prior_dist = self.prior_baseline_.predict(p_req)
        # Strip prior_baseline_'s compound and progress terms so Component B and Component C enter exactly once
        prior_prog_adj = self.prior_baseline_.progress_slope_ * (self.mean_train_prog_ - 0.5)
        raw_circuit_driver_center = (
            prior_dist.expected_seconds
            - self.prior_baseline_.compound_offsets_.get(request.compound_relative, 0.0)
            - prior_prog_adj
        )
        # Subtract structural offset evaluated at the training sample center (mean_train_prog_, MEDIUM, mean_train_age_)
        ref_struct = self._structural_offset(
            compound_relative="MEDIUM",
            compound_absolute=None,
            tyre_age=self.mean_train_age_,
            race_progress=self.mean_train_prog_,
            track_temp_c=None,
            circuit_short_name=request.circuit_short_name,
            regulation_era=request.regulation_era,
        )
        hist_base_anchor = float(raw_circuit_driver_center - ref_struct)

        if not self.use_weekend_qualifying_prior:
            return hist_base_anchor

        # Weekend qualifying/grid adaptation for cold-start improvement
        circ = str(request.circuit_short_name or "")
        if request.qualifying_lap_time_s is not None and circ in self.circuit_qual_to_race_offsets_:
            qual_anchor = float(
                request.qualifying_lap_time_s + self.circuit_qual_to_race_offsets_[circ]
            )
            # Blend qualifying-implied base pace with historical circuit/team/driver prior
            return float(0.45 * qual_anchor + 0.55 * hist_base_anchor)

        if request.grid_position is not None and 1 <= int(request.grid_position) <= 22:
            grid_adj = self.grid_position_slope_s_ * (float(request.grid_position) - 10.5) * 0.25
            return float(hist_base_anchor + np.clip(grid_adj, -0.35, 0.35))

        return hist_base_anchor

    def _estimate_components_at_cutoff(
        self, request: StintForecastRequest
    ) -> tuple[float, StintLatentStatePosterior]:
        """Return (base_pace_anchor_s, stint_latent_state_posterior) at the forecast cutoff."""
        hist_anchor = self._prior_base_pace_anchor(request)
        sched_laps = max(float(request.scheduled_race_laps or 58), 1.0)
        is_new_stint = (
            request.scenario_type in (SCENARIO_NEW_FRESH, SCENARIO_NEW_USED)
            or int(request.clean_laps_observed_current_stint or 0) == 0
        )

        # Cross-stint race-level base pace anchor from previous stint(s) when available
        race_anchor: float | None = None
        if int(request.clean_laps_observed_race or 0) > 0 and request.median_last_3_clean_laps_s is not None:
            prev_cmpd_rel = (
                request.prev_stint_compound_relative
                if is_new_stint and request.prev_stint_compound_relative in COMPOUNDS
                else request.compound_relative
            )
            prev_cmpd_abs = (
                request.prev_stint_compound_absolute
                if is_new_stint and request.prev_stint_compound_absolute
                else request.compound_absolute
            )
            age_gap_race = float(np.clip(request.last_clean_lap_age or 2.0, 1.0, 12.0))
            lap_prev = max(1.0, float(request.planned_start_lap) - age_gap_race)
            prog_prev = lap_prev / sched_laps
            if is_new_stint and request.prev_stint_end_tyre_age is not None:
                # median_last_3_clean_laps_s reflects the last 3 clean laps of the prior stint (~2 laps before pit-in)
                est_prev_age = float(
                    np.clip(float(request.prev_stint_end_tyre_age) - 2.0, 8.0, 18.0)
                )
            else:
                stint_num = max(int(request.stint_number or 2), 2)
                est_prev_age = float(np.clip(lap_prev / float(stint_num - 1), 10.0, 18.0))

            prev_struct = self._structural_offset(
                compound_relative=prev_cmpd_rel,
                compound_absolute=prev_cmpd_abs,
                tyre_age=est_prev_age,
                race_progress=prog_prev,
                track_temp_c=request.track_temperature_c,
                circuit_short_name=request.circuit_short_name,
                regulation_era=request.regulation_era,
            )
            if is_new_stint:
                # Dampen the fresh-tire structural reset jump slightly (0.68) so long prior stints
                # do not over-extrapolate the lap-time drop on the first clean lap of a new stint (0B).
                cur_start_struct = self._structural_offset(
                    compound_relative=request.compound_relative,
                    compound_absolute=request.compound_absolute,
                    tyre_age=float(request.tyre_age_at_start),
                    race_progress=float(request.planned_start_lap) / sched_laps,
                    track_temp_c=request.track_temperature_c,
                    circuit_short_name=request.circuit_short_name,
                    regulation_era=request.regulation_era,
                )
                damped_prev_struct = cur_start_struct + 0.68 * (prev_struct - cur_start_struct)
                raw_race_anchor = float(float(request.median_last_3_clean_laps_s) - damped_prev_struct)
                race_anchor = float(0.88 * raw_race_anchor + 0.12 * hist_anchor)
            else:
                race_anchor = float(float(request.median_last_3_clean_laps_s) - prev_struct)

        if not self.use_stint_offset_adaptation:
            return hist_anchor, self.stint_state_estimator_.reset_state(
                reference_tyre_age=float(request.tyre_age_at_start)
            )

        if is_new_stint:
            # Tire-change state reset: new tire set has zero stint-state deviation (b_s=0, d_s=0),
            # while persistent car/driver pace carries over from race_anchor when available!
            base_anchor = race_anchor if race_anchor is not None else hist_anchor
            return base_anchor, self.stint_state_estimator_.reset_state(
                reference_tyre_age=float(request.tyre_age_at_start)
            )

        # Continuation of active stint with m >= 1 observed clean laps
        base_anchor = (
            float(0.85 * race_anchor + 0.15 * hist_anchor)
            if race_anchor is not None
            else hist_anchor
        )
        obs_ages = request.observed_stint_clean_ages
        obs_times = request.observed_stint_clean_times_s
        obs_progs = request.observed_stint_clean_progress

        if len(obs_ages) > 0 and len(obs_ages) == len(obs_times):
            progs = (
                obs_progs
                if len(obs_progs) == len(obs_ages)
                else tuple(
                    max(0.05, float(request.cutoff_lap_number - (len(obs_ages) - 1 - i)) / sched_laps)
                    for i in range(len(obs_ages))
                )
            )
            struct_residuals = [
                float(t_val)
                - (
                    base_anchor
                    + self._structural_offset(
                        compound_relative=request.compound_relative,
                        compound_absolute=request.compound_absolute,
                        tyre_age=float(a_val),
                        race_progress=float(p_val),
                        track_temp_c=request.track_temperature_c,
                        circuit_short_name=request.circuit_short_name,
                        regulation_era=request.regulation_era,
                    )
                )
                for a_val, t_val, p_val in zip(obs_ages, obs_times, progs, strict=False)
            ]
            posterior = self.stint_state_estimator_.estimate_posterior(
                observed_ages=obs_ages,
                structural_residuals_s=struct_residuals,
                n_clean_stint=int(request.clean_laps_observed_current_stint),
            )
            return base_anchor, posterior

        # Fallback when only summary median_stint_clean_laps_s is populated (e.g. from PaceRequest)
        n_stint = int(request.clean_laps_observed_current_stint or 0)
        age_gap = float(request.last_clean_lap_age or 1.0)
        window_half = 0.5 * float(min(n_stint, 3) - 1)
        ref_age = max(0.0, float(request.tyre_age_at_start) - age_gap - window_half)
        ref_prog = max(
            0.02,
            (float(request.planned_start_lap) - age_gap - window_half) / sched_laps,
        )
        ref_struct = self._structural_offset(
            compound_relative=request.compound_relative,
            compound_absolute=request.compound_absolute,
            tyre_age=ref_age,
            race_progress=ref_prog,
            track_temp_c=request.track_temperature_c,
            circuit_short_name=request.circuit_short_name,
            regulation_era=request.regulation_era,
        )
        fb_resid = (
            float(request.median_stint_clean_laps_s) - (base_anchor + ref_struct)
            if request.median_stint_clean_laps_s is not None
            else 0.0
        )
        posterior = self.stint_state_estimator_.estimate_posterior(
            observed_ages=(),
            structural_residuals_s=(),
            fallback_recent_residual_s=fb_resid,
            n_clean_stint=n_stint,
            reference_tyre_age=ref_age,
        )
        return base_anchor, posterior

    def decompose_single_lap(
        self,
        request: StintForecastRequest,
        step_index: int = 1,
    ) -> PaceDecomposition:
        """Compute the exact 3-component decomposition (Component A, B, C) for step k of a stint."""
        base_anchor, posterior = self._estimate_components_at_cutoff(request)
        sched_laps = max(float(request.scheduled_race_laps or 58), 1.0)
        t_lap = int(request.planned_start_lap + step_index - 1)
        t_age = float(request.tyre_age_at_start + step_index - 1)
        t_prog = float(t_lap) / sched_laps

        prog_plus_temp_s, temp_s = self.degradation_model.race_progress_effect(
            race_progress=t_prog,
            track_temperature_c=request.track_temperature_c,
            circuit_short_name=request.circuit_short_name,
            regulation_era=request.regulation_era,
        )
        tire_eval = self.degradation_model.evaluate_tire(
            compound_relative=request.compound_relative,
            tyre_age=t_age,
            compound_absolute=request.compound_absolute,
            circuit_short_name=request.circuit_short_name,
            regulation_era=request.regulation_era,
            session_key=request.session_key,
        )
        stint_u_s, _ = posterior.evaluate_at_age(t_age)
        # Partition stint_u_s into level offset b_s (merged into Component A car/stint base pace)
        # and slope deviation d_s * da (merged into Component C tire degradation)
        slope_dev_term = float(
            posterior.slope_dev_mean_s_per_lap * (t_age - posterior.reference_tyre_age)
        )
        comp_a = float(base_anchor + posterior.offset_mean_s)
        comp_b = float(prog_plus_temp_s)
        comp_c = float(tire_eval.total_tire_effect_s + slope_dev_term)

        return PaceDecomposition(
            base_pace_s=comp_a,
            race_progression_s=comp_b,
            tire_contribution_s=comp_c,
            compound_offset_s=tire_eval.compound_offset_s,
            age_degradation_s=float(tire_eval.age_degradation_s + slope_dev_term),
            stint_state_adjustment_s=stint_u_s,
            temp_adjustment_s=temp_s,
        )

    def forecast_stint(self, request: StintForecastRequest) -> StintTrajectoryForecast:
        if not self.is_fitted_:
            raise RuntimeError("Fit PaceModelV2 before calling forecast_stint")

        h_laps = max(int(request.horizon_laps), 1)
        sched_laps = max(float(request.scheduled_race_laps or 58), 1.0)
        base_anchor, posterior = self._estimate_components_at_cutoff(request)

        expected_times = np.zeros(h_laps, dtype=float)
        comp_a_arr = np.zeros(h_laps, dtype=float)
        comp_b_arr = np.zeros(h_laps, dtype=float)
        comp_c_arr = np.zeros(h_laps, dtype=float)
        proj_ages = np.zeros(h_laps, dtype=int)
        prog_vals = np.zeros(h_laps, dtype=float)
        support_statuses: list[str] = []
        warnings: list[str | None] = []
        slope_stds = np.zeros(h_laps, dtype=float)

        for k in range(1, h_laps + 1):
            t_lap = int(request.planned_start_lap + k - 1)
            t_age = int(request.tyre_age_at_start + k - 1)
            t_prog = float(t_lap) / sched_laps

            prog_plus_temp_s, _ = self.degradation_model.race_progress_effect(
                race_progress=t_prog,
                track_temperature_c=request.track_temperature_c,
                circuit_short_name=request.circuit_short_name,
                regulation_era=request.regulation_era,
            )
            tire_eval = self.degradation_model.evaluate_tire(
                compound_relative=request.compound_relative,
                tyre_age=float(t_age),
                compound_absolute=request.compound_absolute,
                circuit_short_name=request.circuit_short_name,
                regulation_era=request.regulation_era,
                session_key=request.session_key,
            )

            slope_dev_term = float(
                posterior.slope_dev_mean_s_per_lap * (float(t_age) - posterior.reference_tyre_age)
            )
            comp_a = float(base_anchor + posterior.offset_mean_s)
            comp_b = float(prog_plus_temp_s)
            comp_c = float(tire_eval.total_tire_effect_s + slope_dev_term)

            idx = k - 1
            comp_a_arr[idx] = comp_a
            comp_b_arr[idx] = comp_b
            comp_c_arr[idx] = comp_c
            expected_times[idx] = comp_a + comp_b + comp_c
            proj_ages[idx] = t_age
            prog_vals[idx] = t_prog
            support_statuses.append(tire_eval.support_status)
            warnings.append(tire_eval.extrapolation_warning)
            slope_stds[idx] = tire_eval.slope_std_s_per_lap

        is_new_stint = request.scenario_type in (SCENARIO_NEW_FRESH, SCENARIO_NEW_USED)
        mc = self.trajectory_sampler_.sample_trajectories(
            expected_lap_times_s=expected_times,
            projected_tyre_ages=proj_ages,
            support_statuses=support_statuses,
            stint_posterior=posterior,
            clean_laps_observed_race=int(request.clean_laps_observed_race),
            clean_laps_observed_current_stint=(
                0 if is_new_stint else int(request.clean_laps_observed_current_stint)
            ),
            model_slope_std_s_per_lap=float(np.mean(slope_stds)),
            n_samples=int(request.n_trajectory_samples),
            random_seed=int(request.random_seed),
            independent_laps_ablation=not self.use_correlated_uncertainty,
        )

        lap_forecasts: list[ProjectedLapForecast] = []
        for idx in range(h_laps):
            lap_forecasts.append(
                ProjectedLapForecast(
                    step_index=idx + 1,
                    target_lap_number=int(request.planned_start_lap + idx),
                    projected_tyre_age=int(proj_ages[idx]),
                    race_progress=float(prog_vals[idx]),
                    expected_lap_time_s=float(expected_times[idx]),
                    median_lap_time_s=float(mc["lap_median"][idx]),
                    std_lap_time_s=float(mc["lap_std"][idx]),
                    p25_lap_time_s=float(mc["lap_p25"][idx]),
                    p75_lap_time_s=float(mc["lap_p75"][idx]),
                    p05_lap_time_s=float(mc["lap_p05"][idx]),
                    p95_lap_time_s=float(mc["lap_p95"][idx]),
                    expected_cumulative_time_s=float(mc["cum_expected"][idx]),
                    std_cumulative_time_s=float(mc["cum_std"][idx]),
                    p25_cumulative_time_s=float(mc["cum_p25"][idx]),
                    p75_cumulative_time_s=float(mc["cum_p75"][idx]),
                    p05_cumulative_time_s=float(mc["cum_p05"][idx]),
                    p95_cumulative_time_s=float(mc["cum_p95"][idx]),
                    base_pace_contribution_s=float(comp_a_arr[idx]),
                    race_progression_contribution_s=float(comp_b_arr[idx]),
                    tire_degradation_contribution_s=float(comp_c_arr[idx]),
                    support_status=support_statuses[idx],
                    extrapolation_warning=warnings[idx],
                )
            )

        worst_status = _worst_support_status(support_statuses)
        overall_warning = next((w for w in reversed(warnings) if w is not None), None)
        sampled_tuples = (
            tuple(tuple(float(x) for x in row) for row in mc["trajectories"][:50])
            if request.retain_trajectory_samples
            else None
        )

        return StintTrajectoryForecast(
            session_key=request.session_key,
            driver_number=request.driver_number,
            cutoff_lap_number=request.cutoff_lap_number,
            planned_start_lap=request.planned_start_lap,
            horizon_laps=h_laps,
            scenario_type=request.scenario_type,
            compound_relative=request.compound_relative,
            compound_absolute=request.compound_absolute,
            tyre_age_at_start=request.tyre_age_at_start,
            as_of_utc=request.as_of_utc,
            lap_forecasts=tuple(lap_forecasts),
            expected_total_time_s=float(mc["cum_expected"][-1]),
            median_total_time_s=float(mc["cum_median"][-1]),
            std_total_time_s=float(mc["cum_std"][-1]),
            p25_total_time_s=float(mc["cum_p25"][-1]),
            p75_total_time_s=float(mc["cum_p75"][-1]),
            p05_total_time_s=float(mc["cum_p05"][-1]),
            p95_total_time_s=float(mc["cum_p95"][-1]),
            support_status=worst_status,
            extrapolation_warning=overall_warning,
            model_version=self.model_version,
            degradation_model_version=self.degradation_model.model_version,
            trained_through_utc=self.trained_through_utc_,
            random_seed=request.random_seed,
            future_weather_scenario=request.future_weather_scenario,
            sampled_lap_trajectories_s=sampled_tuples,
        )

    def predict(self, request: PaceRequest) -> PaceDistribution:
        """Stage 2 PaceModel compatibility: predict the target lap requested by PaceRequest."""
        h = max(int(request.horizon or 1), 1)
        cutoff_lap = int(request.lap_number - h)
        planned_start_lap = cutoff_lap + 1
        start_age = max(0, int(request.tyre_age_at_lap_start - (h - 1)))
        sched_laps = int(
            round(float(request.lap_number) / float(request.race_progress))
            if request.race_progress and request.race_progress > 0.01
            else 58
        )
        stint_req = StintForecastRequest(
            session_key=request.session_key,
            driver_number=request.driver_number,
            cutoff_lap_number=cutoff_lap,
            planned_start_lap=planned_start_lap,
            horizon_laps=h,
            compound_relative=request.compound,
            tyre_age_at_start=start_age,
            as_of_utc=request.as_of_utc,
            scenario_type=(
                SCENARIO_CONTINUE_STINT
                if request.clean_laps_observed_current_stint > 0 or request.tyre_age_at_lap_start > 1
                else SCENARIO_NEW_FRESH
            ),
            compound_absolute=request.actual_compound_code,
            season=request.season,
            regulation_era=request.regulation_era or "2023_2025",
            circuit_key=request.circuit_key,
            circuit_short_name=request.circuit_short_name,
            driver_id=request.driver_id,
            team_id=request.team_id,
            stint_number=request.stint_number or 1,
            scheduled_race_laps=sched_laps,
            last_clean_lap_time_s=request.last_clean_lap_time_s,
            median_last_3_clean_laps_s=request.median_last_3_clean_laps_s,
            median_last_5_clean_laps_s=request.median_last_5_clean_laps_s,
            median_stint_clean_laps_s=request.median_stint_clean_laps_s,
            clean_laps_observed_current_stint=request.clean_laps_observed_current_stint,
            clean_laps_observed_race=request.clean_laps_observed_race,
            last_clean_lap_age=request.last_clean_lap_age,
            track_temperature_c=request.track_temperature_c,
            air_temperature_c=request.air_temperature_c,
            n_trajectory_samples=500,
        )
        traj = self.forecast_stint(stint_req)
        end_lap = traj.lap_forecasts[-1]
        conf = (
            "COLD_START"
            if request.clean_laps_observed_race == 0
            else ("HIGH" if request.clean_laps_observed_current_stint >= 3 else "NORMAL")
        )
        return PaceDistribution(
            expected_seconds=end_lap.expected_lap_time_s,
            std_seconds=end_lap.std_lap_time_s,
            model_version=self.model_version,
            trained_through_utc=self.trained_through_utc_,
            median_seconds=end_lap.median_lap_time_s,
            p25_seconds=end_lap.p25_lap_time_s,
            p75_seconds=end_lap.p75_lap_time_s,
            p05_seconds=end_lap.p05_lap_time_s,
            p95_seconds=end_lap.p95_lap_time_s,
            confidence_level=conf,
            forecast_as_of_utc=request.as_of_utc,
        )

    def diagnostics_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "use_weekend_qualifying_prior": self.use_weekend_qualifying_prior,
            "use_stint_offset_adaptation": self.use_stint_offset_adaptation,
            "use_dynamic_stint_slope": self.use_dynamic_stint_slope,
            "use_correlated_uncertainty": self.use_correlated_uncertainty,
            "ar1_rho": round(self.trajectory_sampler_.ar1_rho, 4),
            "base_lap_noise_std_s": round(self.trajectory_sampler_.base_lap_noise_std_s, 4),
            "warm_stint_pace_std_s": round(self.trajectory_sampler_.warm_stint_pace_std_s, 4),
            "degradation_model": self.degradation_model.diagnostics_dict(),
        }


def compare_stint_scenarios(
    model: PaceModelV2 | OpenLoopPaceModelWrapper,
    base_request: StintForecastRequest,
    scenarios: list[HypotheticalStintScenario],
) -> dict[str, StintTrajectoryForecast]:
    """Forecast multiple hypothetical clean-air tire scenarios from a single race cutoff (Section 10.5)."""
    results: dict[str, StintTrajectoryForecast] = {}
    for scen in scenarios:
        is_new = scen.scenario_type in (SCENARIO_NEW_FRESH, SCENARIO_NEW_USED)
        req = StintForecastRequest(
            session_key=base_request.session_key,
            driver_number=base_request.driver_number,
            cutoff_lap_number=base_request.cutoff_lap_number,
            planned_start_lap=base_request.planned_start_lap,
            horizon_laps=scen.horizon_laps,
            compound_relative=scen.compound_relative,
            tyre_age_at_start=scen.tyre_age_at_start,
            as_of_utc=base_request.as_of_utc,
            scenario_type=scen.scenario_type,
            compound_absolute=scen.compound_absolute,
            meeting_key=base_request.meeting_key,
            season=base_request.season,
            regulation_era=base_request.regulation_era,
            circuit_key=base_request.circuit_key,
            circuit_short_name=base_request.circuit_short_name,
            driver_id=base_request.driver_id,
            team_id=base_request.team_id,
            stint_number=(base_request.stint_number + 1 if is_new else base_request.stint_number),
            scheduled_race_laps=base_request.scheduled_race_laps,
            qualifying_lap_time_s=base_request.qualifying_lap_time_s,
            grid_position=base_request.grid_position,
            last_clean_lap_time_s=base_request.last_clean_lap_time_s,
            median_last_3_clean_laps_s=base_request.median_last_3_clean_laps_s,
            median_last_5_clean_laps_s=base_request.median_last_5_clean_laps_s,
            median_stint_clean_laps_s=(
                None if is_new else base_request.median_stint_clean_laps_s
            ),
            clean_laps_observed_current_stint=(
                0 if is_new else base_request.clean_laps_observed_current_stint
            ),
            clean_laps_observed_race=base_request.clean_laps_observed_race,
            last_clean_lap_age=base_request.last_clean_lap_age,
            observed_stint_clean_ages=(
                () if is_new else base_request.observed_stint_clean_ages
            ),
            observed_stint_clean_times_s=(
                () if is_new else base_request.observed_stint_clean_times_s
            ),
            observed_stint_clean_progress=(
                () if is_new else base_request.observed_stint_clean_progress
            ),
            prev_stint_compound_relative=(
                base_request.compound_relative
                if is_new
                else base_request.prev_stint_compound_relative
            ),
            prev_stint_compound_absolute=(
                base_request.compound_absolute
                if is_new
                else base_request.prev_stint_compound_absolute
            ),
            prev_stint_end_tyre_age=(
                max(0, base_request.tyre_age_at_start - 1)
                if is_new
                else base_request.prev_stint_end_tyre_age
            ),
            track_temperature_c=base_request.track_temperature_c,
            air_temperature_c=base_request.air_temperature_c,
            future_weather_scenario=base_request.future_weather_scenario,
            n_trajectory_samples=base_request.n_trajectory_samples,
            random_seed=base_request.random_seed,
            retain_trajectory_samples=base_request.retain_trajectory_samples,
        )
        results[scen.label] = model.forecast_stint(req)
    return results


def build_stage3_experiment_models(
    ridge_alpha: float = 15.0,
) -> dict[str, OpenLoopPaceModelWrapper | PaceModelV2]:
    """Instantiate the controlled Stage 3 experiment matrix (E2, B4, and D0 through D8)."""
    return {
        "B0_E2_compound_median": OpenLoopPaceModelWrapper(
            base_pace_model=CompoundRecentMedianBaseline(
                model_version="E2_compound_recent_median_v1"
            ),
            model_version="B0_E2_compound_median_open_loop",
            degradation_model_version="none_constant_median",
            independent_uncertainty=True,
        ),
        "B4_adaptive_reset_progress": OpenLoopPaceModelWrapper(
            base_pace_model=AdaptiveTireResetProgressBaseline(
                model_version="B4_adaptive_tire_reset_progress_v1"
            ),
            model_version="B4_adaptive_reset_progress_open_loop",
            degradation_model_version="heuristic_reset_drift",
            independent_uncertainty=True,
        ),
        "D0_frozen_pace_v1": OpenLoopPaceModelWrapper(
            base_pace_model=AdditivePaceModel(
                model_version="PaceV1_E8_calibrated",
                ridge_alpha=ridge_alpha,
                include_compound_age=True,
                include_track_temp=True,
                use_online_adaptation=True,
                use_calibrated_intervals=True,
            ),
            model_version="D0_frozen_pace_v1_open_loop",
            degradation_model_version="stage2_pace_v1",
            independent_uncertainty=True,
        ),
        "D1_linear_age_trend": PaceModelV2(
            degradation_model=LinearTireDegradationModel(
                model_version="D1_linear_age_only",
                ridge_alpha=ridge_alpha,
                joint_race_progress=False,
                include_track_temp=False,
            ),
            model_version="D1_recent_pace_linear_age",
            use_weekend_qualifying_prior=False,
            use_stint_offset_adaptation=True,
            use_dynamic_stint_slope=False,
            use_correlated_uncertainty=False,
        ),
        "D2_joint_linear_progress_age": PaceModelV2(
            degradation_model=LinearTireDegradationModel(
                model_version="D2_joint_linear",
                ridge_alpha=ridge_alpha,
                joint_race_progress=True,
                include_track_temp=True,
            ),
            model_version="D2_joint_linear_progress_age",
            use_weekend_qualifying_prior=False,
            use_stint_offset_adaptation=True,
            use_dynamic_stint_slope=False,
            use_correlated_uncertainty=False,
        ),
        "D3_nonlinear_age": PaceModelV2(
            degradation_model=NonlinearTireDegradationModel(
                model_version="D3_nonlinear_age",
                ridge_alpha=ridge_alpha,
            ),
            model_version="D3_nonlinear_degradation",
            use_weekend_qualifying_prior=False,
            use_stint_offset_adaptation=True,
            use_dynamic_stint_slope=False,
            use_correlated_uncertainty=False,
        ),
        "D4_pirelli_absolute_compound": PaceModelV2(
            degradation_model=HierarchicalTireDegradationModel(
                model_version="D4_pirelli_c_code",
                ridge_alpha=ridge_alpha,
                use_absolute_compounds=True,
                use_circuit_pooling=False,
                use_weekend_adaptation=False,
                use_nonlinear_curve=True,
            ),
            model_version="D4_pirelli_absolute_compound",
            use_weekend_qualifying_prior=False,
            use_stint_offset_adaptation=True,
            use_dynamic_stint_slope=False,
            use_correlated_uncertainty=False,
        ),
        "D5_hierarchical_circuit_compound": PaceModelV2(
            degradation_model=HierarchicalTireDegradationModel(
                model_version="D5_hierarchical_circuit_compound",
                ridge_alpha=ridge_alpha,
                use_absolute_compounds=True,
                use_circuit_pooling=True,
                use_weekend_adaptation=False,
                use_nonlinear_curve=True,
            ),
            model_version="D5_hierarchical_circuit_compound",
            use_weekend_qualifying_prior=False,
            use_stint_offset_adaptation=True,
            use_dynamic_stint_slope=False,
            use_correlated_uncertainty=False,
        ),
        "D6_weekend_adaptation": PaceModelV2(
            degradation_model=HierarchicalTireDegradationModel(
                model_version="D6_weekend_adaptation",
                ridge_alpha=ridge_alpha,
                use_absolute_compounds=True,
                use_circuit_pooling=True,
                use_weekend_adaptation=True,
                use_nonlinear_curve=True,
            ),
            model_version="D6_weekend_adaptation",
            use_weekend_qualifying_prior=True,
            use_stint_offset_adaptation=True,
            use_dynamic_stint_slope=False,
            use_correlated_uncertainty=False,
        ),
        "D7_dynamic_stint_state": PaceModelV2(
            degradation_model=HierarchicalTireDegradationModel(
                model_version="D7_hierarchical_degradation",
                ridge_alpha=ridge_alpha,
                use_absolute_compounds=True,
                use_circuit_pooling=True,
                use_weekend_adaptation=True,
                use_nonlinear_curve=True,
            ),
            model_version="D7_dynamic_stint_state",
            use_weekend_qualifying_prior=True,
            use_stint_offset_adaptation=True,
            use_dynamic_stint_slope=True,
            use_correlated_uncertainty=False,
        ),
        "D8_correlated_trajectory_uncertainty": PaceModelV2(
            degradation_model=HierarchicalTireDegradationModel(
                model_version="DegradationV1_D8_hierarchical",
                ridge_alpha=ridge_alpha,
                use_absolute_compounds=True,
                use_circuit_pooling=True,
                use_weekend_adaptation=True,
                use_nonlinear_curve=True,
            ),
            model_version="PaceV2_D8_correlated",
            use_weekend_qualifying_prior=True,
            use_stint_offset_adaptation=True,
            use_dynamic_stint_slope=True,
            use_correlated_uncertainty=True,
        ),
    }
