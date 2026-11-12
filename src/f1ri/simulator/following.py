"""Deterministic no-pass timing for a declared circular field."""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import timedelta

from f1ri.simulator.engine import RaceRun, run_checkpoint_window
from f1ri.simulator.events import Event, EventKind
from f1ri.simulator.proximity import (
    PhysicalRoute,
    PhysicalSnapshot,
    PositionQuality,
    physical_neighborhoods,
)
from f1ri.simulator.state import CarStatus, RaceState, Route, elapsed_us


@dataclass(frozen=True, slots=True)
class FollowingTraversal:
    driver_number: int
    checkpoint: int
    clean_us: int
    effective_us: int
    traffic_delay_us: int
    predecessor_driver: int | None
    predecessor_checkpoint: int | None
    crossing_us: int


@dataclass(frozen=True, slots=True)
class FollowingRun:
    initial: RaceState
    run: RaceRun | None
    traversals: tuple[FollowingTraversal, ...]
    unsupported: dict[int, str]


def effective_traversal_us(clean_us: int, behavior_us: int, admissible_us: int) -> int:
    """A single owner combines an admissible time and a compatible behavior term."""
    if clean_us <= 0 or min(behavior_us, admissible_us) < 0:
        raise ValueError("Clean traversal must be positive and adjustment inputs nonnegative")
    return max(clean_us + behavior_us, admissible_us)


def simulate_circular_following(
    initial: RaceState, physical: PhysicalSnapshot, clean_lap_s: dict[int, float | None],
    *, laps: int, minimum_gap_s: float = 0.1,
) -> FollowingRun:
    """Commit deterministic no-pass crossings for all cars in the circular order.

    Constant speed within each clean lap defines three equal-distance checkpoints.
    Dependencies are ordered by distance traveled from the snapshot. Each
    predecessor passage requires less traveled distance, including at wraparound.
    """
    cutoff = initial.race_start_utc + timedelta(microseconds=initial.race_clock_us)
    if (physical.session_key != initial.session_key or physical.as_of_utc != cutoff or laps < 1):
        raise ValueError("Following requires a matching positive-horizon physical snapshot")
    cars = {car.driver_number: car for car in initial.cars}
    positions = {car.driver_number: car for car in physical.cars}
    if cars.keys() != positions.keys() or not clean_lap_s.keys() <= cars.keys():
        raise ValueError("Physical, race and pace inputs must refer to the same field")
    neighborhoods = physical_neighborhoods(physical)
    step = physical.length_m / 3
    gap_us = elapsed_us(minimum_gap_s)
    if gap_us < 1:
        raise ValueError("Following requires a positive checkpoint headway")
    reasons = {}
    for driver, car in cars.items():
        pos = positions[driver]
        pace = clean_lap_s.get(driver)
        if car.status is not CarStatus.ACTIVE or car.route is not Route.ON_TRACK:
            reasons[driver] = "ROUTE_TRANSITION_UNSUPPORTED"
        elif pos.route is not PhysicalRoute.RACING or pos.quality is not PositionQuality.EXACT_SCENARIO:
            reasons[driver] = "PHYSICAL_ORDER_UNSUPPORTED"
        elif pos.unwrapped_m(physical.length_m) is None or pos.completed_laps != car.completed_laps:
            reasons[driver] = "LAP_ALIGNMENT_UNSUPPORTED"
        elif pos.uncertainty_m != 0 or (len(cars) > 1 and neighborhoods[driver].ahead is None):
            reasons[driver] = "NEIGHBOR_ORDER_UNRESOLVED"
        elif pace is None or not math.isfinite(pace) or pace <= 0:
            reasons[driver] = "PACE_UNSUPPORTED"
        elif math.floor(pos.progress_m / step) != car.sector:
            reasons[driver] = "CHECKPOINT_ALIGNMENT_UNSUPPORTED"
    if reasons:
        return FollowingRun(initial, None, (), reasons)

    pending = []
    for driver, car in cars.items():
        first = car.completed_checkpoints + 1
        end = min(first + laps * 3, initial.ruleset.scheduled_laps * 3 + 1)
        for checkpoint in range(first, end):
            distance = ((checkpoint - car.completed_laps * 3) * step - positions[driver].progress_m)
            pending.append((distance, driver, checkpoint))
    committed: dict[tuple[int, int], FollowingTraversal] = {}
    clock = initial.race_clock_us
    events = []
    for distance, driver, checkpoint in sorted(pending):
        previous = committed.get((driver, checkpoint - 1))
        start = previous.crossing_us if previous is not None else clock
        last_distance = distance - step if previous is not None else 0
        pace_us = elapsed_us(clean_lap_s[driver])
        clean_end = round(distance / physical.length_m * pace_us)
        clean_start = round(last_distance / physical.length_m * pace_us)
        clean = max(1, clean_end - clean_start)
        ahead = neighborhoods[driver].ahead
        leader, leader_cp, admissible = None, None, 0
        if ahead is not None:
            leader = ahead.driver_number
            remaining = distance - ahead.gap_m
            if remaining <= 1e-7:
                # Its passage was at or before the snapshot. Use a conservative
                # headway bound if the follower reaches this first marker early.
                admissible = max(0, clock + gap_us - start)
            else:
                leader_cp = cars[leader].completed_laps * 3 + round(
                    (positions[leader].progress_m + remaining) / step
                )
                prerequisite = committed.get((leader, leader_cp))
                if prerequisite is None:
                    return FollowingRun(initial, None, (), {driver: "PREDECESSOR_WINDOW_UNSUPPORTED"})
                admissible = max(0, prerequisite.crossing_us + gap_us - start)
        effective = effective_traversal_us(clean, 0, admissible)
        at = start + effective
        committed[driver, checkpoint] = FollowingTraversal(
            driver, checkpoint, clean, effective, effective - clean, leader, leader_cp, at,
        )
        kind = EventKind.CHECKPOINT_CROSSING if checkpoint % 3 else EventKind.LAP_COMPLETED
        events.append(Event(at, kind, driver, checkpoint, source="S3_CIRCULAR_NO_PASS"))
    run = replace(run_checkpoint_window(initial, events), capability="S3")
    realized = tuple(committed[frame.event.driver_number, frame.event.checkpoint] for frame in run.trace)
    return FollowingRun(initial, run, realized, {})
