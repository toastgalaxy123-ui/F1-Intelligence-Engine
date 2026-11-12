"""Partially pooled empirical stationary-service mixtures with explicit missingness."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class StationaryServiceModel:
    team_shrinkage: float = 20.0
    slow_stop_threshold_s: float = 4.5
    global_values_: np.ndarray = field(default_factory=lambda: np.array([], dtype=float))
    team_values_: dict[str, np.ndarray] = field(default_factory=dict)
    allow_tail: bool = True

    def fit(self, frame: pd.DataFrame) -> StationaryServiceModel:
        self.global_values_ = np.array([], dtype=float)
        self.team_values_ = {}
        if frame.empty:
            return self
        if "eligible_direct_stationary" in frame:
            frame = frame[frame.eligible_direct_stationary.fillna(False)]
        # Do not impute or turn unavailable stationary measurements into zeros.
        values = pd.to_numeric(frame.stop_duration_s, errors="coerce")
        lanes = pd.to_numeric(frame.lane_duration_s, errors="coerce")
        selected = frame[(values > 0) & (values <= lanes)].copy()
        self.global_values_ = selected.stop_duration_s.to_numpy(float)
        for team, group in selected.groupby("team_id"):
            self.team_values_[str(team)] = group.stop_duration_s.to_numpy(float)
        return self

    @property
    def available(self) -> bool:
        return bool(len(self.global_values_))

    def empirical_values(self, team: str) -> tuple[np.ndarray, np.ndarray]:
        global_values = self.global_values_
        team_values = self.team_values_.get(team, np.array([], dtype=float))
        if not self.allow_tail:
            global_values = global_values[global_values <= self.slow_stop_threshold_s]
            team_values = team_values[team_values <= self.slow_stop_threshold_s]
        return global_values, team_values

    def expected(self, team: str) -> float | None:
        g, t = self.empirical_values(team)
        if not len(g):
            return None
        weight = len(t)/(len(t)+self.team_shrinkage)
        return float((1-weight)*np.mean(g) + weight*np.mean(t)) if len(t) else float(np.mean(g))

    def delay_probability(self, team: str) -> float | None:
        g, t = self.empirical_values(team)
        if not len(g):
            return None
        global_p = float(np.mean(g > self.slow_stop_threshold_s))
        return float((np.sum(t > self.slow_stop_threshold_s)+self.team_shrinkage*global_p)
                     / (len(t)+self.team_shrinkage))

    def sample(self, team: str, n: int, rng: np.random.Generator) -> np.ndarray | None:
        g, t = self.empirical_values(team)
        if not len(g):
            return None
        # This is an empirical ordinary/delayed mixture, retaining observed tail
        # severity without declaring every large duration a mechanical incident.
        samples = rng.choice(g, size=n).astype(float)
        if len(t):
            use_team = rng.random(n) < len(t)/(len(t)+self.team_shrinkage)
            samples[use_team] = rng.choice(t, size=int(use_team.sum()))
        return samples
