"""Race state for the checkpoint engine. All elapsed times are integer microseconds."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SimulationMode(StrEnum):
    HISTORICAL_RECONSTRUCTION = "HISTORICAL_RECONSTRUCTION"
    CONDITIONAL_SIMULATION = "CONDITIONAL_SIMULATION"
    GENUINE_FORECAST = "GENUINE_FORECAST"


class RacePhase(StrEnum):
    PRESTART = "PRESTART"
    RUNNING = "RUNNING"
    SUSPENDED = "SUSPENDED"
    FINISHING = "FINISHING"
    FINISHED = "FINISHED"


class TrackState(StrEnum):
    UNKNOWN = "UNKNOWN"
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    DOUBLE_YELLOW = "DOUBLE_YELLOW"
    VSC = "VSC"
    SC = "SC"
    RED = "RED"


class CarStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    FINISHED = "FINISHED"
    DNS = "DNS"


class Route(StrEnum):
    ON_TRACK = "ON_TRACK"
    PIT_ENTRY = "PIT_ENTRY"
    PIT_LANE = "PIT_LANE"
    PIT_SERVICE = "PIT_SERVICE"
    PIT_EXIT = "PIT_EXIT"
    PIT_TRANSITION_COARSE = "PIT_TRANSITION_COARSE"
    OFF_TRACK = "OFF_TRACK"


def elapsed_us(seconds: float) -> int:
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Elapsed time must be finite and nonnegative")
    return round(seconds * 1_000_000)


@dataclass(frozen=True, slots=True)
class TyreState:
    compound: str
    age_laps: int | None
    stint_number: int = 1
    compound_absolute: str | None = None
    set_id: str | None = None
    initial_age_laps: int | None = None
    stint_start_lap: int | None = None

    def __post_init__(self) -> None:
        if not self.compound.strip() or self.stint_number < 1:
            raise ValueError("Tyres require a compound and positive stint number")
        if self.age_laps is not None and self.age_laps < 0:
            raise ValueError("Tyre age must be nonnegative")
        if self.initial_age_laps is not None and (
            self.initial_age_laps < 0 or self.age_laps is None or self.age_laps < self.initial_age_laps
        ):
            raise ValueError("Tyre age cannot precede the set's initial age")
        if self.stint_start_lap is not None and self.stint_start_lap <= 0:
            raise ValueError("Stint start lap must be positive")


@dataclass(frozen=True, slots=True)
class CarState:
    driver_number: int
    grid_position: int | None
    tyres: TyreState
    team: str | None = None
    status: CarStatus = CarStatus.ACTIVE
    route: Route = Route.ON_TRACK
    completed_checkpoints: int = 0
    last_crossing_us: int | None = None
    finish_us: int | None = None
    pit_box: str | None = None

    def __post_init__(self) -> None:
        if self.driver_number <= 0 or (self.grid_position is not None and self.grid_position <= 0):
            raise ValueError("Driver number and known grid position must be positive")
        if self.completed_checkpoints < 0:
            raise ValueError("Checkpoint count must be nonnegative")
        if (self.completed_checkpoints == 0) != (self.last_crossing_us is None):
            raise ValueError("Checkpoint progress requires a crossing time")
        if self.last_crossing_us is not None and self.last_crossing_us < 0:
            raise ValueError("Crossing time must be nonnegative")
        if (self.status is CarStatus.ACTIVE) == (self.route is Route.OFF_TRACK):
            raise ValueError("Only active cars can occupy the track or pit lane")
        if (self.status is CarStatus.FINISHED) != (self.finish_us is not None):
            raise ValueError("Finished status requires a finish time")
        if self.finish_us is not None and self.finish_us != self.last_crossing_us:
            raise ValueError("A car must finish at its last crossing")
        if (self.route is Route.PIT_SERVICE) != (self.pit_box is not None):
            raise ValueError("Only a car in service can occupy a pit box")

    @property
    def completed_laps(self) -> int:
        return self.completed_checkpoints // 3

    @property
    def sector(self) -> int:
        """Zero-based sector after the most recent crossing."""
        return self.completed_checkpoints % 3

    @property
    def grid_key(self) -> tuple[bool, int, int]:
        return self.grid_position is None, self.grid_position or 0, self.driver_number


@dataclass(frozen=True, slots=True)
class RaceRuleset:
    season: int
    scheduled_laps: int

    def __post_init__(self) -> None:
        if self.season < 2023 or self.scheduled_laps <= 0:
            raise ValueError("A supported season and positive race distance are required")

    @property
    def version(self) -> str:
        return f"{self.season}/s0-v1"


@dataclass(frozen=True, slots=True)
class RaceState:
    session_key: int
    race_start_utc: datetime
    ruleset: RaceRuleset
    mode: SimulationMode
    simulation_id: str
    cars: tuple[CarState, ...]
    race_clock_us: int = 0
    phase: RacePhase = RacePhase.PRESTART
    track_state: TrackState = TrackState.UNKNOWN
    chequered_us: int | None = None

    def __post_init__(self) -> None:
        if self.session_key <= 0 or not self.simulation_id.strip():
            raise ValueError("Session and simulation identity are required")
        if self.race_start_utc.utcoffset() is None or self.race_clock_us < 0:
            raise ValueError("Race state requires a timezone-aware start and nonnegative clock")
        drivers = {car.driver_number for car in self.cars}
        grid = [car.grid_position for car in self.cars if car.grid_position is not None]
        if not self.cars or len(drivers) != len(self.cars) or len(set(grid)) != len(grid):
            raise ValueError("Field must have unique drivers and unique known grid positions")
        if self.chequered_us is not None and not 0 <= self.chequered_us <= self.race_clock_us:
            raise ValueError("Chequered time must be on the elapsed race clock")
        for car in self.cars:
            if car.completed_laps > self.ruleset.scheduled_laps:
                raise ValueError("Car exceeded scheduled race distance")
            if car.last_crossing_us is not None and car.last_crossing_us > self.race_clock_us:
                raise ValueError("Crossing cannot follow the race clock")
            if car.finish_us is not None and (
                self.chequered_us is None or car.finish_us < self.chequered_us
            ):
                raise ValueError("Car finished before the chequered flag")

    @property
    def race_clock_s(self) -> float:
        return self.race_clock_us / 1_000_000

    @property
    def leader_completed_laps(self) -> int:
        return max(car.completed_laps for car in self.cars)

    @property
    def timing_order(self) -> tuple[int, ...]:
        """Order by recorded checkpoint progress, or completed laps at the finish."""
        def key(car: CarState) -> tuple:
            progress = (
                car.completed_laps if self.phase is RacePhase.FINISHED
                else car.completed_checkpoints
            )
            return car.status is CarStatus.DNS, -progress, car.last_crossing_us or 0, car.grid_key

        return tuple(car.driver_number for car in sorted(self.cars, key=key))
