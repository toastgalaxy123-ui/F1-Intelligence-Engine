"""Nonlinear tire-degradation model (D3: warm-up, stable phase, and penalized late-stint cliff)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from f1ri.features.degradation_features import (
    HistoricalSupportTable,
    basis_late_cliff,
    basis_warm_up,
)
from f1ri.models.degradation import DegradationEvaluation
from f1ri.models.degradation_linear import COMPOUNDS, _winsorize_residuals
from f1ri.models.pace_baselines import _max_utc

KNOT_AGE_BY_COMPOUND: dict[str, float] = {
    "SOFT": 14.0,
    "MEDIUM": 20.0,
    "HARD": 26.0,
}


@dataclass
class NonlinearTireDegradationModel:
    """Model D3: Three-phase nonlinear tire degradation estimated jointly with race progression.

    Decomposes D_c(a) (referenced so D_c(0) == 0) into:
    1. Warm-up phase: w_c * (exp(-a / 1.5) - 1.0)
    2. Stable degradation phase: beta_c * a
    3. Penalized late-stint cliff phase: cliff_c * (max(0, a - knot_c) / 10.0)^2
    """

    model_version: str = "D3_nonlinear_degradation_v1"
    ridge_alpha: float = 15.0
    cliff_penalty_multiplier: float = 40.0
    warmup_penalty_multiplier: float = 25.0
    include_track_temp: bool = True

    progress_slope_s_per_race_: float = -1.65
    compound_offsets_s_: dict[str, float] = field(
        default_factory=lambda: {"HARD": 0.35, "MEDIUM": 0.0, "SOFT": -0.42}
    )
    compound_linear_slopes_s_: dict[str, float] = field(
        default_factory=lambda: {"HARD": 0.028, "MEDIUM": 0.040, "SOFT": 0.058}
    )
    compound_warmup_s_: dict[str, float] = field(
        default_factory=lambda: {"HARD": 0.0, "MEDIUM": 0.0, "SOFT": 0.0}
    )
    compound_cliff_quad_s_: dict[str, float] = field(
        default_factory=lambda: {"HARD": 0.0, "MEDIUM": 0.0, "SOFT": 0.0}
    )
    compound_slope_stds_s_: dict[str, float] = field(
        default_factory=lambda: {"HARD": 0.012, "MEDIUM": 0.014, "SOFT": 0.018}
    )
    temp_slope_s_per_c_: float = 0.0
    circuit_mean_temps_: dict[str, float] = field(default_factory=dict)
    global_mean_temp_: float = 33.0
    support_table_: HistoricalSupportTable = field(default_factory=HistoricalSupportTable)
    trained_through_utc_: datetime = field(default_factory=lambda: datetime(1970, 1, 1, tzinfo=UTC))
    is_fitted_: bool = False

    def fit(self, train_df: pd.DataFrame) -> NonlinearTireDegradationModel:
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
            raise ValueError("No eligible clean green dry laps to fit NonlinearTireDegradationModel")

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

        grp_keys = [df["session_key"], df["driver_number"]]
        y_raw = (
            df["observed_lap_time_s"] - df.groupby(grp_keys)["observed_lap_time_s"].transform("mean")
        ).to_numpy(dtype=float)
        y_centered = _winsorize_residuals(y_raw)

        age = df["tyre_age_at_lap_start"].fillna(0.0).to_numpy(dtype=float)
        prog = df["race_progress"].fillna(0.5).to_numpy(dtype=float)
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
            c_mask = (df["compound_relative"] == c).to_numpy(dtype=bool)
            # 1. Linear age basis
            lin_b = np.where(c_mask, age, 0.0)
            lin_s = pd.Series(lin_b, index=df.index)
            cols.append((lin_s - lin_s.groupby(grp_keys).transform("mean")).to_numpy(dtype=float))
            names.append(f"lin_{c}")
            penalties.append(self.ridge_alpha * 4.0)

            # 2. Warm-up basis: (exp(-a/1.5) - 1.0) so value at a=0 is 0
            warm_b = np.where(c_mask, basis_warm_up(age) - 1.0, 0.0)
            warm_s = pd.Series(warm_b, index=df.index)
            cols.append((warm_s - warm_s.groupby(grp_keys).transform("mean")).to_numpy(dtype=float))
            names.append(f"warm_{c}")
            penalties.append(self.ridge_alpha * self.warmup_penalty_multiplier)

            # 3. Late-stint hinge quadratic cliff basis beyond knot_c
            knot = KNOT_AGE_BY_COMPOUND.get(c, 20.0)
            cliff_b = np.where(c_mask, basis_late_cliff(age, knot_age=knot), 0.0)
            cliff_s = pd.Series(cliff_b, index=df.index)
            # Penalize extra heavily if few observations exceed knot
            n_past_knot = int(np.sum(c_mask & (age > knot)))
            sparsity_mult = 1.0 + 20.0 / max(n_past_knot, 1)
            cols.append(
                (cliff_s - cliff_s.groupby(grp_keys).transform("mean")).to_numpy(dtype=float)
            )
            names.append(f"cliff_{c}")
            penalties.append(self.ridge_alpha * self.cliff_penalty_multiplier * sparsity_mult)

        if self.include_track_temp and temp_col in df.columns:
            c_temps = df["circuit_short_name"].map(self.circuit_mean_temps_).fillna(
                self.global_mean_temp_
            )
            t_diff = (df[temp_col].fillna(c_temps) - c_temps).to_numpy(dtype=float)
            cols.append(t_diff)
            names.append("temp")
            penalties.append(self.ridge_alpha * 20.0)

        X = np.column_stack(cols)
        reg_vec = np.asarray(penalties, dtype=float)
        lhs = X.T @ X + np.diag(reg_vec)
        prior_mean = np.zeros(len(names), dtype=float)
        prior_mean[0] = -1.75  # Physical F1 fuel-burn prior (-1.75 s over race distance)
        beta = np.linalg.solve(lhs, X.T @ y_centered + reg_vec * prior_mean)
        coef = dict(zip(names, beta, strict=False))

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
        self.compound_linear_slopes_s_ = {
            "SOFT": float(np.clip(coef.get("lin_SOFT", 0.055), 0.005, 0.16)),
            "MEDIUM": float(np.clip(coef.get("lin_MEDIUM", 0.038), 0.002, 0.12)),
            "HARD": float(np.clip(coef.get("lin_HARD", 0.025), 0.001, 0.09)),
        }
        self.compound_warmup_s_ = {
            c: float(np.clip(coef.get(f"warm_{c}", 0.0), -0.25, 0.25)) for c in COMPOUNDS
        }
        # Late-stint cliff is constrained >= 0 (accelerated wear cannot make worn tires faster)
        self.compound_cliff_quad_s_ = {
            c: float(np.clip(coef.get(f"cliff_{c}", 0.0), 0.0, 0.22)) for c in COMPOUNDS
        }
        self.compound_slope_stds_s_ = {
            c: float(max(se_map.get(f"lin_{c}", 0.012), 0.008)) for c in COMPOUNDS
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
        lin_s = float(self.compound_linear_slopes_s_.get(cmpd, 0.04))
        warm_w = float(self.compound_warmup_s_.get(cmpd, 0.0))
        cliff_q = float(self.compound_cliff_quad_s_.get(cmpd, 0.0))
        knot = KNOT_AGE_BY_COMPOUND.get(cmpd, 20.0)

        warm_term = warm_w * (float(basis_warm_up(age)) - 1.0)
        cliff_term = cliff_q * float(basis_late_cliff(age, knot_age=knot))
        age_deg = float(lin_s * age + warm_term + cliff_term)

        # Local derivative dD/da at current age
        d_warm = -warm_w * float(basis_warm_up(age)) / 1.5
        d_cliff = (
            cliff_q * 2.0 * max(0.0, age - knot) / 100.0 if age > knot else 0.0
        )
        local_slope = float(lin_s + d_warm + d_cliff)

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
            local_slope_s_per_lap=local_slope,
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
            "progress_slope_s_per_race": round(self.progress_slope_s_per_race_, 4),
            "compound_offsets_s": {k: round(v, 4) for k, v in self.compound_offsets_s_.items()},
            "compound_linear_slopes_s_per_lap": {
                k: round(v, 5) for k, v in self.compound_linear_slopes_s_.items()
            },
            "compound_warmup_s": {k: round(v, 4) for k, v in self.compound_warmup_s_.items()},
            "compound_cliff_quad_s": {
                k: round(v, 5) for k, v in self.compound_cliff_quad_s_.items()
            },
            "temp_slope_s_per_c": round(self.temp_slope_s_per_c_, 5),
        }
