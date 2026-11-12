"""Initialize a circular neighborhood at a declared synthetic S2 rejoin."""
from __future__ import annotations

from datetime import timedelta

from f1ri.simulator.events import EventKind
from f1ri.simulator.pit_execution import PitScenarioRun, PitTransitionResult, RejoinStatus
from f1ri.simulator.proximity import PhysicalRoute, PhysicalSnapshot, PositionQuality, TrackPosition
from f1ri.simulator.state import CarStatus, Route, SimulationMode


def resolved_rejoin_snapshot(
    scenario: PitScenarioRun, stop: PitTransitionResult, *, layout_version: str, length_m: float,
) -> PhysicalSnapshot | None:
    """Use equal-distance checkpoints and declared linear motion within each sector."""
    if stop not in scenario.stops:
        raise ValueError("Stop does not belong to the supplied scenario")
    if stop.rejoin_status is not RejoinStatus.RESOLVED or stop.merge_us is None:
        return None
    if scenario.run.final_state.mode is not SimulationMode.CONDITIONAL_SIMULATION:
        raise ValueError("Rejoin positions require a declared synthetic timeline")
    driver = stop.plan.request.driver_number
    clock = stop.merge_us
    frames = scenario.run.trace
    # Use the final same-tick state to include its first-sector crossing.
    state = next(frame.state for frame in reversed(frames) if frame.event.at_us == clock)
    ranks = {number: i for i, number in enumerate(state.timing_order, 1)}
    rows = []
    for car in state.cars:
        number, laps, rank = car.driver_number, car.completed_laps, ranks[car.driver_number]
        if car.status is not CarStatus.ACTIVE or car.route is Route.OFF_TRACK:
            rows.append(TrackPosition(number, PhysicalRoute.OFF_TRACK, laps, rank))
            continue
        if car.route is not Route.ON_TRACK:
            route = PhysicalRoute.UNKNOWN if car.route is Route.PIT_TRANSITION_COARSE else PhysicalRoute.PIT
            rows.append(TrackPosition(number, route, laps, rank, reason=car.route.value))
            continue
        if number == driver:
            rows.append(TrackPosition(number, PhysicalRoute.RACING, laps, rank, length_m / 3, 0.,
                                      quality=PositionQuality.EXACT_SCENARIO))
            continue
        next_crossing = next((frame.event for frame in frames if frame.event.at_us > clock
                              and frame.event.driver_number == number
                              and (frame.event.checkpoint is not None or frame.event.kind in {
                                  EventKind.PIT_ROUTE_ENTRY, EventKind.COARSE_PIT_TRANSITION,
                                  EventKind.RETIREMENT,
                              })), None)
        before = car.last_crossing_us
        if (before is None or next_crossing is None
                or next_crossing.checkpoint != car.completed_checkpoints + 1):
            rows.append(TrackPosition(number, PhysicalRoute.RACING, laps, rank,
                                      reason="SYNTHETIC_CROSSING_BRACKET_MISSING"))
            continue
        duration = next_crossing.at_us - before
        fraction = (clock - before) / duration
        rows.append(TrackPosition(number, PhysicalRoute.RACING, laps, rank,
                                  (car.sector + fraction) * length_m / 3, 0.,
                                  length_m / 3 * 1e6 / duration, PositionQuality.EXACT_SCENARIO))
    return PhysicalSnapshot(state.session_key, state.race_start_utc + timedelta(microseconds=clock),
                            layout_version, length_m, tuple(rows))
