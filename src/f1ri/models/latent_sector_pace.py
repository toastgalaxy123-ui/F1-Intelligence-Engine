"""Research-only regularized sector pace and point-in-time stint adaptation."""
from __future__ import annotations

from math import sqrt
from statistics import mean

import numpy as np

COMPOUNDS = ("SOFT", "MEDIUM", "HARD")


def context(row):
    return (row["tyre_age"], row["race_progress"], row["track_temperature_c"])


def valid_context(row):
    return row["compound"] in COMPOUNDS and row["stint_number"] is not None and all(
        value is not None and np.isfinite(value) for value in context(row))


def stint_key(row):
    return row["session_key"], row["driver_number"], row["stint_number"]


def _design(row, drivers):
    age, progress, temperature = context(row)
    return np.array([1., (age - 15.) / 10., (progress - .5) * 4., (temperature - 35.) / 10.,
                     *(row["compound"] == c for c in COMPOUNDS[:2]),
                     *(row["driver_number"] == d for d in drivers)], dtype=float)


def fit_sector(pool, config, tier_b_weight=0.):
    """Ridge penalties implement partial pooling with zero-mean driver priors."""
    weights = {"A": 1., "B": tier_b_weight}
    weighted = [(r, weights[r["tier"]]) for r in pool if weights.get(r["tier"], 0.) > 0 and valid_context(r)]
    rows, w = zip(*weighted, strict=True) if weighted else ((), ())
    if len(rows) < config["minimum_fit_rows"] or len({stint_key(r) for r in rows}) < config["minimum_fit_stints"]:
        return None
    drivers = sorted({r["driver_number"] for r in rows})
    x = np.array([_design(r, drivers) for r in rows])
    y = np.array([r["sector_elapsed_s"] for r in rows])
    w = np.array(w)
    penalty = np.diag([0.] + [config["context_penalty"]] * 5 + [config["driver_penalty"]] * len(drivers))
    inverse = np.linalg.inv(x.T @ (w[:, None] * x) + penalty)
    beta = inverse @ x.T @ (w * y)
    residuals = y - x @ beta
    # Equal-stint aggregation prevents a long stint from dominating the noise estimate.
    by_stint = {}
    for row, residual in zip(rows, residuals, strict=True):
        by_stint.setdefault(stint_key(row), []).append(float(residual))
    noise = max(config["minimum_noise_s"], sqrt(mean(mean(x*x for x in values) for values in by_stint.values())))
    supported = {}
    for compound in COMPOUNDS:
        values = [context(r) for r in rows if r["compound"] == compound]
        if values:
            supported[compound] = [(min(column), max(column)) for column in zip(*values, strict=True)]
    return {"drivers": drivers, "beta": beta.tolist(), "inverse": inverse.tolist(), "noise_s": noise,
            "context_ranges": supported, "training_ids": [r["opportunity_id"] for r in rows],
            "training_stints": len(by_stint), "latest_training_availability_utc": max(r["available_by_utc"] for r in rows)}


def sector_prediction(model, target, history, config):
    if model is None:
        return None, "INSUFFICIENT_TRAINING_PREFIX"
    if model["latest_training_availability_utc"] >= target["as_of_utc"] or target["opportunity_id"] in model["training_ids"]:
        raise ValueError("Sector prediction requires a strictly earlier training prefix")
    if not valid_context(target):
        return None, "CUTOFF_CONTEXT_UNAVAILABLE"
    ranges = model["context_ranges"].get(target["compound"])
    tolerances = (config["maximum_age_extrapolation"], config["maximum_progress_extrapolation"],
                  config["maximum_temperature_extrapolation_c"])
    if ranges is None or any(not lo - tol <= x <= hi + tol for (lo, hi), x, tol in zip(
        ranges, context(target), tolerances, strict=True
    )):
        return None, "CONTEXT_OUTSIDE_TRAINING_SUPPORT"
    beta = np.array(model["beta"])
    design = _design(target, model["drivers"])
    global_estimate = float(design @ beta)
    training = set(model["training_ids"])
    clean = [r for r in history if r["tier"] == "A" and r["checkpoint"] == target["checkpoint"]
             and stint_key(r) == stint_key(target) and valid_context(r)
             and r["compound"] == target["compound"] and r["opportunity_id"] not in training
             and r["opportunity_id"] != target["opportunity_id"] and r["available_by_utc"] < target["as_of_utc"]]
    n = len(clean)
    prior = config["adaptation_prior_count"]
    adjustment = sum(r["sector_elapsed_s"] - float(_design(r, model["drivers"]) @ beta) for r in clean) / (n + prior)
    unknown_driver = 1 / config["driver_penalty"] if target["driver_number"] not in model["drivers"] else 0.
    base_variance = 1 + unknown_driver + float(design @ np.array(model["inverse"]) @ design)
    scale = lambda count: model["noise_s"] * sqrt(base_variance + 1 / (count + prior))
    return {"static_s": global_estimate, "adapted_s": global_estimate + adjustment,
            "adaptation_s": adjustment, "prior_count": n,
            "prior_ids": [r["opportunity_id"] for r in clean],
            "static_scale_s": scale(0), "adapted_scale_s": scale(n)}, "SUPPORTED"


def interval_from_prefix(rows, cutoff, scale, minimum):
    # One latest eligible score per independent calibration stint.
    ordered = sorted((r for r in rows if r["available_by_utc"] < cutoff),
                     key=lambda r: (r["available_by_utc"], r["opportunity_id"]))
    independent = {stint_key(r): r for r in ordered}
    scores = [abs(r["signed_residual_s"]) / r["raw_scale_s"] for r in independent.values()]
    widths = [float(np.quantile(scores, p, method="higher")) * scale for p in (.5, .9)] if len(scores) >= minimum else [None, None]
    return widths, len(scores)
