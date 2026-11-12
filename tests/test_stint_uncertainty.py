from __future__ import annotations

import numpy as np

from f1ri.models.contracts import SUPPORT_EXTRAPOLATED, SUPPORT_SUPPORTED
from f1ri.models.stint_state import StintLatentStatePosterior
from f1ri.models.trajectory_uncertainty import CorrelatedTrajectorySampler


def test_correlated_trajectory_sampler_reproducibility_and_variance_scaling():
    state = StintLatentStatePosterior(
        offset_mean_s=0.10,
        slope_dev_mean_s_per_lap=0.01,
        offset_std_s=0.25,
        slope_dev_std_s_per_lap=0.018,
        cov_offset_slope=0.0,
        reference_tyre_age=4.0,
        n_stint_clean_laps=4,
    )
    mean_laps = np.full(10, 92.0, dtype=float)
    target_ages = np.arange(5, 15, dtype=int)
    support_statuses = [SUPPORT_SUPPORTED] * 10

    sampler = CorrelatedTrajectorySampler()
    res_corr_1 = sampler.sample_trajectories(
        expected_lap_times_s=mean_laps,
        projected_tyre_ages=target_ages,
        support_statuses=support_statuses,
        stint_posterior=state,
        clean_laps_observed_race=15,
        clean_laps_observed_current_stint=4,
        n_samples=2000,
        random_seed=42,
        independent_laps_ablation=False,
    )
    res_corr_2 = sampler.sample_trajectories(
        expected_lap_times_s=mean_laps,
        projected_tyre_ages=target_ages,
        support_statuses=support_statuses,
        stint_posterior=state,
        clean_laps_observed_race=15,
        clean_laps_observed_current_stint=4,
        n_samples=2000,
        random_seed=42,
        independent_laps_ablation=False,
    )
    res_indep = sampler.sample_trajectories(
        expected_lap_times_s=mean_laps,
        projected_tyre_ages=target_ages,
        support_statuses=support_statuses,
        stint_posterior=state,
        clean_laps_observed_race=15,
        clean_laps_observed_current_stint=4,
        n_samples=2000,
        random_seed=42,
        independent_laps_ablation=True,
    )

    # 1. Exact seed reproducibility
    np.testing.assert_allclose(res_corr_1["cum_p05"], res_corr_2["cum_p05"])
    np.testing.assert_allclose(res_corr_1["cum_p95"], res_corr_2["cum_p95"])

    # 2. Strictly ordered cumulative and per-lap quantiles
    for k in range(10):
        assert (
            res_corr_1["lap_p05"][k]
            < res_corr_1["lap_p25"][k]
            < res_corr_1["lap_median"][k]
            < res_corr_1["lap_p75"][k]
            < res_corr_1["lap_p95"][k]
        )
        assert (
            res_corr_1["cum_p05"][k]
            < res_corr_1["cum_p25"][k]
            < res_corr_1["cum_median"][k]
            < res_corr_1["cum_p75"][k]
            < res_corr_1["cum_p95"][k]
        )

    # 3. Correlated trajectory cumulative std at H=10 MUST exceed independent lap-noise cumulative std
    assert res_corr_1["cum_std"][-1] > res_indep["cum_std"][-1] * 1.8

    # 4. Extrapolated support inflates uncertainty relative to supported ages
    res_extrap = sampler.sample_trajectories(
        expected_lap_times_s=mean_laps,
        projected_tyre_ages=target_ages,
        support_statuses=[SUPPORT_EXTRAPOLATED] * 10,
        stint_posterior=state,
        clean_laps_observed_race=15,
        clean_laps_observed_current_stint=4,
        n_samples=2000,
        random_seed=42,
        independent_laps_ablation=False,
    )
    assert res_extrap["cum_std"][-1] > res_corr_1["cum_std"][-1]
