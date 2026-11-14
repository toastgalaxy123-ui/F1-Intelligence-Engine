"""Fit and measure the successor using the frozen Stage 3 implementations."""
from copy import copy
from dataclasses import asdict, replace
from math import isfinite

import numpy as np
import pandas as pd

from f1ri.features.degradation_features import row_to_stint_forecast_request
from f1ri.models.contracts import SCENARIO_CONTINUE_STINT
from f1ri.models.stint_forecast import build_stage3_experiment_models
from f1ri.validation.stint_backtest import evaluate_open_loop_trajectories_for_df
from f1ri.validation.stint_metrics import (
    compute_cumulative_trajectory_metrics,
    compute_lap_horizon_metrics,
)

MODELS = (
    "B0_E2_compound_median", "B4_adaptive_reset_progress", "D0_frozen_pace_v1",
    "D3_nonlinear_age", "D5_hierarchical_circuit_compound", "D8_correlated_trajectory_uncertainty",
)
D8 = MODELS[-1]
NO_ADAPTATION = "D8R1_without_stint_adaptation"


def cutoff_request(row):
    """Honor a verified age of zero instead of falling through to endpoint age."""
    r = row_to_stint_forecast_request(row)
    return replace(r, tyre_age_at_start=int(row["tyre_age_at_forecast_start"]))


def fit_and_evaluate(frame, groups, plan, *, successor_only=False):
    config = plan["validation"]
    trains = frame[frame.session_key.isin(groups["train"])].copy()
    calibration = frame[frame.session_key.isin(groups["calibration"])].copy()
    evaluations = frame[frame.session_key.isin(groups["test"]) &
                        frame.cutoff_lap_number.mod(config["cutoff_stride"]).eq(0)]
    models = build_stage3_experiment_models()
    forecasts = []
    for name in ((D8,) if successor_only else MODELS):
        model = models[name]
        if name == D8:
            model.model_version = plan["model_version"]
            model.degradation_model.model_version = "DegradationV1_D8R1"
        print(f"Stage 3-R fitting {name}", flush=True)
        model.fit(trains, calibration_df=calibration)
        print(f"Stage 3-R validating {name}", flush=True)
        pred, _ = evaluate_open_loop_trajectories_for_df(
            evaluations, model, name, "test", n_samples_override=config["samples"], request_factory=cutoff_request)
        forecasts.append(pred)
    d8 = models[D8]
    if successor_only:
        return d8, forecasts[0]
    no_adapt = copy(d8)
    no_adapt.use_stint_offset_adaptation = False
    no_adapt.use_dynamic_stint_slope = False
    adapt, _ = evaluate_open_loop_trajectories_for_df(
        evaluations, no_adapt, NO_ADAPTATION, "test", n_samples_override=config["samples"], request_factory=cutoff_request)
    forecasts.append(adapt)
    return d8, pd.concat(forecasts, ignore_index=True)


def summaries(predictions, groups, horizons):
    result = {}
    match_keys = ["session_key", "driver_number", "cutoff_lap_number", "scenario_type", "requested_horizon"]
    matched = None
    for name, group in predictions.groupby("model_id", sort=True):
        opportunities = pd.MultiIndex.from_frame(group[match_keys]).sort_values()
        if not opportunities.is_unique or matched is not None and not matched.equals(opportunities):
            raise ValueError(f"Unmatched held-out targets for {name}")
        matched = opportunities
        cont = group[group.scenario_type.eq(SCENARIO_CONTINUE_STINT)]
        result[name] = {}
        for cohort, sub in (("all_test", cont), ("historical_uninspected_test",
                             cont[cont.session_key.isin(groups["test_historical_uninspected"])])):
            result[name][cohort] = {"endpoint": {}, "cumulative": {}}
            for horizon in horizons:
                end = compute_lap_horizon_metrics(sub, horizon)
                cum = compute_cumulative_trajectory_metrics(sub, horizon)
                for metrics, col, elig, actual in (
                    (end, "endpoint_abs_error_s", "target_is_clean_green", "endpoint_residual_s"),
                    (cum, "cumulative_abs_error_s", "trajectory_is_complete_clean", "cumulative_residual_s"),
                ):
                    rows = sub[sub.requested_horizon.eq(horizon) & sub[elig]].dropna(subset=[col])
                    metrics.update(median_absolute_error_s=float(rows[col].median()) if len(rows) else None,
                                   median_signed_error_s=float(rows[actual].median()) if len(rows) else None,
                                   p90_absolute_error_s=float(rows[col].quantile(.9)) if len(rows) else None,
                                   unique_drivers=int(rows.driver_id.nunique()) if len(rows) else 0)
                result[name][cohort]["endpoint"][horizon] = end
                result[name][cohort]["cumulative"][horizon] = cum
        replacement = group[~group.scenario_type.eq(SCENARIO_CONTINUE_STINT)]
        result[name]["conditional_known_replacement"] = {
            scenario: {h: compute_cumulative_trajectory_metrics(rows, h) for h in horizons}
            for scenario, rows in replacement.groupby("scenario_type")}
    return result


def adaptation_comparison(predictions, horizons):
    family = predictions[predictions.model_id.isin([D8, NO_ADAPTATION]) &
                         predictions.scenario_type.eq(SCENARIO_CONTINUE_STINT)]
    return [{"model": model, "regime": regime, "horizon": h,
             "endpoint": compute_lap_horizon_metrics(rows, h),
             "cumulative": compute_cumulative_trajectory_metrics(rows, h)}
            for (model, regime), rows in family.groupby(["model_id", "cold_start_regime"]) for h in horizons]


def gates(scores, config, *, replacement_ok, strategy_ok, serialization_ok, reproducible):
    primary = scores[D8]["all_test"]
    untouched = scores[D8]["historical_uninspected_test"]
    targets = (("endpoint", 1, "race_weighted_mae_s"), ("cumulative", 5, "cum_race_weighted_mae_s"))
    comparison = {}
    for side, h, key in targets:
        value = untouched[side][h][key]
        benchmarks = {base: scores[base]["historical_uninspected_test"][side][h][key] for base in MODELS[:2]}
        comparison[f"{side}_h{h}"] = {"d8r1_s": value, "baselines_s": benchmarks,
                                       "pass": value is not None and all(v is not None and value <= v
                                                                         for v in benchmarks.values())}
    coverage = {h: {k: primary["cumulative"][h][f"cum_coverage_{k}"] for k in (50, 90)}
                for h in (5, 10, 15, 20)}
    return {
        "G0_data_provenance": True, "G1_numerical_integrity": serialization_ok,
        "G2_mean_quality": all(v["pass"] for v in comparison.values()),
        "G3_uncertainty_quality": all(
            cov[90] is not None and cov[90] >= config["minimum_test_coverage_90"] and
            cov[50] is not None and cov[50] >= config["minimum_test_coverage_50"] for cov in coverage.values()),
        "G4_replacement_forecasting": replacement_ok, "G5_strategy_integration": strategy_ok,
        "G6_reproducibility": reproducible, "matched_mean_benchmarks": comparison, "coverage_by_horizon": coverage,
    }


def forecast_integrity(batch):
    summary = batch.forecast
    expected = [r.expected_lap_time_s for r in summary.lap_forecasts]
    return bool(expected and all(isfinite(x) and 30 < x < 240 for x in expected)
                and np.isfinite(batch.lap_samples_s).all()
                and np.allclose(batch.lap_samples_s.cumsum(axis=1), batch.cumulative_samples_s)
                and np.isclose(sum(expected), summary.expected_total_time_s))


def forecast_document(request, batch):
    flash = asdict(replace(batch.forecast, sampled_lap_trajectories_s=None))
    return {"request": asdict(request), "forecast": flash, "numerical_integrity": forecast_integrity(batch),
            "stream_id": batch.random_stream_id, "model_hash": batch.artifact_sha256}
