from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from f1ri.simulator.events import EventKind
from f1ri.simulator.pit_execution import (
    PitCarPlan,
    PitStopRequest,
    PitTransitionPlan,
    RejoinStatus,
    ReplacementTyres,
    ResolvedPitRoute,
    simulate_pit_scenario,
)
from f1ri.simulator.pit_proximity import resolved_rejoin_snapshot
from f1ri.simulator.proximity import physical_neighborhoods
from f1ri.simulator.state import (
    CarState,
    RacePhase,
    RaceRuleset,
    RaceState,
    Route,
    SimulationMode,
    TrackState,
    TyreState,
)

START = datetime(2025, 10, 5, tzinfo=UTC)


def grid(distance=5):
    return RaceState(
        9896, START, RaceRuleset(2025, distance), SimulationMode.CONDITIONAL_SIMULATION,
        "synthetic", (CarState(63, 1, TyreState("MEDIUM", 2)),
                      CarState(4, 2, TyreState("MEDIUM", 1))), track_state=TrackState.GREEN,
    )


def route(**changes):
    return replace(ResolvedPitRoute(70, 74, 92, 3, 90, 103, 121, 157, 200, "A"),
                   **changes)


def stop(driver=63, *, timing=None, net=None, lap=2, replacement=None, cutoff=START):
    tires = replacement or ReplacementTyres("HARD", 4, "C2", "used-hard")
    request = PitStopRequest(9896, driver, cutoff, lap, tires, "test")
    return PitTransitionPlan(request, route=timing, net_loss_s=net)


def test_resolved_two_car_stop_conserves_time_and_invalidates_normal_route():
    pit = stop(timing=route(timing_line_s=99))
    scenario = simulate_pit_scenario(grid(), (
        PitCarPlan(63, 0, (90.,) * 5, (pit,)),
        PitCarPlan(4, 0, (92.,) * 5),
    ))
    result, = scenario.stops
    assert result.route_entry_us == 160_000_000
    assert result.service_start_us == 182_000_000
    assert result.service_end_us == 185_000_000
    assert result.lane_exit_us - result.lane_entry_us == 29_000_000
    assert result.merge_us == 211_000_000
    assert result.normal_two_lap_us == 180_000_000
    assert result.elapsed_two_lap_us == 200_000_000
    assert result.net_loss_us == 20_000_000
    assert result.rejoin_status is RejoinStatus.RESOLVED
    assert result.passages[0].driver_number == 4
    assert result.passages[0].delta_us == 3_666_667
    crossings = {f.event.checkpoint: f for f in scenario.run.trace if f.event.driver_number == 63
                 and f.event.kind is EventKind.LAP_COMPLETED}
    assert len(crossings) == 5
    assert crossings[6].event.at_us == 189_000_000
    assert crossings[6].event.generation == 1
    in_lap_car = crossings[6].state.cars[0]
    assert in_lap_car.route is Route.PIT_LANE
    assert in_lap_car.tyres.age_laps == 4
    assert crossings[9].state.cars[0].tyres.age_laps == 5
    assert scenario.run.final_state.cars[0].finish_us == 470_000_000
    assert scenario.run.final_state.timing_order == (4, 63)


@pytest.mark.parametrize("net", (22.5, -1.25))
def test_coarse_loss_posts_once_without_physical_timestamps(net):
    field = simulate_pit_scenario(grid(), (
        PitCarPlan(63, 0, (90., 90., 95., 95., 95.), (stop(net=net),)),
        PitCarPlan(4, 0, (92.,) * 5),
    ))
    result, = field.stops
    assert result.elapsed_two_lap_us == round((185 + net) * 1_000_000)
    assert result.net_loss_us == round(net * 1_000_000)
    assert result.rejoin_status is RejoinStatus.UNRESOLVED
    assert result.route_entry_us is result.service_start_us is result.merge_us is None
    assert field.run.final_state.cars[0].tyres.age_laps == 7
    pitter = [frame for frame in field.run.trace if frame.event.driver_number == 63]
    assert not any(frame.event.checkpoint in (7, 8) for frame in pitter)
    fit = next(frame for frame in pitter if frame.event.kind is EventKind.COARSE_PIT_TRANSITION)
    assert fit.state.cars[0].route is Route.PIT_TRANSITION_COARSE
    assert resolved_rejoin_snapshot(field, result, layout_version="synthetic", length_m=900.) is None


def test_resolved_s2_merge_initializes_physical_neighbors():
    scenario = simulate_pit_scenario(grid(), (
        PitCarPlan(63, 0, (90.,) * 5, (stop(timing=route(timing_line_s=99)),)),
        PitCarPlan(4, 0, (92.,) * 5),
    ))
    stop_result, = scenario.stops
    physical = resolved_rejoin_snapshot(scenario, stop_result, layout_version="synthetic", length_m=900.)
    assert physical is not None and physical.complete
    assert physical.as_of_utc == START + timedelta(seconds=211)
    order = physical_neighborhoods(physical)
    assert order[4].ahead.driver_number == 63
    assert order[4].ahead.gap_m == pytest.approx(35.869568, abs=1e-5)


def test_shared_box_orders_arrivals_and_wait_is_charged_once():
    plans = (PitCarPlan(63, 0, (90.,) * 5, (stop(timing=route()),)),
             PitCarPlan(4, 0, (90.,) * 5, (stop(4, timing=route()),)))
    shared = simulate_pit_scenario(grid(), plans)
    assert shared == simulate_pit_scenario(grid(), plans[::-1])
    first, second = shared.stops
    assert second.service_start_us == first.service_end_us
    assert (first.queue_wait_us, second.queue_wait_us) == (0, 3_000_000)
    assert (first.net_loss_us, second.net_loss_us) == (20_000_000, 23_000_000)
    parallel = simulate_pit_scenario(grid(), (plans[0], replace(
        plans[1], stops=(stop(4, timing=route(box_id="B")),),
    )))
    a, b = parallel.stops
    assert a.service_start_us == b.service_start_us
    assert a.net_loss_us == b.net_loss_us == 20_000_000
    assert a.rejoin_status is b.rejoin_status is RejoinStatus.UNRESOLVED


def test_merge_uncertainty_and_lap_counts_are_independent():
    field = simulate_pit_scenario(grid(), (
        PitCarPlan(63, 0, (90.,) * 5, (stop(timing=route(timing_uncertainty_s=5)),)),
        PitCarPlan(4, 0, (160., 150., 110., 110., 110.)),
    ))
    result, = field.stops
    # The other car passes the same physical sector-1 marker on a different lap.
    assert result.passages[0].completed_laps == 1
    assert result.passages[0].delta_us == -1_000_000
    assert result.rejoin_status is RejoinStatus.UNRESOLVED
    assert field.run.final_state.cars[1].completed_laps < field.run.final_state.cars[0].completed_laps


def test_overlapping_coarse_transition_blocks_precise_resolved_order():
    field = simulate_pit_scenario(grid(), (
        PitCarPlan(63, 0, (90.,) * 5, (stop(timing=route()),)),
        PitCarPlan(4, 0, (92.,) * 5, (stop(4, net=20),)),
    ))
    assert field.stops[0].merge_us == 211_000_000
    assert field.stops[0].rejoin_status is RejoinStatus.UNRESOLVED
    assert field.stops[1].merge_us is None


def test_resolved_pit_exchange_is_classified_as_a_pit_cycle():
    field = simulate_pit_scenario(grid(), (
        PitCarPlan(63, 0, (90.,) * 5, (stop(timing=route(merge_s=127)),)),
        PitCarPlan(4, 0, (92.,) * 5),
    ))
    result, = field.stops
    assert result.passages[0].delta_us == -2_333_333
    assert result.position_change_cause == "PIT_CYCLE_POSITION_CHANGE"
    assert all(frame.event.kind is not EventKind.ON_TRACK_PASS for frame in field.run.trace)


def test_neutralized_pit_requires_explicit_synthetic_timing():
    initial = replace(grid(), track_state=TrackState.VSC)
    coarse = stop(net=12)
    coarse = replace(coarse, request=replace(coarse.request, track_state=TrackState.VSC))
    field = simulate_pit_scenario(initial, (
        PitCarPlan(63, 0, (90.,) * 5, (coarse,)), PitCarPlan(4, 0, (92.,) * 5),
    ))
    assert field.stops[0].net_loss_us == 12_000_000
    assert field.run.final_state.track_state is TrackState.VSC
    with pytest.raises(ValueError, match="synthetic timing"):
        replace(coarse, cost_source="P1")


def test_repeated_same_compound_stops_require_distinct_declared_sets():
    fresh = ReplacementTyres("MEDIUM", set_id="m1")
    used = ReplacementTyres("MEDIUM", 3, set_id="m2")
    first = stop(net=4, replacement=fresh)
    second = stop(net=5, lap=4, replacement=used)
    plans = (PitCarPlan(63, 0, (90.,) * 6, (first, second), (fresh, used)),
             PitCarPlan(4, 0, (91.,) * 6))
    field = simulate_pit_scenario(grid(6), plans)
    assert field.stops[0].new_stint_id == field.stops[1].old_stint_id
    assert field.run.final_state.cars[0].tyres == TyreState("MEDIUM", 5, 3, None, "m2", 3, 5)
    with pytest.raises(ValueError, match="inventory"):
        simulate_pit_scenario(grid(6), (replace(plans[0], available_sets=(fresh,)), plans[1]))


def test_running_partial_lap_finishes_once_before_wholly_future_pit():
    state = grid()
    clock = 110_000_000
    cutoff = START + timedelta(microseconds=clock)
    cars = tuple(replace(car, completed_checkpoints=3, last_crossing_us=90_000_000)
                 for car in state.cars)
    state = replace(state, cars=cars, phase=RacePhase.RUNNING, race_clock_us=clock)
    plans = (PitCarPlan(63, 90_000_000, (90.,) * 4, (stop(net=20, lap=3, cutoff=cutoff),)),
             PitCarPlan(4, 90_000_000, (91.,) * 4))
    field = simulate_pit_scenario(state, plans)
    laps = [f.event.checkpoint // 3 for f in field.run.trace
            if f.event.driver_number == 63 and f.event.kind is EventKind.LAP_COMPLETED]
    assert laps == [2, 3, 4, 5]
    with pytest.raises(ValueError, match="partial"):
        simulate_pit_scenario(state, (replace(plans[0], stops=(stop(net=20, cutoff=cutoff),)),
                                      plans[1]))


def test_chequered_flag_can_censor_a_two_lap_pit_window():
    field = simulate_pit_scenario(grid(3), (
        PitCarPlan(63, 0, (90.,) * 3, (stop(timing=route()),)),
        PitCarPlan(4, 0, (50.,) * 3),
    ))
    result, = field.stops
    assert field.run.final_state.chequered_us == 150_000_000
    assert field.run.final_state.cars[0].completed_laps == 2
    assert result.timing_quality == "INCOMPLETE"
    assert result.elapsed_two_lap_us is result.net_loss_us is result.new_stint_id is None
    assert result.rejoin_status is RejoinStatus.UNRESOLVED
