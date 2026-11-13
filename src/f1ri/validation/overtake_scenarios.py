"""Deterministic S4-A validation fixtures using existing S2 and S3 contracts."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from f1ri.simulator.events import Event, EventKind
from f1ri.simulator.overtaking import OvertakeAttempt, simulate_scripted_overtaking
from f1ri.simulator.pit_proximity import resolved_rejoin_snapshot
from f1ri.simulator.proximity import PhysicalRoute, PhysicalSnapshot, PositionQuality, TrackPosition
from f1ri.simulator.state import (
    CarState,
    RacePhase,
    RaceRuleset,
    RaceState,
    SimulationMode,
    TrackState,
    TyreState,
)
from f1ri.validation.pit_integration_backtest import _synthetic


def field(positions, name):
    clock = datetime(2025, 1, 1, tzinfo=UTC)
    cars = tuple(CarState(d, i, TyreState("MEDIUM", 2), completed_checkpoints=lap*3 + int(m // 300),
                          last_crossing_us=0) for i, (d, lap, m) in enumerate(positions, 1))
    state = RaceState(1, clock, RaceRuleset(2025, 20), SimulationMode.CONDITIONAL_SIMULATION, name,
                      cars, phase=RacePhase.RUNNING, track_state=TrackState.GREEN)
    physical = PhysicalSnapshot(1, clock, "s4-test-loop", 900., tuple(
        TrackPosition(d, PhysicalRoute.RACING, lap, i, m, 0., quality=PositionQuality.EXACT_SCENARIO)
        for i, (d, lap, m) in enumerate(positions, 1)))
    return state, physical


def fixtures():
    specs = {}
    pair = ((1, 3, 200.), (2, 3, 150.))
    queue = (*pair, (3, 3, 100.))

    def add(name, positions, scripts, pace=None, environment=(), **kwargs):
        state, physical = field(positions, name)
        attempts = tuple(OvertakeAttempt(
            f"{name}-{i+1}", name, d, leader, physical.layout_version, at, at, end,
            cp, "S4_SYNTHETIC_FIXTURE",
        ) for i, (d, leader, cp, at, end) in enumerate(scripts))
        specs[name] = state, physical, pace or {1: 90., 2: 45.}, attempts, environment, kwargs

    one = (2, 1, 10, 0, 40_000_000)
    add("authorized_pair", pair, (one,))
    add("no_pass", pair, ())
    add("queue", queue, (one,), {1: 90., 2: 45., 3: 40.})
    add("successive_passes", queue, (one, (3, 1, 10, 7_600_000, 40_000_000)), {1: 90., 2: 45., 3: 40.})
    add("insufficient_pace", pair, (one,), {1: 90., 2: 95.})
    add("pit_cancel", pair, (one,), environment=(Event(7_500_000, EventKind.PIT_ROUTE_ENTRY, 1),))
    add("lapping", ((1, 2, 200.), (2, 3, 150.)), (one,))
    add("unlapping", ((1, 3, 200.), (2, 2, 150.)), ((2, 1, 7, 0, 40_000_000),))
    add("conflict", queue, (one, (3, 2, 10, 0, 40_000_000)), {1: 90., 2: 45., 3: 40.})
    add("prior_delay", pair, ((2, 1, 11, 10_100_000, 40_000_000),))
    add("expired", pair, ((2, 1, 10, 0, 7_000_000),))
    for flag in TrackState:
        if flag is not TrackState.GREEN:
            add(f"flag_{flag.value.lower()}", pair, ((2, 1, 10, 5_000_000, 40_000_000),),
                environment=(Event(5_000_000, EventKind.TRACK_STATE_CHANGE, track_state=flag),),
                end_us=30_000_000)
    add("unsupported", pair, (one,))
    state, physical, *rest = specs["unsupported"]
    specs["unsupported"] = (state, replace(physical, cars=(
        replace(physical.cars[0], progress_m=None, uncertainty_m=None, quality=PositionQuality.UNSUPPORTED),
        physical.cars[1],
    )), *rest)
    s2 = _synthetic()
    resolved = s2["resolved"]
    stop, = resolved.stops
    merged = next(frame.state for frame in reversed(resolved.run.trace) if frame.event.at_us == stop.merge_us)
    physical = resolved_rejoin_snapshot(resolved, stop, layout_version="s4-test-loop", length_m=900.)
    script = OvertakeAttempt("after-resolved-rejoin", merged.simulation_id, 63, 4, physical.layout_version,
                             stop.merge_us, stop.merge_us, stop.merge_us + 35_000_000, 8, "S2_RESOLVED")
    specs["pass_after_rejoin"] = merged, physical, {63: 60., 4: 92.}, (script,), (), {}
    clock = 155_000_000
    start = replace(next(f.state for f in reversed(resolved.run.trace) if f.event.at_us <= clock), race_clock_us=clock)
    positions = tuple(TrackPosition(c.driver_number, PhysicalRoute.RACING, c.completed_laps, c.grid_position,
                                    900. * (clock / 1e6 / (90 if c.driver_number == 63 else 92) % 1),
                                    0., quality=PositionQuality.EXACT_SCENARIO) for c in start.cars)
    snapshot = replace(physical, as_of_utc=start.race_start_utc + timedelta(microseconds=clock), cars=positions)
    itinerary = tuple(f.event for f in resolved.run.trace if f.event.driver_number == 63
                      and stop.route_entry_us <= f.event.at_us <= stop.merge_us
                      and (f.event.checkpoint is None or f.event.source in {"PIT_ROUTE_LINE", "COMMON_MERGE"}))
    specs["dynamic_rejoin"] = start, snapshot, {63: 60., 4: 92.}, (script,), itinerary, {}
    return specs, s2


def execute(spec, gap):
    initial, physical, pace, attempts, environment, options = spec
    return simulate_scripted_overtaking(initial, physical, pace, attempts, laps=2, minimum_gap_s=gap,
                                        environment=environment, **options)
