from __future__ import annotations

import numpy as np

from f1ri.models.stint_state import BayesianStintStateEstimator


def test_bayesian_stint_state_posterior_update_and_slope_learning():
    est = BayesianStintStateEstimator(
        prior_offset_std_s=0.55,
        prior_slope_std_s_per_lap=0.022,
        obs_noise_std_s=0.20,
        enable_slope_update=True,
    )

    # Suppose a driver's current tire set is degrading +0.035 s/lap faster than the prior curve,
    # and has a +0.30 s baseline offset at the reference age.
    ages = [2, 3, 4, 5, 6, 7, 8, 9]
    ages_arr = np.asarray(ages, dtype=float)
    ref_age = float(np.mean(ages_arr[-3:]))
    true_offset = 0.30
    true_extra_slope = 0.035
    residuals = true_offset + true_extra_slope * (ages_arr - ref_age)

    state = est.estimate_posterior(
        observed_ages=ages,
        structural_residuals_s=residuals,
        n_clean_stint=len(ages),
    )
    assert state.n_stint_clean_laps == 8
    assert state.offset_mean_s > 0.20
    assert 0.005 < state.slope_dev_mean_s_per_lap < true_extra_slope
    # Posterior standard deviations must shrink strictly below prior standard deviations
    assert state.offset_std_s < est.prior_offset_std_s
    assert state.slope_dev_std_s_per_lap < est.prior_slope_std_s_per_lap

    # Project over future ages 10..19: expected deviation and std must grow monotonically with age
    future_ages = np.arange(10, 20, dtype=float)
    evals = [state.evaluate_at_age(a) for a in future_ages]
    means = np.array([e[0] for e in evals])
    stds = np.array([e[1] for e in evals])
    assert np.all(np.diff(means) > 0.0)
    assert np.all(np.diff(stds) > 0.0)


def test_stint_state_resets_on_new_stint():
    est = BayesianStintStateEstimator()
    state_fresh = est.reset_state(reference_tyre_age=0.0)
    assert state_fresh.n_stint_clean_laps == 0
    assert state_fresh.offset_mean_s == 0.0
    assert state_fresh.slope_dev_mean_s_per_lap == 0.0
    assert state_fresh.offset_std_s == est.prior_offset_std_s
    assert state_fresh.slope_dev_std_s_per_lap == est.prior_slope_std_s_per_lap
