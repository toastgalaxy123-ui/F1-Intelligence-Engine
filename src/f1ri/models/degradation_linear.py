"""Linear tire-degradation and race-progression estimators (D1, D2, and Issue 4 baseline)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from f1ri.features.degradation_features import HistoricalSupportTable
from f1ri.models.contracts import PaceDistribution, PaceRequest
from f1ri.models.degradation import DegradationEvaluation
from f1ri.models.pace_baselines import HistoricalCircuitPriorBaseline, _max_utc

COMPOUNDS = ("HARD", "MEDIUM", "SOFT")


def _winsorize_residuals(y: np.ndarray, clip_mad_mult: float = 4.5) -> np.ndarray:
    """Huber-style winsorization of extreme residuals so mechanical failures don't distort slopes."""
    if len(y) < 5:
        return y
    med = float(np.median(y))
    mad = max(float(np.median(np.abs(y - med))), 0.15)
    bound = clip_mad_mult * 1.4826 * mad
    return np.clip(y, med - bound, med + bound)


@dataclass
class AdaptiveTireResetProgressBaseline:
    """Prerequisite Issue 4 Baseline: Online recent clean-lap median + fresh-tire reset + progress drift.

    Tests how much predictive accuracy comes from a well-designed online heuristic (recent clean
    median + compound/tire-reset jump on pit stops + fixed race-progress drift) versus a full
    jointly estimated degradation model.
    """

    model_version: str = "B4_adaptive_tire_reset_progress_v1"
    prior_baseline_: HistoricalCircuitPriorBaseline = field(
        default_factory=HistoricalCircuitPriorBaseline
    )
    progress_slope_s_per_race_: float = -1.55
    net_within_stint_drift_s_per_lap_: dict[str, float] = field(
        default_factory=lambda: {"SOFT": 0.025, "MEDIUM": 0.014, "HARD": 0.008}
    )
    compound_offsets_s_: dict[str, float] = field(
        default_factory=lambda: {"SOFT": -0.45, "MEDIUM": 0.0, "HARD": 0.35}
    )
    fresh_tire_reset_per_lap_s_: float = 0.035
    residual_std_: float = 0.36
    trained_through_utc_: datetime = field(default_factory=lambda: datetime(1970, 1, 1, tzinfo=UTC))
    is_fitted_: bool = False

    def fit(self, train_df: pd.DataFrame) -> AdaptiveTireResetProgressBaseline:
        h_col = "requested_horizon" if "requested_horizon" in train_df.columns else "horizon"
        cmpd_col = "compound_relative" if "compound_relative" in train_df.columns else "compound"
        sub = train_df.copy()
        if "compound" not in sub.columns:
            sub["compound"] = sub[cmpd_col]
        if "horizon" not in sub.columns:
            sub["horizon"] = sub[h_col]

        self.prior_baseline_ = HistoricalCircuitPriorBaseline(shrinkage_k=5.0).fit(sub)
        self.trained_through_utc_ = self.prior_baseline_.trained_through_utc_

        clean = sub[
            sub["target_is_clean_green"].fillna(False).astype(bool)
            & sub["observed_target_lap_time_s"].notna()
            & (sub["horizon"] == 1)
        ].copy()
        if not clean.empty:
            stint_sub = clean[clean["median_stint_clean_laps_s"].notna()].copy()
            if not stint_sub.empty:
                diffs = _winsorize_residuals(
                    stint_sub["observed_target_lap_time_s"].to_numpy(dtype=float)
                    - stint_sub["median_stint_clean_laps_s"].to_numpy(dtype=float)
                )
                steps = np.clip(
                    stint_sub["last_clean_lap_age"].fillna(1.0).to_numpy(dtype=float) + 0.5,
                    1.0,
                    10.0,
                )
                rates = diffs / steps
                for c in COMPOUNDS:
                    c_mask = (stint_sub["compound"] == c).to_numpy(dtype=bool)
                    if c_mask.sum() >= 10:
                        self.net_within_stint_drift_s_per_lap_[c] = float(
                            np.clip(np.median(rates[c_mask]), -0.03, 0.06)
                        )
                mad = float(np.median(np.abs(diffs - np.median(diffs))))
                self.residual_std_ = max(float(1.4826 * mad), 0.22)

        self.is_fitted_ = True
        return self

    def predict_from_fields(
        self,
        *,
        compound: str,
        tyre_age_target: int,
        race_progress_target: float,
        horizon_steps: int,
        median_stint_clean_s: float | None,
        median_last_3_clean_s: float | None,
        clean_laps_stint: int,
        clean_laps_race: int,
        last_clean_lap_age: int | None,
        prev_compound: str | None,
        prev_end_age: int | None,
        fallback_expected_s: float,
    ) -> float:
        cmpd = compound if compound in COMPOUNDS else "MEDIUM"
        if clean_laps_stint > 0 and median_stint_clean_s is not None:
            gap = float(last_clean_lap_age or horizon_steps)
            eff_steps = np.clip(gap + 0.5 * min(clean_laps_stint - 1, 2), 1.0, 18.0)
            drift = self.net_within_stint_drift_s_per_lap_.get(cmpd, 0.014)
            return float(median_stint_clean_s + 0.65 * drift * eff_steps)

        if clean_laps_race > 0 and median_last_3_clean_s is not None:
            # Post-pit fresh/used tire reset + compound switch + progress drift
            p_cmpd = prev_compound if prev_compound in COMPOUNDS else cmpd
            p_age = float(np.clip(prev_end_age if prev_end_age is not None else 15.0, 8.0, 18.0))
            cmpd_delta = self.compound_offsets_s_.get(cmpd, 0.0) - self.compound_offsets_s_.get(
                p_cmpd, 0.0
            )
            age_reset_delta = (
                0.55 * self.fresh_tire_reset_per_lap_s_ * (float(tyre_age_target) - p_age)
            )
            gap_laps = float(np.clip(last_clean_lap_age or (horizon_steps + 2), 1.0, 18.0))
            prog_delta = self.progress_slope_s_per_race_ * (gap_laps / 58.0)
            return float(median_last_3_clean_s + 0.5 * cmpd_delta + age_reset_delta + prog_delta)

        return float(fallback_expected_s)

    def predict(self, request: PaceRequest) -> PaceDistribution:
        if not self.is_fitted_:
            raise RuntimeError("Fit AdaptiveTireResetProgressBaseline first")
        prior_dist = self.prior_baseline_.predict(request)
        exp_s = self.predict_from_fields(
            compound=request.compound,
            tyre_age_target=request.tyre_age_at_lap_start,
            race_progress_target=float(request.race_progress or 0.5),
            horizon_steps=int(request.horizon),
            median_stint_clean_s=request.median_stint_clean_laps_s,
            median_last_3_clean_s=request.median_last_3_clean_laps_s,
            clean_laps_stint=int(request.clean_laps_observed_current_stint),
            clean_laps_race=int(request.clean_laps_observed_race),
            last_clean_lap_age=request.last_clean_lap_age,
            prev_compound=None,
            prev_end_age=None,
            fallback_expected_s=prior_dist.expected_seconds,
        )
        std = self.residual_std_ * (1.0 + 0.06 * max(0, int(request.horizon) - 1))
        return PaceDistribution(
            expected_seconds=exp_s,
            std_seconds=float(std),
            model_version=self.model_version,
            trained_through_utc=self.trained_through_utc_,
            confidence_level="NORMAL" if request.clean_laps_observed_current_stint > 0 else "LOW",
            forecast_as_of_utc=request.as_of_utc,
        )


@dataclass
class LinearTireDegradationModel:
    """Models D1 and D2: Linear tire-age degradation with optional joint race-progression estimation.

    - When `joint_race_progress=False` (D1): estimates net within-stint linear tire-age trends
      without separating race progression.
    - When `joint_race_progress=True` (D2): estimates race-progression slope F_r(l) and
      compound-specific tire-age degradation slopes beta_c jointly using cross-stint and
      cross-driver variation at matched race progress.
    """

    model_version: str = "D2_joint_linear_degradation_v1"
    ridge_alpha: float = 15.0
    joint_race_progress: bool = True
    include_track_temp: bool = True

    progress_slope_s_per_race_: float = -1.65
    compound_offsets_s_: dict[str, float] = field(
        default_factory=lambda: {"HARD": 0.35, "MEDIUM": 0.0, "SOFT": -0.42}
    )
    compound_age_slopes_s_: dict[str, float] = field(
        default_factory=lambda: {"HARD": 0.032, "MEDIUM": 0.045, "SOFT": 0.065}
    )
    compound_slope_stds_s_: dict[str, float] = field(
        default_factory=lambda: {"HARD": 0.012, "MEDIUM": 0.014, "SOFT": 0.018}
    )
    temp_slope_s_per_c_: float = 0.0
    circuit_mean_temps_: dict[str, float] = field(default_factory=dict)
    global_mean_temp_: float = 33.0
    support_table_: HistoricalSupportTable = field(default_factory=HistoricalSupportTable)
    trained_through_utc_: datetime = field(default_factory=lambda: datetime(1970, 1, 1, tzinfo=UTC))
    identifiability_condition_number_: float = 1.0
    is_confounded_warning_: bool = False
    is_fitted_: bool = False

    def fit(self, train_df: pd.DataFrame) -> LinearTireDegradationModel:
        cmpd_col = (
            "compound_relative" if "compound_relative" in train_df.columns else "compound"
        )
        clean_col = (
            "is_clean_green_pace_lap"
            if "is_clean_green_pace_lap" in train_df.columns
            else "target_is_clean_green"
        )
        obs_col = (
            "observed_lap_time_s"
            if "observed_lap_time_s" in train_df.columns
            else "observed_target_lap_time_s"
        )
        temp_col = (
            "track_temperature_c"
            if "track_temperature_c" in train_df.columns
            else "last_known_track_temp_c"
        )

        df = train_df[
            train_df[clean_col].fillna(False).astype(bool)
            & train_df[obs_col].notna()
            & train_df[cmpd_col].isin(COMPOUNDS)
        ].copy()
        if "requested_horizon" in df.columns:
            df = df[df["requested_horizon"] == 1].copy()
        elif "horizon" in df.columns:
            df = df[df["horizon"] == 1].copy()

        if df.empty:
            raise ValueError("No eligible clean green dry laps to fit LinearTireDegradationModel")

        self.trained_through_utc_ = _max_utc(df)
        df["compound_relative"] = df[cmpd_col].astype(str)
        df["is_clean_green_pace_lap"] = True
        df["observed_lap_time_s"] = df[obs_col].astype(float)
        self.support_table_ = HistoricalSupportTable().fit(df)

        if temp_col in df.columns and df[temp_col].notna().any():
            self.global_mean_temp_ = float(df[temp_col].median())
            for circ, grp in df.groupby("circuit_short_name"):
                if pd.notna(circ) and grp[temp_col].notna().any():
                    self.circuit_mean_temps_[str(circ)] = float(grp[temp_col].median())

        # Center within (session_key, driver_number) to isolate within-driver-race longitudinal dynamics
        grp_keys = [df["session_key"], df["driver_number"]]
        y_raw = (
            df["observed_lap_time_s"] - df.groupby(grp_keys)["observed_lap_time_s"].transform("mean")
        ).to_numpy(dtype=float)
        y_centered = _winsorize_residuals(y_raw)

        age = df["tyre_age_at_lap_start"].fillna(0.0).to_numpy(dtype=float)
        prog = df["race_progress"].fillna(0.5).to_numpy(dtype=float)

        if not self.joint_race_progress:
            # D1: Fit within-stint net linear slope per compound without race-progress separation
            self.progress_slope_s_per_race_ = 0.0
            self.compound_offsets_s_ = {"HARD": 0.35, "MEDIUM": 0.0, "SOFT": -0.42}
            stint_keys = [df["session_key"], df["driver_number"], df["stint_number"]]
            y_stint = _winsorize_residuals(
                (
                    df["observed_lap_time_s"]
                    - df.groupby(stint_keys)["observed_lap_time_s"].transform("mean")
                ).to_numpy(dtype=float)
            )
            a_stint = (
                df["tyre_age_at_lap_start"]
                - df.groupby(stint_keys)["tyre_age_at_lap_start"].transform("mean")
            ).to_numpy(dtype=float)
            for c in COMPOUNDS:
                c_mask = (df["compound_relative"] == c).to_numpy(dtype=bool)
                if c_mask.sum() >= 5:
                    ac = a_stint[c_mask]
                    yc = y_stint[c_mask]
                    slope = float(np.dot(ac, yc) / (np.dot(ac, ac) + self.ridge_alpha * 10.0))
                    self.compound_age_slopes_s_[c] = float(np.clip(slope, -0.02, 0.14))
            self.is_fitted_ = True
            return self

        # D2: Joint estimation of race_progress slope, compound initial offsets, and compound age slopes
        x_prog = (
            prog - pd.Series(prog, index=df.index).groupby(grp_keys).transform("mean").to_numpy()
        )
        cols: list[np.ndarray] = [x_prog]
        names: list[str] = ["progress"]
        penalties: list[float] = [self.ridge_alpha * 12.0]

        for c in ("SOFT", "HARD"):
            ind = (df["compound_relative"] == c).astype(float)
            ind_c = (ind - ind.groupby(grp_keys).transform("mean")).to_numpy(dtype=float)
            cols.append(ind_c)
            names.append(f"cmpd_{c}")
            penalties.append(self.ridge_alpha * 2.0)

        for c in COMPOUNDS:
            c_age = np.where(df["compound_relative"] == c, age, 0.0)
            c_age_s = pd.Series(c_age, index=df.index)
            c_age_c = (c_age_s - c_age_s.groupby(grp_keys).transform("mean")).to_numpy(dtype=float)
            cols.append(c_age_c)
            names.append(f"age_{c}")
            penalties.append(self.ridge_alpha * 4.0)

        if self.include_track_temp and temp_col in df.columns:
            c_temps = df["circuit_short_name"].map(self.circuit_mean_temps_).fillna(
                self.global_mean_temp_
            )
            t_diff = (df[temp_col].fillna(c_temps) - c_temps).to_numpy(dtype=float)
            cols.append(t_diff)
            names.append("temp")
            penalties.append(self.ridge_alpha * 20.0)

        X = np.column_stack(cols)
        # Check collinearity between progress and total tire age (scientific safeguard for Section 17.3)
        age_centered = (
            pd.Series(age, index=df.index)
            - pd.Series(age, index=df.index).groupby(grp_keys).transform("mean")
        ).to_numpy(dtype=float)
        if np.std(x_prog) > 1e-8 and np.std(age_centered) > 1e-8:
            corr_pa = abs(float(np.corrcoef(x_prog, age_centered)[0, 1]))
        else:
            corr_pa = 1.0
        self.is_confounded_warning_ = bool(corr_pa > 0.985)

        xtx = X.T @ X
        s_vals = np.linalg.svd(xtx, compute_uv=False)
        self.identifiability_condition_number_ = float(
            s_vals[0] / max(s_vals[-1], 1e-9)
        )

        reg_vec = np.asarray(penalties, dtype=float)
        lhs = xtx + np.diag(reg_vec)
        prior_mean = np.zeros(len(names), dtype=float)
        prior_mean[0] = -1.75  # Physical F1 fuel-burn prior (-1.75 s over race distance)
        beta = np.linalg.solve(lhs, X.T @ y_centered + reg_vec * prior_mean)
        coef = dict(zip(names, beta, strict=False))

        # Estimate residual variance and parameter standard errors
        res = y_centered - X @ beta
        sigma2 = float(np.median(res**2) / 0.4549)
        cov_beta = sigma2 * np.linalg.inv(lhs)
        se_map = dict(zip(names, np.sqrt(np.maximum(np.diag(cov_beta), 1e-8)), strict=False))

        self.progress_slope_s_per_race_ = float(np.clip(coef.get("progress", -1.75), -2.15, -1.30))
        self.compound_offsets_s_ = {
            "SOFT": float(np.clip(coef.get("cmpd_SOFT", -0.45), -1.5, 0.2)),
            "MEDIUM": 0.0,
            "HARD": float(np.clip(coef.get("cmpd_HARD", 0.35), -0.2, 1.5)),
        }
        self.compound_age_slopes_s_ = {
            "SOFT": float(np.clip(coef.get("age_SOFT", 0.060), 0.005, 0.18)),
            "MEDIUM": float(np.clip(coef.get("age_MEDIUM", 0.042), 0.002, 0.14)),
            "HARD": float(np.clip(coef.get("age_HARD", 0.028), 0.001, 0.10)),
        }
        # Inflate slope uncertainty if fuel and degradation are nearly collinear
        confound_mult = 2.5 if self.is_confounded_warning_ else 1.0
        self.compound_slope_stds_s_ = {
            c: float(max(se_map.get(f"age_{c}", 0.012) * confound_mult, 0.008))
            for c in COMPOUNDS
        }
        self.temp_slope_s_per_c_ = (
            float(np.clip(coef.get("temp", 0.0), -0.04, 0.04))
            if self.include_track_temp
            else 0.0
        )
        self.is_fitted_ = True
        return self

    def evaluate_tire(
        self,
        compound_relative: str,
        tyre_age: float,
        *,
        compound_absolute: str | None = None,
        circuit_short_name: str | None = None,
        regulation_era: str = "2023_2025",
        session_key: int | None = None,
    ) -> DegradationEvaluation:
        _ = session_key
        cmpd = compound_relative if compound_relative in COMPOUNDS else "MEDIUM"
        age = max(float(tyre_age), 0.0)
        c_off = float(self.compound_offsets_s_.get(cmpd, 0.0))
        slope = float(self.compound_age_slopes_s_.get(cmpd, 0.04))
        age_deg = float(slope * age)
        status, warn = self.support_table_.classify_support(
            regulation_era=regulation_era,
            circuit_short_name=circuit_short_name,
            compound_relative=cmpd,
            compound_absolute=compound_absolute,
            projected_tyre_age=round(age),
        )
        return DegradationEvaluation(
            compound_offset_s=c_off,
            age_degradation_s=age_deg,
            total_tire_effect_s=c_off + age_deg,
            local_slope_s_per_lap=slope,
            slope_std_s_per_lap=float(self.compound_slope_stds_s_.get(cmpd, 0.014)),
            support_status=status,
            extrapolation_warning=warn,
        )

    def race_progress_effect(
        self,
        race_progress: float,
        track_temperature_c: float | None = None,
        circuit_short_name: str | None = None,
        regulation_era: str = "2023_2025",
    ) -> tuple[float, float]:
        _ = regulation_era
        prog_s = float(self.progress_slope_s_per_race_ * float(race_progress))
        temp_s = 0.0
        if self.include_track_temp and track_temperature_c is not None:
            ref_t = self.circuit_mean_temps_.get(
                str(circuit_short_name or ""), self.global_mean_temp_
            )
            temp_s = float(self.temp_slope_s_per_c_ * (float(track_temperature_c) - ref_t))
        return float(prog_s + temp_s), float(temp_s)

    def diagnostics_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "joint_race_progress": self.joint_race_progress,
            "progress_slope_s_per_race": round(self.progress_slope_s_per_race_, 4),
            "compound_offsets_s": {k: round(v, 4) for k, v in self.compound_offsets_s_.items()},
            "compound_age_slopes_s_per_lap": {
                k: round(v, 5) for k, v in self.compound_age_slopes_s_.items()
            },
            "compound_slope_stds_s_per_lap": {
                k: round(v, 5) for k, v in self.compound_slope_stds_s_.items()
            },
            "temp_slope_s_per_c": round(self.temp_slope_s_per_c_, 5),
            "is_confounded_warning": self.is_confounded_warning_,
            "identifiability_condition_number": round(self.identifiability_condition_number_, 2),
        }
