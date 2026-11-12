from __future__ import annotations

import numpy as np
import pandas as pd

from f1ri.validation.stint_metrics import (
    classify_cold_start_regime,
    compute_cumulative_trajectory_metrics,
    compute_lap_horizon_metrics,
    paired_race_bootstrap_comparison,
)


def _make_forecasts_df() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    rows = []
    for sk, circ in [(9912, "Monza"), (9896, "Marina Bay"), (9955, "Spielberg")]:
        for i in range(20):
            obs_lap = 84.0 + float(rng.normal(0.0, 0.4))
            obs_cum = obs_lap * 5.0
            err_cand = float(rng.normal(0.05, 0.25))
            err_base = err_cand * 2.2
            for model_id, err in [("D8_cand", err_cand), ("D0_base", err_base)]:
                pred_lap = obs_lap - err
                pred_cum = obs_cum - err * 5.0
                rows.append(
                    {
                        "model_id": model_id,
                        "session_key": sk,
                        "stint_id": f"{sk}_{i % 5}_1",
                        "circuit_short_name": circ,
                        "driver_number": (i % 5) + 1,
                        "requested_horizon": 5,
                        "target_is_clean_green": i < 18,
                        "trajectory_is_complete_clean": i < 16,
                        "trajectory_censoring_reason": "COMPLETE_CLEAN" if i < 16 else "FUTURE_PIT_TRANSITION",
                        "observed_target_lap_time_s": obs_lap,
                        "expected_endpoint_lap_time_s": pred_lap,
                        "std_endpoint_lap_time_s": 0.35,
                        "p25_endpoint_lap_time_s": pred_lap - 0.24,
                        "p75_endpoint_lap_time_s": pred_lap + 0.24,
                        "p05_endpoint_lap_time_s": pred_lap - 0.58,
                        "p95_endpoint_lap_time_s": pred_lap + 0.58,
                        "observed_cumulative_time_s": obs_cum,
                        "expected_total_time_s": pred_cum,
                        "std_total_time_s": 1.4,
                        "p25_total_time_s": pred_cum - 0.95,
                        "p75_total_time_s": pred_cum + 0.95,
                        "p05_total_time_s": pred_cum - 2.30,
                        "p95_total_time_s": pred_cum + 2.30,
                        "observed_pace_change_s": 0.35,
                        "predicted_pace_change_s": 0.32 if model_id == "D8_cand" else 0.0,
                    }
                )
    return pd.DataFrame(rows)


def test_cold_start_regime_separation():
    # Issue 2 verification: 0A vs 0B separation
    assert classify_cold_start_regime(0, 0) == "0A_no_prior_clean_race_laps"
    assert classify_cold_start_regime(12, 0) == "0B_first_clean_lap_of_new_stint_with_race_history"
    assert classify_cold_start_regime(14, 2) == "1_2_early_stint"
    assert classify_cold_start_regime(20, 6) == "3_plus_warm_stint"


def test_stint_metrics_and_paired_race_bootstrap():
    df = _make_forecasts_df()
    df_cand = df[df["model_id"] == "D8_cand"]

    lap_m = compute_lap_horizon_metrics(df_cand, horizon=5)
    assert lap_m["eligible_forecasts"] > 0
    assert lap_m["unique_races"] == 3
    assert lap_m["race_weighted_mae_s"] > 0

    cum_m = compute_cumulative_trajectory_metrics(df_cand, horizon=5)
    assert cum_m["complete_eligible_trajectories"] > 0
    assert cum_m["cum_race_weighted_mae_s"] > 0
    assert "FUTURE_PIT_TRANSITION" in cum_m["censoring_breakdown"]

    boot = paired_race_bootstrap_comparison(
        df,
        candidate_model_id="D8_cand",
        baseline_model_id="D0_base",
        horizon=5,
        use_cumulative=True,
    )
    assert boot["n_races"] == 3
    assert boot["races_improved"] == 3
    assert boot["mean_race_mae_delta_s"] < 0.0
    assert boot["bootstrap_ci90_upper_s"] < 0.0
