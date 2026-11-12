"""Online Bayesian / Kalman latent stint-state estimation (Model D7)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class StintLatentStatePosterior:
    """Posterior distribution over stint-specific pace offset b_s and degradation slope deviation d_s.

    State vector z_s = [b_s, d_s]^T referenced at `reference_tyre_age`:
        u_s(a) = b_s + d_s * (a - reference_tyre_age)
    """

    offset_mean_s: float
    slope_dev_mean_s_per_lap: float
    offset_std_s: float
    slope_dev_std_s_per_lap: float
    cov_offset_slope: float
    reference_tyre_age: float
    n_stint_clean_laps: int

    def evaluate_at_age(self, projected_tyre_age: float) -> tuple[float, float]:
        """Return (expected_stint_deviation_s, std_stint_deviation_s) at projected_tyre_age."""
        da = float(projected_tyre_age) - self.reference_tyre_age
        mean_u = float(self.offset_mean_s + self.slope_dev_mean_s_per_lap * da)
        var_u = max(
            float(
                self.offset_std_s**2
                + 2.0 * da * self.cov_offset_slope
                + (da**2) * (self.slope_dev_std_s_per_lap**2)
            ),
            1e-6,
        )
        return mean_u, float(np.sqrt(var_u))


@dataclass
class BayesianStintStateEstimator:
    """Conjugate linear-Gaussian Bayesian filter for stint-specific offset b_s and slope deviation d_s.

    At a pit stop / new stint (n_stint_clean_laps == 0), the tire-specific latent state resets to
    its prior N([0, 0], diag(prior_offset_std_s^2, prior_slope_std_s_per_lap^2)), while persistent
    driver/car race pace is carried separately in Component A.
    """

    prior_offset_std_s: float = 0.55
    prior_slope_std_s_per_lap: float = 0.022
    obs_noise_std_s: float = 0.28
    max_slope_dev_s_per_lap: float = 0.045
    enable_slope_update: bool = True

    def reset_state(self, reference_tyre_age: float = 0.0) -> StintLatentStatePosterior:
        """Return the fresh prior state for a newly started stint (tire-change state reset)."""
        return StintLatentStatePosterior(
            offset_mean_s=0.0,
            slope_dev_mean_s_per_lap=0.0,
            offset_std_s=float(self.prior_offset_std_s),
            slope_dev_std_s_per_lap=float(self.prior_slope_std_s_per_lap),
            cov_offset_slope=0.0,
            reference_tyre_age=float(reference_tyre_age),
            n_stint_clean_laps=0,
        )

    def estimate_posterior(
        self,
        observed_ages: tuple[int, ...] | list[int],
        structural_residuals_s: tuple[float, ...] | list[float] | np.ndarray,
        *,
        fallback_recent_residual_s: float | None = None,
        n_clean_stint: int = 0,
        reference_tyre_age: float | None = None,
    ) -> StintLatentStatePosterior:
        """Compute exact linear-Gaussian posterior for z_s = [b_s, d_s]^T from current-stint clean laps."""
        ages_arr = np.asarray(observed_ages, dtype=float)
        res_arr = np.asarray(structural_residuals_s, dtype=float)
        m = min(len(ages_arr), len(res_arr))

        if m == 0:
            if n_clean_stint > 0 and fallback_recent_residual_s is not None:
                # Fallback when only summary median is supplied
                ref_a = float(reference_tyre_age or 5.0)
                prec_0 = 1.0 / max(self.prior_offset_std_s**2, 1e-4)
                prec_obs = float(n_clean_stint) / max(self.obs_noise_std_s**2, 1e-4)
                post_var_b = 1.0 / (prec_0 + prec_obs)
                post_b = float(post_var_b * prec_obs * float(fallback_recent_residual_s))
                return StintLatentStatePosterior(
                    offset_mean_s=post_b,
                    slope_dev_mean_s_per_lap=0.0,
                    offset_std_s=float(np.sqrt(post_var_b)),
                    slope_dev_std_s_per_lap=float(self.prior_slope_std_s_per_lap),
                    cov_offset_slope=0.0,
                    reference_tyre_age=ref_a,
                    n_stint_clean_laps=int(n_clean_stint),
                )
            return self.reset_state(reference_tyre_age=float(reference_tyre_age or 0.0))

        ages_arr = ages_arr[:m]
        res_arr = res_arr[:m]

        # Robustly winsorize individual stint residuals around the stint median so a single
        # driver lock-up lap does not corrupt the estimated degradation slope d_s.
        med_r = float(np.median(res_arr))
        if m >= 3:
            mad_r = max(float(np.median(np.abs(res_arr - med_r))), 0.12)
            res_clean = np.clip(res_arr, med_r - 3.0 * 1.4826 * mad_r, med_r + 3.0 * 1.4826 * mad_r)
        else:
            res_clean = res_arr

        # Anchor reference age on the recent clean laps (up to last 3) so b_s tracks current level
        recent_k = min(m, 3)
        ref_age = (
            float(reference_tyre_age)
            if reference_tyre_age is not None
            else float(np.mean(ages_arr[-recent_k:]))
        )
        da = ages_arr - ref_age

        # Recency weighting: recent clean laps receive higher weight for level b_s
        lap_weights = np.exp(-0.18 * np.arange(m - 1, -1, -1, dtype=float))
        sigma2 = max(self.obs_noise_std_s**2, 1e-4)

        if not self.enable_slope_update or m < 3 or (np.max(ages_arr) - np.min(ages_arr)) < 2.5:
            # Update offset b_s only; keep slope deviation d_s at 0 with prior uncertainty
            prec_0 = 1.0 / max(self.prior_offset_std_s**2, 1e-4)
            prec_obs = float(np.sum(lap_weights)) / sigma2
            post_var_b = 1.0 / (prec_0 + prec_obs)
            # Use robust median of recent 3 structural residuals when available
            recent_target = float(np.median(res_clean[-recent_k:]))
            post_b = float(post_var_b * (prec_obs * recent_target))
            return StintLatentStatePosterior(
                offset_mean_s=post_b,
                slope_dev_mean_s_per_lap=0.0,
                offset_std_s=float(np.sqrt(post_var_b)),
                slope_dev_std_s_per_lap=float(self.prior_slope_std_s_per_lap),
                cov_offset_slope=0.0,
                reference_tyre_age=ref_age,
                n_stint_clean_laps=m,
            )

        # Full 2D conjugate linear-Gaussian Bayesian update for z_s = [b_s, d_s]^T
        H = np.column_stack([np.ones(m, dtype=float), da])
        W = np.diag(lap_weights / sigma2)
        prior_prec = np.diag(
            [
                1.0 / max(self.prior_offset_std_s**2, 1e-5),
                1.0 / max(self.prior_slope_std_s_per_lap**2, 1e-6),
            ]
        )
        post_prec = prior_prec + H.T @ W @ H
        post_cov = np.linalg.inv(post_prec)
        post_mean = post_cov @ (H.T @ W @ res_clean)

        # Anchor b_s tightly to the robust recent 3-lap residual median while preserving learned slope d_s
        d_s = float(
            np.clip(post_mean[1], -self.max_slope_dev_s_per_lap, self.max_slope_dev_s_per_lap)
        )
        recent_res_adj = res_clean[-recent_k:] - d_s * da[-recent_k:]
        b_recent = float(np.median(recent_res_adj))
        w_b = float(m) / (float(m) + 0.6)
        b_s = float(w_b * b_recent + (1.0 - w_b) * float(post_mean[0]))

        return StintLatentStatePosterior(
            offset_mean_s=b_s,
            slope_dev_mean_s_per_lap=d_s,
            offset_std_s=float(np.sqrt(max(post_cov[0, 0], 1e-6))),
            slope_dev_std_s_per_lap=float(np.sqrt(max(post_cov[1, 1], 1e-8))),
            cov_offset_slope=float(post_cov[0, 1]),
            reference_tyre_age=ref_age,
            n_stint_clean_laps=m,
        )
