"""Chronological development-only clean traversal baselines."""
from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
from math import hypot, sqrt
from statistics import mean, median

import numpy as np

MODELS = ("R0", "R1", "R2", "R3")


def split_drivers(rows, config):
    drivers = {r["driver_number"] for r in rows}
    ordered = sorted(drivers, key=lambda d: sha256(f"{config['seed']}/{d}".encode()).hexdigest())
    return set(ordered[:round(len(ordered) * config["validation_driver_fraction"])])


def earlier(target, observations):
    return [r for r in observations if r["opportunity_id"] != target["opportunity_id"]
            and r["outcome"]["available_by_utc"] < target["as_of_utc"]]


def tire_context(target, control, config):
    a, b = target["tyre_age"], control["tyre_age"]
    t, c = target["track_temperature_c"], control["track_temperature_c"]
    return (target["compound"] == control["compound"] and a is not None and b is not None
            and abs(a - b) <= config["maximum_tyre_age_delta"]
            and abs(target["race_progress"] - control["race_progress"]) <= config["maximum_progress_delta"]
            and (t is None or c is None or abs(t - c) <= config["maximum_track_temperature_delta_c"]))


def predict(target, pool, history, config):
    """R2 uses an evaluation driver's own prior clean data as online adaptation."""
    result = {}
    elapsed = lambda r: r["outcome"]["observed_s"]
    minimum = config["minimum_pool"]
    if len(pool) >= minimum:
        base = median(map(elapsed, pool))
        result["R0"] = (base, pool, None)
        matched = [r for r in pool if tire_context(target, r, config)]
        weight = len(matched) / (len(matched) + config["hierarchical_shrinkage"])
        pooled = (1 - weight) * base + weight * median(map(elapsed, matched)) if matched else base
        result["R3"] = (pooled, pool, None)
    same_stint = [r for r in history if target["stint_number"] is not None
                  and r["driver_number"] == target["driver_number"] and r["stint_number"] == target["stint_number"]
                  and tire_context(target, r, config)]
    if len(same_stint) >= config["minimum_same_stint"]:
        result["R2"] = (median(map(elapsed, same_stint)), same_stint, None)
    fractions = [r for r in pool if r["outcome"]["stage1_clean_lap"]
                 and r["outcome"]["full_lap_observed_s"] is not None
                 and 0 < elapsed(r) < r["outcome"]["full_lap_observed_s"]]
    if target["d8_lap_s"] is not None and target["d8_lap_std_s"] is not None and len(fractions) >= max(2, minimum):
        ratios = [elapsed(r) / r["outcome"]["full_lap_observed_s"] for r in fractions]
        fraction = median(ratios)
        uncertainty = hypot(target["d8_lap_std_s"] * fraction,
                            target["d8_lap_s"] * float(np.std(ratios, ddof=1)))
        result["R1"] = (target["d8_lap_s"] * fraction, fractions, uncertainty)
    return result


def diagnostics(rows):
    if not rows:
        return {"n": 0, "mae_s": None, "rmse_s": None, "bias_s": None, "absolute_error_p90_s": None,
                "clean_stints": 0, "coverage_50": None, "coverage_90": None, "interval_n": 0}
    residuals = [r["signed_residual_s"] for r in rows]
    intervals = [r for r in rows if r["half_width_90_s"] is not None]
    return {
        "n": len(rows), "mae_s": mean(map(abs, residuals)), "rmse_s": sqrt(mean(x*x for x in residuals)),
        "bias_s": mean(residuals), "absolute_error_p90_s": float(np.quantile(np.abs(residuals), .9)),
        "clean_stints": len({(r["driver_number"], r["stint_number"]) for r in rows}),
        "coverage_50": mean(abs(r["signed_residual_s"]) <= r["half_width_50_s"] for r in intervals) if intervals else None,
        "coverage_90": mean(abs(r["signed_residual_s"]) <= r["half_width_90_s"] for r in intervals) if intervals else None,
        "interval_n": len(intervals),
    }


def validate_and_match(rows, plan):
    config = plan["reference"]
    validation = split_drivers(rows, config)
    clean, calibration, evaluated, traffic, matches = defaultdict(list), defaultdict(list), [], [], []
    groups = defaultdict(list)
    for row in rows:
        if row["horizon_type"] in ("H1", "H2"):
            groups[row["session_key"], row["horizon_type"], row["checkpoint"]].append(row)
    for (key, horizon, checkpoint), group in sorted(groups.items()):
        for target in sorted(group, key=lambda r: (r["as_of_utc"], r["opportunity_id"])):
            outcome = target["outcome"]
            group_key = key, horizon, checkpoint
            if outcome["clean_control"] or outcome["following_label"]:
                history = earlier(target, clean[group_key])
                pool = [r for r in history if r["driver_number"] not in validation]
                predictions = predict(target, pool, history, config)
                for model, (estimate, sources, std) in sorted(predictions.items()):
                    residual = outcome["observed_s"] - estimate
                    previous = [r for r in calibration[group_key, model] if r["available_by_utc"] < target["as_of_utc"]
                                and r["opportunity_id"] != target["opportunity_id"]]
                    errors = [abs(r["signed_residual_s"]) for r in previous]
                    interval = ([float(np.quantile(errors, p, method="higher")) for p in (.5, .9)]
                                if len(errors) >= config["interval_minimum_calibration"] else [None, None])
                    prediction = {k: target[k] for k in ("opportunity_id", "session_key", "driver_number", "stint_number",
                                                         "horizon_type", "checkpoint", "as_of_utc", "tyre_age",
                                                         "race_progress", "compound", "track_temperature_c",
                                                         "physical_leader", "initial_gap_m",
                                                         "prior_clean_history_count", "pace_reference_status")}
                    prediction.update(model=model, observed_s=outcome["observed_s"], predicted_s=estimate,
                                      episode_id=outcome["episode_id"],
                                      relative_clean_pace_s=target["relative_clean_pace_s"],
                                      close_exposure_s=target["support"]["close_exposure_s"],
                                      signed_residual_s=residual, propagated_d8_std_s=std,
                                      half_width_50_s=interval[0], half_width_90_s=interval[1],
                                      available_by_utc=outcome["available_by_utc"], training_count=len(sources),
                                      training_ids=[r["opportunity_id"] for r in sources],
                                      latest_training_availability_utc=max(r["outcome"]["available_by_utc"] for r in sources),
                                      calibration_count=len(previous),
                                      cohort="HELD_OUT_DRIVER" if target["driver_number"] in validation else "DEVELOPMENT_PREFIX")
                    if outcome["clean_control"]:
                        if target["driver_number"] in validation:
                            evaluated.append(prediction)
                        else:
                            calibration[group_key, model].append(prediction)
                    else:
                        matched = [r for r in previous if tire_context(target, r, config)]
                        # One earlier traversal per control driver/stint. Use its latest residual.
                        distinct = {(r["driver_number"], r["stint_number"]): r
                                    for r in sorted(matched, key=lambda r: (r["available_by_utc"], r["opportunity_id"]))}
                        control_ids = [r["opportunity_id"] for r in distinct.values()]
                        traffic.append(prediction | {"matched_controls": len(control_ids), "control_ids": control_ids,
                                                     "validated_reference": False, "signed_proxy_s": None})
                        matches.append({"opportunity_id": target["opportunity_id"], "model": model,
                                        "session_key": key, "horizon_type": horizon, "checkpoint": checkpoint,
                                        "available_earlier_clean_residuals": len(previous), "matched_context": len(matched),
                                        "matched_distinct_stints": len(control_ids), "control_ids": control_ids,
                                        "control_residuals_s": [r["signed_residual_s"] for r in distinct.values()]})
            if outcome["clean_control"]:
                clean[group_key].append(target)
    references, best = {}, {}
    gate = plan["feasibility"]
    for key in sorted({r["session_key"] for r in rows}):
        for horizon in ("H1", "H2"):
            local = [r for r in evaluated if (r["session_key"], r["horizon_type"]) == (key, horizon)]
            per_id = Counter(r["opportunity_id"] for r in local)
            common = {identity for identity, count in per_id.items() if count == len(MODELS)}
            metrics = {}
            for model in MODELS:
                predictions = [r for r in local if r["model"] == model]
                stats = diagnostics(predictions)
                metrics[model] = stats | {
                    "common": diagnostics([r for r in predictions if r["opportunity_id"] in common]),
                    "bias_by_tyre_age": _slices(predictions, "tyre_age", 10),
                    "bias_by_progress": _slices(predictions, "race_progress", .25),
                    "bias_by_prior_clean_laps": _slices(predictions, "prior_clean_history_count", 3),
                    "bias_by_d8_support": _slices(predictions, "pace_reference_status"),
                    "independent_gate_pass": stats["n"] >= gate["minimum_clean_reference_predictions_per_race"]
                    and stats["clean_stints"] >= gate["minimum_independent_clean_stints_per_race"]
                    and stats["mae_s"] <= gate["reference_mae_max_s"]
                    and abs(stats["bias_s"]) <= gate["reference_absolute_bias_max_s"]
                    and stats["absolute_error_p90_s"] <= gate["reference_error_p90_max_s"],
                }
            references[f"{key}/{horizon}"] = metrics
            # Model selection is descriptive. Confirmatory use requires a fresh cohort.
            eligible = [model for model in MODELS if metrics[model]["independent_gate_pass"]]
            best[key, horizon] = eligible[0] if eligible else None
    match_index = {(r["opportunity_id"], r["model"]): r for r in matches}
    for row in traffic:
        row["validated_reference"] = row["model"] == best[row["session_key"], row["horizon_type"]]
        if row["validated_reference"] and row["matched_controls"] >= config["minimum_matched_controls"]:
            control = match_index[row["opportunity_id"], row["model"]]
            row["signed_proxy_s"] = row["signed_residual_s"] - median(control["control_residuals_s"])
    return evaluated, traffic, matches, references, sorted(validation)


def _slices(rows, feature, width=None):
    groups = defaultdict(list)
    for row in rows:
        value = row[feature]
        label = "missing" if value is None else str(round((value // width) * width, 3) if width else value)
        groups[label].append(row)
    return {label: {"n": len(group), "bias_s": mean(r["signed_residual_s"] for r in group)}
            for label, group in sorted(groups.items())}
