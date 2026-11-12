"""Common tire-degradation model interfaces and additive decomposition contracts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import pandas as pd


@dataclass(frozen=True)
class DegradationEvaluation:
    """Evaluated tire contribution C_{r,c} + D_{r,c}(a) and uncertainty at tire age a."""

    compound_offset_s: float
    age_degradation_s: float
    total_tire_effect_s: float
    local_slope_s_per_lap: float
    slope_std_s_per_lap: float
    support_status: str
    extrapolation_warning: str | None = None


@dataclass(frozen=True)
class PaceDecomposition:
    """Explicit 3-component clean-air lap time decomposition (Section 7 & Section 9.1).

    T_{d,r,l} = base_pace_s (Component A: driver/car pace)
              + race_progression_s (Component B: fuel/race progression + track temp)
              + tire_contribution_s (Component C: compound offset + age degradation + stint state)
    """

    base_pace_s: float
    race_progression_s: float
    tire_contribution_s: float
    compound_offset_s: float
    age_degradation_s: float
    stint_state_adjustment_s: float
    temp_adjustment_s: float

    @property
    def total_expected_lap_time_s(self) -> float:
        return float(self.base_pace_s + self.race_progression_s + self.tire_contribution_s)


class TireDegradationModel(Protocol):
    """Protocol for pluggable tire-degradation estimators (D0 through D7)."""

    model_version: str
    trained_through_utc_: datetime

    def fit(self, train_df: pd.DataFrame) -> TireDegradationModel: ...

    def evaluate_tire(
        self,
        compound_relative: str,
        tyre_age: float,
        *,
        compound_absolute: str | None = None,
        circuit_short_name: str | None = None,
        regulation_era: str = "2023_2025",
        session_key: int | None = None,
    ) -> DegradationEvaluation: ...

    def race_progress_effect(
        self,
        race_progress: float,
        track_temperature_c: float | None = None,
        circuit_short_name: str | None = None,
        regulation_era: str = "2023_2025",
    ) -> tuple[float, float]:
        """Return (progression_plus_temp_s, temp_only_s)."""
        ...

    def diagnostics_dict(self) -> dict[str, Any]: ...
