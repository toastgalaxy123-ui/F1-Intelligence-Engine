"""Probabilistic calibration metrics: interval coverage, sharpness, and Gaussian CRPS."""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import norm


def gaussian_crps(mu: np.ndarray, sigma: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Analytical Continuous Ranked Probability Score (CRPS) for N(mu, sigma^2)."""
    s = np.maximum(np.asarray(sigma, dtype=float), 1e-6)
    z = (np.asarray(y, dtype=float) - np.asarray(mu, dtype=float)) / s
    return s * (z * (2.0 * norm.cdf(z) - 1.0) + 2.0 * norm.pdf(z) - 1.0 / math.sqrt(math.pi))


def compute_probabilistic_metrics(df: pd.DataFrame) -> dict[str, float | int | None]:
    """Compute 50%/90% interval coverage, sharpness (mean width), and CRPS on eligible rows."""
    eligible = df[
        df["target_is_clean_green"].fillna(False).astype(bool)
        & df["observed_target_lap_time_s"].notna()
        & df["expected_seconds"].notna()
    ]
    n = len(eligible)
    if n == 0:
        return {
            "n_eligible": 0,
            "coverage_50": None,
            "coverage_90": None,
            "mean_width_50_s": None,
            "mean_width_90_s": None,
            "crps_s": None,
        }

    y = eligible["observed_target_lap_time_s"].to_numpy(dtype=float)
    mu = eligible["expected_seconds"].to_numpy(dtype=float)
    std = eligible["std_seconds"].to_numpy(dtype=float)
    p25 = eligible["p25_seconds"].to_numpy(dtype=float)
    p75 = eligible["p75_seconds"].to_numpy(dtype=float)
    p05 = eligible["p05_seconds"].to_numpy(dtype=float)
    p95 = eligible["p95_seconds"].to_numpy(dtype=float)

    in_50 = (y >= p25) & (y <= p75)
    in_90 = (y >= p05) & (y <= p95)
    crps_vals = gaussian_crps(mu, std, y)

    return {
        "n_eligible": int(n),
        "coverage_50": round(float(np.mean(in_50)), 4),
        "coverage_90": round(float(np.mean(in_90)), 4),
        "mean_width_50_s": round(float(np.mean(p75 - p25)), 4),
        "mean_width_90_s": round(float(np.mean(p95 - p05)), 4),
        "crps_s": round(float(np.mean(crps_vals)), 4),
    }


def conditional_calibration_breakdown(
    df: pd.DataFrame, group_col: str
) -> list[dict[str, Any]]:
    eligible = df[
        df["target_is_clean_green"].fillna(False).astype(bool)
        & df["observed_target_lap_time_s"].notna()
        & df["expected_seconds"].notna()
    ]
    rows: list[dict[str, Any]] = []
    for key, grp in eligible.groupby(group_col, dropna=False):
        m = compute_probabilistic_metrics(grp)
        m[group_col] = "MISSING" if pd.isna(key) else str(key)
        rows.append(m)
    return rows
