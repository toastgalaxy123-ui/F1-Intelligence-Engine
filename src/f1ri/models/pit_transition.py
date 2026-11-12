"""Regularized residual tire-transition effects; no separate warm-up penalty."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class PitTransitionModel:
    shrinkage: float = 25.0
    effects_: dict[str, float] = field(default_factory=dict)
    identified_: bool = False

    def fit(self, frame: pd.DataFrame, residuals: np.ndarray) -> PitTransitionModel:
        self.effects_, self.identified_ = {}, False
        if len(frame) < 10 or "new_compound" not in frame:
            return self
        for compound, indices in frame.reset_index(drop=True).groupby("new_compound").groups.items():
            vals = residuals[np.array(list(indices), dtype=int)]
            self.effects_[str(compound)] = float(len(vals)/(len(vals)+self.shrinkage)*np.median(vals))
        self.identified_ = True
        return self

    def predict(self, compound: str) -> float:
        return self.effects_.get(compound, 0.0)
