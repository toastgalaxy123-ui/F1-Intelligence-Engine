"""Domain types: simulation has no dependency on OpenF1 column names."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class DriverState:
    driver_number: int
    position: int
    completed_laps: int
    compound: str
    tyre_age_laps: int
    gap_to_leader_seconds: float | None
    in_race: bool = True


@dataclass(frozen=True)
class RaceState:
    session_key: int
    as_of_utc: datetime
    scheduled_laps: int
    track_status: str
    drivers: tuple[DriverState, ...]


@dataclass(frozen=True)
class StrategyCandidate:
    driver_number: int
    pit_on_laps: tuple[int, ...]
    planned_compounds: tuple[str, ...]


@dataclass(frozen=True)
class SimulationSummary:
    session_key: int
    as_of_utc: datetime
    model_version: str
    n_simulations: int
    random_seed: int
    finish_position_probabilities: dict[int, dict[int, float]]
