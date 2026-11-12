"""Circular physical neighborhoods independent of official classification."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class PhysicalRoute(StrEnum):
    RACING = "RACING"
    PIT = "PIT"
    OFF_TRACK = "OFF_TRACK"
    UNKNOWN = "UNKNOWN"


class PositionQuality(StrEnum):
    EXACT_SCENARIO = "EXACT_SCENARIO"
    OBSERVED_ESTIMATE = "OBSERVED_ESTIMATE"
    INTERPOLATED = "INTERPOLATED"
    EXTRAPOLATED = "EXTRAPOLATED"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class TrackPosition:
    driver_number: int
    route: PhysicalRoute
    completed_laps: int | None
    classification: int | None
    progress_m: float | None = None
    uncertainty_m: float | None = None
    speed_mps: float | None = None
    quality: PositionQuality = PositionQuality.UNSUPPORTED
    observed_at_utc: datetime | None = None
    lap_aligned: bool = True
    reason: str | None = None

    def __post_init__(self) -> None:
        if (
            self.driver_number <= 0 or (self.completed_laps is not None and self.completed_laps < 0)
            or (self.classification is not None and self.classification <= 0)
        ):
            raise ValueError("Invalid physical driver identity or race progress")
        if (self.progress_m is None) != (self.uncertainty_m is None):
            raise ValueError("A localized position requires its uncertainty radius")
        if (self.quality is PositionQuality.UNSUPPORTED) != (self.progress_m is None):
            raise ValueError("Physical position quality and availability disagree")
        if self.progress_m is not None and self.route is not PhysicalRoute.RACING:
            raise ValueError("Racing-loop progress is reserved for cars on the racing route")
        for value in (self.progress_m, self.uncertainty_m, self.speed_mps):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("Physical position, uncertainty and speed must be finite and nonnegative")
        if self.observed_at_utc is not None and self.observed_at_utc.utcoffset() is None:
            raise ValueError("Location observation times require a timezone")

    def unwrapped_m(self, length_m: float) -> float | None:
        if self.lap_aligned and self.completed_laps is not None and self.progress_m is not None:
            return self.completed_laps * length_m + self.progress_m
        return None


@dataclass(frozen=True, slots=True)
class PhysicalSnapshot:
    session_key: int
    as_of_utc: datetime
    layout_version: str
    length_m: float
    cars: tuple[TrackPosition, ...]

    def __post_init__(self) -> None:
        drivers = {car.driver_number for car in self.cars}
        if (
            self.session_key <= 0 or not self.layout_version.strip() or self.as_of_utc.utcoffset() is None
            or not math.isfinite(self.length_m) or self.length_m <= 0
            or not self.cars or len(drivers) != len(self.cars)
        ):
            raise ValueError("Physical snapshot requires a layout, shared clock and unique drivers")
        if any(car.progress_m is not None and (
            car.progress_m >= self.length_m or car.uncertainty_m >= self.length_m / 2
        ) for car in self.cars):
            raise ValueError("Wrapped position or uncertainty exceeds its racing loop")

    @property
    def complete(self) -> bool:
        return all(car.route in (PhysicalRoute.PIT, PhysicalRoute.OFF_TRACK)
                   or car.progress_m is not None for car in self.cars)


@dataclass(frozen=True, slots=True)
class Neighbor:
    driver_number: int
    gap_m: float
    lower_m: float
    upper_m: float
    lap_advantage: int | None
    encounter: str
    approximate_gap_s: float | None


@dataclass(frozen=True, slots=True)
class PhysicalNeighborhood:
    driver_number: int
    ahead: Neighbor | None
    behind: Neighbor | None
    coverage: str
    ahead_reason: str | None
    behind_reason: str | None


def _relation(
    subject: TrackPosition, target: TrackPosition, gap_m: float, length_m: float,
) -> tuple[int | None, str]:
    own, other = subject.unwrapped_m(length_m), target.unwrapped_m(length_m)
    if own is None or other is None:
        return None, "LAP_RELATION_UNKNOWN"
    # Compare progress at the same place on the loop, including across the line.
    laps = round((own + gap_m - other) / length_m)
    return laps, "LAPPING" if laps > 0 else "BEING_LAPPED" if laps < 0 else "SAME_LAP"


def _neighbor(
    subject: TrackPosition, others: list[TrackPosition], length_m: float, *, forward: bool,
) -> tuple[Neighbor | None, str | None]:
    candidates = []
    for other in others:
        if other.driver_number == subject.driver_number:
            continue
        signed = other.progress_m - subject.progress_m
        gap = (signed if forward else -signed) % length_m
        error = subject.uncertainty_m + other.uncertainty_m
        if min(gap, length_m - gap) <= error:
            return None, "CIRCULAR_ORDER_OVERLAP"
        candidates.append((gap, error, other))
    if not candidates:
        return None, "NO_LOCALIZED_NEIGHBOR"
    candidates.sort(key=lambda row: row[0])
    gap, error, other = candidates[0]
    if any(gap + error >= distance - spread for distance, spread, _ in candidates[1:]):
        return None, "NEAREST_NEIGHBOR_OVERLAP"
    follower, leader = (subject, other) if forward else (other, subject)
    laps, encounter = _relation(follower, leader, gap, length_m)
    speed = follower.speed_mps
    return Neighbor(other.driver_number, gap, gap - error, gap + error, laps, encounter,
                    gap / speed if speed is not None and speed > 0 else None), None


def physical_neighborhoods(snapshot: PhysicalSnapshot) -> dict[int, PhysicalNeighborhood]:
    """Return local order among supported samples and report incomplete field coverage."""
    coverage = "COMPLETE" if snapshot.complete else "SUBSET"
    localized = [car for car in snapshot.cars if car.progress_m is not None]
    neighborhoods = {}
    for car in snapshot.cars:
        if car.progress_m is None:
            reason = car.reason or "CAR_NOT_LOCALIZED_ON_RACING_ROUTE"
            neighborhoods[car.driver_number] = PhysicalNeighborhood(
                car.driver_number, None, None, coverage, reason, reason,
            )
            continue
        ahead, a_reason = _neighbor(car, localized, snapshot.length_m, forward=True)
        behind, b_reason = _neighbor(car, localized, snapshot.length_m, forward=False)
        neighborhoods[car.driver_number] = PhysicalNeighborhood(
            car.driver_number, ahead, behind, coverage, a_reason, b_reason,
        )
    return neighborhoods
