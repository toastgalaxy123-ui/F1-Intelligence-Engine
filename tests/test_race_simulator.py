from dataclasses import replace
from datetime import UTC, datetime

import pytest

from f1ri.simulator.engine import (
    CarPlan,
    SyntheticPitStop,
    TimingMode,
    run_timeline,
    simulate_deterministic,
)
from f1ri.simulator.events import Event, EventKind
from f1ri.simulator.scheduler import Scheduler
from f1ri.simulator.state import (
    CarState,
    CarStatus,
    RacePhase,
    RaceRuleset,
    RaceState,
    Route,
    SimulationMode,
    TrackState,
    TyreState,
)


def _grid(laps: int, *drivers: int) -> RaceState:
    cars = tuple(CarState(d, p, TyreState("MEDIUM", 2)) for p, d in enumerate(drivers, 1))
    return RaceState(1, datetime(2025, 10, 5, tzinfo=UTC), RaceRuleset(2025, laps),
                     SimulationMode.CONDITIONAL_SIMULATION, "synthetic", cars)


def _cars(state: RaceState) -> dict[int, CarState]:
    return {car.driver_number: car for car in state.cars}


def test_free_running_matches_independent_lap_totals():
    state = _grid(10, 63, 1, 4)
    plans = (CarPlan(63, (90.,) * 10), CarPlan(1, (89.5,) * 10, start_gap_s=1),
             CarPlan(4, (91.,) * 10, start_gap_s=2))
    run = simulate_deterministic(state, plans, sector_fractions=(0.23, 0.41, 0.36))
    assert run == simulate_deterministic(state, reversed(plans),
                                        sector_fractions=(0.23, 0.41, 0.36))
    assert run.final_state.phase is RacePhase.FINISHED
    assert run.final_state.timing_order == (1, 63, 4)
    assert {driver: car.finish_us for driver, car in _cars(run.final_state).items()} == {
        plan.driver_number: int((sum(plan.lap_times_s) + plan.start_gap_s) * 1_000_000)
        for plan in plans
    }
    assert all(car.tyres.age_laps == 12 for car in run.final_state.cars)
    times = [frame.state.race_clock_us for frame in run.trace]
    assert times == sorted(times)
    assert sum(frame.event.kind is EventKind.LAP_COMPLETED for frame in run.trace) == 30


def test_no_pass_enforces_checkpoint_gap():
    state = _grid(10, 63, 1)
    plans = (CarPlan(63, (90.,) * 10), CarPlan(1, (89.5,) * 10, start_gap_s=1))
    run = simulate_deterministic(state, plans, timing=TimingMode.NO_PASS)
    crossings = {(frame.event.driver_number, frame.event.checkpoint): frame.event.at_us
                 for frame in run.trace if frame.event.checkpoint}
    assert run.final_state.timing_order == (63, 1)
    assert all(crossings[1, n] - crossings[63, n] >= 100_000 for n in range(1, 31))
    assert crossings[1, 30] == 900_100_000


def test_pit_delay_applies_once_and_changes_route_and_tyres():
    state = _grid(3, 63, 1)
    stop = SyntheticPitStop(after_lap=1, net_cost_s=20., compound="HARD", tyre_initial_age=1)
    run = simulate_deterministic(
        state, (CarPlan(63, (90.,) * 3, pits=(stop,)),
                CarPlan(1, (90.,) * 3, start_gap_s=5)),
    )
    pit_entry = next(frame for frame in run.trace if frame.event.kind is EventKind.PIT_ENTRY)
    pit_exit = next(frame for frame in run.trace if frame.event.kind is EventKind.PIT_EXIT)
    assert _cars(pit_entry.state)[63].route is Route.PIT_LANE
    assert _cars(pit_entry.state)[63].completed_laps == 1
    assert _cars(pit_exit.state)[63].tyres == TyreState("HARD", 1, 2)
    assert _cars(pit_exit.state)[63].route is Route.ON_TRACK
    assert _cars(run.final_state)[63].tyres == TyreState("HARD", 3, 2)
    assert _cars(run.final_state)[63].finish_us == 290_000_000
    assert run.final_state.timing_order == (1, 63)


def test_lapped_car_finishes_at_its_next_crossing_including_equal_time_flag():
    state = _grid(2, 63, 1, 4)
    run = simulate_deterministic(
        state, (CarPlan(63, (180.,) * 2), CarPlan(1, (90.,) * 2), CarPlan(4, (200.,) * 2)),
    )
    assert run.final_state.chequered_us == 180_000_000
    assert run.final_state.race_clock_us == 200_000_000
    assert [(d, _cars(run.final_state)[d].completed_laps)
            for d in run.final_state.timing_order] == [(1, 2), (63, 1), (4, 1)]


def test_retired_car_does_not_complete_more_laps():
    run = simulate_deterministic(
        _grid(3, 63, 1), (CarPlan(63, (90.,) * 3, retirement_s=91.),
                         CarPlan(1, (92.,) * 3)),
    )
    car = _cars(run.final_state)[63]
    assert car.status is CarStatus.RETIRED and car.route is Route.OFF_TRACK
    assert car.completed_laps == 1 and car.finish_us is None
    assert all(frame.event.at_us <= 91_000_000
               for frame in run.trace if frame.event.driver_number == 63)
    assert run.final_state.timing_order == (1, 63)


def test_scheduler_precedence_ties_duplicates_and_invalidation():
    scheduler = Scheduler({1: 2, 63: 1})
    slower = Event(20, EventKind.CHECKPOINT_CROSSING, 1, 1)
    quicker = Event(20, EventKind.CHECKPOINT_CROSSING, 63, 1, generation=0)
    status = Event(20, EventKind.TRACK_STATE_CHANGE, track_state=TrackState.VSC)
    for event in (slower, quicker, status):
        scheduler.schedule(event)
    with pytest.raises(ValueError, match="Duplicate"):
        scheduler.schedule(slower)
    replacement = replace(quicker, generation=scheduler.invalidate(63))
    scheduler.schedule(replacement)
    assert list(scheduler) == [status, replacement, slower]
    with pytest.raises(ValueError, match="committed"):
        scheduler.schedule(Event(10, EventKind.RACE_START))


def test_invalid_timeline_and_forecast_input_are_rejected():
    grid = _grid(1, 63)
    start = Event(0, EventKind.RACE_START)
    with pytest.raises(ValueError, match="Pit exit requires"):
        run_timeline(grid, (start, Event(1, EventKind.PIT_EXIT, 63)))
    with pytest.raises(ValueError, match="checkpoint sequence"):
        run_timeline(grid, (start, Event(1, EventKind.CHECKPOINT_CROSSING, 63, 2)))
    with pytest.raises(ValueError, match="conditional simulation"):
        simulate_deterministic(replace(grid, mode=SimulationMode.GENUINE_FORECAST),
                               (CarPlan(63, (90.,)),))
