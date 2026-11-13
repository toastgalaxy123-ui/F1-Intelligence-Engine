"""Run chronological and held-out driver tests of clean-sector references."""
from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
from statistics import median

import numpy as np

from f1ri.models.latent_sector_pace import (
    fit_sector,
    interval_from_prefix,
    sector_prediction,
    stint_key,
)
from f1ri.validation.traffic_segment_reference import (
    MODELS,
    diagnostics,
    predict,
    tire_context,
)

R4 = ("R4-A", "R4-B", "R4-B-TIER-B")
ALL_MODELS = MODELS + R4


def assign_folds(drivers, seed, count):
    ordered = sorted(set(drivers), key=lambda d: sha256(f"{seed}/{d}".encode()).hexdigest())
    return {driver: i % count for i, driver in enumerate(ordered)}


def _legacy_row(row):
    return row | {"outcome": {"available_by_utc": row["available_by_utc"], "observed_s": row["sector_elapsed_s"],
                              "full_lap_observed_s": row["lap_elapsed_s"], "stage1_clean_lap": row["stage1_clean"]}}


def _prediction_row(row, model, estimate, sources, count, scale=None, **extra):
    fields = ("opportunity_id", "session_key", "driver_number", "stint_number", "checkpoint", "as_of_utc",
              "available_by_utc", "compound", "tyre_age", "race_progress", "track_temperature_c", "tier")
    return {k: row[k] for k in fields} | {
        "model": model, "predicted_s": estimate, "observed_s": row["sector_elapsed_s"],
        "signed_residual_s": row["sector_elapsed_s"] - estimate,
        "training_count": len(sources), "latest_training_availability_utc": max(r["available_by_utc"] for r in sources),
        "prior_clean_sectors": count, "raw_scale_s": scale, "half_width_50_s": None, "half_width_90_s": None,
    } | extra


def replay_sector_references(rows, config, baseline, *, sensitivity_weights=()):
    if len({r["session_key"] for r in rows}) != 1:
        raise ValueError("Sector replay requires a single permitted race/layout")
    folds = assign_folds((r["driver_number"] for r in rows), config["seed"], config["folds"])
    rows = sorted((r for r in rows if r["as_of_utc"] is not None),
                  key=lambda r: (r["as_of_utc"], r["opportunity_id"]))
    clean = [r for r in rows if r["tier"] in ("A", "B")]
    by_sector = {sector: [r for r in clean if r["checkpoint"] == sector] for sector in range(3)}
    evaluated, availability, traffic, models, calibration, model_cache, following_controls = (
        [], [], [], {}, defaultdict(list), {}, defaultdict(list),
    )
    for target in rows:
        if target["tier"] != "A" and not target["following_label"]:
            continue
        cutoff, driver, sector = target["as_of_utc"], target["driver_number"], target["checkpoint"]
        history = [r for r in by_sector[sector] if r["available_by_utc"] < cutoff
                   and r["opportunity_id"] != target["opportunity_id"]]
        same_stint = [r for r in history if r["tier"] == "A" and stint_key(r) == stint_key(target)]
        fold = folds[driver]
        for evaluation in ("A", "B"):
            # Each test fold has a disjoint calibration fold. Both are excluded
            # from the fitted global model. Rotating uses all drivers once for test.
            if target["following_label"] and evaluation != "B":
                continue
            if evaluation == "A":
                excluded, cal_fold, role = {driver}, None, "TEST"
                passes = [(fold, excluded, cal_fold, role)]
            else:
                test_fold_for_calibration = (fold - 1) % config["folds"]
                passes = [(fold, {d for d in folds if folds[d] in (fold, (fold + 1) % config["folds"])},
                           (fold + 1) % config["folds"], "TEST"),
                          (test_fold_for_calibration, {d for d in folds if folds[d] in (fold, test_fold_for_calibration)},
                           fold, "CALIBRATION")]
            for test_fold, excluded, cal_fold, role in passes:
                if target["following_label"] and role != "TEST":
                    continue
                pool = [r for r in history if r["driver_number"] not in excluded]
                a_pool = [r for r in pool if r["tier"] == "A"]
                count = len(same_stint)
                predictions = []
                status = {}
                if role == "TEST" and not target["following_label"]:
                    for model, (estimate, sources, scale) in predict(
                        _legacy_row(target), [_legacy_row(r) for r in a_pool],
                        [_legacy_row(r) for r in same_stint], baseline,
                    ).items():
                        predictions.append(_prediction_row(target, model, estimate, sources, count, scale))
                policies = [("A", 0.), ("B", config["model"]["tier_b_weight"])]
                policies.extend((f"B-{weight:.2f}", weight) for weight in sensitivity_weights)
                for label, weight in policies:
                    fit_pool = a_pool if label == "A" else pool
                    signature = (label, tuple(r["opportunity_id"] for r in fit_pool))
                    if signature not in model_cache:
                        fit = fit_sector(fit_pool, config["model"], weight)
                        model_id = sha256(repr(signature).encode()).hexdigest() if fit else None
                        model_cache[signature] = model_id
                        if fit:
                            models[model_id] = fit | {"tier_policy": label, "session_key": target["session_key"],
                                                      "checkpoint": sector, "tier_b_weight": weight}
                    model_id = model_cache[signature]
                    fit = models.get(model_id)
                    latent, reason = sector_prediction(fit, target, same_stint, config["model"])
                    candidates = ("R4-A", "R4-B") if label == "A" else (
                        "R4-B-TIER-B" if label == "B" else f"R4-B-TIER-{label}",
                    )
                    status.update(dict.fromkeys(candidates, reason))
                    if not latent:
                        continue
                    selected = set(fit["training_ids"])
                    sources = [r for r in fit_pool if r["opportunity_id"] in selected]
                    for model in candidates:
                        static = model == "R4-A"
                        predictions.append(_prediction_row(
                            target, model, latent["static_s" if static else "adapted_s"], sources, count,
                            latent["static_scale_s" if static else "adapted_scale_s"], model_id=model_id,
                            adjustment_s=0. if static else latent["adaptation_s"],
                            adaptation_ids=[] if static else latent["prior_ids"],
                        ))
                for row in predictions:
                    model = row["model"]
                    row.update(evaluation=evaluation, test_fold=test_fold, calibration_fold=cal_fold, role=role)
                    key = (test_fold, model)
                    if role == "CALIBRATION":
                        calibration[key].append(row)
                        continue
                    if evaluation == "B" and model.startswith("R4-"):
                        previous = calibration[key]
                        widths, n = interval_from_prefix(previous, cutoff, row["raw_scale_s"],
                                                        config["calibration"]["minimum_independent_stints"])
                        row.update(half_width_50_s=widths[0], half_width_90_s=widths[1], calibration_stints=n)
                        row["calibration_status"] = "CALIBRATED_PREFIX" if widths[1] is not None else "INSUFFICIENT_CALIBRATION_STINTS"
                    if target["following_label"]:
                        earlier_controls = [r for r in calibration[key] if r["checkpoint"] == sector
                                            and r["available_by_utc"] < cutoff and tire_context(target, r, baseline)]
                        unique = {stint_key(r): r for r in sorted(
                            earlier_controls, key=lambda r: (r["available_by_utc"], r["opportunity_id"]))}
                        following_controls[target["opportunity_id"], model] = list(unique.values())
                        traffic.append(row)
                    else:
                        evaluated.append(row)
                if role == "TEST":
                    availability.append({
                        "opportunity_id": target["opportunity_id"], "session_key": target["session_key"],
                        "driver_number": driver, "stint_number": target["stint_number"], "checkpoint": sector,
                        "evaluation": evaluation, "test_fold": test_fold, "as_of_utc": cutoff,
                        "prior_clean_sectors": count, "available_tier_a": len(a_pool),
                        "available_tier_b": len(pool) - len(a_pool), "available_training_stints": len({stint_key(r) for r in a_pool}),
                        "following": target["following_label"], "model_status": status,
                    })
    return evaluated, availability, traffic, models, folds, following_controls, [
        r for group in calibration.values() for r in group
    ]


def _cluster_interval(per_stint):
    if len(per_stint) < 2:
        return None
    values = np.array([np.mean(errors) for errors in per_stint.values()])
    rng = np.random.default_rng(530303)
    means = rng.choice(values, size=(1000, len(values)), replace=True).mean(axis=1)
    return np.quantile(means, [.025, .975]).tolist()


def summarize(predictions, availability, gate):
    result = {}
    for evaluation in ("A", "B"):
        local = [r for r in predictions if r["evaluation"] == evaluation]
        denominators = [r for r in availability if r["evaluation"] == evaluation and not r["following"]]
        models = MODELS + ("R4-A", "R4-B")
        common = {key for key, n in Counter(r["opportunity_id"] for r in local if r["model"] in models).items()
                  if n == len(models)}
        result[evaluation] = {}
        models_present = ALL_MODELS + tuple(sorted({r["model"] for r in local} - set(ALL_MODELS)))
        for model in models_present:
            rows = [r for r in local if r["model"] == model]
            stats = diagnostics(rows)
            intervals = [r for r in rows if r["half_width_90_s"] is not None]
            errors = [r["signed_residual_s"] for r in rows]
            per_stint = defaultdict(list)
            for row in rows:
                per_stint[stint_key(row)].append(abs(row["signed_residual_s"]))
            support = len(rows) / len(denominators) if denominators else 0.
            passed = (stats["clean_stints"] >= gate["minimum_independent_stints"]
                      and len(rows) >= gate["minimum_predictions"] and support >= gate["minimum_operational_support"]
                      and stats["mae_s"] <= gate["maximum_mae_s"] and abs(stats["bias_s"]) <= gate["maximum_absolute_bias_s"]
                      and stats["absolute_error_p90_s"] <= gate["maximum_p90_error_s"])
            result[evaluation][model] = stats | {
                "support_fraction": support, "point_gate_pass": passed, "race_count": int(bool(rows)),
                "common": diagnostics([r for r in rows if r["opportunity_id"] in common]),
                "interval_stints": len({stint_key(r) for r in intervals}),
                "median_half_width_90_s": median(r["half_width_90_s"] for r in intervals) if intervals else None,
                "median_half_width_50_s": median(r["half_width_50_s"] for r in intervals) if intervals else None,
                "width_smaller_than_planning_effect_fraction": sum(r["half_width_90_s"] < gate["planning_effect_s"]
                                                                 for r in intervals) / len(intervals) if intervals else None,
                "median_absolute_error_s": median(map(abs, errors)) if errors else None,
                "residual_std_s": float(np.std(errors, ddof=1)) if len(errors) > 1 else None,
                "stint_weighted_mae_s": float(np.mean([np.mean(x) for x in per_stint.values()])) if per_stint else None,
                "stint_bootstrap_mae_95": _cluster_interval(per_stint),
                "calibration_by_tyre_age": {
                    name: diagnostics([r for r in rows if r["tyre_age"] is not None and lo <= r["tyre_age"] < hi])
                    for name, lo, hi in (("0–9", 0, 10), ("10–19", 10, 20), ("20+", 20, 1000))
                },
                "history": {str(i): diagnostics([r for r in rows if min(3, r["prior_clean_sectors"]) == i]) | {
                    "eligible": sum(min(3, r["prior_clean_sectors"]) == i for r in denominators)}
                    for i in range(4)},
            }
        paired = {r["opportunity_id"] for r in local if r["model"] == "R4-B"} & {
            r["opportunity_id"] for r in local if r["model"] == "R4-B-TIER-B"}
        for model in ("R4-B", "R4-B-TIER-B"):
            result[evaluation][model]["tier_sensitivity_common"] = diagnostics(
                [r for r in local if r["model"] == model and r["opportunity_id"] in paired])
    return result
