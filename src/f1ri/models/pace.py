"""Deliberately simple baseline; NOT a validated race-pace forecast."""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np
import pandas as pd


@dataclass
class MedianPaceBaseline:
    """A sanity-check baseline fitted on past observations only.

    Full pace modeling must account for fuel, traffic, weather and track status.
    """
    group_medians_: dict[tuple[int, str], float] | None = None
    global_median_: float | None = None

    def fit(self, past: pd.DataFrame) -> "MedianPaceBaseline":
        required = {"driver_number", "compound", "observed_lap_time_s"}
        if not required.issubset(past.columns):
            raise ValueError(f"Missing features: {required - set(past.columns)}")
        if past.empty:
            raise ValueError("No training data")
        d = past.dropna(subset=["driver_number", "compound", "observed_lap_time_s"])
        if d.empty:
            raise ValueError("No valid lap times")
        self.global_median_ = float(d["observed_lap_time_s"].median())
        grouped = d.groupby(["driver_number", "compound"])["observed_lap_time_s"]
        self.group_medians_ = {(int(a), str(b)): float(v)
                               for (a, b), v in grouped.median().items()}
        return self

    def predict_seconds(self, driver_number: int, compound: str) -> float:
        if self.group_medians_ is None or self.global_median_ is None:
            raise RuntimeError("Fit this baseline first")
        return self.group_medians_.get((driver_number, compound), self.global_median_)
