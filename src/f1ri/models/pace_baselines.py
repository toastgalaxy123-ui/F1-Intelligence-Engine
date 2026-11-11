"""Point-in-time pace baseline estimators (E0 through E3)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from f1ri.models.contracts import PaceDistribution, PaceRequest


def _max_utc(df: pd.DataFrame) -> datetime:
    if "forecast_as_of_utc" in df.columns and df["forecast_as_of_utc"].notna().any():
        ts = pd.to_datetime(df["forecast_as_of_utc"], utc=True).max()
        return ts.to_pydatetime()
    return datetime(2025, 1, 1, tzinfo=UTC)


@dataclass
class HistoricalCircuitPriorBaseline:
    """Baseline A (E0): Historical circuit + team + driver + compound reference pace.

    Uses zero observations from the current race. Shrinks circuit/team/driver offsets
    toward parent group means so unseen circuits, teams, or drivers have valid fallbacks.
    """

    shrinkage_k: float = 5.0
    model_version: str = "E0_historical_circuit_prior_v1"
    global_median_: float = 90.0
    global_std_: float = 1.5
    circuit_medians_: dict[str, float] = field(default_factory=dict)
    circuit_stds_: dict[str, float] = field(default_factory=dict)
    era_offsets_: dict[str, float] = field(default_factory=dict)
    team_offsets_: dict[tuple[str, str], float] = field(default_factory=dict)
    team_global_offsets_: dict[str, float] = field(default_factory=dict)
    driver_offsets_: dict[str, float] = field(default_factory=dict)
    compound_offsets_: dict[str, float] = field(default_factory=dict)
    progress_slope_: float = -1.2
    trained_through_utc_: datetime = field(default_factory=lambda: datetime(1970, 1, 1, tzinfo=UTC))
    is_fitted_: bool = False

    def fit(self, train_df: pd.DataFrame) -> HistoricalCircuitPriorBaseline:
        clean = train_df[
            train_df["target_is_clean_green"].fillna(False).astype(bool)
            & train_df["observed_target_lap_time_s"].notna()
            & (train_df["horizon"] == 1)
        ].copy()
        if clean.empty:
            raise ValueError("No eligible clean green training examples for HistoricalCircuitPriorBaseline")

        self.trained_through_utc_ = _max_utc(clean)
        y = clean["observed_target_lap_time_s"].to_numpy(dtype=float)
        self.global_median_ = float(np.median(y))
        mad = float(np.median(np.abs(y - self.global_median_)))
        self.global_std_ = max(float(1.4826 * mad), 0.45)

        # Circuit medians (anchored on latest available 2023-2025 season per circuit when available)
        self.circuit_medians_ = {}
        self.circuit_stds_ = {}
        for circ, grp in clean.groupby("circuit_short_name"):
            if pd.isna(circ):
                continue
            base_grp = grp[grp["regulation_era"] == "2023_2025"]
            if base_grp.empty:
                base_grp = grp
            vals = base_grp["observed_target_lap_time_s"].to_numpy(dtype=float)
            max_season = base_grp["season"].max()
            latest_vals = base_grp[base_grp["season"] == max_season][
                "observed_target_lap_time_s"
            ].to_numpy(dtype=float)
            med = float(0.65 * np.median(latest_vals) + 0.35 * np.median(vals))
            c_mad = float(np.median(np.abs(vals - np.median(vals))))
            self.circuit_medians_[str(circ)] = med
            self.circuit_stds_[str(circ)] = max(float(1.4826 * c_mad), 0.35)

        clean["circuit_base"] = clean["circuit_short_name"].map(self.circuit_medians_).fillna(
            self.global_median_
        )
        # Session-level centering for team/driver relative pace so year-to-year track shifts do not confound team offsets
        session_medians = clean.groupby("session_key")["observed_target_lap_time_s"].transform(
            "median"
        )
        clean["resid_c"] = clean["observed_target_lap_time_s"] - session_medians

        base_circuits = set(
            clean[clean["regulation_era"] == "2023_2025"]["circuit_short_name"].dropna().unique()
        )
        self.era_offsets_ = {"2023_2025": 0.0}
        for era, grp in clean.groupby("regulation_era"):
            if str(era) == "2023_2025":
                continue
            shared_grp = grp[grp["circuit_short_name"].isin(base_circuits)]
            if not shared_grp.empty:
                era_diff = (
                    shared_grp["observed_target_lap_time_s"] - shared_grp["circuit_base"]
                ).to_numpy(dtype=float)
                n_races = int(shared_grp["session_key"].nunique())
                w = n_races / (n_races + 2.5)
                self.era_offsets_[str(era)] = float(w * np.median(era_diff))
            else:
                self.era_offsets_[str(era)] = 0.0

        # Team offsets (shrunk)
        self.team_global_offsets_ = {}
        for team, grp in clean.groupby("team_id"):
            n = len(grp)
            w = n / (n + self.shrinkage_k)
            self.team_global_offsets_[str(team)] = float(w * grp["resid_c"].median())

        self.team_offsets_ = {}
        for (era, team), grp in clean.groupby(["regulation_era", "team_id"]):
            n = len(grp)
            w = n / (n + self.shrinkage_k)
            parent = self.team_global_offsets_.get(str(team), 0.0)
            self.team_offsets_[(str(era), str(team))] = float(
                w * grp["resid_c"].median() + (1.0 - w) * parent
            )

        clean["team_off"] = [
            self.team_offsets_.get(
                (str(e), str(t)), self.team_global_offsets_.get(str(t), 0.0)
            )
            for e, t in zip(clean["regulation_era"], clean["team_id"], strict=False)
        ]
        clean["resid_ct"] = clean["resid_c"] - clean["team_off"]

        # Driver offsets (shrunk)
        self.driver_offsets_ = {}
        for drv, grp in clean.groupby("driver_id"):
            n = len(grp)
            w = n / (n + self.shrinkage_k * 2.0)
            self.driver_offsets_[str(drv)] = float(w * grp["resid_ct"].median())

        clean["drv_off"] = clean["driver_id"].map(self.driver_offsets_).fillna(0.0)
        clean["resid_ctd"] = clean["resid_ct"] - clean["drv_off"]

        # Compound offsets and progress slope
        self.compound_offsets_ = {}
        for cmpd, grp in clean.groupby("compound"):
            n = len(grp)
            w = n / (n + self.shrinkage_k)
            self.compound_offsets_[str(cmpd)] = float(w * grp["resid_ctd"].median())

        if clean["race_progress"].notna().sum() > 10:
            p = clean["race_progress"].to_numpy(dtype=float)
            r = clean["resid_ctd"].to_numpy(dtype=float)
            p_centered = p - np.mean(p)
            denom = float(np.dot(p_centered, p_centered)) + 10.0
            self.progress_slope_ = float(np.dot(p_centered, r) / denom)

        self.is_fitted_ = True
        return self

    def predict(self, request: PaceRequest) -> PaceDistribution:
        if not self.is_fitted_:
            raise RuntimeError("Fit HistoricalCircuitPriorBaseline before calling predict")
        circ = request.circuit_short_name or ""
        unseen_circuit = circ not in self.circuit_medians_
        base = self.circuit_medians_.get(circ, self.global_median_)
        era = request.regulation_era or "2023_2025"
        era_off = self.era_offsets_.get(era, 0.0) if not unseen_circuit else 0.0
        team = request.team_id or "UNKNOWN_TEAM"
        drv = request.driver_id or "UNKNOWN_DRIVER"
        team_off = self.team_offsets_.get(
            (era, team), self.team_global_offsets_.get(team, 0.0)
        )
        drv_off = self.driver_offsets_.get(drv, 0.0)
        cmpd_off = self.compound_offsets_.get(request.compound, 0.0)
        prog_adj = (
            self.progress_slope_ * (float(request.race_progress) - 0.5)
            if request.race_progress is not None
            else 0.0
        )
        expected = float(base + era_off + team_off + drv_off + cmpd_off + prog_adj)
        std = self.circuit_stds_.get(circ, self.global_std_)
        if unseen_circuit or (team not in self.team_global_offsets_ and drv not in self.driver_offsets_):
            std = max(std * 1.35, 0.90)
        return PaceDistribution(
            expected_seconds=expected,
            std_seconds=float(std),
            model_version=self.model_version,
            trained_through_utc=self.trained_through_utc_,
            confidence_level="COLD_START",
            forecast_as_of_utc=request.as_of_utc,
        )


@dataclass
class RecentCleanLapMedianBaseline:
    """Baseline B (E1): Median of up to last 3 clean green laps in the current race, falling back to E0."""

    fallback_prior: HistoricalCircuitPriorBaseline = field(
        default_factory=HistoricalCircuitPriorBaseline
    )
    model_version: str = "E1_recent_clean_median_v1"
    residual_std_: float = 0.45
    trained_through_utc_: datetime = field(default_factory=lambda: datetime(1970, 1, 1, tzinfo=UTC))
    is_fitted_: bool = False

    def fit(self, train_df: pd.DataFrame) -> RecentCleanLapMedianBaseline:
        self.fallback_prior.fit(train_df)
        self.trained_through_utc_ = self.fallback_prior.trained_through_utc_
        clean = train_df[
            train_df["target_is_clean_green"].fillna(False).astype(bool)
            & train_df["observed_target_lap_time_s"].notna()
            & train_df["median_last_3_clean_laps_s"].notna()
            & (train_df["horizon"] == 1)
        ]
        if not clean.empty:
            res = (
                clean["observed_target_lap_time_s"].to_numpy(dtype=float)
                - clean["median_last_3_clean_laps_s"].to_numpy(dtype=float)
            )
            mad = float(np.median(np.abs(res - np.median(res))))
            self.residual_std_ = max(float(1.4826 * mad), 0.25)
        self.is_fitted_ = True
        return self

    def predict(self, request: PaceRequest) -> PaceDistribution:
        if not self.is_fitted_:
            raise RuntimeError("Fit RecentCleanLapMedianBaseline before calling predict")
        if request.median_last_3_clean_laps_s is not None:
            scale = self.residual_std_ * (1.0 + 0.06 * (max(request.horizon, 1) - 1))
            return PaceDistribution(
                expected_seconds=float(request.median_last_3_clean_laps_s),
                std_seconds=float(scale),
                model_version=self.model_version,
                trained_through_utc=self.trained_through_utc_,
                confidence_level="NORMAL" if request.clean_laps_observed_race >= 3 else "LOW",
                forecast_as_of_utc=request.as_of_utc,
            )
        prior_dist = self.fallback_prior.predict(request)
        return PaceDistribution(
            expected_seconds=prior_dist.expected_seconds,
            std_seconds=prior_dist.std_seconds,
            model_version=self.model_version,
            trained_through_utc=self.trained_through_utc_,
            confidence_level="COLD_START",
            forecast_as_of_utc=request.as_of_utc,
        )


@dataclass
class CompoundRecentMedianBaseline:
    """Baseline C (E2): Median of up to last 3 clean green laps on the current stint/compound."""

    fallback_recent: RecentCleanLapMedianBaseline = field(
        default_factory=RecentCleanLapMedianBaseline
    )
    model_version: str = "E2_compound_recent_median_v1"
    residual_std_: float = 0.40
    trained_through_utc_: datetime = field(default_factory=lambda: datetime(1970, 1, 1, tzinfo=UTC))
    is_fitted_: bool = False

    def fit(self, train_df: pd.DataFrame) -> CompoundRecentMedianBaseline:
        self.fallback_recent.fit(train_df)
        self.trained_through_utc_ = self.fallback_recent.trained_through_utc_
        clean = train_df[
            train_df["target_is_clean_green"].fillna(False).astype(bool)
            & train_df["observed_target_lap_time_s"].notna()
            & train_df["median_stint_clean_laps_s"].notna()
            & (train_df["horizon"] == 1)
        ]
        if not clean.empty:
            res = (
                clean["observed_target_lap_time_s"].to_numpy(dtype=float)
                - clean["median_stint_clean_laps_s"].to_numpy(dtype=float)
            )
            mad = float(np.median(np.abs(res - np.median(res))))
            self.residual_std_ = max(float(1.4826 * mad), 0.22)
        self.is_fitted_ = True
        return self

    def predict(self, request: PaceRequest) -> PaceDistribution:
        if not self.is_fitted_:
            raise RuntimeError("Fit CompoundRecentMedianBaseline before calling predict")
        if request.median_stint_clean_laps_s is not None:
            scale = self.residual_std_ * (1.0 + 0.06 * (max(request.horizon, 1) - 1))
            conf = "HIGH" if request.clean_laps_observed_current_stint >= 3 else "NORMAL"
            return PaceDistribution(
                expected_seconds=float(request.median_stint_clean_laps_s),
                std_seconds=float(scale),
                model_version=self.model_version,
                trained_through_utc=self.trained_through_utc_,
                confidence_level=conf,
                forecast_as_of_utc=request.as_of_utc,
            )
        fb = self.fallback_recent.predict(request)
        return PaceDistribution(
            expected_seconds=fb.expected_seconds,
            std_seconds=max(fb.std_seconds * 1.1, self.residual_std_),
            model_version=self.model_version,
            trained_through_utc=self.trained_through_utc_,
            confidence_level="LOW" if request.clean_laps_observed_race > 0 else "COLD_START",
            forecast_as_of_utc=request.as_of_utc,
        )


@dataclass
class RaceProgressDriftBaseline:
    """Baseline D (E3): Compound-specific recent pace plus linear race-progress drift."""

    base_compound_model: CompoundRecentMedianBaseline = field(
        default_factory=CompoundRecentMedianBaseline
    )
    model_version: str = "E3_race_progress_drift_v1"
    per_lap_drift_s_: float = -0.025
    residual_std_: float = 0.39
    trained_through_utc_: datetime = field(default_factory=lambda: datetime(1970, 1, 1, tzinfo=UTC))
    is_fitted_: bool = False

    def fit(self, train_df: pd.DataFrame) -> RaceProgressDriftBaseline:
        self.base_compound_model.fit(train_df)
        self.trained_through_utc_ = self.base_compound_model.trained_through_utc_
        clean = train_df[
            train_df["target_is_clean_green"].fillna(False).astype(bool)
            & train_df["observed_target_lap_time_s"].notna()
            & train_df["median_stint_clean_laps_s"].notna()
            & (train_df["horizon"] == 1)
        ].copy()
        if not clean.empty:
            diffs = (
                clean["observed_target_lap_time_s"].to_numpy(dtype=float)
                - clean["median_stint_clean_laps_s"].to_numpy(dtype=float)
            )
            eff_delta_laps = np.clip(
                clean["last_clean_lap_age"].fillna(1.0).to_numpy(dtype=float)
                + 0.5 * np.minimum(clean["clean_laps_observed_current_stint"].to_numpy(dtype=float) - 1.0, 2.0),
                1.0,
                10.0,
            )
            denom = float(np.dot(eff_delta_laps, eff_delta_laps)) + 50.0
            self.per_lap_drift_s_ = float(np.clip(np.dot(eff_delta_laps, diffs) / denom, -0.08, 0.08))
            res = diffs - self.per_lap_drift_s_ * eff_delta_laps
            mad = float(np.median(np.abs(res - np.median(res))))
            self.residual_std_ = max(float(1.4826 * mad), 0.22)
        self.is_fitted_ = True
        return self

    def predict(self, request: PaceRequest) -> PaceDistribution:
        if not self.is_fitted_:
            raise RuntimeError("Fit RaceProgressDriftBaseline before calling predict")
        if request.median_stint_clean_laps_s is not None:
            n_stint = max(int(request.clean_laps_observed_current_stint), 1)
            age_gap = float(request.last_clean_lap_age or request.horizon)
            eff_steps = age_gap + 0.5 * min(n_stint - 1, 2)
            expected = float(request.median_stint_clean_laps_s + self.per_lap_drift_s_ * eff_steps)
            scale = self.residual_std_ * (1.0 + 0.06 * (max(request.horizon, 1) - 1))
            return PaceDistribution(
                expected_seconds=expected,
                std_seconds=float(scale),
                model_version=self.model_version,
                trained_through_utc=self.trained_through_utc_,
                confidence_level="HIGH" if n_stint >= 3 else "NORMAL",
                forecast_as_of_utc=request.as_of_utc,
            )
        return self.base_compound_model.predict(request)
