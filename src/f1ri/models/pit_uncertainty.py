"""Reproducible shared pit-parameter draws and sample-based distribution utilities."""
from __future__ import annotations

import numpy as np

from f1ri.models.contracts import PitSharedScenario


def sample_shared_pit_scenario(circuit: str, n_samples: int, random_seed: int) -> PitSharedScenario:
    rng = np.random.default_rng(random_seed)
    return PitSharedScenario(circuit, tuple(rng.normal(size=n_samples)), tuple(rng.normal(size=n_samples)))


def empirical_crps(samples: np.ndarray, observed: float) -> float:
    """Exact CRPS for an empirical predictive CDF in O(n log n)."""
    x = np.sort(np.asarray(samples, dtype=float))
    if not len(x) or not np.isfinite(x).all() or not np.isfinite(observed):
        raise ValueError("CRPS requires finite samples and observation")
    ranks = np.arange(1, len(x)+1)
    return float(np.mean(np.abs(x-observed))-np.sum((2*ranks-len(x)-1)*x)/len(x)**2)


def summarize_samples(samples: np.ndarray) -> dict[str, float]:
    return {"expected_loss_s": float(np.mean(samples)), "median_loss_s": float(np.median(samples)),
            "std_loss_s": float(np.std(samples)),
            **{f"p{q:02d}_loss_s": float(np.quantile(samples, q/100)) for q in (5, 25, 75, 95)}}
