"""Historical open-loop stint backtesting, ablation suite, Plots A-L, and Stage 3 runner."""
from __future__ import annotations

import json
import pickle
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from f1ri.features.degradation_features import (
    build_degradation_dataset_audit,
    row_to_stint_forecast_request,
)
from f1ri.features.stint_examples import (
    DEFAULT_STINT_HORIZONS,
    populate_stint_training_examples,
)
from f1ri.features.stint_observations import populate_session_stint_observations
from f1ri.ingestion.batch import load_experiment_cohort
from f1ri.ingestion.compound_allocations import populate_silver_compound_allocations
from f1ri.models.contracts import (
    SCENARIO_CONTINUE_STINT,
    SCENARIO_NEW_FRESH,
    StintForecastModel,
)
from f1ri.models.degradation_hierarchical import HierarchicalTireDegradationModel
from f1ri.models.stint_forecast import (
    PaceModelV2,
    build_stage3_experiment_models,
    compare_stint_scenarios,
)
from f1ri.models.trajectory_uncertainty import estimate_within_stint_autocorrelation
from f1ri.simulator.contracts import HypotheticalStintScenario
from f1ri.validation.backtest import _git_sha
from f1ri.validation.leakage import validate_stint_forecast_timestamp
from f1ri.validation.splits import (
    TemporalSplitManifest,
    build_split_manifest,
    iter_rolling_origin_splits,
)
from f1ri.validation.stint_metrics import (
    classify_cold_start_regime,
    compute_cumulative_trajectory_metrics,
    compute_lap_horizon_metrics,
    paired_race_bootstrap_comparison,
)


def evaluate_open_loop_trajectories_for_df(
    eval_df: pd.DataFrame,
    model: StintForecastModel,
    model_id: str,
    split_name: str,
    *,
    n_samples_override: int | None = None,
    record_lap_rows: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Evaluate a fitted StintForecastModel across all cutoff x scenario x horizon rows in eval_df.

    Grouping by (session_key, driver_number, cutoff_lap_number, scenario_type) issues one
    open-loop trajectory call up to the maximum requested horizon at that cutoff and slices
    every requested horizon H in {1, 3, 5, 10, 15, 20} from that single immutable forecast.
    """
    if eval_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    forecast_rows: list[dict[str, Any]] = []
    lap_rows: list[dict[str, Any]] = []

    group_cols = ["session_key", "driver_number", "cutoff_lap_number", "scenario_type"]
    for _, grp in eval_df.groupby(group_cols, sort=False):
        ordered_grp = grp.sort_values("requested_horizon")
        max_row = ordered_grp.iloc[-1]
        base_req = row_to_stint_forecast_request(max_row)
        if n_samples_override is not None:
            object.__setattr__(base_req, "n_trajectory_samples", int(n_samples_override))

        traj = model.forecast_stint(base_req)
        validate_stint_forecast_timestamp(base_req, traj)
        laps_by_step = {lf.step_index: lf for lf in traj.lap_forecasts}

        if record_lap_rows:
            obs_times_max = (
                json.loads(max_row["observed_lap_times_json"])
                if pd.notna(max_row.get("observed_lap_times_json"))
                else []
            )
            obs_clean_max = (
                json.loads(max_row["observed_lap_clean_flags_json"])
                if pd.notna(max_row.get("observed_lap_clean_flags_json"))
                else []
            )
            for lf in traj.lap_forecasts:
                k_idx = lf.step_index - 1
                obs_t = (
                    float(obs_times_max[k_idx])
                    if k_idx < len(obs_times_max) and obs_times_max[k_idx] is not None
                    else None
                )
                is_cln = bool(obs_clean_max[k_idx]) if k_idx < len(obs_clean_max) else False
                lap_rows.append(
                    {
                        "model_id": model_id,
                        "split": split_name,
                        "session_key": int(max_row["session_key"]),
                        "circuit_short_name": str(max_row.get("circuit_short_name") or ""),
                        "driver_number": int(max_row["driver_number"]),
                        "driver_id": str(max_row.get("driver_id") or ""),
                        "stint_id": str(max_row.get("stint_id") or ""),
                        "stint_number": int(max_row.get("stint_number") or 1),
                        "cutoff_lap_number": int(max_row["cutoff_lap_number"]),
                        "scenario_type": str(max_row["scenario_type"]),
                        "step_index": lf.step_index,
                        "target_lap_number": lf.target_lap_number,
                        "projected_tyre_age": lf.projected_tyre_age,
                        "compound_relative": str(max_row["compound_relative"]),
                        "compound_absolute": (
                            str(max_row["compound_absolute"])
                            if pd.notna(max_row.get("compound_absolute"))
                            else None
                        ),
                        "expected_lap_time_s": lf.expected_lap_time_s,
                        "median_lap_time_s": lf.median_lap_time_s,
                        "std_lap_time_s": lf.std_lap_time_s,
                        "p25_lap_time_s": lf.p25_lap_time_s,
                        "p75_lap_time_s": lf.p75_lap_time_s,
                        "p05_lap_time_s": lf.p05_lap_time_s,
                        "p95_lap_time_s": lf.p95_lap_time_s,
                        "expected_cumulative_time_s": lf.expected_cumulative_time_s,
                        "base_pace_contribution_s": lf.base_pace_contribution_s,
                        "race_progression_contribution_s": lf.race_progression_contribution_s,
                        "tire_degradation_contribution_s": lf.tire_degradation_contribution_s,
                        "support_status": lf.support_status,
                        "observed_lap_time_s": obs_t,
                        "is_clean_green": is_cln,
                        "residual_s": (obs_t - lf.expected_lap_time_s) if obs_t is not None else None,
                    }
                )

        for _, row in ordered_grp.iterrows():
            h_req = int(row["requested_horizon"])
            lf_h = laps_by_step.get(h_req)
            if lf_h is None:
                continue

            # Compute predicted pace change (late minus early) over horizon h_req
            pred_pace_change_s: float | None = None
            if h_req >= 3:
                k_win = max(1, h_req // 3)
                early_exp = float(
                    np.mean([laps_by_step[k].expected_lap_time_s for k in range(1, k_win + 1)])
                )
                late_exp = float(
                    np.mean(
                        [
                            laps_by_step[k].expected_lap_time_s
                            for k in range(h_req - k_win + 1, h_req + 1)
                        ]
                    )
                )
                pred_pace_change_s = round(late_exp - early_exp, 4)

            obs_end = (
                float(row["observed_target_lap_time_s"])
                if pd.notna(row.get("observed_target_lap_time_s"))
                else None
            )
            obs_cum = (
                float(row["observed_cumulative_time_s"])
                if pd.notna(row.get("observed_cumulative_time_s"))
                else None
            )
            end_res = (obs_end - lf_h.expected_lap_time_s) if obs_end is not None else None
            cum_res = (obs_cum - lf_h.expected_cumulative_time_s) if obs_cum is not None else None

            n_race = int(row.get("clean_laps_observed_race") or 0)
            n_stint = int(row.get("clean_laps_observed_current_stint") or 0)

            forecast_rows.append(
                {
                    "model_id": model_id,
                    "model_version": traj.model_version,
                    "degradation_model_version": traj.degradation_model_version,
                    "split": split_name,
                    "session_key": int(row["session_key"]),
                    "season": int(row["season"]) if pd.notna(row.get("season")) else None,
                    "regulation_era": str(row.get("regulation_era") or "2023_2025"),
                    "circuit_short_name": str(row.get("circuit_short_name") or "Unknown"),
                    "stint_id": str(row.get("stint_id") or ""),
                    "driver_number": int(row["driver_number"]),
                    "driver_id": str(row.get("driver_id") or ""),
                    "team_id": str(row.get("team_id") or ""),
                    "cutoff_lap_number": int(row["cutoff_lap_number"]),
                    "planned_start_lap": int(row["planned_start_lap"]),
                    "target_lap_number": int(row["target_lap_number"]),
                    "requested_horizon": h_req,
                    "scenario_type": str(row["scenario_type"]),
                    "forecast_as_of_utc": row["forecast_as_of_utc"],
                    "trained_through_utc": traj.trained_through_utc,
                    "stint_number": int(row.get("stint_number") or 1),
                    "compound_relative": str(row["compound_relative"]),
                    "compound_absolute": (
                        str(row["compound_absolute"])
                        if pd.notna(row.get("compound_absolute"))
                        else None
                    ),
                    "tyre_age_at_forecast_start": int(row["tyre_age_at_forecast_start"] or 0),
                    "tyre_age_at_lap_start": int(row["tyre_age_at_lap_start"] or 0),
                    "race_progress": float(row["race_progress"] or 0.0),
                    "last_known_track_temp_c": (
                        float(row["last_known_track_temp_c"])
                        if pd.notna(row.get("last_known_track_temp_c"))
                        else None
                    ),
                    "clean_laps_observed_current_stint": n_stint,
                    "clean_laps_observed_race": n_race,
                    "cold_start_regime": classify_cold_start_regime(n_race, n_stint),
                    "expected_endpoint_lap_time_s": lf_h.expected_lap_time_s,
                    "median_endpoint_lap_time_s": lf_h.median_lap_time_s,
                    "std_endpoint_lap_time_s": lf_h.std_lap_time_s,
                    "p25_endpoint_lap_time_s": lf_h.p25_lap_time_s,
                    "p75_endpoint_lap_time_s": lf_h.p75_lap_time_s,
                    "p05_endpoint_lap_time_s": lf_h.p05_lap_time_s,
                    "p95_endpoint_lap_time_s": lf_h.p95_lap_time_s,
                    "expected_total_time_s": lf_h.expected_cumulative_time_s,
                    "std_total_time_s": lf_h.std_cumulative_time_s,
                    "p25_total_time_s": lf_h.p25_cumulative_time_s,
                    "p75_total_time_s": lf_h.p75_cumulative_time_s,
                    "p05_total_time_s": lf_h.p05_cumulative_time_s,
                    "p95_total_time_s": lf_h.p95_cumulative_time_s,
                    "base_pace_contribution_s": lf_h.base_pace_contribution_s,
                    "race_progression_contribution_s": lf_h.race_progression_contribution_s,
                    "tire_degradation_contribution_s": lf_h.tire_degradation_contribution_s,
                    "support_status": lf_h.support_status,
                    "observed_target_lap_time_s": obs_end,
                    "target_is_clean_green": bool(row.get("target_is_clean_green", False)),
                    "target_exclusion_reason": (
                        str(row["target_exclusion_reason"])
                        if pd.notna(row.get("target_exclusion_reason"))
                        else None
                    ),
                    "trajectory_is_complete_clean": bool(
                        row.get("trajectory_is_complete_clean", False)
                    ),
                    "trajectory_censoring_reason": str(
                        row.get("trajectory_censoring_reason") or "UNKNOWN"
                    ),
                    "observed_cumulative_time_s": obs_cum,
                    "observed_pace_change_s": (
                        float(row["observed_pace_change_s"])
                        if pd.notna(row.get("observed_pace_change_s"))
                        else None
                    ),
                    "predicted_pace_change_s": pred_pace_change_s,
                    "endpoint_residual_s": end_res,
                    "endpoint_abs_error_s": abs(end_res) if end_res is not None else None,
                    "cumulative_residual_s": cum_res,
                    "cumulative_abs_error_s": abs(cum_res) if cum_res is not None else None,
                }
            )

    return pd.DataFrame(forecast_rows), pd.DataFrame(lap_rows)


def summarize_model_performance(
    forecasts_df: pd.DataFrame,
    horizons: tuple[int, ...] = DEFAULT_STINT_HORIZONS,
) -> dict[str, Any]:
    """Compute Dataset A (lap-level) and Dataset B (cumulative trajectory) metrics across horizons."""
    cont_df = forecasts_df[forecasts_df["scenario_type"] == SCENARIO_CONTINUE_STINT]
    new_stint_df = forecasts_df[forecasts_df["scenario_type"] != SCENARIO_CONTINUE_STINT]

    lap_by_h = {f"h{h}": compute_lap_horizon_metrics(cont_df, h) for h in horizons}
    cum_by_h = {
        f"h{h}": compute_cumulative_trajectory_metrics(cont_df, h)
        for h in (5, 10, 15, 20)
        if h in horizons
    }
    new_stint_summary = {
        "h1_lap": compute_lap_horizon_metrics(new_stint_df, 1),
        "h5_cum": compute_cumulative_trajectory_metrics(new_stint_df, 5),
        "h10_cum": compute_cumulative_trajectory_metrics(new_stint_df, 10),
    }
    return {
        "dataset_a_individual_laps": lap_by_h,
        "dataset_b_complete_trajectories": cum_by_h,
        "new_stint_scenarios": new_stint_summary,
    }


def generate_stage3_diagnostic_plots(
    all_forecasts: pd.DataFrame,
    d8_lap_df: pd.DataFrame,
    d8_model: PaceModelV2,
    stint_obs_df: pd.DataFrame,
    lap_ctx_df: pd.DataFrame,
    all_examples_df: pd.DataFrame,
    plots_dir: Path,
) -> list[str]:
    """Generate the 12 standardized Stage 3 diagnostic plots (Plots A-L) + Stint Replay & Strategy."""
    plots_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []

    d8_vt = all_forecasts[
        (all_forecasts["model_id"] == "D8_correlated_trajectory_uncertainty")
        & all_forecasts["split"].isin(["val", "test"])
        & (all_forecasts["scenario_type"] == SCENARIO_CONTINUE_STINT)
    ].copy()

    # =========================================================================
    # Figure 1: Plots A, B, C, D (Trajectories, Degradation Curves, Tire Age, Stint Length)
    # =========================================================================
    fig1, axes1 = plt.subplots(2, 2, figsize=(13.5, 10.0), dpi=140)

    # Plot A: Actual vs Predicted Stint Trajectories (H=10 complete clean trajectories)
    ax = axes1[0, 0]
    h10_clean = d8_vt[
        (d8_vt["requested_horizon"] == 10)
        & d8_vt["trajectory_is_complete_clean"].fillna(False).astype(bool)
    ]
    for cmpd, color in [("SOFT", "#DC2626"), ("MEDIUM", "#D97706"), ("HARD", "#2563EB")]:
        c_grp = h10_clean[h10_clean["compound_relative"] == cmpd]
        if not c_grp.empty:
            ax.scatter(
                c_grp["expected_total_time_s"] / 10.0,
                c_grp["observed_cumulative_time_s"] / 10.0,
                s=18,
                alpha=0.7,
                color=color,
                label=f"{cmpd} (n={len(c_grp)})",
            )
    if not h10_clean.empty:
        lo = float(min(h10_clean["expected_total_time_s"].min(), h10_clean["observed_cumulative_time_s"].min()) / 10.0 - 1.0)
        hi = float(max(h10_clean["expected_total_time_s"].max(), h10_clean["observed_cumulative_time_s"].max()) / 10.0 + 1.0)
        ax.plot([lo, hi], [lo, hi], "k--", linewidth=1.2, label="y = x")
    ax.set_title("Plot A — Observed vs Open-Loop 10-Lap Mean Stint Pace (s/lap)")
    ax.set_xlabel("Predicted 10-Lap Mean Pace (s/lap)")
    ax.set_ylabel("Observed 10-Lap Mean Pace (s/lap)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)

    # Plot B: Estimated Degradation Curves by Compound (Age 0..35, pure tire effect C_c + D_c(a) vs net with fuel)
    ax = axes1[0, 1]
    ages_grid = np.arange(0, 36, dtype=float)
    for cmpd, color in [("SOFT", "#DC2626"), ("MEDIUM", "#D97706"), ("HARD", "#2563EB")]:
        pure_deg = [
            d8_model.degradation_model.evaluate_tire(
                cmpd, a, circuit_short_name="Sakhir", regulation_era="2023_2025"
            ).total_tire_effect_s
            for a in ages_grid
        ]
        net_with_fuel = [
            pure_deg[i]
            + d8_model.degradation_model.race_progress_effect(a / 57.0, circuit_short_name="Sakhir")[0]
            for i, a in enumerate(ages_grid)
        ]
        ax.plot(ages_grid, pure_deg, "-", color=color, linewidth=2.0, label=f"{cmpd} Pure Tire C+D(a)")
        ax.plot(ages_grid, net_with_fuel, "--", color=color, linewidth=1.4, alpha=0.8, label=f"{cmpd} Net (Tire + Fuel)")
    ax.set_title("Plot B — Estimated Tire Degradation vs Net Stint Evolution (Sakhir)")
    ax.set_xlabel("Tire Age (laps)")
    ax.set_ylabel("Lap Time Contribution vs Reference (s)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=7, ncol=2)

    # Plot C: Open-Loop H=5 Residual vs Tire Age
    ax = axes1[1, 0]
    h5_el = d8_vt[
        (d8_vt["requested_horizon"] == 5)
        & d8_vt["target_is_clean_green"].fillna(False).astype(bool)
        & d8_vt["endpoint_residual_s"].notna()
    ].copy()
    for cmpd, color in [("SOFT", "#DC2626"), ("MEDIUM", "#D97706"), ("HARD", "#2563EB")]:
        c_sub = h5_el[h5_el["compound_relative"] == cmpd]
        if not c_sub.empty:
            ax.scatter(
                c_sub["tyre_age_at_lap_start"],
                c_sub["endpoint_residual_s"],
                s=12,
                alpha=0.45,
                color=color,
                label=cmpd,
            )
    if not h5_el.empty:
        bins = pd.cut(h5_el["tyre_age_at_lap_start"], bins=[0, 5, 10, 15, 20, 25, 30, 40, 60])
        b_mean = h5_el.groupby(bins, observed=False)["endpoint_residual_s"].mean()
        b_x = [iv.mid for iv in b_mean.index if pd.notna(iv)]
        ax.plot(b_x, b_mean.to_numpy(dtype=float), "k-o", linewidth=1.8, markersize=4, label="Binned Mean")
    ax.axhline(0.0, color="red", linestyle="--", linewidth=1.0)
    ax.set_ylim(-2.5, 2.5)
    ax.set_title("Plot C — Open-Loop (H=5) Residual vs Projected Tire Age")
    ax.set_xlabel("Projected Tire Age at Target Lap (laps)")
    ax.set_ylabel("Residual: Observed - Predicted (s)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=7)

    # Plot D: Residual vs Observed Stint Length
    ax = axes1[1, 1]
    if not h5_el.empty and not stint_obs_df.empty:
        stint_len_map = dict(
            zip(stint_obs_df["stint_id"], stint_obs_df["observed_stint_laps"], strict=False)
        )
        h5_el["observed_stint_laps"] = h5_el["stint_id"].map(stint_len_map).fillna(20)
        ax.scatter(
            h5_el["observed_stint_laps"],
            h5_el["endpoint_residual_s"],
            s=12,
            alpha=0.4,
            color="#0F766E",
        )
        len_bins = pd.cut(h5_el["observed_stint_laps"], bins=[0, 10, 15, 20, 25, 30, 40, 70])
        l_mean = h5_el.groupby(len_bins, observed=False)["endpoint_residual_s"].mean()
        l_x = [iv.mid for iv in l_mean.index if pd.notna(iv)]
        ax.plot(l_x, l_mean.to_numpy(dtype=float), "k-o", linewidth=1.8, markersize=4, label="Binned Mean")
    ax.axhline(0.0, color="red", linestyle="--", linewidth=1.0)
    ax.set_ylim(-2.5, 2.5)
    ax.set_title("Plot D — Open-Loop Residual vs Total Observed Stint Length")
    ax.set_xlabel("Total Observed Stint Duration (laps)")
    ax.set_ylabel("Residual (s)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)

    fig1.tight_layout()
    p1 = plots_dir / "stage3_trajectories_abcd.png"
    fig1.savefig(p1)
    plt.close(fig1)
    saved.append(str(p1))

    # =========================================================================
    # Figure 2: Plots E, F, G, H (Horizons, Cumulative MAE, Coverage, Bias)
    # =========================================================================
    fig2, axes2 = plt.subplots(2, 2, figsize=(13.5, 10.0), dpi=140)

    compare_models = [
        ("B0_E2_compound_median", "E2 Compound Median", "#64748B", "--s"),
        ("D0_frozen_pace_v1", "D0 Frozen Pace V1", "#DC2626", "--^"),
        ("D5_hierarchical_circuit_compound", "D5 Hierarchical Deg", "#2563EB", "-d"),
        ("D8_correlated_trajectory_uncertainty", "D8 Pace V2 (Full)", "#0F766E", "-o"),
    ]

    # Plot E: Open-loop Lap MAE vs Horizon (1, 3, 5, 10, 15, 20) on Held-Out Test
    ax = axes2[0, 0]
    test_cont = all_forecasts[
        (all_forecasts["split"] == "test")
        & (all_forecasts["scenario_type"] == SCENARIO_CONTINUE_STINT)
    ]
    h_list = [1, 3, 5, 10, 15, 20]
    for m_id, m_lbl, col, style in compare_models:
        m_sub = test_cont[test_cont["model_id"] == m_id]
        if m_sub.empty:
            continue
        maes = [compute_lap_horizon_metrics(m_sub, h)["race_weighted_mae_s"] for h in h_list]
        valid_xy = [(h, v) for h, v in zip(h_list, maes, strict=False) if v is not None]
        if valid_xy:
            ax.plot(
                [x for x, _ in valid_xy],
                [y for _, y in valid_xy],
                style,
                color=col,
                linewidth=1.8,
                label=m_lbl,
            )
    ax.set_title("Plot E — Open-Loop Endpoint Lap MAE vs Horizon (2025 Test)")
    ax.set_xlabel("Forecast Horizon H (laps ahead)")
    ax.set_ylabel("Race-Weighted Lap MAE (s)")
    ax.set_xticks(h_list)
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)

    # Plot F: Cumulative-Time MAE vs Horizon (5, 10, 15, 20) on Held-Out Test
    ax = axes2[0, 1]
    cum_h_list = [5, 10, 15, 20]
    for m_id, m_lbl, col, style in compare_models:
        m_sub = test_cont[test_cont["model_id"] == m_id]
        if m_sub.empty:
            continue
        c_maes = [
            compute_cumulative_trajectory_metrics(m_sub, h)["cum_race_weighted_mae_s"]
            for h in cum_h_list
        ]
        valid_xy = [(h, v) for h, v in zip(cum_h_list, c_maes, strict=False) if v is not None]
        if valid_xy:
            ax.plot(
                [x for x, _ in valid_xy],
                [y for _, y in valid_xy],
                style,
                color=col,
                linewidth=1.8,
                label=m_lbl,
            )
    ax.set_title("Plot F — Cumulative Stint-Time MAE vs Horizon (2025 Test)")
    ax.set_xlabel("Trajectory Horizon H (laps)")
    ax.set_ylabel("Race-Weighted Cumulative Time MAE (s)")
    ax.set_xticks(cum_h_list)
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)

    # Plot G: Cumulative Prediction Interval Coverage vs Horizon (D7 i.i.d. vs D8 Correlated)
    ax = axes2[1, 0]
    for m_id, m_lbl, col in [
        ("D7_dynamic_stint_state", "D7 Independent Laps", "#DC2626"),
        ("D8_correlated_trajectory_uncertainty", "D8 Correlated Trajectories", "#0F766E"),
    ]:
        m_sub = test_cont[test_cont["model_id"] == m_id]
        if m_sub.empty:
            continue
        cov90 = [
            compute_cumulative_trajectory_metrics(m_sub, h)["cum_coverage_90"]
            for h in cum_h_list
        ]
        cov50 = [
            compute_cumulative_trajectory_metrics(m_sub, h)["cum_coverage_50"]
            for h in cum_h_list
        ]
        v90 = [(h, v) for h, v in zip(cum_h_list, cov90, strict=False) if v is not None]
        v50 = [(h, v) for h, v in zip(cum_h_list, cov50, strict=False) if v is not None]
        if v90:
            ax.plot(
                [x for x, _ in v90],
                [y for _, y in v90],
                "-o",
                color=col,
                linewidth=1.8,
                label=f"{m_lbl} (90% CI)",
            )
        if v50:
            ax.plot(
                [x for x, _ in v50],
                [y for _, y in v50],
                "--s",
                color=col,
                linewidth=1.4,
                label=f"{m_lbl} (50% CI)",
            )
    ax.axhline(0.90, color="black", linestyle=":", linewidth=1.1, label="Nominal 90% / 50%")
    ax.axhline(0.50, color="black", linestyle=":", linewidth=1.1)
    ax.set_ylim(0.25, 1.02)
    ax.set_xticks(cum_h_list)
    ax.set_title("Plot G — Cumulative Stint-Time Interval Coverage vs Horizon")
    ax.set_xlabel("Trajectory Horizon H (laps)")
    ax.set_ylabel("Empirical Coverage")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=7, loc="lower left")

    # Plot H: Predicted vs Observed Cumulative Time (across H=5, 10, 15, 20)
    ax = axes2[1, 1]
    for h_val, col in [(5, "#3B82F6"), (10, "#10B981"), (15, "#F59E0B"), (20, "#EF4444")]:
        c_sub = d8_vt[
            (d8_vt["requested_horizon"] == h_val)
            & d8_vt["trajectory_is_complete_clean"].fillna(False).astype(bool)
        ]
        if not c_sub.empty:
            ax.scatter(
                c_sub["expected_total_time_s"],
                c_sub["observed_cumulative_time_s"],
                s=14,
                alpha=0.65,
                color=col,
                label=f"H={h_val} (n={len(c_sub)})",
            )
    all_c = d8_vt[d8_vt["trajectory_is_complete_clean"].fillna(False).astype(bool)]
    if not all_c.empty:
        lo_c = float(all_c["expected_total_time_s"].min() - 10.0)
        hi_c = float(all_c["expected_total_time_s"].max() + 10.0)
        ax.plot([lo_c, hi_c], [lo_c, hi_c], "k--", linewidth=1.2)
    ax.set_title("Plot H — Predicted vs Observed Total Stint Time S_H (s)")
    ax.set_xlabel("Predicted Cumulative Stint Time (s)")
    ax.set_ylabel("Observed Cumulative Stint Time (s)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)

    fig2.tight_layout()
    p2 = plots_dir / "stage3_horizons_efgh.png"
    fig2.savefig(p2)
    plt.close(fig2)
    saved.append(str(p2))

    # =========================================================================
    # Figure 3: Plots I, J, K, L (Circuit, Compound/Era, Pace Change, Autocorrelation)
    # =========================================================================
    fig3, axes3 = plt.subplots(2, 2, figsize=(13.5, 10.0), dpi=140)

    # Plot I: 5-Lap Cumulative MAE by Circuit (D0 Frozen Pace V1 vs D8 Pace V2 across 2025 Val+Test)
    ax = axes3[0, 0]
    vt_h5 = all_forecasts[
        all_forecasts["split"].isin(["val", "test"])
        & (all_forecasts["requested_horizon"] == 5)
        & (all_forecasts["scenario_type"] == SCENARIO_CONTINUE_STINT)
        & all_forecasts["trajectory_is_complete_clean"].fillna(False).astype(bool)
        & all_forecasts["model_id"].isin(
            ["B0_E2_compound_median", "D0_frozen_pace_v1", "D8_correlated_trajectory_uncertainty"]
        )
    ]
    if not vt_h5.empty:
        piv_c = (
            vt_h5.groupby(["circuit_short_name", "model_id"])["cumulative_abs_error_s"]
            .mean()
            .unstack("model_id")
            .dropna(how="all")
        )
        x_c = np.arange(len(piv_c))
        w = 0.27
        if "B0_E2_compound_median" in piv_c.columns:
            ax.bar(x_c - w, piv_c["B0_E2_compound_median"], width=w, label="E2 Median", color="#94A3B8")
        if "D0_frozen_pace_v1" in piv_c.columns:
            ax.bar(x_c, piv_c["D0_frozen_pace_v1"], width=w, label="D0 Pace V1", color="#DC2626")
        if "D8_correlated_trajectory_uncertainty" in piv_c.columns:
            ax.bar(
                x_c + w,
                piv_c["D8_correlated_trajectory_uncertainty"],
                width=w,
                label="D8 Pace V2",
                color="#0F766E",
            )
        ax.set_xticks(x_c)
        ax.set_xticklabels([str(c)[:9] for c in piv_c.index], fontsize=7, rotation=20)
    ax.set_title("Plot I — 5-Lap Cumulative Time MAE by Circuit (2025 Val + Test)")
    ax.set_ylabel("5-Lap Cumulative MAE (s)")
    ax.grid(True, alpha=0.3, axis="y")
    ax.legend(fontsize=8)

    # Plot J: Performance by Pirelli Absolute Compound (2025) & 2026 Transfer (D9 vs D10)
    ax = axes3[0, 1]
    tr_h5 = all_forecasts[
        (all_forecasts["split"] == "transfer_2026")
        & (all_forecasts["requested_horizon"] == 5)
        & (all_forecasts["scenario_type"] == SCENARIO_CONTINUE_STINT)
        & all_forecasts["trajectory_is_complete_clean"].fillna(False).astype(bool)
        & all_forecasts["model_id"].isin(
            ["D0_2026_frozen_pace_v1", "D9_frozen_2026_transfer", "D10_progressive_2026_adaptation"]
        )
    ]
    if not tr_h5.empty:
        piv_26 = (
            tr_h5.groupby(["session_key", "circuit_short_name", "model_id"])["cumulative_abs_error_s"]
            .mean()
            .unstack("model_id")
            .reset_index()
            .sort_values("session_key")
        )
        x26 = np.arange(len(piv_26))
        if "D9_frozen_2026_transfer" in piv_26.columns:
            ax.plot(
                x26,
                piv_26["D9_frozen_2026_transfer"],
                "--s",
                color="#DC2626",
                linewidth=1.8,
                label="D9 Frozen Historical (2023-25)",
            )
        if "D10_progressive_2026_adaptation" in piv_26.columns:
            ax.plot(
                x26,
                piv_26["D10_progressive_2026_adaptation"],
                "-o",
                color="#0F766E",
                linewidth=1.8,
                label="D10 Progressive 2026 Adaptation",
            )
        ax.set_xticks(x26)
        ax.set_xticklabels([f"2026\n{r['circuit_short_name'][:8]}" for _, r in piv_26.iterrows()], fontsize=7)
    ax.set_title("Plot J — 2026 Regulation Transfer: 5-Lap Cumulative MAE (s)")
    ax.set_ylabel("5-Lap Cumulative MAE (s)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)

    # Plot K: Predicted vs Observed Stint Pace Change (Delta T = T_late - T_early, H=10)
    ax = axes3[1, 0]
    pc_sub = d8_vt[
        (d8_vt["requested_horizon"] == 10)
        & d8_vt["trajectory_is_complete_clean"].fillna(False).astype(bool)
        & d8_vt["observed_pace_change_s"].notna()
        & d8_vt["predicted_pace_change_s"].notna()
    ]
    if not pc_sub.empty:
        for cmpd, col in [("SOFT", "#DC2626"), ("MEDIUM", "#D97706"), ("HARD", "#2563EB")]:
            cs = pc_sub[pc_sub["compound_relative"] == cmpd]
            if not cs.empty:
                ax.scatter(
                    cs["predicted_pace_change_s"],
                    cs["observed_pace_change_s"],
                    s=18,
                    alpha=0.65,
                    color=col,
                    label=cmpd,
                )
        ax.plot([-1.5, 2.0], [-1.5, 2.0], "k--", linewidth=1.2, label="y = x")
        ax.axhline(0.0, color="gray", linestyle=":", linewidth=1.0)
        ax.axvline(0.0, color="gray", linestyle=":", linewidth=1.0)
    ax.set_xlim(-1.5, 2.0)
    ax.set_ylim(-2.0, 2.5)
    ax.set_title("Plot K — Predicted vs Observed 10-Lap Stint Pace Change (s)")
    ax.set_xlabel("Predicted Pace Change T_late - T_early (s)")
    ax.set_ylabel("Observed Pace Change T_late - T_early (s)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)

    # Plot L: Residual Autocorrelation Within Stints (Lags 1..5)
    ax = axes3[1, 1]
    h1_d8 = d8_vt[
        (d8_vt["requested_horizon"] == 1)
        & d8_vt["target_is_clean_green"].fillna(False).astype(bool)
        & d8_vt["endpoint_residual_s"].notna()
    ].copy()
    h1_d5 = all_forecasts[
        (all_forecasts["model_id"] == "D5_hierarchical_circuit_compound")
        & all_forecasts["split"].isin(["val", "test"])
        & (all_forecasts["requested_horizon"] == 1)
        & (all_forecasts["scenario_type"] == SCENARIO_CONTINUE_STINT)
        & all_forecasts["target_is_clean_green"].fillna(False).astype(bool)
        & all_forecasts["endpoint_residual_s"].notna()
    ].copy()
    acf_d8 = estimate_within_stint_autocorrelation(h1_d8, "endpoint_residual_s", max_lag=5)
    acf_d5 = estimate_within_stint_autocorrelation(h1_d5, "endpoint_residual_s", max_lag=5)
    lags = list(range(1, 6))
    w_l = 0.35
    ax.bar(
        np.array(lags) - w_l / 2,
        [acf_d5.get(k, 0.0) for k in lags],
        width=w_l,
        color="#64748B",
        label="D5 Population Model Residuals",
    )
    ax.bar(
        np.array(lags) + w_l / 2,
        [acf_d8.get(k, 0.0) for k in lags],
        width=w_l,
        color="#0F766E",
        label="D8 Online Stint-State Residuals",
    )
    ax.axhline(0.0, color="black", linewidth=1.0)
    ax.set_xticks(lags)
    ax.set_title("Plot L — Within-Stint Residual Autocorrelation by Lag")
    ax.set_xlabel("Lag (Clean Laps Within Stint)")
    ax.set_ylabel("Autocorrelation r(k)")
    ax.grid(True, alpha=0.3, axis="y")
    ax.legend(fontsize=8)

    fig3.tight_layout()
    p3 = plots_dir / "stage3_generalization_ijkl.png"
    fig3.savefig(p3)
    plt.close(fig3)
    saved.append(str(p3))

    # =========================================================================
    # Figure 4: Historical Stint Replay & Counterfactual Strategy Comparison (Sections 18 & 23)
    # =========================================================================
    fig4, axes4 = plt.subplots(1, 2, figsize=(13.5, 5.2), dpi=140)

    # Panel 1: Russell (Driver 63) at 2025 Singapore GP (9896), Stint 2 (HARD), Forecast at Lap 30 for H=20
    ax = axes4[0]
    rus_laps = lap_ctx_df[
        (lap_ctx_df["session_key"] == 9896) & (lap_ctx_df["driver_number"] == 63)
    ].sort_values("lap_number")
    rus_cut = all_examples_df[
        (all_examples_df["session_key"] == 9896)
        & (all_examples_df["driver_number"] == 63)
        & (all_examples_df["cutoff_lap_number"] == 30)
        & (all_examples_df["requested_horizon"] == 20)
        & (all_examples_df["scenario_type"] == SCENARIO_CONTINUE_STINT)
    ]
    if not rus_laps.empty and not rus_cut.empty:
        stint2_laps = rus_laps[(rus_laps["lap_number"] >= 16) & (rus_laps["lap_number"] <= 55)]
        clean_l = stint2_laps[stint2_laps["is_clean_green_pace_lap"].fillna(False).astype(bool)]
        excl_l = stint2_laps[
            ~stint2_laps["is_clean_green_pace_lap"].fillna(False).astype(bool)
            & (stint2_laps["observed_lap_time_s"] < 110.0)
        ]
        ax.scatter(
            clean_l["lap_number"],
            clean_l["observed_lap_time_s"],
            color="#1E293B",
            s=24,
            label="Observed Clean Laps",
            zorder=4,
        )
        if not excl_l.empty:
            ax.scatter(
                excl_l["lap_number"],
                excl_l["observed_lap_time_s"],
                color="#94A3B8",
                marker="x",
                s=28,
                label="Stage 1 Excluded Laps",
                zorder=3,
            )

        req_rus = row_to_stint_forecast_request(rus_cut.iloc[0])
        traj_d8 = d8_model.forecast_stint(req_rus)
        f_laps = [lf.target_lap_number for lf in traj_d8.lap_forecasts]
        f_exp = [lf.expected_lap_time_s for lf in traj_d8.lap_forecasts]
        f_p25 = [lf.p25_lap_time_s for lf in traj_d8.lap_forecasts]
        f_p75 = [lf.p75_lap_time_s for lf in traj_d8.lap_forecasts]
        f_p05 = [lf.p05_lap_time_s for lf in traj_d8.lap_forecasts]
        f_p95 = [lf.p95_lap_time_s for lf in traj_d8.lap_forecasts]

        ax.axvline(30, color="#DC2626", linestyle="--", linewidth=1.2, label="Forecast Cutoff (Lap 30)")
        ax.plot(f_laps, f_exp, "-", color="#0F766E", linewidth=2.2, label="Pace V2 Open-Loop Trajectory")
        ax.fill_between(f_laps, f_p25, f_p75, color="#0F766E", alpha=0.28, label="50% Band")
        ax.fill_between(f_laps, f_p05, f_p95, color="#0F766E", alpha=0.12, label="90% Band")

    ax.set_title("Historical Stint Replay — Russell 2025 Singapore (Hard Stint, Cutoff Lap 30)")
    ax.set_xlabel("Race Lap Number")
    ax.set_ylabel("Lap Time (s)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=7, loc="upper right")

    # Panel 2: Counterfactual Strategy Comparison (Section 23: Continue Worn Mediums vs Switch to Fresh Hards/Softs)
    ax = axes4[1]
    rus_m_cut = all_examples_df[
        (all_examples_df["session_key"] == 9896)
        & (all_examples_df["driver_number"] == 63)
        & (all_examples_df["cutoff_lap_number"] == 18)
        & (all_examples_df["requested_horizon"] == 15)
        & (all_examples_df["scenario_type"] == SCENARIO_CONTINUE_STINT)
    ]
    if not rus_m_cut.empty:
        base_scen_req = row_to_stint_forecast_request(rus_m_cut.iloc[0])
        scenarios = [
            HypotheticalStintScenario(
                label="Continue Worn MEDIUM (age 18..32)",
                scenario_type=SCENARIO_CONTINUE_STINT,
                compound_relative="MEDIUM",
                tyre_age_at_start=base_scen_req.tyre_age_at_start,
                horizon_laps=15,
                compound_absolute="C4",
            ),
            HypotheticalStintScenario(
                label="Switch Fresh HARD (age 0..14)",
                scenario_type=SCENARIO_NEW_FRESH,
                compound_relative="HARD",
                tyre_age_at_start=0,
                horizon_laps=15,
                compound_absolute="C3",
            ),
            HypotheticalStintScenario(
                label="Switch Fresh SOFT (age 0..14)",
                scenario_type=SCENARIO_NEW_FRESH,
                compound_relative="SOFT",
                tyre_age_at_start=0,
                horizon_laps=15,
                compound_absolute="C5",
            ),
        ]
        comp_res = compare_stint_scenarios(d8_model, base_scen_req, scenarios)
        colors_s = ["#D97706", "#2563EB", "#DC2626"]
        for (lbl, tr_f), col in zip(comp_res.items(), colors_s, strict=False):
            steps = [lf.step_index for lf in tr_f.lap_forecasts]
            exps = [lf.expected_lap_time_s for lf in tr_f.lap_forecasts]
            p25s = [lf.p25_lap_time_s for lf in tr_f.lap_forecasts]
            p75s = [lf.p75_lap_time_s for lf in tr_f.lap_forecasts]
            ax.plot(
                steps,
                exps,
                "-o",
                markersize=4,
                color=col,
                linewidth=1.9,
                label=f"{lbl} (Sum={tr_f.expected_total_time_s:.1f}s)",
            )
            ax.fill_between(steps, p25s, p75s, color=col, alpha=0.15)

    ax.set_title("Counterfactual Clean-Air Strategy Comparison — 15-Lap Horizon (Singapore)")
    ax.set_xlabel("Projected Stint Step k (1..15)")
    ax.set_ylabel("Projected Clean-Air Lap Time (s)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=7.5)

    fig4.tight_layout()
    p4 = plots_dir / "stage3_stint_replay_and_strategy.png"
    fig4.savefig(p4)
    plt.close(fig4)
    saved.append(str(p4))

    return saved


def run_stage3_pipeline(
    root: Path,
    experiment_config_path: Path | None = None,
) -> dict[str, Any]:
    """Execute the full Stage 3 pipeline: stint tables, audit, D0-D10 backtest, ablations, and plots."""
    db_path = root / "data" / "warehouse" / "f1ri.duckdb"
    if not db_path.exists():
        raise FileNotFoundError(f"Warehouse not found: {db_path}. Run 'f1ri ingest-batch' first.")

    exp_cfg_path = experiment_config_path or (
        root / "config" / "experiments" / "stage3_degradation_v1.yaml"
    )
    cohort = load_experiment_cohort(root, exp_cfg_path)

    con = duckdb.connect(str(db_path))
    try:
        populate_silver_compound_allocations(con, root)
        populate_session_stint_observations(con)
        populate_stint_training_examples(con, horizons=DEFAULT_STINT_HORIZONS)
        audit = build_degradation_dataset_audit(con)

        bounds_rows = con.execute("""
            SELECT session_key, MIN(lap_start_utc), MAX(lap_end_estimate_utc)
            FROM gold.lap_context
            GROUP BY session_key
        """).fetchall()
        session_bounds = {int(r[0]): (r[1], r[2]) for r in bounds_rows}
        split_manifest: TemporalSplitManifest = build_split_manifest(cohort, session_bounds)

        cohort_keys = (
            split_manifest.train_keys
            + split_manifest.val_keys
            + split_manifest.test_keys
            + split_manifest.transfer_2026_keys
        )
        placeholders = ", ".join("?" for _ in cohort_keys)
        all_examples = con.execute(
            f"""
            SELECT * FROM gold.stint_training_examples
            WHERE session_key IN ({placeholders})
            ORDER BY season, session_key, driver_number, cutoff_lap_number, scenario_type, requested_horizon
            """,
            list(cohort_keys),
        ).df()
        stint_obs_df = con.execute(
            f"SELECT * FROM gold.stint_observations WHERE session_key IN ({placeholders})",
            list(cohort_keys),
        ).df()
        lap_ctx_9896 = con.execute(
            "SELECT * FROM gold.lap_context WHERE session_key = 9896 ORDER BY driver_number, lap_number"
        ).df()
    finally:
        con.close()

    train_df = all_examples[all_examples["session_key"].isin(split_manifest.train_keys)].copy()
    val_df = all_examples[all_examples["session_key"].isin(split_manifest.val_keys)].copy()
    test_df = all_examples[all_examples["session_key"].isin(split_manifest.test_keys)].copy()
    transfer_df = all_examples[
        all_examples["session_key"].isin(split_manifest.transfer_2026_keys)
    ].copy()

    models = build_stage3_experiment_models(ridge_alpha=15.0)
    forecast_frames: list[pd.DataFrame] = []
    d8_lap_frames: list[pd.DataFrame] = []
    experiment_summaries: dict[str, Any] = {}

    for exp_id, model in models.items():
        is_d8 = exp_id == "D8_correlated_trajectory_uncertainty"
        n_samp = 1000 if is_d8 else 250

        model.fit(train_df, calibration_df=val_df)
        val_f, val_l = evaluate_open_loop_trajectories_for_df(
            val_df, model, exp_id, "val", n_samples_override=n_samp, record_lap_rows=is_d8
        )
        test_f, test_l = evaluate_open_loop_trajectories_for_df(
            test_df, model, exp_id, "test", n_samples_override=n_samp, record_lap_rows=is_d8
        )
        forecast_frames.extend([val_f, test_f])
        if is_d8:
            d8_lap_frames.extend([val_l, test_l])

        untouched_f = test_f[test_f["session_key"].isin(split_manifest.untouched_test_keys)]
        experiment_summaries[exp_id] = {
            "val": summarize_model_performance(val_f),
            "test_all": summarize_model_performance(test_f),
            "test_untouched_new_races": summarize_model_performance(untouched_f),
        }

    # =========================================================================
    # 2026 Regulation-Era Transfer & Progressive Adaptation (D9 & D10, Section 15)
    # =========================================================================
    hist_23_25_keys = (
        split_manifest.train_keys + split_manifest.val_keys + split_manifest.test_keys
    )
    hist_23_25_df = all_examples[all_examples["session_key"].isin(hist_23_25_keys)].copy()

    # D9: Frozen 2023-2025 Pace V2 evaluated directly on 2026
    d9_frozen = PaceModelV2(
        degradation_model=HierarchicalTireDegradationModel(
            model_version="D9_frozen_2023_2025_degradation",
            ridge_alpha=15.0,
            use_absolute_compounds=True,
            use_circuit_pooling=True,
            use_weekend_adaptation=True,
            use_nonlinear_curve=True,
        ),
        model_version="D9_frozen_2026_transfer",
        use_weekend_qualifying_prior=True,
        use_stint_offset_adaptation=True,
        use_dynamic_stint_slope=True,
        use_correlated_uncertainty=True,
    ).fit(hist_23_25_df, calibration_df=val_df)
    d9_f, _ = evaluate_open_loop_trajectories_for_df(
        transfer_df, d9_frozen, "D9_frozen_2026_transfer", "transfer_2026", n_samples_override=600
    )
    forecast_frames.append(d9_f)
    experiment_summaries["D9_frozen_2026_transfer"] = {
        "transfer_2026": summarize_model_performance(d9_f)
    }

    # D10: Progressive 2026 adaptation using rolling-origin splits across the 5 2026 races
    d10_chunks: list[pd.DataFrame] = []
    for expanding_train_keys, target_2026_sk in iter_rolling_origin_splits(
        hist_23_25_keys, split_manifest.transfer_2026_keys
    ):
        roll_train_df = all_examples[all_examples["session_key"].isin(expanding_train_keys)].copy()
        d10_adapt = PaceModelV2(
            degradation_model=HierarchicalTireDegradationModel(
                model_version="D10_progressive_2026_degradation",
                ridge_alpha=15.0,
                use_absolute_compounds=True,
                use_circuit_pooling=True,
                use_weekend_adaptation=True,
                use_nonlinear_curve=True,
            ),
            model_version="D10_progressive_2026_adaptation",
            use_weekend_qualifying_prior=True,
            use_stint_offset_adaptation=True,
            use_dynamic_stint_slope=True,
            use_correlated_uncertainty=True,
        ).fit(roll_train_df, calibration_df=val_df)
        target_26_df = transfer_df[transfer_df["session_key"] == target_2026_sk]
        chunk_f, _ = evaluate_open_loop_trajectories_for_df(
            target_26_df,
            d10_adapt,
            "D10_progressive_2026_adaptation",
            "transfer_2026",
            n_samples_override=600,
        )
        d10_chunks.append(chunk_f)

    d10_f = pd.concat(d10_chunks, ignore_index=True) if d10_chunks else pd.DataFrame()
    forecast_frames.append(d10_f)
    experiment_summaries["D10_progressive_2026_adaptation"] = {
        "transfer_2026": summarize_model_performance(d10_f)
    }

    all_forecasts = pd.concat(forecast_frames, ignore_index=True)
    d8_lap_df = pd.concat(d8_lap_frames, ignore_index=True) if d8_lap_frames else pd.DataFrame()

    # =========================================================================
    # Ablation Summary Table (Section 14.1) & Paired Statistical Comparisons (Section 13.5)
    # =========================================================================
    test_cont_all = all_forecasts[
        (all_forecasts["split"] == "test")
        & (all_forecasts["scenario_type"] == SCENARIO_CONTINUE_STINT)
    ]
    val_test_cont = all_forecasts[
        all_forecasts["split"].isin(["val", "test"])
        & (all_forecasts["scenario_type"] == SCENARIO_CONTINUE_STINT)
    ]

    paired_comparisons = {
        "D8_vs_D0_PaceV1_test_h5_cum": paired_race_bootstrap_comparison(
            test_cont_all, "D8_correlated_trajectory_uncertainty", "D0_frozen_pace_v1", horizon=5, use_cumulative=True
        ),
        "D8_vs_D0_PaceV1_test_h10_cum": paired_race_bootstrap_comparison(
            test_cont_all, "D8_correlated_trajectory_uncertainty", "D0_frozen_pace_v1", horizon=10, use_cumulative=True
        ),
        "D8_vs_E2_test_h5_cum": paired_race_bootstrap_comparison(
            test_cont_all, "D8_correlated_trajectory_uncertainty", "B0_E2_compound_median", horizon=5, use_cumulative=True
        ),
        "D8_vs_E2_test_h10_cum": paired_race_bootstrap_comparison(
            test_cont_all, "D8_correlated_trajectory_uncertainty", "B0_E2_compound_median", horizon=10, use_cumulative=True
        ),
        "D8_vs_B4_adaptive_test_h5_cum": paired_race_bootstrap_comparison(
            test_cont_all, "D8_correlated_trajectory_uncertainty", "B4_adaptive_reset_progress", horizon=5, use_cumulative=True
        ),
        "D8_vs_D0_val_test_h5_cum": paired_race_bootstrap_comparison(
            val_test_cont, "D8_correlated_trajectory_uncertainty", "D0_frozen_pace_v1", horizon=5, use_cumulative=True
        ),
        "D10_vs_D9_2026_transfer_h5_cum": paired_race_bootstrap_comparison(
            all_forecasts[
                (all_forecasts["split"] == "transfer_2026")
                & (all_forecasts["scenario_type"] == SCENARIO_CONTINUE_STINT)
            ],
            "D10_progressive_2026_adaptation",
            "D9_frozen_2026_transfer",
            horizon=5,
            use_cumulative=True,
        ),
    }

    # Cold-start regime breakdown on D8 vs D0 vs E2 (Issue 2 verification)
    cold_start_breakdown: dict[str, Any] = {}
    for m_id in [
        "B0_E2_compound_median",
        "D0_frozen_pace_v1",
        "D8_correlated_trajectory_uncertainty",
    ]:
        sub_m = val_test_cont[
            (val_test_cont["model_id"] == m_id)
            & (val_test_cont["requested_horizon"] == 1)
            & val_test_cont["target_is_clean_green"].fillna(False).astype(bool)
        ]
        by_reg = []
        for reg, r_grp in sub_m.groupby("cold_start_regime"):
            by_reg.append(
                {
                    "regime": str(reg),
                    "n_laps": len(r_grp),
                    "mae_s": round(float(r_grp["endpoint_abs_error_s"].mean()), 4),
                    "rmse_s": round(float(np.sqrt((r_grp["endpoint_residual_s"] ** 2).mean())), 4),
                    "bias_s": round(float(r_grp["endpoint_residual_s"].mean()), 4),
                }
            )
        cold_start_breakdown[m_id] = by_reg

    # Save Stage 3 Artifacts (never touching artifacts/models/pace_v1 or artifacts/runs/stage2_pace_v1)
    run_dir = root / "artifacts" / "runs" / "stage3_degradation_v1"
    run_dir.mkdir(parents=True, exist_ok=True)
    model_dir = root / "artifacts" / "models" / "degradation_v1"
    model_dir.mkdir(parents=True, exist_ok=True)

    if exp_cfg_path.exists():
        (run_dir / "config.yaml").write_text(
            exp_cfg_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    (run_dir / "splits.json").write_text(
        json.dumps(split_manifest.to_dict(), indent=2), encoding="utf-8"
    )
    (run_dir / "data_manifest.json").write_text(
        json.dumps(
            {
                "cohort_sessions": len(cohort),
                "total_stint_observations": len(stint_obs_df),
                "total_stint_training_examples": len(all_examples),
                "pirelli_allocations_path": "data/raw/reference/pirelli_allocations.csv",
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    d8_model = models["D8_correlated_trajectory_uncertainty"]
    assert isinstance(d8_model, PaceModelV2)
    model_params = d8_model.diagnostics_dict()
    (model_dir / "model_params.json").write_text(
        json.dumps(model_params, indent=2), encoding="utf-8"
    )
    with open(model_dir / "pace_v2_degradation_v1.pkl", "wb") as f:
        pickle.dump(d8_model, f)

    run_metadata = {
        "run_at_utc": datetime.now(UTC).isoformat(),
        "git_sha": _git_sha(root),
        "stage2_frozen_tag": "stage2-v1",
        "random_seed": 42,
        "horizons": list(DEFAULT_STINT_HORIZONS),
        "fitted_model_diagnostics": model_params,
    }
    (run_dir / "run_metadata.json").write_text(
        json.dumps(run_metadata, indent=2), encoding="utf-8"
    )

    all_forecasts.to_parquet(run_dir / "forecasts.parquet", index=False)
    if not d8_lap_df.empty:
        d8_lap_df.to_parquet(run_dir / "forecast_laps.parquet", index=False)

    plot_files = generate_stage3_diagnostic_plots(
        all_forecasts=all_forecasts,
        d8_lap_df=d8_lap_df,
        d8_model=d8_model,
        stint_obs_df=stint_obs_df,
        lap_ctx_df=lap_ctx_9896,
        all_examples_df=all_examples,
        plots_dir=run_dir / "plots",
    )

    metrics_payload = {
        "audit": audit,
        "experiments": experiment_summaries,
        "paired_race_comparisons": paired_comparisons,
        "cold_start_regime_breakdown": cold_start_breakdown,
        "plots": plot_files,
    }
    (run_dir / "metrics.json").write_text(
        json.dumps(metrics_payload, indent=2), encoding="utf-8"
    )

    return metrics_payload
