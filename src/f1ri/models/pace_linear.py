"""Regularized additive race-pace models (E4 through E8)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from f1ri.features.pace_features import row_to_pace_request
from f1ri.models.contracts import PaceDistribution, PaceRequest
from f1ri.models.pace_baselines import HistoricalCircuitPriorBaseline
from f1ri.models.pace_calibration import ResidualIntervalCalibrator

COMPOUNDS = ("SOFT", "MEDIUM", "HARD")


@dataclass
class AdditivePaceModel:
    """Regularized additive pace model decomposing base pace, race progress, tire age, and weather.

    Supports controlled ablation across experiments E4 through E8:
    - E4: Base additive model (circuit + team/era + driver + compound + race_progress), no tire-age slope, no online adaptation.
    - E5: E4 + compound-specific tire-age degradation slopes.
    - E6: E5 + track-temperature effect.
    - E7: E6 + online empirical-Bayes race/stint pace adaptation.
    - E8: E7 + calibrated residual uncertainty intervals.
    """

    model_version: str = "PaceV1_E8_calibrated"
    ridge_alpha: float = 15.0
    include_compound_age: bool = True
    include_quadratic_age: bool = False
    include_track_temp: bool = True
    use_online_adaptation: bool = True
    use_calibrated_intervals: bool = True
    online_race_shrinkage_k: float = 2.0
    online_stint_shrinkage_k: float = 0.6
    min_std_seconds: float = 0.18

    # Fitted parameters
    prior_baseline_: HistoricalCircuitPriorBaseline = field(
        default_factory=HistoricalCircuitPriorBaseline
    )
    calibrator_: ResidualIntervalCalibrator = field(
        default_factory=ResidualIntervalCalibrator
    )
    progress_slope_: float = -1.6
    compound_offsets_: dict[str, float] = field(default_factory=dict)
    compound_age_slopes_: dict[str, float] = field(default_factory=dict)
    compound_age_quad_slopes_: dict[str, float] = field(default_factory=dict)
    within_stint_drift_: dict[str, float] = field(default_factory=dict)
    mean_train_age_: float = 14.5
    mean_train_prog_: float = 0.52
    temp_slope_: float = 0.0
    circuit_mean_temps_: dict[str, float] = field(default_factory=dict)
    global_mean_temp_: float = 33.0
    uncalibrated_std_: float = 0.35
    trained_through_utc_: datetime = field(default_factory=lambda: datetime(1970, 1, 1, tzinfo=UTC))
    is_fitted_: bool = False

    def fit(
        self,
        train_df: pd.DataFrame,
        calibration_df: pd.DataFrame | None = None,
    ) -> AdditivePaceModel:
        clean = train_df[
            train_df["target_is_clean_green"].fillna(False).astype(bool)
            & train_df["observed_target_lap_time_s"].notna()
            & (train_df["horizon"] == 1)
        ].copy()
        if clean.empty:
            raise ValueError("No eligible clean green rows to fit AdditivePaceModel")

        self.prior_baseline_ = HistoricalCircuitPriorBaseline(shrinkage_k=5.0).fit(train_df)
        self.trained_through_utc_ = self.prior_baseline_.trained_through_utc_

        # Record circuit mean track temperatures for centering
        if clean["last_known_track_temp_c"].notna().any():
            self.global_mean_temp_ = float(clean["last_known_track_temp_c"].median())
        self.circuit_mean_temps_ = {}
        for circ, grp in clean.groupby("circuit_short_name"):
            if pd.notna(circ) and grp["last_known_track_temp_c"].notna().any():
                self.circuit_mean_temps_[str(circ)] = float(
                    grp["last_known_track_temp_c"].median()
                )

        # Within-(session, driver) centering isolates dynamic slopes (progress, compound, tire age, temp)
        # from static driver/car/circuit baseline differences.
        drv_race_means = clean.groupby(["session_key", "driver_number"])[
            "observed_target_lap_time_s"
        ].transform("mean")
        y_centered = (clean["observed_target_lap_time_s"] - drv_race_means).to_numpy(dtype=float)

        prog = clean["race_progress"].fillna(0.5).to_numpy(dtype=float)
        prog_drv_mean = clean.groupby(["session_key", "driver_number"])["race_progress"].transform(
            "mean"
        ).fillna(0.5).to_numpy(dtype=float)
        x_prog = prog - prog_drv_mean

        age = clean["tyre_age_at_lap_start"].fillna(5.0).to_numpy(dtype=float)
        age_drv_mean = (
            clean.groupby(["session_key", "driver_number"])["tyre_age_at_lap_start"]
            .transform("mean")
            .fillna(5.0)
            .to_numpy(dtype=float)
        )

        feature_cols: list[np.ndarray] = [x_prog]
        col_names: list[str] = ["progress"]
        penalties: list[float] = [self.ridge_alpha]

        # Compound indicator offsets relative to MEDIUM
        for cmpd in ("SOFT", "HARD"):
            ind = (clean["compound"] == cmpd).astype(float)
            ind_centered = (
                ind
                - ind.groupby([clean["session_key"], clean["driver_number"]]).transform("mean")
            ).to_numpy(dtype=float)
            feature_cols.append(ind_centered)
            col_names.append(f"cmpd_{cmpd}")
            penalties.append(self.ridge_alpha * 2.0)

        if self.include_compound_age:
            for cmpd in COMPOUNDS:
                c_age = np.where(clean["compound"] == cmpd, age, 0.0)
                c_age_s = pd.Series(c_age, index=clean.index)
                c_age_centered = (
                    c_age_s
                    - c_age_s.groupby([clean["session_key"], clean["driver_number"]]).transform("mean")
                ).to_numpy(dtype=float)
                feature_cols.append(c_age_centered)
                col_names.append(f"age_{cmpd}")
                penalties.append(self.ridge_alpha * 5.0)

                if self.include_quadratic_age:
                    c_age2 = np.where(clean["compound"] == cmpd, (age / 20.0) ** 2, 0.0)
                    c_age2_s = pd.Series(c_age2, index=clean.index)
                    c_age2_centered = (
                        c_age2_s
                        - c_age2_s.groupby(
                            [clean["session_key"], clean["driver_number"]]
                        ).transform("mean")
                    ).to_numpy(dtype=float)
                    feature_cols.append(c_age2_centered)
                    col_names.append(f"age2_{cmpd}")
                    penalties.append(self.ridge_alpha * 25.0)
        else:
            # Shared small tire-age placeholder disabled in E4
            _ = age_drv_mean

        if self.include_track_temp:
            circ_temps = (
                clean["circuit_short_name"]
                .map(self.circuit_mean_temps_)
                .fillna(self.global_mean_temp_)
            )
            t_diff = (
                clean["last_known_track_temp_c"].fillna(circ_temps) - circ_temps
            ).to_numpy(dtype=float)
            feature_cols.append(t_diff)
            col_names.append("temp")
            penalties.append(self.ridge_alpha * 20.0)

        design_mat = np.column_stack(feature_cols)
        reg_diag = np.diag(np.asarray(penalties, dtype=float))
        lhs = design_mat.T @ design_mat + reg_diag
        rhs = design_mat.T @ y_centered
        beta = np.linalg.solve(lhs, rhs)
        coef_map = dict(zip(col_names, beta, strict=False))

        self.mean_train_age_ = float(np.median(age))
        self.mean_train_prog_ = float(np.median(prog))

        # Physical/statistical bounds on race-progress slope and tire-age slopes
        self.progress_slope_ = float(np.clip(coef_map.get("progress", -1.5), -3.2, -0.3))
        self.compound_offsets_ = {
            "SOFT": float(np.clip(coef_map.get("cmpd_SOFT", -0.45), -1.5, 0.4)),
            "MEDIUM": 0.0,
            "HARD": float(np.clip(coef_map.get("cmpd_HARD", 0.35), -0.3, 1.5)),
        }
        if self.include_compound_age:
            self.compound_age_slopes_ = {
                "SOFT": float(np.clip(coef_map.get("age_SOFT", 0.055), 0.005, 0.16)),
                "MEDIUM": float(np.clip(coef_map.get("age_MEDIUM", 0.038), 0.002, 0.12)),
                "HARD": float(np.clip(coef_map.get("age_HARD", 0.025), 0.0, 0.09)),
            }
            if self.include_quadratic_age:
                self.compound_age_quad_slopes_ = {
                    cmpd: float(np.clip(coef_map.get(f"age2_{cmpd}", 0.0), -0.05, 0.15))
                    for cmpd in COMPOUNDS
                }
            else:
                self.compound_age_quad_slopes_ = {cmpd: 0.0 for cmpd in COMPOUNDS}
        else:
            self.compound_age_slopes_ = {cmpd: 0.0 for cmpd in COMPOUNDS}
            self.compound_age_quad_slopes_ = {cmpd: 0.0 for cmpd in COMPOUNDS}

        # Estimate robust compound-specific within-stint net drift (fuel burn + tire wear combined)
        # from strictly historical training examples where a stint median was already observed.
        self.within_stint_drift_ = {}
        stint_sub = clean[clean["median_stint_clean_laps_s"].notna()].copy()
        if not stint_sub.empty:
            eff_d = np.clip(
                stint_sub["last_clean_lap_age"].fillna(1.0).to_numpy(dtype=float)
                + 0.5
                * np.minimum(
                    stint_sub["clean_laps_observed_current_stint"].to_numpy(dtype=float) - 1.0,
                    2.0,
                ),
                1.0,
                10.0,
            )
            stint_sub["per_lap_rate"] = (
                stint_sub["observed_target_lap_time_s"].to_numpy(dtype=float)
                - stint_sub["median_stint_clean_laps_s"].to_numpy(dtype=float)
            ) / eff_d
            global_rate = float(np.clip(stint_sub["per_lap_rate"].median(), -0.04, 0.05))
            for cmpd in COMPOUNDS:
                c_grp = stint_sub[stint_sub["compound"] == cmpd]
                n_c = len(c_grp)
                w_c = n_c / (n_c + 25.0)
                c_med = float(c_grp["per_lap_rate"].median()) if n_c > 0 else global_rate
                self.within_stint_drift_[cmpd] = float(
                    np.clip(w_c * c_med + (1.0 - w_c) * global_rate, -0.04, 0.06)
                )
        else:
            self.within_stint_drift_ = {"SOFT": 0.028, "MEDIUM": 0.015, "HARD": 0.010}

        self.temp_slope_ = (
            float(np.clip(coef_map.get("temp", 0.0), -0.04, 0.04))
            if self.include_track_temp
            else 0.0
        )

        self.is_fitted_ = True

        # Fit residual uncertainty calibrator on calibration_df (if supplied) or training OOF
        cal_source = calibration_df if calibration_df is not None and not calibration_df.empty else clean
        raw_preds = np.array(
            [self._predict_point(row_to_pace_request(r))[0] for _, r in cal_source.iterrows()],
            dtype=float,
        )
        valid_mask = (
            cal_source["target_is_clean_green"].fillna(False).astype(bool)
            & cal_source["observed_target_lap_time_s"].notna()
        ).to_numpy(dtype=bool)
        if valid_mask.any():
            res = (
                cal_source.loc[valid_mask, "observed_target_lap_time_s"].to_numpy(dtype=float)
                - raw_preds[valid_mask]
            )
            mad = float(np.median(np.abs(res - np.median(res))))
            self.uncalibrated_std_ = max(float(1.4826 * mad), self.min_std_seconds)

        self.calibrator_ = ResidualIntervalCalibrator(min_std_seconds=self.min_std_seconds).fit(
            cal_source, raw_preds
        )
        return self

    def _structural_delta(
        self,
        compound: str,
        age_target: float,
        age_ref: float,
        prog_target: float,
        prog_ref: float,
    ) -> float:
        slope_a = self.compound_age_slopes_.get(compound, 0.03)
        quad_a = self.compound_age_quad_slopes_.get(compound, 0.0)
        d_prog = self.progress_slope_ * (prog_target - prog_ref)
        d_age = slope_a * (age_target - age_ref) + quad_a * (
            (age_target / 20.0) ** 2 - (age_ref / 20.0) ** 2
        )
        return float(d_prog + d_age)

    def _predict_point(self, request: PaceRequest) -> tuple[float, float]:
        """Return (expected_seconds, extra_uncertainty_scale_factor)."""
        prior_dist = self.prior_baseline_.predict(request)
        circ = request.circuit_short_name or ""
        unseen_circuit = circ not in self.prior_baseline_.circuit_medians_

        cmpd = request.compound if request.compound in COMPOUNDS else "MEDIUM"
        age_t = float(max(request.tyre_age_at_lap_start, 0))
        prog_t = float(request.race_progress if request.race_progress is not None else 0.5)

        # Temperature effect relative to circuit mean
        temp_adj = 0.0
        if self.include_track_temp and request.track_temperature_c is not None:
            ref_temp = self.circuit_mean_temps_.get(circ, self.global_mean_temp_)
            temp_adj = self.temp_slope_ * (float(request.track_temperature_c) - ref_temp)

        # Remove prior_baseline_'s own compound and progress adjustments before applying structural decomposition
        prior_prog_adj = (
            self.prior_baseline_.progress_slope_ * (prog_t - 0.5)
            if request.race_progress is not None
            else 0.0
        )
        hist_pred = (
            prior_dist.expected_seconds
            - self.prior_baseline_.compound_offsets_.get(request.compound, 0.0)
            - prior_prog_adj
            + self.compound_offsets_.get(cmpd, 0.0)
            + self._structural_delta(
                cmpd, age_t, self.mean_train_age_, prog_t, self.mean_train_prog_
            )
            + temp_adj
        )

        if not self.use_online_adaptation:
            return float(hist_pred), (1.35 if unseen_circuit else 1.0)

        n_stint = int(request.clean_laps_observed_current_stint or 0)
        n_race = int(request.clean_laps_observed_race or 0)
        sched_laps = max(
            float(request.lap_number) / max(prog_t, 1e-3) if prog_t > 0 else 58.0,
            1.0,
        )

        # Compute race-level anchor from previous stint(s) when available in the current race
        race_anchored: float | None = None
        if n_race > 0 and request.median_last_3_clean_laps_s is not None:
            age_gap_race = float(request.last_clean_lap_age or (request.horizon + 2))
            prog_ref_race = max(0.0, prog_t - age_gap_race / sched_laps)
            lap_prev = max(1.0, float(request.lap_number) - age_gap_race)
            stint_num = max(int(request.stint_number or 2), 2)
            est_prev_end_age = float(np.clip(lap_prev / float(stint_num - 1), 10.0, 22.0))
            race_anchored = float(
                float(request.median_last_3_clean_laps_s)
                + self._structural_delta(
                    cmpd, age_t, est_prev_end_age, prog_t, prog_ref_race
                )
            )

        if n_stint > 0 and request.median_stint_clean_laps_s is not None:
            # Within-stint prediction anchored on the robust 3-lap stint clean median
            age_gap = float(request.last_clean_lap_age or request.horizon)
            window_half_span = 0.5 * float(min(n_stint, 3) - 1)
            eff_delta_laps = np.clip(age_gap + window_half_span, 1.0, 12.0)

            net_drift_per_lap = self.within_stint_drift_.get(cmpd, 0.015)
            # Dampen linear extrapolation slightly over multi-lap horizons
            stint_anchored = float(
                float(request.median_stint_clean_laps_s)
                + 0.65 * net_drift_per_lap * eff_delta_laps
            )

            # If only 1 clean lap has been observed on a post-pit stint (n_stint == 1, n_race > 1),
            # partially pool toward the current race's cross-stint anchor (never the old multi-year prior).
            if n_stint == 1 and n_race > 1 and race_anchored is not None:
                expected = float(0.88 * stint_anchored + 0.12 * race_anchored)
            else:
                expected = float(stint_anchored)
            return expected, 1.0

        if race_anchored is not None:
            # First lap(s) on a new stint before any clean lap on the new stint is observed:
            # use the current race's cross-stint structural anchor directly.
            return float(race_anchored), 1.15

        return float(hist_pred), (1.35 if unseen_circuit else 1.10)

    def predict(self, request: PaceRequest) -> PaceDistribution:
        if not self.is_fitted_:
            raise RuntimeError("Fit AdditivePaceModel before calling predict")
        expected, extra_scale = self._predict_point(request)
        if self.use_calibrated_intervals and self.calibrator_.is_fitted_:
            return self.calibrator_.make_distribution(
                expected_seconds=expected,
                request=request,
                model_version=self.model_version,
                trained_through_utc=self.trained_through_utc_,
                extra_scale_factor=extra_scale,
            )
        std = max(self.uncalibrated_std_ * extra_scale, self.min_std_seconds)
        return PaceDistribution(
            expected_seconds=expected,
            std_seconds=float(std),
            model_version=self.model_version,
            trained_through_utc=self.trained_through_utc_,
            confidence_level="NORMAL" if request.clean_laps_observed_current_stint >= 2 else "LOW",
            forecast_as_of_utc=request.as_of_utc,
        )

    def diagnostics_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "progress_slope_s_per_race": round(self.progress_slope_, 4),
            "compound_offsets_s": {k: round(v, 4) for k, v in self.compound_offsets_.items()},
            "compound_age_slopes_s_per_lap": {
                k: round(v, 5) for k, v in self.compound_age_slopes_.items()
            },
            "within_stint_drift_s_per_lap": {
                k: round(v, 5) for k, v in self.within_stint_drift_.items()
            },
            "compound_age_quad_slopes": {
                k: round(v, 5) for k, v in self.compound_age_quad_slopes_.items()
            },
            "temp_slope_s_per_c": round(self.temp_slope_, 5),
            "online_stint_shrinkage_k": self.online_stint_shrinkage_k,
            "online_race_shrinkage_k": self.online_race_shrinkage_k,
            "calibrator_global_scale_s": round(self.calibrator_.global_scale_, 4),
            "calibrator_bucket_scales_s": {
                k: round(v, 4) for k, v in self.calibrator_.bucket_scales_.items()
            },
        }
