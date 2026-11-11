"""Chronological backtesting, error slicing, residual diagnostics, and Stage 2 experiment runner."""
from __future__ import annotations

import json
import pickle
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from f1ri.features.pace_examples import populate_pace_training_examples
from f1ri.features.pace_features import build_dataset_quality_audit, row_to_pace_request
from f1ri.ingestion.batch import load_experiment_cohort
from f1ri.models.contracts import PaceModel
from f1ri.models.pace import AdditivePaceModel, build_experiment_models
from f1ri.validation.calibration import (
    compute_probabilistic_metrics,
    conditional_calibration_breakdown,
)
from f1ri.validation.leakage import (
    assert_examples_point_in_time_valid,
     validate_forecast_timestamp,
)
from f1ri.validation.splits import (
    TemporalSplitManifest,
    build_split_manifest,
    iter_rolling_origin_splits,
)


def generate_predictions_for_df(
    eval_df: pd.DataFrame,
    model: PaceModel,
    model_id: str,
    split_name: str,
) -> pd.DataFrame:
    """Evaluate a fitted PaceModel across every forecast opportunity row in eval_df."""
    if eval_df.empty:
        return pd.DataFrame()

    out_rows: list[dict[str, Any]] = []
    for _, row in eval_df.iterrows():
        req = row_to_pace_request(row)
        dist = model.predict(req)
        validate_forecast_timestamp(req, dist)
        obs = (
            float(row["observed_target_lap_time_s"])
            if pd.notna(row.get("observed_target_lap_time_s"))
            else None
        )
        is_clean = bool(row.get("target_is_clean_green", False)) and obs is not None
        residual = (obs - dist.expected_seconds) if obs is not None else None

        out_rows.append(
            {
                "model_id": model_id,
                "model_version": dist.model_version,
                "split": split_name,
                "session_key": int(row["session_key"]),
                "season": int(row["season"]) if pd.notna(row.get("season")) else None,
                "regulation_era": str(row.get("regulation_era") or "OTHER"),
                "circuit_short_name": str(row.get("circuit_short_name") or "Unknown"),
                "driver_number": int(row["driver_number"]),
                "driver_id": str(row.get("driver_id") or ""),
                "team_id": str(row.get("team_id") or ""),
                "cutoff_lap_number": int(row["cutoff_lap_number"]),
                "target_lap_number": int(row["target_lap_number"]),
                "horizon": int(row["horizon"]),
                "forecast_as_of_utc": row["forecast_as_of_utc"],
                "trained_through_utc": dist.trained_through_utc,
                "stint_number": int(row["stint_number"]) if pd.notna(row.get("stint_number")) else None,
                "compound": str(row.get("compound") or "UNKNOWN"),
                "tyre_age_at_lap_start": int(row["tyre_age_at_lap_start"] or 0),
                "race_progress": float(row["race_progress"] or 0.0),
                "last_known_track_temp_c": (
                    float(row["last_known_track_temp_c"])
                    if pd.notna(row.get("last_known_track_temp_c"))
                    else None
                ),
                "clean_laps_observed_current_stint": int(
                    row.get("clean_laps_observed_current_stint") or 0
                ),
                "clean_laps_observed_race": int(row.get("clean_laps_observed_race") or 0),
                "expected_seconds": dist.expected_seconds,
                "median_seconds": dist.median_seconds,
                "std_seconds": dist.std_seconds,
                "p25_seconds": dist.p25_seconds,
                "p75_seconds": dist.p75_seconds,
                "p05_seconds": dist.p05_seconds,
                "p95_seconds": dist.p95_seconds,
                "confidence_level": dist.confidence_level,
                "observed_target_lap_time_s": obs,
                "target_is_clean_green": is_clean,
                "target_exclusion_reason": (
                    str(row["target_exclusion_reason"])
                    if pd.notna(row.get("target_exclusion_reason"))
                    else None
                ),
                "residual_s": residual,
                "abs_error_s": abs(residual) if residual is not None else None,
            }
        )
    return pd.DataFrame(out_rows)


def compute_accuracy_metrics(pred_df: pd.DataFrame, horizon: int = 1) -> dict[str, Any]:
    """Compute lap-weighted MAE, race-weighted MAE, RMSE, bias, and probabilistic metrics."""
    sub_h = pred_df[pred_df["horizon"] == horizon] if "horizon" in pred_df.columns else pred_df
    total_forecasts = len(sub_h)
    eligible = sub_h[
        sub_h["target_is_clean_green"].fillna(False).astype(bool)
        & sub_h["observed_target_lap_time_s"].notna()
        & sub_h["expected_seconds"].notna()
    ].copy()

    if eligible.empty:
        return {
            "horizon": horizon,
            "total_forecasts": int(total_forecasts),
            "eligible_clean_laps": 0,
            "eligible_pct": 0.0,
            "lap_weighted_mae_s": None,
            "race_weighted_mae_s": None,
            "rmse_s": None,
            "bias_s": None,
            "median_ae_s": None,
        }

    res = (
        eligible["observed_target_lap_time_s"].to_numpy(dtype=float)
        - eligible["expected_seconds"].to_numpy(dtype=float)
    )
    abs_err = np.abs(res)
    lap_mae = float(np.mean(abs_err))
    rmse = float(np.sqrt(np.mean(res**2)))
    bias = float(np.mean(res))
    med_ae = float(np.median(abs_err))

    race_maes = eligible.groupby("session_key")["abs_error_s"].mean().to_numpy(dtype=float)
    race_weighted_mae = float(np.mean(race_maes)) if len(race_maes) > 0 else lap_mae

    prob = compute_probabilistic_metrics(eligible)
    return {
        "horizon": int(horizon),
        "total_forecasts": int(total_forecasts),
        "eligible_clean_laps": len(eligible),
        "eligible_pct": round(100.0 * len(eligible) / max(total_forecasts, 1), 2),
        "lap_weighted_mae_s": round(lap_mae, 4),
        "race_weighted_mae_s": round(race_weighted_mae, 4),
        "rmse_s": round(rmse, 4),
        "bias_s": round(bias, 4),
        "median_ae_s": round(med_ae, 4),
        **prob,
    }


def slice_error_breakdown(
    pred_df: pd.DataFrame, group_col: str, horizon: int = 1
) -> list[dict[str, Any]]:
    sub = pred_df[
        (pred_df["horizon"] == horizon)
        & pred_df["target_is_clean_green"].fillna(False).astype(bool)
        & pred_df["observed_target_lap_time_s"].notna()
    ].copy()
    if sub.empty:
        return []
    rows: list[dict[str, Any]] = []
    for k, grp in sub.groupby(group_col, dropna=False):
        m = compute_accuracy_metrics(grp, horizon=horizon)
        rows.append(
            {
                group_col: "MISSING" if pd.isna(k) else str(k),
                "n_laps": m["eligible_clean_laps"],
                "mae_s": m["lap_weighted_mae_s"],
                "rmse_s": m["rmse_s"],
                "bias_s": m["bias_s"],
                "coverage_50": m.get("coverage_50"),
                "coverage_90": m.get("coverage_90"),
            }
        )
    return rows


def paired_race_comparison(
    pred_df: pd.DataFrame,
    candidate_model_id: str,
    baseline_model_id: str,
    horizon: int = 1,
) -> dict[str, Any]:
    """Compare candidate vs baseline at the race level to account for within-race correlation."""
    sub = pred_df[
        (pred_df["horizon"] == horizon)
        & pred_df["target_is_clean_green"].fillna(False).astype(bool)
        & pred_df["observed_target_lap_time_s"].notna()
        & pred_df["model_id"].isin([candidate_model_id, baseline_model_id])
    ]
    by_race = (
        sub.groupby(["session_key", "circuit_short_name", "model_id"])["abs_error_s"]
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
    se_diff = float(np.std(diffs, ddof=1) / np.sqrt(n_races)) if n_races > 1 else 0.0
    races_won = int(np.sum(diffs < 0.0))

    per_race_records = []
    for (sk, circ), r in by_race.iterrows():
        c_val = float(r[candidate_model_id])
        b_val = float(r[baseline_model_id])
        per_race_records.append(
            {
                "session_key": int(sk),
                "circuit_short_name": str(circ),
                "candidate_mae_s": round(c_val, 4),
                "baseline_mae_s": round(b_val, 4),
                "delta_mae_s": round(c_val - b_val, 4),
            }
        )

    return {
        "candidate_model": candidate_model_id,
        "baseline_model": baseline_model_id,
        "n_races": n_races,
        "races_improved": races_won,
        "mean_race_mae_delta_s": round(mean_diff, 4),
        "se_race_mae_delta_s": round(se_diff, 4),
        "ci90_lower_s": round(mean_diff - 1.645 * se_diff, 4),
        "ci90_upper_s": round(mean_diff + 1.645 * se_diff, 4),
        "per_race": per_race_records,
    }


def generate_diagnostic_plots(
    pred_df: pd.DataFrame,
    model_id: str,
    plots_dir: Path,
) -> list[str]:
    """Generate the 8 diagnostic plots (Plots A through H) required in Section 10.3."""
    plots_dir.mkdir(parents=True, exist_ok=True)
    sub = pred_df[
        (pred_df["model_id"] == model_id)
        & (pred_df["horizon"] == 1)
        & pred_df["target_is_clean_green"].fillna(False).astype(bool)
        & pred_df["observed_target_lap_time_s"].notna()
    ].copy()
    if sub.empty:
        return []

    saved_files: list[str] = []

    # Combined 4-panel figure 1: Plots A, B, C, D
    fig1, axes1 = plt.subplots(2, 2, figsize=(13, 9.5), dpi=140)

    # Plot A: Actual vs Predicted
    ax = axes1[0, 0]
    for circ, grp in sub.groupby("circuit_short_name"):
        ax.scatter(
            grp["expected_seconds"],
            grp["observed_target_lap_time_s"],
            s=14,
            alpha=0.65,
            label=str(circ),
        )
    lims = [
        min(sub["expected_seconds"].min(), sub["observed_target_lap_time_s"].min()) - 1.0,
        max(sub["expected_seconds"].max(), sub["observed_target_lap_time_s"].max()) + 1.0,
    ]
    ax.plot(lims, lims, "k--", linewidth=1.2, label="y = x")
    ax.set_title("Plot A — Actual vs Predicted Clean-Air Lap Time (s)")
    ax.set_xlabel("Predicted Lap Time (s)")
    ax.set_ylabel("Observed Lap Time (s)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=7, loc="upper left")

    # Plot B: Residual vs Tire Age
    ax = axes1[0, 1]
    for cmpd, grp in sub.groupby("compound"):
        ax.scatter(
            grp["tyre_age_at_lap_start"],
            grp["residual_s"],
            s=14,
            alpha=0.55,
            label=str(cmpd),
        )
    age_bins = sub.groupby(pd.cut(sub["tyre_age_at_lap_start"], bins=[0, 5, 10, 15, 20, 25, 35, 60]), observed=False)["residual_s"].mean()
    bin_centers = [iv.mid for iv in age_bins.index if pd.notna(iv)]
    ax.plot(bin_centers, age_bins.to_numpy(dtype=float), "k-o", linewidth=1.8, markersize=4, label="Binned Mean")
    ax.axhline(0.0, color="red", linestyle="--", linewidth=1.0)
    ax.set_ylim(-2.0, 2.0)
    ax.set_title("Plot B — Residual (Observed - Predicted) vs Tire Age")
    ax.set_xlabel("Tire Age at Lap Start (laps)")
    ax.set_ylabel("Residual (s)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=7)

    # Plot C: Residual vs Race Progress
    ax = axes1[1, 0]
    ax.scatter(sub["race_progress"], sub["residual_s"], s=12, alpha=0.45, color="#2563EB")
    prog_bins = sub.groupby(pd.cut(sub["race_progress"], bins=np.linspace(0, 1, 11)), observed=False)["residual_s"].mean()
    p_centers = [iv.mid for iv in prog_bins.index if pd.notna(iv)]
    ax.plot(p_centers, prog_bins.to_numpy(dtype=float), "k-o", linewidth=1.8, markersize=4, label="Binned Mean")
    ax.axhline(0.0, color="red", linestyle="--", linewidth=1.0)
    ax.set_ylim(-2.0, 2.0)
    ax.set_title("Plot C — Residual vs Race Progress (l / N)")
    ax.set_xlabel("Scheduled Race Progress Fraction")
    ax.set_ylabel("Residual (s)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)

    # Plot D: Residual vs Track Temperature
    ax = axes1[1, 1]
    t_sub = sub[sub["last_known_track_temp_c"].notna()]
    if not t_sub.empty:
        ax.scatter(
            t_sub["last_known_track_temp_c"],
            t_sub["residual_s"],
            s=14,
            alpha=0.5,
            color="#D97706",
        )
    ax.axhline(0.0, color="red", linestyle="--", linewidth=1.0)
    ax.set_ylim(-2.0, 2.0)
    ax.set_title("Plot D — Residual vs Point-in-Time Track Temperature (°C)")
    ax.set_xlabel("Track Temperature (°C)")
    ax.set_ylabel("Residual (s)")
    ax.grid(True, alpha=0.3)

    fig1.tight_layout()
    p1 = plots_dir / "stage2_residuals_abcd.png"
    fig1.savefig(p1)
    plt.close(fig1)
    saved_files.append(str(p1))

    # Combined 4-panel figure 2: Plots E, F, G, H
    fig2, axes2 = plt.subplots(2, 2, figsize=(13, 9.5), dpi=140)

    # Plot E: Error distribution by compound
    ax = axes2[0, 0]
    cmpd_order = [c for c in ("SOFT", "MEDIUM", "HARD") if c in sub["compound"].unique()]
    data_by_c = [sub[sub["compound"] == c]["residual_s"].dropna().to_numpy(dtype=float) for c in cmpd_order]
    if data_by_c:
        ax.boxplot(data_by_c, tick_labels=cmpd_order, showfliers=False)
    ax.axhline(0.0, color="red", linestyle="--", linewidth=1.0)
    ax.set_title("Plot E — Residual Distribution by Tire Compound")
    ax.set_ylabel("Residual (s)")
    ax.grid(True, alpha=0.3)

    # Plot F: MAE by Race (comparing E2 baseline vs Pace V1 across 2025 val/test and 2026 transfer)
    ax = axes2[0, 1]
    all_h1 = pred_df[
        (pred_df["horizon"] == 1)
        & pred_df["target_is_clean_green"].fillna(False).astype(bool)
        & pred_df["model_id"].isin(
            ["E2_compound_recent_median", model_id, "E10_progressive_2026_adaptation"]
        )
    ].copy()
    all_h1["plot_model"] = all_h1["model_id"].replace(
        {"E10_progressive_2026_adaptation": model_id}
    )
    race_piv = (
        all_h1.groupby(["session_key", "circuit_short_name", "season", "plot_model"])["abs_error_s"]
        .mean()
        .unstack("plot_model")
        .reset_index()
        .sort_values(["season", "session_key"])
    )
    if not race_piv.empty and model_id in race_piv.columns:
        labels = [f"{r['season']}\n{r['circuit_short_name'][:6]}" for _, r in race_piv.iterrows()]
        x = np.arange(len(labels))
        w = 0.36
        if "E2_compound_recent_median" in race_piv.columns:
            ax.bar(x - w / 2, race_piv["E2_compound_recent_median"], width=w, label="E2 Compound Median", color="#94A3B8")
        ax.bar(x + w / 2, race_piv[model_id], width=w, label="Pace V1 (E8 / E10)", color="#0F766E")
        ax.set_xticks(x)
        ax.set_xticklabels(labels, fontsize=7)
    ax.set_title("Plot F — Held-Out Next-Lap MAE by Race (s)")
    ax.set_ylabel("MAE (s)")
    ax.grid(True, alpha=0.3, axis="y")
    ax.legend(fontsize=8)

    # Plot G: Prediction Interval Calibration
    ax = axes2[1, 0]
    cal_by_split = []
    for sp_name, grp in sub.groupby("split"):
        m = compute_probabilistic_metrics(grp)
        cal_by_split.append((str(sp_name), m["coverage_50"] or 0.0, m["coverage_90"] or 0.0))
    ax.plot([0, 1], [0, 1], "k--", linewidth=1.2, label="Ideal Calibration")
    for sp_name, c50, c90 in cal_by_split:
        ax.plot([0.50, 0.90], [c50, c90], "-o", linewidth=1.8, markersize=6, label=f"Split: {sp_name}")
    ax.set_xlim(0.4, 1.0)
    ax.set_ylim(0.4, 1.0)
    ax.set_title("Plot G — Prediction Interval Calibration (50% & 90%)")
    ax.set_xlabel("Nominal Coverage")
    ax.set_ylabel("Empirical Coverage")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)

    # Plot H: Chronological Performance & 2026 Adaptation (E9 Frozen vs E10 Progressive)
    ax = axes2[1, 1]
    e9_10 = pred_df[
        (pred_df["horizon"] == 1)
        & pred_df["target_is_clean_green"].fillna(False).astype(bool)
        & pred_df["model_id"].isin(["E9_frozen_2026_transfer", "E10_progressive_2026_adaptation"])
    ]
    if not e9_10.empty:
        piv_26 = (
            e9_10.groupby(["session_key", "circuit_short_name", "model_id"])["abs_error_s"]
            .mean()
            .unstack("model_id")
            .reset_index()
            .sort_values("session_key")
        )
        x26 = np.arange(len(piv_26))
        lbl26 = [f"2026 {r['circuit_short_name']}" for _, r in piv_26.iterrows()]
        if "E9_frozen_2026_transfer" in piv_26.columns:
            ax.plot(x26, piv_26["E9_frozen_2026_transfer"], "-s", label="E9 Frozen (2023-25)", color="#DC2626")
        if "E10_progressive_2026_adaptation" in piv_26.columns:
            ax.plot(x26, piv_26["E10_progressive_2026_adaptation"], "-o", label="E10 Progressive 2026", color="#16A34A")
        ax.set_xticks(x26)
        ax.set_xticklabels(lbl26, fontsize=8)
    ax.set_title("Plot H — 2026 Regulation Era Transfer vs Progressive Adaptation")
    ax.set_ylabel("Next-Lap MAE (s)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)

    fig2.tight_layout()
    p2 = plots_dir / "stage2_diagnostics_efgh.png"
    fig2.savefig(p2)
    plt.close(fig2)
    saved_files.append(str(p2))

    return saved_files


def _git_sha(root: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        return res.stdout.strip() or "UNKNOWN"
    except OSError:
        return "UNKNOWN"


def run_stage2_pipeline(
    root: Path,
    experiment_config_path: Path | None = None,
) -> dict[str, Any]:
    """Execute the full Stage 2 pipeline: examples build, dataset audit, E0-E10 backtest, and artifacts."""
    db_path = root / "data" / "warehouse" / "f1ri.duckdb"
    if not db_path.exists():
        raise FileNotFoundError(f"Warehouse not found: {db_path}. Run 'f1ri ingest-batch' first.")

    cohort = load_experiment_cohort(root, experiment_config_path)
    con = duckdb.connect(str(db_path))
    try:
        populate_pace_training_examples(con, horizons=(1, 3, 5))
        audit = build_dataset_quality_audit(con)

        bounds_rows = con.execute("""
            SELECT session_key, MIN(lap_start_utc), MAX(lap_end_estimate_utc)
            FROM gold.lap_context
            GROUP BY session_key
        """).fetchall()
        session_bounds = {int(r[0]): (r[1], r[2]) for r in bounds_rows}
        split_manifest: TemporalSplitManifest = build_split_manifest(cohort, session_bounds)

        all_examples = con.execute("""
            SELECT * FROM gold.pace_training_examples
            ORDER BY season, session_key, driver_number, cutoff_lap_number, horizon
        """).df()
    finally:
        con.close()

    assert_examples_point_in_time_valid(all_examples)

    train_df = all_examples[all_examples["session_key"].isin(split_manifest.train_keys)].copy()
    val_df = all_examples[all_examples["session_key"].isin(split_manifest.val_keys)].copy()
    test_df = all_examples[all_examples["session_key"].isin(split_manifest.test_keys)].copy()
    transfer_df = all_examples[
        all_examples["session_key"].isin(split_manifest.transfer_2026_keys)
    ].copy()

    models = build_experiment_models(ridge_alpha=15.0)
    pred_frames: list[pd.DataFrame] = []
    experiment_summaries: dict[str, Any] = {}

    # Fit E0 through E8 on train_df (using OOF/train for E0-E7 and val_df for E8 interval calibration on test)
    for exp_id, model in models.items():
        if isinstance(model, AdditivePaceModel) and exp_id == "E8_pace_v1_calibrated":
            model.fit(train_df, calibration_df=val_df)
        elif isinstance(model, AdditivePaceModel):
            model.fit(train_df)
        else:
            model.fit(train_df)

        val_preds = generate_predictions_for_df(val_df, model, exp_id, "val")
        test_preds = generate_predictions_for_df(test_df, model, exp_id, "test")
        pred_frames.extend([val_preds, test_preds])

        untouched_test_preds = test_preds[
            test_preds["session_key"].isin(split_manifest.untouched_test_keys)
        ]
        experiment_summaries[exp_id] = {
            "val_h1": compute_accuracy_metrics(val_preds, horizon=1),
            "val_h3": compute_accuracy_metrics(val_preds, horizon=3),
            "val_h5": compute_accuracy_metrics(val_preds, horizon=5),
            "test_h1": compute_accuracy_metrics(test_preds, horizon=1),
            "test_h3": compute_accuracy_metrics(test_preds, horizon=3),
            "test_h5": compute_accuracy_metrics(test_preds, horizon=5),
            "untouched_test_h1": compute_accuracy_metrics(untouched_test_preds, horizon=1),
        }

    # E9: Frozen 2023-2025 model (trained on train + val + test 2023-2025) evaluated directly on 2026
    hist_23_25_df = all_examples[
        all_examples["session_key"].isin(
            split_manifest.train_keys + split_manifest.val_keys + split_manifest.test_keys
        )
    ].copy()
    frozen_2026_model = AdditivePaceModel(
        model_version="E9_frozen_2026_transfer",
        ridge_alpha=15.0,
        include_compound_age=True,
        include_track_temp=True,
        use_online_adaptation=True,
        use_calibrated_intervals=True,
    ).fit(hist_23_25_df, calibration_df=val_df)
    e9_preds = generate_predictions_for_df(
        transfer_df, frozen_2026_model, "E9_frozen_2026_transfer", "transfer_2026"
    )
    pred_frames.append(e9_preds)
    experiment_summaries["E9_frozen_2026_transfer"] = {
        "transfer_2026_h1": compute_accuracy_metrics(e9_preds, horizon=1),
        "transfer_2026_h3": compute_accuracy_metrics(e9_preds, horizon=3),
        "transfer_2026_h5": compute_accuracy_metrics(e9_preds, horizon=5),
    }

    # E10: Progressive 2026 adaptation using rolling-origin splits across 2026 races
    e10_chunks: list[pd.DataFrame] = []
    base_keys = split_manifest.train_keys + split_manifest.val_keys + split_manifest.test_keys
    for expanding_train_keys, target_2026_sk in iter_rolling_origin_splits(
        base_keys, split_manifest.transfer_2026_keys
    ):
        roll_train_df = all_examples[all_examples["session_key"].isin(expanding_train_keys)].copy()
        adapt_model = AdditivePaceModel(
            model_version="E10_progressive_2026_adaptation",
            ridge_alpha=15.0,
            include_compound_age=True,
            include_track_temp=True,
            use_online_adaptation=True,
            use_calibrated_intervals=True,
        ).fit(roll_train_df, calibration_df=val_df)

        target_26_df = transfer_df[transfer_df["session_key"] == target_2026_sk]
        e10_chunks.append(
            generate_predictions_for_df(
                target_26_df,
                adapt_model,
                "E10_progressive_2026_adaptation",
                "transfer_2026",
            )
        )

    e10_preds = pd.concat(e10_chunks, ignore_index=True) if e10_chunks else pd.DataFrame()
    pred_frames.append(e10_preds)
    experiment_summaries["E10_progressive_2026_adaptation"] = {
        "transfer_2026_h1": compute_accuracy_metrics(e10_preds, horizon=1),
        "transfer_2026_h3": compute_accuracy_metrics(e10_preds, horizon=3),
        "transfer_2026_h5": compute_accuracy_metrics(e10_preds, horizon=5),
    }

    # Also evaluate E2 baseline on 2026 for comparison
    e2_2026_preds = generate_predictions_for_df(
        transfer_df,
        models["E2_compound_recent_median"],
        "E2_compound_recent_median",
        "transfer_2026",
    )
    pred_frames.append(e2_2026_preds)
    experiment_summaries["E2_compound_recent_median"]["transfer_2026_h1"] = (
        compute_accuracy_metrics(e2_2026_preds, horizon=1)
    )

    all_preds = pd.concat(pred_frames, ignore_index=True)

    # Sliced diagnostics on Pace V1 (E8) across val + test
    e8_eval = all_preds[
        (all_preds["model_id"] == "E8_pace_v1_calibrated")
        & all_preds["split"].isin(["val", "test"])
    ].copy()
    e8_eval["tyre_age_bucket"] = pd.cut(
        e8_eval["tyre_age_at_lap_start"],
        bins=[-1, 5, 15, 25, 100],
        labels=["00_05_FRESH", "06_15_MID", "16_25_USED", "26_PLUS_OLD"],
    ).astype(str)
    e8_eval["race_progress_bucket"] = pd.cut(
        e8_eval["race_progress"],
        bins=[0.0, 0.33, 0.67, 1.5],
        labels=["1_EARLY", "2_MIDDLE", "3_LATE"],
    ).astype(str)
    e8_eval["clean_laps_bucket"] = pd.cut(
        e8_eval["clean_laps_observed_current_stint"],
        bins=[-1, 0, 2, 100],
        labels=["0_COLD", "1_2_EARLY", "3_PLUS_WARM"],
    ).astype(str)

    slicing_report = {
        "by_race": slice_error_breakdown(e8_eval, "circuit_short_name", horizon=1),
        "by_compound": slice_error_breakdown(e8_eval, "compound", horizon=1),
        "by_tyre_age_bucket": slice_error_breakdown(e8_eval, "tyre_age_bucket", horizon=1),
        "by_stint_number": slice_error_breakdown(e8_eval, "stint_number", horizon=1),
        "by_race_progress_bucket": slice_error_breakdown(
            e8_eval, "race_progress_bucket", horizon=1
        ),
        "by_clean_laps_bucket": slice_error_breakdown(e8_eval, "clean_laps_bucket", horizon=1),
        "by_team": slice_error_breakdown(e8_eval, "team_id", horizon=1),
        "conditional_calibration_by_compound": conditional_calibration_breakdown(
            e8_eval[e8_eval["horizon"] == 1], "compound"
        ),
    }

    val_test_preds = all_preds[all_preds["split"].isin(["val", "test"])]
    paired_comparisons = {
        "E8_vs_E2_val_and_test": paired_race_comparison(
            val_test_preds, "E8_pace_v1_calibrated", "E2_compound_recent_median", horizon=1
        ),
        "E8_vs_E3_val_and_test": paired_race_comparison(
            val_test_preds, "E8_pace_v1_calibrated", "E3_race_progress_drift", horizon=1
        ),
        "E8_vs_E1_val_and_test": paired_race_comparison(
            val_test_preds, "E8_pace_v1_calibrated", "E1_recent_clean_median", horizon=1
        ),
        "E10_vs_E9_2026_transfer": paired_race_comparison(
            all_preds[all_preds["split"] == "transfer_2026"],
            "E10_progressive_2026_adaptation",
            "E9_frozen_2026_transfer",
            horizon=1,
        ),
    }

    # Save artifacts
    run_dir = root / "artifacts" / "runs" / "stage2_pace_v1"
    run_dir.mkdir(parents=True, exist_ok=True)
    model_dir = root / "artifacts" / "models" / "pace_v1"
    model_dir.mkdir(parents=True, exist_ok=True)

    exp_cfg_file = experiment_config_path or (
        root / "config" / "experiments" / "stage2_pace_v1.yaml"
    )
    if exp_cfg_file.exists():
        (run_dir / "config.yaml").write_text(
            exp_cfg_file.read_text(encoding="utf-8"), encoding="utf-8"
        )

    (run_dir / "splits.json").write_text(
        json.dumps(split_manifest.to_dict(), indent=2), encoding="utf-8"
    )

    e8_model = models["E8_pace_v1_calibrated"]
    assert isinstance(e8_model, AdditivePaceModel)
    model_params = e8_model.diagnostics_dict()
    (model_dir / "model_params.json").write_text(
        json.dumps(model_params, indent=2), encoding="utf-8"
    )
    with open(model_dir / "pace_v1.pkl", "wb") as f:
        pickle.dump(e8_model, f)

    run_metadata = {
        "run_at_utc": datetime.now(UTC).isoformat(),
        "git_sha": _git_sha(root),
        "random_seed": 42,
        "total_training_examples": len(all_examples),
        "fitted_model_diagnostics": model_params,
    }
    (run_dir / "run_metadata.json").write_text(
        json.dumps(run_metadata, indent=2), encoding="utf-8"
    )

    # Save predictions.parquet
    all_preds.to_parquet(run_dir / "predictions.parquet", index=False)

    plot_files = generate_diagnostic_plots(
        all_preds, "E8_pace_v1_calibrated", run_dir / "plots"
    )

    metrics_payload = {
        "audit": audit,
        "experiments": experiment_summaries,
        "slicing_e8": slicing_report,
        "paired_race_comparisons": paired_comparisons,
        "plots": plot_files,
    }
    (run_dir / "metrics.json").write_text(
        json.dumps(metrics_payload, indent=2), encoding="utf-8"
    )

    return metrics_payload
