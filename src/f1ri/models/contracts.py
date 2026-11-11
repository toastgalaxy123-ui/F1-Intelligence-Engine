"""Stable interfaces so model upgrades don't change simulation code."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

Z_50 = 0.6744897501960817
Z_90 = 1.6448536269514722


@dataclass(frozen=True)
class PaceRequest:
    session_key: int
    driver_number: int
    lap_number: int
    compound: str
    tyre_age_at_lap_start: int
    track_temperature_c: float | None
    # Caller must provide only information known at the forecast cutoff.
    as_of_utc: datetime
    horizon: int = 1
    season: int | None = None
    regulation_era: str | None = None
    circuit_key: int | None = None
    circuit_short_name: str | None = None
    driver_id: str | None = None
    team_id: str | None = None
    race_progress: float | None = None
    stint_number: int | None = None
    current_stint_laps_completed: int = 0
    tyre_age_at_stint_start: int = 0
    last_clean_lap_time_s: float | None = None
    median_last_3_clean_laps_s: float | None = None
    median_last_5_clean_laps_s: float | None = None
    median_stint_clean_laps_s: float | None = None
    clean_laps_observed_current_stint: int = 0
    clean_laps_observed_race: int = 0
    last_clean_lap_age: int | None = None
    air_temperature_c: float | None = None
    temperature_observation_age_s: float | None = None
    actual_compound_code: str | None = None
    compound_mapping_source: str | None = None
    compound_mapping_confidence: str | None = None


@dataclass(frozen=True)
class PaceDistribution:
    expected_seconds: float
    std_seconds: float
    model_version: str
    trained_through_utc: datetime
    median_seconds: float | None = None
    p25_seconds: float | None = None
    p75_seconds: float | None = None
    p05_seconds: float | None = None
    p95_seconds: float | None = None
    confidence_level: str = "NORMAL"
    forecast_as_of_utc: datetime | None = None

    def __post_init__(self) -> None:
        s = max(float(self.std_seconds), 1e-6)
        med = float(self.expected_seconds) if self.median_seconds is None else float(self.median_seconds)
        p25 = med - Z_50 * s if self.p25_seconds is None else float(self.p25_seconds)
        p75 = med + Z_50 * s if self.p75_seconds is None else float(self.p75_seconds)
        p05 = med - Z_90 * s if self.p05_seconds is None else float(self.p05_seconds)
        p95 = med + Z_90 * s if self.p95_seconds is None else float(self.p95_seconds)
        object.__setattr__(self, "median_seconds", med)
        object.__setattr__(self, "p25_seconds", p25)
        object.__setattr__(self, "p75_seconds", p75)
        object.__setattr__(self, "p05_seconds", p05)
        object.__setattr__(self, "p95_seconds", p95)


class PaceModel(Protocol):
    def predict(self, request: PaceRequest) -> PaceDistribution: ...
