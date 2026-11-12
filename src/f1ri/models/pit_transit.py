"""Robust partially pooled circuit/era locations and identifiable lane components."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class PooledCircuitModel:
    shrinkage: float = 12.0
    global_location_: float = 0.0
    locations_: dict[str, float] = field(default_factory=dict)
    era_offsets_: dict[int, float] = field(default_factory=dict)
    counts_: dict[str, int] = field(default_factory=dict)
    race_counts_: dict[str, int] = field(default_factory=dict)
    residuals_: np.ndarray = field(default_factory=lambda: np.array([], dtype=float))
    residual_std_: float = 1.0

    def fit(self, frame: pd.DataFrame, target: str, *, weighted: bool = False) -> PooledCircuitModel:
        self.locations_, self.era_offsets_, self.counts_, self.race_counts_ = {}, {}, {}, {}
        selected = frame[pd.to_numeric(frame[target], errors="coerce").notna()].copy()
        if selected.empty:
            raise ValueError(f"No observed values for {target}")
        y = selected[target].to_numpy(float)
        self.global_location_ = float(np.median(y))
        for circuit, group in selected.groupby("circuit_short_name"):
            n = len(group)
            values = group[target].to_numpy(float)
            location = float(np.median(values))
            if weighted and "label_std_s" in group:
                uncertainties = group.label_std_s.fillna(3).to_numpy(float)
                weights = 1/np.maximum(uncertainties**2, .25)
                # Weighted median is robust to service tails, with label quality
                # controlling influence rather than erasing measurement error.
                order = np.argsort(values)
                location = float(values[order][np.searchsorted(np.cumsum(weights[order]), weights.sum()/2)])
            w = n/(n+self.shrinkage)
            self.locations_[str(circuit)] = w*location+(1-w)*self.global_location_
            self.counts_[str(circuit)] = n
            self.race_counts_[str(circuit)] = int(group.session_key.nunique())
        residual = y-np.array([self.locations_[str(c)] for c in selected.circuit_short_name])
        # An era term is learned only after circuit centering, with strong pooling.
        for era in (0, 1):
            mask = (selected.season.to_numpy(int) >= 2026) == bool(era)
            if mask.any():
                n = int(selected.loc[mask, "session_key"].nunique())
                self.era_offsets_[era] = n/(n+5)*float(np.median(residual[mask]))
        residual -= np.array([self.era_offsets_.get(int(s >= 2026), 0) for s in selected.season])
        self.residuals_ = residual
        self.residual_std_ = max(float(1.4826*np.median(np.abs(residual-np.median(residual)))), .3)
        return self

    def predict(self, circuit: str, season: int) -> float:
        return self.locations_.get(circuit, self.global_location_) + self.era_offsets_.get(int(season >= 2026), 0)

    def parameter_std(self, circuit: str) -> float:
        races = self.race_counts_.get(circuit, 0)
        return self.residual_std_/np.sqrt(max(races, 1)) if races else max(self.residual_std_, 5)
