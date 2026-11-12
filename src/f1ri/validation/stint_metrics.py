"""Lap-level and cumulative stint-trajectory evaluation metrics (Section 13)."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from f1ri.validation.calibration import gaussian_crps


def compute_lap_horizon_metrics(
    forecasts_df: pd.DataFrame,
    horizon: int,
) -> dict[str, Any]:
    """Evaluate Dataset A (individual endpoint target laps at horizon H) with forecast/stint/race weighting."""
    sub_h = forecasts_df[forecasts_df["requested_horizon"] == int(horizon)]
    total_opps = len(sub_h)
    eligible = sub_h[
        sub_h["target_is_clean_green"].fillna(False).astype(bool)
        & sub_h["observed_target_lap_time_s"].notna()
        & sub_h["expected_endpoint_lap_time_s"].notna()
    ].copy()

    if eligible.empty:
        return {
            "horizon": int(horizon),
            "forecast_opportunities": int(total_opps),
            "eligible_forecasts": 0,
            "unique_stints": 0,
            "unique_races": 0,
            "unique_circuits": 0,
            "forecast_weighted_mae_s": None,
            "stint_weighted_mae_s": None,
            "race_weighted_mae_s": None,
            "rmse_s": None,
            "bias_s": None,
            "coverage_50": None,
            "coverage_90": None,
            "lap_crps_s": None,
        }

    y_true = eligible["observed_target_lap_time_s"].to_numpy(dtype=float)
    y_pred = eligible["expected_endpoint_lap_time_s"].to_numpy(dtype=float)
    res = y_true - y_pred
    abs_err = np.abs(res)
    eligible["abs_err"] = abs_err

    fw_mae = float(np.mean(abs_err))
    sw_mae = (
        float(eligible.groupby("stint_id")["abs_err"].mean().mean())
        if "stint_id" in eligible.columns
        else fw_mae
    )
    rw_mae = float(eligible.groupby("session_key")["abs_err"].mean().mean())
    rmse = float(np.sqrt(np.mean(res**2)))
    bias = float(np.mean(res))

    cov_50 = float(
        np.mean(
            (y_true >= eligible["p25_endpoint_lap_time_s"].to_numpy(dtype=float))
            & (y_true <= eligible["p75_endpoint_lap_time_s"].to_numpy(dtype=float))
        )
    )
    cov_90 = float(
        np.mean(
            (y_true >= eligible["p05_endpoint_lap_time_s"].to_numpy(dtype=float))
            & (y_true <= eligible["p95_endpoint_lap_time_s"].to_numpy(dtype=float))
        )
    )
    crps = float(
        np.mean(
            gaussian_crps(
                y_pred,
                eligible["std_endpoint_lap_time_s"].to_numpy(dtype=float),
                y_true,
            )
        )
    )

    return {
        "horizon": int(horizon),
        "forecast_opportunities": int(total_opps),
        "eligible_forecasts": len(eligible),
        "unique_stints": int(eligible["stint_id"].nunique()) if "stint_id" in eligible.columns else 0,
        "unique_races": int(eligible["session_key"].nunique()),
        "unique_circuits": int(eligible["circuit_short_name"].nunique()),
        "forecast_weighted_mae_s": round(fw_mae, 4),
        "stint_weighted_mae_s": round(sw_mae, 4),
        "race_weighted_mae_s": round(rw_mae, 4),
        "rmse_s": round(rmse, 4),
        "bias_s": round(bias, 4),
        "coverage_50": round(cov_50, 4),
        "coverage_90": round(cov_90, 4),
        "lap_crps_s": round(crps, 4),
    }


def compute_cumulative_trajectory_metrics(
    forecasts_df: pd.DataFrame,
    horizon: int,
) -> dict[str, Any]:
    """Evaluate Dataset B (complete clean H-lap trajectories) for cumulative stint time and pace change."""
    sub_h = forecasts_df[forecasts_df["requested_horizon"] == int(horizon)]
    total_opps = len(sub_h)

    censoring_counts = (
        sub_h["trajectory_censoring_reason"]
        .fillna("UNKNOWN")
        .value_counts()
        .to_dict()
        if "trajectory_censoring_reason" in sub_h.columns and not sub_h.empty
        else {}
    )

    eligible = sub_h[
        sub_h["trajectory_is_complete_clean"].fillna(False).astype(bool)
        & sub_h["observed_cumulative_time_s"].notna()
        & sub_h["expected_total_time_s"].notna()
    ].copy()

    if eligible.empty:
        return {
            "horizon": int(horizon),
            "forecast_opportunities": int(total_opps),
            "complete_eligible_trajectories": 0,
            "complete_eligible_pct": 0.0,
            "unique_stints": 0,
            "unique_races": 0,
            "unique_circuits": 0,
            "cum_forecast_weighted_mae_s": None,
            "cum_stint_weighted_mae_s": None,
            "cum_race_weighted_mae_s": None,
            "per_lap_equiv_mae_s": None,
            "cum_rmse_s": None,
            "cum_bias_s": None,
            "cum_coverage_50": None,
            "cum_coverage_90": None,
            "cum_crps_s": None,
            "pace_change_mae_s": None,
            "pace_change_bias_s": None,
            "censoring_breakdown": {str(k): int(v) for k, v in censoring_counts.items()},
        }

    s_true = eligible["observed_cumulative_time_s"].to_numpy(dtype=float)
    s_pred = eligible["expected_total_time_s"].to_numpy(dtype=float)
    cum_res = s_true - s_pred
    cum_abs = np.abs(cum_res)
    eligible["cum_abs_err"] = cum_abs

    fw_cum_mae = float(np.mean(cum_abs))
    sw_cum_mae = (
        float(eligible.groupby("stint_id")["cum_abs_err"].mean().mean())
        if "stint_id" in eligible.columns
        else fw_cum_mae
    )
    rw_cum_mae = float(eligible.groupby("session_key")["cum_abs_err"].mean().mean())
    cum_rmse = float(np.sqrt(np.mean(cum_res**2)))
    cum_bias = float(np.mean(cum_res))

    cov_50 = float(
        np.mean(
            (s_true >= eligible["p25_total_time_s"].to_numpy(dtype=float))
            & (s_true <= eligible["p75_total_time_s"].to_numpy(dtype=float))
        )
    )
    cov_90 = float(
        np.mean(
            (s_true >= eligible["p05_total_time_s"].to_numpy(dtype=float))
            & (s_true <= eligible["p95_total_time_s"].to_numpy(dtype=float))
        )
    )
    cum_crps = float(
        np.mean(
            gaussian_crps(
                s_pred,
                eligible["std_total_time_s"].to_numpy(dtype=float),
                s_true,
            )
        )
    )

    pace_change_mae: float | None = None
    pace_change_bias: float | None = None
    if (
        "observed_pace_change_s" in eligible.columns
        and "predicted_pace_change_s" in eligible.columns
    ):
        pc_sub = eligible.dropna(subset=["observed_pace_change_s", "predicted_pace_change_s"])
        if not pc_sub.empty:
            pc_res = (
                pc_sub["observed_pace_change_s"].to_numpy(dtype=float)
                - pc_sub["predicted_pace_change_s"].to_numpy(dtype=float)
            )
            pace_change_mae = round(float(np.mean(np.abs(pc_res))), 4)
            pace_change_bias = round(float(np.mean(pc_res)), 4)

    return {
        "horizon": int(horizon),
        "forecast_opportunities": int(total_opps),
        "complete_eligible_trajectories": len(eligible),
        "complete_eligible_pct": round(100.0 * len(eligible) / max(total_opps, 1), 2),
        "unique_stints": int(eligible["stint_id"].nunique()) if "stint_id" in eligible.columns else 0,
        "unique_races": int(eligible["session_key"].nunique()),
        "unique_circuits": int(eligible["circuit_short_name"].nunique()),
        "cum_forecast_weighted_mae_s": round(fw_cum_mae, 4),
        "cum_stint_weighted_mae_s": round(sw_cum_mae, 4),
        "cum_race_weighted_mae_s": round(rw_cum_mae, 4),
        "per_lap_equiv_mae_s": round(rw_cum_mae / max(int(horizon), 1), 4),
        "cum_rmse_s": round(cum_rmse, 4),
        "cum_bias_s": round(cum_bias, 4),
        "cum_coverage_50": round(cov_50, 4),
        "cum_coverage_90": round(cov_90, 4),
        "cum_crps_s": round(cum_crps, 4),
        "pace_change_mae_s": pace_change_mae,
        "pace_change_bias_s": pace_change_bias,
        "censoring_breakdown": {str(k): int(v) for k, v in censoring_counts.items()},
    }


def paired_race_bootstrap_comparison(
    forecasts_df: pd.DataFrame,
    candidate_model_id: str,
    baseline_model_id: str,
    *,
    horizon: int = 5,
    use_cumulative: bool = True,
    n_bootstrap: int = 2000,
    random_seed: int = 42,
) -> dict[str, Any]:
    """Paired race-level comparison with cluster-bootstrap 90% confidence intervals (Section 13.5)."""
    sub = forecasts_df[
        (forecasts_df["requested_horizon"] == int(horizon))
        & forecasts_df["model_id"].isin([candidate_model_id, baseline_model_id])
    ].copy()

    if use_cumulative:
        sub = sub[
            sub["trajectory_is_complete_clean"].fillna(False).astype(bool)
            & sub["observed_cumulative_time_s"].notna()
        ].copy()
        sub["err_val"] = (
            sub["observed_cumulative_time_s"].to_numpy(dtype=float)
            - sub["expected_total_time_s"].to_numpy(dtype=float)
        )
    else:
        sub = sub[
            sub["target_is_clean_green"].fillna(False).astype(bool)
            & sub["observed_target_lap_time_s"].notna()
        ].copy()
        sub["err_val"] = (
            sub["observed_target_lap_time_s"].to_numpy(dtype=float)
            - sub["expected_endpoint_lap_time_s"].to_numpy(dtype=float)
        )
    sub["abs_err"] = np.abs(sub["err_val"])

    by_race = (
        sub.groupby(["session_key", "circuit_short_name", "model_id"])["abs_err"]
        .mean()
        .unstack("model_id")
        .dropna()
    )
    if (
        by_race.empty
        or candidate_model_id not in by_race.columns
        or baseline_model_id not in by_race.columns
    ):
        return {}

    diffs = (by_race[candidate_model_id] - by_race[baseline_model_id]).to_numpy(dtype=float)
    n_races = len(diffs)
    mean_diff = float(np.mean(diffs))
    races_won = int(np.sum(diffs < 0.0))

    rng = np.random.default_rng(int(random_seed))
    if n_races > 1:
        boot_idx = rng.integers(0, n_races, size=(n_bootstrap, n_races))
        boot_means = np.mean(diffs[boot_idx], axis=1)
        ci05, ci95 = np.quantile(boot_means, [0.05, 0.95])
        se_diff = float(np.std(boot_means, ddof=1))
    else:
        ci05, ci95, se_diff = mean_diff, mean_diff, 0.0

    per_race: list[dict[str, Any]] = []
    for (sk, circ), row in by_race.iterrows():
        c_v = float(row[candidate_model_id])
        b_v = float(row[baseline_model_id])
        per_race.append(
            {
                "session_key": int(sk),
                "circuit_short_name": str(circ),
                "candidate_mae_s": round(c_v, 4),
                "baseline_mae_s": round(b_v, 4),
                "delta_mae_s": round(c_v - b_v, 4),
            }
        )

    return {
        "candidate_model": candidate_model_id,
        "baseline_model": baseline_model_id,
        "horizon": int(horizon),
        "metric_type": "cumulative_time_mae_s" if use_cumulative else "lap_time_mae_s",
        "n_races": n_races,
        "races_improved": races_won,
        "mean_race_mae_delta_s": round(mean_diff, 4),
        "bootstrap_se_s": round(se_diff, 4),
        "bootstrap_ci90_lower_s": round(float(ci05), 4),
        "bootstrap_ci90_upper_s": round(float(ci95), 4),
        "per_race": per_race,
    }


def classify_cold_start_regime(
    clean_laps_race: int,
    clean_laps_stint: int,
) -> str:
    """Explicit separation of the two types of cold starts (Prerequisite Issue 2)."""
    if int(clean_laps_race or 0) <= 0:
        return "0A_no_prior_clean_race_laps"
    if int(clean_laps_stint or 0) <= 0:
        return "0B_first_clean_lap_of_new_stint_with_race_history"
    if int(clean_laps_stint or 0) <= 2:
        return "1_2_early_stint"
    return "3_plus_warm_stint"
