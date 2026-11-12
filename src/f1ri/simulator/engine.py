"""Deterministic checkpoint mechanics shared by scenarios and historical replay."""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum

from f1ri.simulator.contracts import RaceState as StrategyRaceState
from f1ri.simulator.contracts import SimulationSummary, StrategyCandidate
from f1ri.simulator.events import Event, EventKind
from f1ri.simulator.scheduler import Scheduler
from f1ri.simulator.state import (
    CarState,
    CarStatus,
    RacePhase,
    RaceState,
    Route,
    SimulationMode,
    TrackState,
    TyreState,
    elapsed_us,
)


@dataclass(frozen=True, slots=True)
class TraceFrame:
    event: Event
    state: RaceState


@dataclass(frozen=True, slots=True)
class RaceRun:
    final_state: RaceState
    trace: tuple[TraceFrame, ...]
    capability: str = "S0"


def _cross(
    state: RaceState, car: CarState, event: Event,
) -> tuple[RaceState, CarState]:
    checkpoint = event.checkpoint
    assert checkpoint is not None
    historical = state.mode is SimulationMode.HISTORICAL_RECONSTRUCTION
    if checkpoint != car.completed_checkpoints + 1 and not (
        historical and car.completed_checkpoints < checkpoint
        and (checkpoint - 1) // 3 == car.completed_laps
    ):
        raise ValueError(f"Invalid checkpoint sequence for driver {car.driver_number}")
    if car.last_crossing_us is not None and event.at_us <= car.last_crossing_us:
        raise ValueError("A car's crossings must advance in time")
    if state.phase is RacePhase.SUSPENDED and not historical:
        raise ValueError("Racing progress is suspended")
    car = replace(car, completed_checkpoints=checkpoint, last_crossing_us=event.at_us)
    if event.kind is not EventKind.LAP_COMPLETED:
        return state, car
    if car.tyres.age_laps is not None:
        car = replace(car, tyres=replace(car.tyres, age_laps=car.tyres.age_laps + 1))
    if state.chequered_us is None and car.completed_laps == state.ruleset.scheduled_laps:
        state = replace(state, chequered_us=event.at_us, phase=RacePhase.FINISHING)
    if state.chequered_us is not None:
        car = replace(car, status=CarStatus.FINISHED, route=Route.OFF_TRACK, finish_us=event.at_us)
    return state, car


def run_timeline(initial: RaceState, events: Iterable[Event]) -> RaceRun:
    """Apply observed or scenario events to a prestart grid."""
    if initial.phase is not RacePhase.PRESTART or any(
        car.completed_checkpoints or car.status not in {CarStatus.ACTIVE, CarStatus.DNS}
        or (car.status is CarStatus.ACTIVE and car.route is not Route.ON_TRACK)
        for car in initial.cars
    ):
        raise ValueError("The checkpoint engine requires a prestart grid")
    scheduler = Scheduler(
        {car.driver_number: car.grid_position for car in initial.cars},
        scheduled_laps=initial.ruleset.scheduled_laps,
    )
    for event in events:
        scheduler.schedule(event)
    state = initial
    trace = []
    for event in scheduler:
        if event.at_us < state.race_clock_us:
            raise ValueError("Race clock cannot move backwards")
        if state.phase is RacePhase.FINISHED:
            break
        if state.phase is RacePhase.PRESTART and event.kind is not EventKind.RACE_START:
            raise ValueError("The timeline must start with RACE_START")
        state = replace(state, race_clock_us=event.at_us)
        if event.kind is EventKind.RACE_START:
            if state.phase is not RacePhase.PRESTART or event.at_us != 0:
                raise ValueError("Start event requires the prestart grid at time zero")
            state = replace(state, phase=RacePhase.RUNNING)
        elif event.kind is EventKind.TRACK_STATE_CHANGE:
            assert event.track_state is not None
            phase = state.phase
            if state.chequered_us is None:
                phase = (
                    RacePhase.SUSPENDED if event.track_state is TrackState.RED
                    else RacePhase.RUNNING
                )
            state = replace(state, track_state=event.track_state, phase=phase)
        elif event.kind in {
            EventKind.CHECKPOINT_CROSSING, EventKind.LAP_COMPLETED,
            EventKind.PIT_ENTRY, EventKind.PIT_EXIT, EventKind.RETIREMENT,
        }:
            index = next(i for i, car in enumerate(state.cars)
                         if car.driver_number == event.driver_number)
            car = state.cars[index]
            if car.status is not CarStatus.ACTIVE:
                # Scenario clocks provisionally schedule full distances for all cars.
                if state.mode is SimulationMode.CONDITIONAL_SIMULATION:
                    continue
                raise ValueError(f"Observed event after driver {car.driver_number} stopped")
            if event.kind in {EventKind.CHECKPOINT_CROSSING, EventKind.LAP_COMPLETED}:
                state, car = _cross(state, car, event)
            elif event.kind is EventKind.RETIREMENT:
                car = replace(car, status=CarStatus.RETIRED, route=Route.OFF_TRACK)
            elif event.kind is EventKind.PIT_ENTRY:
                if car.route is not Route.ON_TRACK:
                    raise ValueError("Pit entry requires a car on track")
                car = replace(car, route=Route.PIT_LANE)
            else:
                if car.route is not Route.PIT_LANE:
                    raise ValueError("Pit exit requires a car in the pit lane")
                if event.tyres is not None and event.tyres.stint_number <= car.tyres.stint_number:
                    raise ValueError("A tyre change must advance the stint number")
                car = replace(car, route=Route.ON_TRACK, tyres=event.tyres or car.tyres)
            state = replace(state, cars=state.cars[:index] + (car,) + state.cars[index + 1:])
        else:
            raise ValueError(f"Unsupported Stage 5 event {event.kind.name}")
        if all(car.status is not CarStatus.ACTIVE for car in state.cars):
            state = replace(state, phase=RacePhase.FINISHED)
        trace.append(TraceFrame(event, state))
    if not trace:
        raise ValueError("The timeline requires a race start")
    return RaceRun(trace[-1].state, tuple(trace))


class TimingMode(StrEnum):
    FREE_RUNNING = "FREE_RUNNING"
    NO_PASS = "NO_PASS"


@dataclass(frozen=True, slots=True)
class SyntheticPitStop:
    after_lap: int
    net_cost_s: float
    compound: str
    tyre_initial_age: int = 0


@dataclass(frozen=True, slots=True)
class CarPlan:
    driver_number: int
    lap_times_s: tuple[float, ...]
    start_gap_s: float = 0.0
    pits: tuple[SyntheticPitStop, ...] = ()
    retirement_s: float | None = None


def simulate_deterministic(
    initial: RaceState,
    plans: Iterable[CarPlan],
    *,
    sector_fractions: tuple[float, float, float] = (1 / 3, 1 / 3, 1 / 3),
    timing: TimingMode = TimingMode.FREE_RUNNING,
    minimum_gap_s: float = 0.1,
) -> RaceRun:
    """Run a declared green-flag scenario with a full distance plan for each starter."""
    if initial.mode is not SimulationMode.CONDITIONAL_SIMULATION:
        raise ValueError("Synthetic input requires conditional simulation mode")
    if (
        len(sector_fractions) != 3
        or not all(math.isfinite(fraction) and fraction > 0 for fraction in sector_fractions)
        or not math.isclose(sum(sector_fractions), 1, rel_tol=0, abs_tol=1e-12)
    ):
        raise ValueError("Three positive sector fractions must sum to one")
    cars = sorted(
        (car for car in initial.cars if car.status is CarStatus.ACTIVE),
        key=lambda car: car.grid_key,
    )
    plan_list = tuple(plans)
    by_driver = {plan.driver_number: plan for plan in plan_list}
    if len(by_driver) != len(plan_list) or set(by_driver) != {car.driver_number for car in cars}:
        raise ValueError("Each starter requires one plan")
    if timing not in (TimingMode.FREE_RUNNING, TimingMode.NO_PASS):
        raise ValueError("Unsupported deterministic timing mode")
    gap_us = elapsed_us(minimum_gap_s)
    if timing is TimingMode.NO_PASS and (
        not gap_us or any(plan.pits or plan.retirement_s is not None for plan in plan_list)
    ):
        raise ValueError("No-pass timing requires a positive gap and a shared uninterrupted route")
    events = [Event(0, EventKind.RACE_START), Event(0, EventKind.TRACK_STATE_CHANGE,
                                                  track_state=TrackState.GREEN)]
    ahead: list[int] = []
    distance = initial.ruleset.scheduled_laps
    for car in cars:
        plan = by_driver[car.driver_number]
        if len(plan.lap_times_s) != distance:
            raise ValueError("Provide one lap time per scheduled lap")
        pits = {stop.after_lap: stop for stop in plan.pits}
        if len(pits) != len(plan.pits) or any(not 1 <= lap < distance for lap in pits):
            raise ValueError("Pit laps must be unique and before the scheduled final lap")
        stint = car.tyres.stint_number
        now = elapsed_us(plan.start_gap_s)
        crossings = []
        for lap, seconds in enumerate(plan.lap_times_s, start=1):
            duration = elapsed_us(seconds)
            offsets = (round(duration * sector_fractions[0]),
                       round(duration * sum(sector_fractions[:2])), duration)
            for sector, offset in enumerate(offsets, start=1):
                previous_offset = offsets[sector - 2] if sector > 1 else 0
                if offset <= previous_offset:
                    raise ValueError("Each sector must have positive elapsed time")
                checkpoint = (lap - 1) * 3 + sector
                now += offset - previous_offset
                if timing is TimingMode.NO_PASS and ahead:
                    now = max(now, ahead[checkpoint - 1] + gap_us)
                crossings.append(now)
                kind = EventKind.LAP_COMPLETED if sector == 3 else EventKind.CHECKPOINT_CROSSING
                events.append(Event(now, kind, car.driver_number, checkpoint))
            if lap in pits:
                stop = pits[lap]
                cost = elapsed_us(stop.net_cost_s)
                if not cost:
                    raise ValueError("A coarse pit stop requires a positive route-exclusion time")
                stint += 1
                tyres = TyreState(stop.compound, stop.tyre_initial_age, stint)
                events.append(Event(now, EventKind.PIT_ENTRY, car.driver_number,
                                    source="SYNTHETIC_NET_COST"))
                now += cost
                events.append(Event(now, EventKind.PIT_EXIT, car.driver_number, tyres=tyres,
                                    source="SYNTHETIC_NET_COST"))
        if plan.retirement_s is not None:
            events.append(Event(elapsed_us(plan.retirement_s), EventKind.RETIREMENT,
                                car.driver_number))
        ahead = crossings
    return run_timeline(initial, events)


def simulate_remaining_race(
    state: StrategyRaceState, strategy: StrategyCandidate, n_simulations: int, seed: int
) -> SimulationSummary:
    raise NotImplementedError(
        "Strategy probabilities require the Stage 5 forecasting gates in docs/STAGE5_SPEC.md."
    )
