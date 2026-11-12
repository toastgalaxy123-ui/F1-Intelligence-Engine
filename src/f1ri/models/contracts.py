"""Stable interfaces so model upgrades don't change simulation code."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

Z_50 = 0.6744897501960817
Z_90 = 1.6448536269514722

SUPPORT_SUPPORTED = "SUPPORTED"
SUPPORT_LIMITED = "LIMITED_SUPPORT"
SUPPORT_EXTRAPOLATED = "EXTRAPOLATED"
SUPPORT_UNSUPPORTED = "UNSUPPORTED"

SCENARIO_CONTINUE_STINT = "CONTINUE_CURRENT_STINT"
SCENARIO_NEW_FRESH = "NEW_STINT_FRESH_TIRES"
SCENARIO_NEW_USED = "NEW_STINT_USED_TIRES"


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


@dataclass(frozen=True)
class StintForecastRequest:
    """Point-in-time request for an open-loop or rolling multi-lap stint trajectory forecast."""

    session_key: int
    driver_number: int
    cutoff_lap_number: int
    planned_start_lap: int
    horizon_laps: int
    compound_relative: str
    tyre_age_at_start: int
    as_of_utc: datetime
    scenario_type: str = SCENARIO_CONTINUE_STINT
    compound_absolute: str | None = None
    compound_mapping_confidence: str = "UNKNOWN"
    meeting_key: int | None = None
    season: int | None = None
    regulation_era: str = "2023_2025"
    circuit_key: int | None = None
    circuit_short_name: str | None = None
    driver_id: str | None = None
    team_id: str | None = None
    stint_number: int = 1
    scheduled_race_laps: int = 58
    qualifying_lap_time_s: float | None = None
    grid_position: int | None = None
    last_clean_lap_time_s: float | None = None
    median_last_3_clean_laps_s: float | None = None
    median_last_5_clean_laps_s: float | None = None
    median_stint_clean_laps_s: float | None = None
    clean_laps_observed_current_stint: int = 0
    clean_laps_observed_race: int = 0
    last_clean_lap_age: int | None = None
    observed_stint_clean_ages: tuple[int, ...] = ()
    observed_stint_clean_times_s: tuple[float, ...] = ()
    observed_stint_clean_progress: tuple[float, ...] = ()
    prev_stint_compound_relative: str | None = None
    prev_stint_compound_absolute: str | None = None
    prev_stint_end_tyre_age: int | None = None
    track_temperature_c: float | None = None
    air_temperature_c: float | None = None
    future_weather_scenario: str = "CONSTANT_LAST_OBSERVED"
    n_trajectory_samples: int = 2000
    random_seed: int = 42
    retain_trajectory_samples: bool = False


@dataclass(frozen=True)
class ProjectedLapForecast:
    """Lap-level projection within a multi-lap stint forecast trajectory."""

    step_index: int
    target_lap_number: int
    projected_tyre_age: int
    race_progress: float
    expected_lap_time_s: float
    median_lap_time_s: float
    std_lap_time_s: float
    p25_lap_time_s: float
    p75_lap_time_s: float
    p05_lap_time_s: float
    p95_lap_time_s: float
    expected_cumulative_time_s: float
    std_cumulative_time_s: float
    p25_cumulative_time_s: float
    p75_cumulative_time_s: float
    p05_cumulative_time_s: float
    p95_cumulative_time_s: float
    base_pace_contribution_s: float
    race_progression_contribution_s: float
    tire_degradation_contribution_s: float
    support_status: str = SUPPORT_SUPPORTED
    extrapolation_warning: str | None = None


@dataclass(frozen=True)
class StintTrajectoryForecast:
    """Complete probabilistic open-loop forecast for a future stint trajectory."""

    session_key: int
    driver_number: int
    cutoff_lap_number: int
    planned_start_lap: int
    horizon_laps: int
    scenario_type: str
    compound_relative: str
    compound_absolute: str | None
    tyre_age_at_start: int
    as_of_utc: datetime
    lap_forecasts: tuple[ProjectedLapForecast, ...]
    expected_total_time_s: float
    median_total_time_s: float
    std_total_time_s: float
    p25_total_time_s: float
    p75_total_time_s: float
    p05_total_time_s: float
    p95_total_time_s: float
    support_status: str
    extrapolation_warning: str | None
    model_version: str
    degradation_model_version: str
    trained_through_utc: datetime
    random_seed: int = 42
    future_weather_scenario: str = "CONSTANT_LAST_OBSERVED"
    sampled_lap_trajectories_s: tuple[tuple[float, ...], ...] | None = None


class StintForecastModel(Protocol):
    def forecast_stint(self, request: StintForecastRequest) -> StintTrajectoryForecast: ...
