"""Correlated Monte Carlo trajectory uncertainty sampler (Model D8, Section 11)."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from f1ri.models.contracts import (
    SUPPORT_EXTRAPOLATED,
    SUPPORT_LIMITED,
    SUPPORT_UNSUPPORTED,
)
from f1ri.models.stint_state import StintLatentStatePosterior


@dataclass
class CorrelatedTrajectorySampler:
    """Samples correlated multi-lap stint trajectories decomposing 3 sources of uncertainty:

    1. Persistent pace-level error eta_pace ~ N(0, sigma_pace^2): constant across the stint.
    2. Persistent degradation-slope error eta_deg ~ N(0, sigma_deg^2): accumulates with step/age.
    3. Autocorrelated lap-level variation eps_k = rho * eps_{k-1} + sqrt(1 - rho^2) * sigma_lap * xi_k.
    """

    ar1_rho: float = 0.30
    base_lap_noise_std_s: float = 0.28
    cold_race_pace_std_s: float = 0.95
    cold_stint_pace_std_s: float = 0.56
    warm_stint_pace_std_s: float = 0.24
    base_deg_slope_std_s: float = 0.024
    outlier_mixture_prob: float = 0.07
    outlier_scale_mult: float = 2.5
    empirical_scale_mult_: float = 1.0
    cum_horizon_scale_: dict[int, float] = field(default_factory=dict)
    is_calibrated_: bool = False

    def fit_calibration(
        self,
        calib_residuals_h1: np.ndarray,
        calib_ar1_rho: float | None = None,
    ) -> CorrelatedTrajectorySampler:
        """Calibrate innovation dispersion and autocorrelation from validation stint residuals."""
        valid = calib_residuals_h1[np.isfinite(calib_residuals_h1)]
        if len(valid) >= 20:
            med = float(np.median(valid))
            mad = float(np.median(np.abs(valid - med)))
            emp_std = max(float(1.4826 * mad), 0.22)
            # Partition total 1-step variance into persistent + innovation components
            self.base_lap_noise_std_s = float(np.clip(emp_std * 0.95, 0.22, 0.45))
            self.warm_stint_pace_std_s = float(np.clip(emp_std * 0.78, 0.19, 0.34))

        if calib_ar1_rho is not None and np.isfinite(calib_ar1_rho):
            self.ar1_rho = float(np.clip(calib_ar1_rho, 0.15, 0.65))

        self.is_calibrated_ = True
        return self

    def sample_trajectories(
        self,
        expected_lap_times_s: np.ndarray,
        projected_tyre_ages: np.ndarray,
        support_statuses: list[str],
        stint_posterior: StintLatentStatePosterior,
        *,
        clean_laps_observed_race: int,
        clean_laps_observed_current_stint: int,
        model_slope_std_s_per_lap: float = 0.014,
        n_samples: int = 2000,
        random_seed: int = 42,
        independent_laps_ablation: bool = False,
    ) -> dict[str, np.ndarray]:
        """Generate (n_samples, H) correlated lap-time trajectories and marginal/cumulative summaries."""
        h_laps = len(expected_lap_times_s)
        if h_laps == 0:
            return {}

        rng = np.random.default_rng(int(random_seed))

        # 1. Persistent pace-level uncertainty sigma_pace
        if clean_laps_observed_race <= 0:
            sigma_pace = self.cold_race_pace_std_s
        elif clean_laps_observed_current_stint <= 0:
            sigma_pace = self.cold_stint_pace_std_s
        elif clean_laps_observed_current_stint == 1:
            sigma_pace = max(self.warm_stint_pace_std_s * 1.45, stint_posterior.offset_std_s * 0.85)
        elif clean_laps_observed_current_stint == 2:
            sigma_pace = max(self.warm_stint_pace_std_s * 1.15, stint_posterior.offset_std_s * 0.75)
        else:
            sigma_pace = max(
                self.warm_stint_pace_std_s,
                min(stint_posterior.offset_std_s * 0.65, 0.24),
            )

        # 2. Persistent degradation-slope uncertainty sigma_deg
        sigma_deg = float(
            np.hypot(
                max(model_slope_std_s_per_lap, self.base_deg_slope_std_s),
                stint_posterior.slope_dev_std_s_per_lap * 0.75,
            )
        )

        # Step-specific support multipliers for extrapolation risk
        support_mult = np.ones(h_laps, dtype=float)
        for k, status in enumerate(support_statuses):
            if status == SUPPORT_LIMITED:
                support_mult[k] = 1.10
            elif status == SUPPORT_EXTRAPOLATED:
                support_mult[k] = 1.28
            elif status == SUPPORT_UNSUPPORTED:
                support_mult[k] = 1.50

        steps_from_cutoff = np.arange(1, h_laps + 1, dtype=float)

        if independent_laps_ablation:
            # Ablation mode: i.i.d. sampling without persistent pace/slope correlation across steps
            marginal_var = (
                sigma_pace**2
                + (steps_from_cutoff * sigma_deg) ** 2
                + (self.base_lap_noise_std_s * support_mult) ** 2
            )
            marginal_std = np.sqrt(marginal_var)
            z_iid = rng.standard_normal((n_samples, h_laps))
            trajectories = expected_lap_times_s[None, :] + z_iid * marginal_std[None, :]
        else:
            # Correlated sampling: persistent pace error + persistent slope error + AR(1) lap noise
            eta_pace = rng.normal(0.0, sigma_pace, size=(n_samples, 1))
            eta_deg = rng.normal(0.0, sigma_deg, size=(n_samples, 1))

            # Heavy-tailed mixture innovations for realistic lap-level variability
            z_norm = rng.standard_normal((n_samples, h_laps))
            is_outlier = rng.random((n_samples, h_laps)) < self.outlier_mixture_prob
            scale_norm = np.sqrt(
                1.0
                - self.outlier_mixture_prob
                + self.outlier_mixture_prob * (self.outlier_scale_mult**2)
            )
            xi = np.where(is_outlier, z_norm * self.outlier_scale_mult, z_norm) / scale_norm

            eps = np.zeros((n_samples, h_laps), dtype=float)
            rho = float(np.clip(self.ar1_rho, 0.0, 0.85))
            innov_scale = np.sqrt(max(1.0 - rho**2, 1e-4))
            eps[:, 0] = self.base_lap_noise_std_s * support_mult[0] * xi[:, 0]
            for k in range(1, h_laps):
                eps[:, k] = (
                    rho * eps[:, k - 1]
                    + innov_scale * self.base_lap_noise_std_s * support_mult[k] * xi[:, k]
                )

            # Sublinear accumulation of slope uncertainty over short horizons, linear over long stints
            eff_deg_steps = np.power(steps_from_cutoff, 0.92) * support_mult
            trajectories = (
                expected_lap_times_s[None, :]
                + eta_pace
                + eta_deg * eff_deg_steps[None, :]
                + eps
            )

        cum_trajectories = np.cumsum(trajectories, axis=1)

        lap_q05, lap_q25, lap_q50, lap_q75, lap_q95 = np.quantile(
            trajectories, [0.05, 0.25, 0.50, 0.75, 0.95], axis=0
        )
        lap_std = np.std(trajectories, axis=0, ddof=1)

        cum_q05, cum_q25, cum_q50, cum_q75, cum_q95 = np.quantile(
            cum_trajectories, [0.05, 0.25, 0.50, 0.75, 0.95], axis=0
        )
        cum_std = np.std(cum_trajectories, axis=0, ddof=1)
        cum_expected = np.cumsum(expected_lap_times_s)

        # Center quantile bands around analytical expected_lap_times_s / cum_expected to eliminate Monte Carlo mean jitter
        mc_lap_mean = np.mean(trajectories, axis=0)
        lap_shift = expected_lap_times_s - mc_lap_mean
        mc_cum_mean = np.mean(cum_trajectories, axis=0)
        cum_shift = cum_expected - mc_cum_mean

        return {
            "trajectories": trajectories + lap_shift[None, :],
            "cum_trajectories": cum_trajectories + cum_shift[None, :],
            "lap_median": lap_q50 + lap_shift,
            "lap_std": np.maximum(lap_std, 0.16),
            "lap_p05": lap_q05 + lap_shift,
            "lap_p25": lap_q25 + lap_shift,
            "lap_p75": lap_q75 + lap_shift,
            "lap_p95": lap_q95 + lap_shift,
            "cum_expected": cum_expected,
            "cum_median": cum_q50 + cum_shift,
            "cum_std": np.maximum(cum_std, 0.16),
            "cum_p05": cum_q05 + cum_shift,
            "cum_p25": cum_q25 + cum_shift,
            "cum_p75": cum_q75 + cum_shift,
            "cum_p95": cum_q95 + cum_shift,
        }


def estimate_within_stint_autocorrelation(
    stint_residuals_df: pd.DataFrame,
    residual_col: str = "residual_s",
    max_lag: int = 5,
) -> dict[int, float]:
    """Compute empirical lag-1..max_lag autocorrelation of clean-lap residuals within stints."""
    if stint_residuals_df.empty or residual_col not in stint_residuals_df.columns:
        return {lag: 0.0 for lag in range(1, max_lag + 1)}

    df = stint_residuals_df.dropna(subset=[residual_col]).sort_values(
        ["session_key", "driver_number", "stint_number", "target_lap_number"]
    )
    autocorrs: dict[int, float] = {}
    for lag in range(1, max_lag + 1):
        x_list: list[float] = []
        y_list: list[float] = []
        for _, grp in df.groupby(["session_key", "driver_number", "stint_number"]):
            if len(grp) <= lag:
                continue
            r = grp[residual_col].to_numpy(dtype=float)
            laps = grp["target_lap_number"].to_numpy(dtype=int)
            for i in range(len(r) - lag):
                if laps[i + lag] - laps[i] == lag:
                    x_list.append(float(r[i]))
                    y_list.append(float(r[i + lag]))
        if len(x_list) >= 10 and np.std(x_list) > 1e-8 and np.std(y_list) > 1e-8:
            autocorrs[lag] = round(float(np.corrcoef(x_list, y_list)[0, 1]), 4)
        else:
            autocorrs[lag] = 0.0
    return autocorrs
