"""Chronological calibration and H1 control audits for the fixed R4-B model."""
from collections import Counter
from statistics import median

import numpy as np

from f1ri.models.latent_sector_pace import stint_key
from f1ri.validation.traffic_segment_reference import diagnostics


def calibration_census(target, forecast, residuals, snapshots, evidence, folds, plan):
    cutoff = target["as_of_utc"]
    fold = folds[target["driver_number"]]
    target_training = set(snapshots[forecast["model_id"]]["training_ids"]) if forecast else set()
    eligible, ledger = [], []
    for row in residuals:
        identity = row["opportunity_id"]
        source = evidence[identity]
        trained = snapshots[row["model_id"]]["training_ids"]
        reason = (
            "UNAPPROVED_RACE_SOURCE" if row["session_key"] != target["session_key"] else
            "TIMING_UNRESOLVED" if not source["timing_pass"] else
            "INSUFFICIENT_CLEAN_EVIDENCE" if source["tier"] != "A" else
            "SAME_STINT_DEPENDENCE" if stint_key(source) == stint_key(target) else
            "DIFFERENT_CALIBRATION_FOLD" if row["test_fold"] != fold else
            "FUTURE_RESIDUAL_UNAVAILABLE" if row["available_by_utc"] >= cutoff else
            "TRAIN_CALIBRATION_OVERLAP" if identity in target_training or identity in trained
            or row["latest_training_availability_utc"] >= row["as_of_utc"] else None
        )
        if reason is None:
            eligible.append(row)
        ledger.append({"target_id": target["opportunity_id"], "residual_id": identity, "target_fold": fold,
                       "residual_test_fold": row["test_fold"], "source_tier": source["tier"],
                       "available_by_utc": row["available_by_utc"], "target_cutoff_utc": cutoff,
                       "model_id": row["model_id"], "fitted_prefix_overlap": len(target_training.intersection(trained)),
                       "out_of_sample": identity not in trained and row["latest_training_availability_utc"] < row["as_of_utc"],
                       "rejection": reason})
    ordered = sorted(eligible, key=lambda r: (r["available_by_utc"], r["opportunity_id"]))
    distinct = {stint_key(row): row for row in ordered}
    selected_ids = {r["opportunity_id"] for r in distinct.values()}
    for row in ledger:
        if row["rejection"] is None and row["residual_id"] not in selected_ids:
            row["rejection"] = "SAME_STINT_DEPENDENCE"
    counts = Counter(row["rejection"] or "ELIGIBLE" for row in ledger)
    intervals = {}
    if forecast and len(distinct) >= plan["minimum_independent_stints"]:
        errors = [abs(r["signed_residual_s"]) for r in distinct.values()]
        standardized = [abs(r["signed_residual_s"]) / r["raw_scale_s"] for r in distinct.values()]
        # U2 preserves one score per stint and uses that stint's worst
        # observed standardized residual. This is descriptive calibration.
        cluster = {}
        for row in ordered:
            key = stint_key(row)
            score = abs(row["signed_residual_s"]) / row["raw_scale_s"]
            cluster[key] = max(cluster.get(key, 0.), score)
        for method, values, scale in (("U0", errors, 1.), ("U1", standardized, forecast["raw_scale_s"]),
                                      ("U2", list(cluster.values()), forecast["raw_scale_s"])):
            intervals[method] = [float(np.quantile(values, p, method="higher")) * scale for p in (.5, .9)]
    return ledger, {"opportunity_id": target["opportunity_id"], "session_key": target["session_key"],
                    "as_of_utc": cutoff, "race_progress": target["race_progress"], "following": target["following_label"],
                    "independent_prior_stints": len(distinct), "prior_residuals": len(eligible),
                    "reasons": dict(counts), "intervals": intervals,
                    "status": ("OUT_OF_SUPPORT_CONTEXT" if forecast is None else "AVAILABLE" if intervals else
                               "INSUFFICIENT_PRIOR_CALIBRATION_STINTS")}


def control_census(target, population, residuals, config, test_fold, folds):
    residual_ids = {r["opportunity_id"]: r for r in residuals if r["test_fold"] == test_fold
                    and r["available_by_utc"] < target["as_of_utc"]
                    and folds[r["driver_number"]] == (test_fold + 1) % config["folds"]}
    tol = config
    filters = (
        ("approved_race", lambda r: r["session_key"] == target["session_key"]),
        ("distinct_stint", lambda r: r["opportunity_id"] != target["opportunity_id"] and r["stint_number"] is not None
         and stint_key(r) != stint_key(target)),
        ("chronologically_earlier", lambda r: r["target_end_utc"] is not None and r["target_end_utc"] < target["as_of_utc"]),
        ("evidence_before_cutoff", lambda r: r["available_by_utc"] is not None and r["available_by_utc"] < target["as_of_utc"]),
        ("same_sector", lambda r: r["checkpoint"] == target["checkpoint"]),
        ("compound", lambda r: target["compound"] is not None and r["compound"] == target["compound"]),
        ("tyre_age", lambda r: r["tyre_age"] is not None and target["tyre_age"] is not None
         and abs(r["tyre_age"] - target["tyre_age"]) <= tol["maximum_tyre_age_delta"]),
        ("race_progress", lambda r: abs(r["race_progress"] - target["race_progress"]) <= tol["maximum_progress_delta"]),
        ("environment", lambda r: r["track_temperature_c"] is None or target["track_temperature_c"] is None
         or abs(r["track_temperature_c"] - target["track_temperature_c"]) <= tol["maximum_track_temperature_delta_c"]),
        ("clean_evidence", lambda r: r["tier"] == "A"),
        ("independent_reference", lambda r: r["opportunity_id"] in residual_ids),
        ("valid_residual", lambda r: np.isfinite(residual_ids[r["opportunity_id"]]["signed_residual_s"])),
    )
    candidates = list(population)
    counts, recoverable, audit = {"population": len(candidates)}, {}, []
    failures = {}
    for label, accept in filters:
        survivors = []
        for row in candidates:
            if accept(row):
                survivors.append(row)
            else:
                failures[row["opportunity_id"]] = label
        candidates = survivors
        counts[label] = len(candidates)
        if label == "environment":
            # Tier B could qualify with new physical confirmation. A candidate
            # in a fitting/test fold cannot become an independent residual.
            recoverable = {stint_key(r): r for r in sorted(candidates, key=lambda r: (
                r["tier"] == "A", r["available_by_utc"], r["opportunity_id"],
            )) if r["tier"] in ("A", "B")
                and folds.get(r["driver_number"]) == (test_fold + 1) % tol["folds"]}
    matched = {stint_key(r): r for r in sorted(candidates, key=lambda r: (r["available_by_utc"], r["opportunity_id"]))}
    counts["independent_matched_stints"] = len(matched)
    ids = {r["opportunity_id"] for r in matched.values()}
    for row in population:
        identity = row["opportunity_id"]
        reason = failures.get(identity) or (None if identity in ids else "repeated_control_stint")
        audit.append({"target_id": target["opportunity_id"], "control_id": identity, "first_exclusion": reason,
                      "source_tier": row["tier"], "control_fold": folds[row["driver_number"]],
                      "calibration_fold_required": (test_fold + 1) % tol["folds"],
                      "control_available_by_utc": row["available_by_utc"]})
    minimum = tol["minimum_matched_controls"]
    return {"opportunity_id": target["opportunity_id"], "session_key": target["session_key"], "counts": counts,
            "matched_control_ids": sorted(ids), "recoverable_stint_upper_bound": len(recoverable),
            "potential_additional_stints": len(recoverable.keys() - matched.keys()),
            "recoverable_control_seed_ids": sorted(r["opportunity_id"] for r in recoverable.values()),
            "first_decisive_failure": next((name for name, n in counts.items() if n < minimum),
                                          "independent_stint_minimum" if len(matched) < minimum else None)}, audit


def interval_metrics(rows, effect):
    if not rows:
        return diagnostics([]) | {"median_90_half_width_s": None, "fraction_half_width_below_effect": None}
    return diagnostics(rows) | {"median_90_half_width_s": median(r["half_width_90_s"] for r in rows),
                                "fraction_half_width_below_effect": np.mean([r["half_width_90_s"] < effect for r in rows]).item()}
