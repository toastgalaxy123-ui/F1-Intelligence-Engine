"""Hierarchical partially-pooled tire-degradation models (D4, D5, D6)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from f1ri.features.degradation_features import (
    HistoricalSupportTable,
    era_qualified_compound,
)
from f1ri.models.degradation import DegradationEvaluation
from f1ri.models.degradation_linear import COMPOUNDS, _winsorize_residuals
from f1ri.models.degradation_nonlinear import NonlinearTireDegradationModel


@dataclass
class HierarchicalTireDegradationModel:
    """Partially pooled hierarchical degradation model across era, C1-C6 compound, circuit, and weekend.

    Implements the conceptual decomposition from Section 8 (Model D2 / D4-D6):
        D_{r,c}(a) = D_{era,c}(a) + delta_{circuit,c}(a) + delta_{weekend,c}(a)

    Supports controlled ablation flags:
    - `use_absolute_compounds`: Use Pirelli C1-C6 era-qualified compounds (D4+) vs relative only.
    - `use_circuit_pooling`: Partially pool circuit-specific degradation slopes by effective stint count (D5+).
    - `use_weekend_adaptation`: Adapt compound offsets/slopes using prior stints or track temp in the weekend (D6+).
    - `use_nonlinear_curve`: Include penalized warm-up and late-stint cliff terms.
    """

    model_version: str = "D5_hierarchical_degradation_v1"
    ridge_alpha: float = 15.0
    use_absolute_compounds: bool = True
    use_circuit_pooling: bool = True
    use_weekend_adaptation: bool = False
    use_nonlinear_curve: bool = True
    circuit_shrinkage_stints_k: float = 12.0
    abs_compound_shrinkage_stints_k: float = 8.0
    era_shrinkage_stints_k: float = 10.0

    # Fitted base nonlinear/linear estimator
    base_model_: NonlinearTireDegradationModel = field(
        default_factory=NonlinearTireDegradationModel
    )
    # Era-qualified absolute compound initial offsets (relative to weekend MEDIUM) and slope adjustments
    abs_compound_slope_deltas_: dict[str, float] = field(default_factory=dict)
    abs_compound_stint_counts_: dict[str, int] = field(default_factory=dict)
    # Circuit x relative_compound slope adjustments (shrunk by distinct stint count)
    circuit_slope_deltas_: dict[tuple[str, str], float] = field(default_factory=dict)
    circuit_compound_offsets_: dict[tuple[str, str], float] = field(default_factory=dict)
    circuit_stint_counts_: dict[tuple[str, str], int] = field(default_factory=dict)
    # Era-specific progress and degradation adjustments (for 2026 transfer vs progressive adaptation)
    era_progress_slopes_: dict[str, float] = field(default_factory=dict)
    era_slope_deltas_: dict[tuple[str, str], float] = field(default_factory=dict)
    support_table_: HistoricalSupportTable = field(default_factory=HistoricalSupportTable)
    trained_through_utc_: datetime = field(default_factory=lambda: datetime(1970, 1, 1, tzinfo=UTC))
    is_fitted_: bool = False

    def fit(self, train_df: pd.DataFrame) -> HierarchicalTireDegradationModel:
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

        self.base_model_ = NonlinearTireDegradationModel(
            model_version=f"{self.model_version}_base",
            ridge_alpha=self.ridge_alpha,
            cliff_penalty_multiplier=(40.0 if self.use_nonlinear_curve else 1e6),
            warmup_penalty_multiplier=(25.0 if self.use_nonlinear_curve else 1e6),
        ).fit(train_df)
        self.trained_through_utc_ = self.base_model_.trained_through_utc_
        self.support_table_ = self.base_model_.support_table_

        df = train_df[
            train_df[clean_col].fillna(False).astype(bool)
            & train_df[obs_col].notna()
            & train_df[cmpd_col].isin(COMPOUNDS)
        ].copy()
        if "requested_horizon" in df.columns:
            df = df[df["requested_horizon"] == 1].copy()
        elif "horizon" in df.columns:
            df = df[df["horizon"] == 1].copy()

        df["compound_relative"] = df[cmpd_col].astype(str)
        df["observed_lap_time_s"] = df[obs_col].astype(float)
        if "stint_id" not in df.columns:
            df["stint_id"] = (
                df["session_key"].astype(str)
                + "_"
                + df["driver_number"].astype(str)
                + "_"
                + df["stint_number"].fillna(1).astype(int).astype(str)
            )
        if "compound_absolute" not in df.columns:
            df["compound_absolute"] = (
                df["actual_compound_code"] if "actual_compound_code" in df.columns else None
            )

        # Compute within-stint empirical slopes after removing the global race-progression effect
        prog = df["race_progress"].fillna(0.5).to_numpy(dtype=float)
        age = df["tyre_age_at_lap_start"].fillna(0.0).to_numpy(dtype=float)
        cmpd_rel = df["compound_relative"].to_numpy(dtype=str)

        base_deg = np.array(
            [
                self.base_model_.evaluate_tire(c, a).age_degradation_s
                for c, a in zip(cmpd_rel, age, strict=False)
            ],
            dtype=float,
        )
        prog_eff = self.base_model_.progress_slope_s_per_race_ * prog
        df["resid_after_global"] = df["observed_lap_time_s"] - prog_eff - base_deg

        # Center within each individual stint to isolate residual slope deviations delta_slope
        stint_mean_r = df.groupby("stint_id")["resid_after_global"].transform("mean")
        stint_mean_a = df.groupby("stint_id")["tyre_age_at_lap_start"].transform("mean")
        df["r_within_stint"] = _winsorize_residuals(
            (df["resid_after_global"] - stint_mean_r).to_numpy(dtype=float)
        )
        df["a_within_stint"] = (df["tyre_age_at_lap_start"] - stint_mean_a).to_numpy(dtype=float)

        # 1. Era-qualified Pirelli absolute compound slope deltas (C1-C6 within regulation_era)
        self.abs_compound_slope_deltas_ = {}
        self.abs_compound_stint_counts_ = {}
        if self.use_absolute_compounds:
            df["era_abs_key"] = [
                era_qualified_compound(e, ca, cr)
                for e, ca, cr in zip(
                    df["regulation_era"],
                    df["compound_absolute"],
                    df["compound_relative"],
                    strict=False,
                )
            ]
            for key, grp in df.groupby("era_abs_key"):
                if "__REL_" in str(key):
                    continue
                n_stints = int(grp["stint_id"].nunique())
                self.abs_compound_stint_counts_[str(key)] = n_stints
                da = grp["a_within_stint"].to_numpy(dtype=float)
                dr = grp["r_within_stint"].to_numpy(dtype=float)
                denom = float(np.dot(da, da)) + 80.0
                raw_delta = float(np.dot(da, dr) / denom)
                w = n_stints / (n_stints + self.abs_compound_shrinkage_stints_k)
                self.abs_compound_slope_deltas_[str(key)] = float(
                    np.clip(w * raw_delta, -0.025, 0.025)
                )

        # Remove absolute compound slope adjustment before estimating circuit-specific slope deltas
        abs_delta_vec = np.array(
            [
                self.abs_compound_slope_deltas_.get(
                    era_qualified_compound(e, ca, cr), 0.0
                )
                for e, ca, cr in zip(
                    df["regulation_era"],
                    df["compound_absolute"],
                    df["compound_relative"],
                    strict=False,
                )
            ],
            dtype=float,
        )
        df["r_after_abs"] = df["r_within_stint"] - abs_delta_vec * df["a_within_stint"]

        # 2. Hierarchical circuit x compound partial pooling (shrunk by effective stint count)
        self.circuit_slope_deltas_ = {}
        self.circuit_compound_offsets_ = {}
        self.circuit_stint_counts_ = {}
        if self.use_circuit_pooling:
            for (circ, rel_c), grp in df.groupby(["circuit_short_name", "compound_relative"]):
                if pd.isna(circ):
                    continue
                n_stints = int(grp["stint_id"].nunique())
                self.circuit_stint_counts_[(str(circ), str(rel_c))] = n_stints
                da = grp["a_within_stint"].to_numpy(dtype=float)
                dr = grp["r_after_abs"].to_numpy(dtype=float)
                denom = float(np.dot(da, da)) + 100.0
                raw_delta = float(np.dot(da, dr) / denom)
                w = n_stints / (n_stints + self.circuit_shrinkage_stints_k)
                self.circuit_slope_deltas_[(str(circ), str(rel_c))] = float(
                    np.clip(w * raw_delta, -0.030, 0.030)
                )

            # Also partially pool circuit-specific compound step offsets (e.g. SOFT vs MEDIUM gap at circuit)
            drv_race_keys = [df["session_key"], df["driver_number"]]
            df["y_minus_prog_deg"] = (
                df["observed_lap_time_s"]
                - prog_eff
                - base_deg
                - abs_delta_vec * age
            )
            df["y_drv_centered"] = df["y_minus_prog_deg"] - df.groupby(drv_race_keys)[
                "y_minus_prog_deg"
            ].transform("mean")
            for (circ, rel_c), grp in df.groupby(["circuit_short_name", "compound_relative"]):
                if pd.isna(circ) or str(rel_c) == "MEDIUM":
                    continue
                n_stints = int(grp["stint_id"].nunique())
                w = n_stints / (n_stints + self.circuit_shrinkage_stints_k * 1.5)
                global_off = self.base_model_.compound_offsets_s_.get(str(rel_c), 0.0)
                resid_off = float(grp["y_drv_centered"].median()) - global_off * 0.5
                self.circuit_compound_offsets_[(str(circ), str(rel_c))] = float(
                    np.clip(w * resid_off * 0.35, -0.30, 0.30)
                )

        # 3. Regulation-era specific adjustments (for 2026_PLUS when 2026 races are in train_df)
        self.era_progress_slopes_ = {"2023_2025": self.base_model_.progress_slope_s_per_race_}
        self.era_slope_deltas_ = {}
        for era, grp in df.groupby("regulation_era"):
            era_s = str(era)
            if era_s == "2023_2025":
                continue
            n_era_stints = int(grp["stint_id"].nunique())
            w_era = n_era_stints / (n_era_stints + self.era_shrinkage_stints_k)
            for rel_c, c_grp in grp.groupby("compound_relative"):
                n_c_stints = int(c_grp["stint_id"].nunique())
                da = c_grp["a_within_stint"].to_numpy(dtype=float)
                dr = c_grp["r_after_abs"].to_numpy(dtype=float)
                raw_d = float(np.dot(da, dr) / (float(np.dot(da, da)) + 80.0))
                w_c = n_c_stints / (n_c_stints + self.era_shrinkage_stints_k)
                self.era_slope_deltas_[(era_s, str(rel_c))] = float(
                    np.clip(w_era * w_c * raw_d, -0.025, 0.025)
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
        base_eval = self.base_model_.evaluate_tire(
            compound_relative=compound_relative,
            tyre_age=tyre_age,
            compound_absolute=compound_absolute,
            circuit_short_name=circuit_short_name,
            regulation_era=regulation_era,
            session_key=session_key,
        )
        cmpd = compound_relative if compound_relative in COMPOUNDS else "MEDIUM"
        circ = str(circuit_short_name or "")
        era = str(regulation_era or "2023_2025")
        age = max(float(tyre_age), 0.0)

        abs_key = era_qualified_compound(era, compound_absolute, cmpd)
        abs_slope_delta = (
            self.abs_compound_slope_deltas_.get(abs_key, 0.0)
            if self.use_absolute_compounds
            else 0.0
        )
        circ_slope_delta = (
            self.circuit_slope_deltas_.get((circ, cmpd), 0.0)
            if self.use_circuit_pooling
            else 0.0
        )
        circ_cmpd_off = (
            self.circuit_compound_offsets_.get((circ, cmpd), 0.0)
            if self.use_circuit_pooling
            else 0.0
        )
        era_slope_delta = self.era_slope_deltas_.get((era, cmpd), 0.0)

        total_slope_delta = float(
            np.clip(abs_slope_delta + circ_slope_delta + era_slope_delta, -0.035, 0.045)
        )
        # Ensure physical non-negative tire degradation rate in stable phase
        min_allowed_delta = -0.85 * self.base_model_.compound_linear_slopes_s_.get(cmpd, 0.03)
        total_slope_delta = max(total_slope_delta, min_allowed_delta)

        c_off = float(base_eval.compound_offset_s + circ_cmpd_off)
        age_deg = float(base_eval.age_degradation_s + total_slope_delta * age)
        local_slope = float(base_eval.local_slope_s_per_lap + total_slope_delta)

        # Reflect hierarchical sample support in slope uncertainty
        n_circ_stints = self.circuit_stint_counts_.get((circ, cmpd), 0)
        support_infl = 1.0 + 0.35 / (1.0 + 0.15 * n_circ_stints)
        if base_eval.support_status in ("EXTRAPOLATED", "UNSUPPORTED"):
            support_infl *= 1.45

        return DegradationEvaluation(
            compound_offset_s=c_off,
            age_degradation_s=age_deg,
            total_tire_effect_s=c_off + age_deg,
            local_slope_s_per_lap=local_slope,
            slope_std_s_per_lap=float(base_eval.slope_std_s_per_lap * support_infl),
            support_status=base_eval.support_status,
            extrapolation_warning=base_eval.extrapolation_warning,
        )

    def race_progress_effect(
        self,
        race_progress: float,
        track_temperature_c: float | None = None,
        circuit_short_name: str | None = None,
        regulation_era: str = "2023_2025",
    ) -> tuple[float, float]:
        return self.base_model_.race_progress_effect(
            race_progress=race_progress,
            track_temperature_c=track_temperature_c,
            circuit_short_name=circuit_short_name,
            regulation_era=regulation_era,
        )

    def diagnostics_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "use_absolute_compounds": self.use_absolute_compounds,
            "use_circuit_pooling": self.use_circuit_pooling,
            "use_weekend_adaptation": self.use_weekend_adaptation,
            "use_nonlinear_curve": self.use_nonlinear_curve,
            "base_model": self.base_model_.diagnostics_dict(),
            "abs_compound_slope_deltas_s_per_lap": {
                k: round(v, 5) for k, v in self.abs_compound_slope_deltas_.items()
            },
            "circuit_slope_deltas_s_per_lap": {
                f"{c}_{r}": round(v, 5) for (c, r), v in self.circuit_slope_deltas_.items()
            },
            "era_slope_deltas_s_per_lap": {
                f"{e}_{r}": round(v, 5) for (e, r), v in self.era_slope_deltas_.items()
            },
        }
