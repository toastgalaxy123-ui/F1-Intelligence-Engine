from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from f1ri.simulator.events import Event, EventKind
from f1ri.simulator.following import simulate_circular_following
from f1ri.simulator.overtaking import OvertakeAttempt, OvertakeStatus, simulate_scripted_overtaking
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


def field(positions=((1, 3, 200.), (2, 3, 150.))):
    clock = datetime(2025, 1, 1, tzinfo=UTC)
    cars = tuple(CarState(d, i, TyreState("MEDIUM", 2), completed_checkpoints=lap * 3 + int(m // 300),
                          last_crossing_us=0) for i, (d, lap, m) in enumerate(positions, 1))
    state = RaceState(1, clock, RaceRuleset(2025, 20), SimulationMode.CONDITIONAL_SIMULATION, "test",
                      cars, phase=RacePhase.RUNNING, track_state=TrackState.GREEN)
    physical = PhysicalSnapshot(1, clock, "test-loop", 900., tuple(
        TrackPosition(d, PhysicalRoute.RACING, lap, i, m, 0., quality=PositionQuality.EXACT_SCENARIO)
        for i, (d, lap, m) in enumerate(positions, 1)))
    return state, physical


def attempt(driver=2, target=1, checkpoint=10, proposed=0, *, name="a"):
    return OvertakeAttempt(name, "test", driver, target, "test-loop", proposed, proposed, 40_000_000,
                           checkpoint, "TEST_SCRIPT")


def run(state, physical, scripts, *, pace=None, **kwargs):
    return simulate_scripted_overtaking(state, physical, pace or {1: 90., 2: 45.}, scripts, laps=2, **kwargs)


def test_authorized_pass_commits_crossing_and_default_delegates_to_t1():
    state, physical = field()
    old = simulate_circular_following(state, physical, {1: 90., 2: 45.}, laps=2)
    unmodified = run(state, physical, ())
    assert (unmodified.run, unmodified.traversals) == (old.run, old.traversals)
    result = run(state, physical, (attempt(),))
    passed, = result.results
    assert passed.status is OvertakeStatus.COMPLETED
    assert passed.classification_before == (1, 2)
    assert passed.classification_after == (2, 1)
    assert passed.committed_us == 7_500_000
    assert result == run(state, physical, (attempt(),))
    assert [row.crossing_us for row in result.traversals if row.checkpoint == 10] == [7_500_000, 10_000_000]
    for row in result.traversals:
        assert row.effective_us == row.clean_us + row.traffic_delay_us
        assert row.traffic_delay_us >= 0


@pytest.mark.parametrize(("laps", "relation", "order"), [
    ((2, 3), "LAPPING", (2, 1)),
    ((3, 2), "BEING_LAPPED", (1, 2)),
])
def test_lapped_exchange_changes_physical_precedence_only(laps, relation, order):
    state, physical = field(((1, laps[0], 200.), (2, laps[1], 150.)))
    result = run(state, physical, (replace(attempt(checkpoint=laps[1]*3+1), expected_relationship=relation),))
    passed, = result.results
    assert passed.status is OvertakeStatus.COMPLETED
    assert passed.relationship == relation
    assert passed.classification_before == passed.classification_after == order


def test_three_car_queue_and_conflicting_attempts_are_deterministic():
    state, physical = field(((1, 3, 200.), (2, 3, 150.), (3, 3, 100.)))
    first, conflict = attempt(), attempt(3, 2, name="b")
    result = run(state, physical, (conflict, first), pace={1: 90., 2: 45., 3: 40.})
    assert result == run(state, physical, (first, conflict), pace={1: 90., 2: 45., 3: 40.})
    rejected, passed = result.results
    assert rejected.reason == "CONFLICT_WITH_PENDING_PASS"
    assert passed.status is OvertakeStatus.COMPLETED
    assert dict(passed.neighbors_after) == {1: 2, 2: 3, 3: 1}
    assert [row.driver_number for row in result.traversals if row.checkpoint == 10] == [2, 1, 3]


@pytest.mark.parametrize("flag", [f for f in TrackState if f is not TrackState.GREEN])
def test_neutralization_rejects_an_ordinary_pass(flag):
    state, physical = field()
    result = run(state, physical, (attempt(proposed=5_000_000),),
                 environment=(Event(5_000_000, EventKind.TRACK_STATE_CHANGE, track_state=flag),), end_us=20_000_000)
    assert result.results[0].reason == "NON_GREEN_OR_RACE_PHASE"


def test_pit_entry_takes_precedence_at_the_planned_pass_tick():
    state, physical = field()
    result = run(state, physical, (attempt(),),
                 environment=(Event(7_500_000, EventKind.PIT_ROUTE_ENTRY, 1),), end_us=30_000_000)
    assert result.results[0].status is OvertakeStatus.SUPERSEDED_BY_ROUTE_CHANGE
    assert all(frame.event.kind is not EventKind.ON_TRACK_PASS for frame in result.run.trace)
    assert any(frame.event.kind is EventKind.PIT_ROUTE_ENTRY for frame in result.run.trace)


def test_unsupported_and_expired_attempts_have_no_pass_crossing():
    state, physical = field()
    unknown = replace(physical.cars[0], progress_m=None, uncertainty_m=None, quality=PositionQuality.UNSUPPORTED)
    result = run(state, replace(physical, cars=(unknown, physical.cars[1])), (attempt(),))
    assert result.run is None and result.results[0].status is OvertakeStatus.UNSUPPORTED
    expired = run(state, physical, (replace(attempt(), window_end_us=7_000_000),))
    assert expired.results[0].status is OvertakeStatus.EXPIRED
    assert all(frame.event.kind is not EventKind.ON_TRACK_PASS for frame in expired.run.trace)


def test_two_distinct_passes_rewire_same_boundary_without_rewriting_prior_crossings():
    state, physical = field(((1, 3, 200.), (2, 3, 150.), (3, 3, 100.)))
    scripts = (attempt(), attempt(3, 1, proposed=7_600_000, name="second"))
    result = run(state, physical, scripts, pace={1: 90., 2: 45., 3: 40.})
    assert all(row.status is OvertakeStatus.COMPLETED for row in result.results)
    assert [row.driver_number for row in result.traversals if row.checkpoint == 10] == [2, 3, 1]
    assert dict(result.results[-1].neighbors_after) == {1: 3, 2: 1, 3: 2}


def test_post_pass_clean_floor_preserves_previously_committed_following_delay():
    state, physical = field()
    result = run(state, physical, (attempt(checkpoint=11, proposed=10_100_000),))
    first, second = [r for r in result.traversals if r.driver_number == 2][:2]
    assert (first.crossing_us, first.traffic_delay_us) == (10_100_000, 2_600_000)
    assert second.crossing_us == first.crossing_us + second.clean_us
    assert second.traffic_delay_us == 0
    later = run(state, physical, (attempt(checkpoint=11, proposed=10_200_000),))
    assert later.results[0].committed_us == 25_150_000


def test_s2_resolved_merge_can_initialize_later_script_and_coarse_cannot():
    fixtures = _synthetic()
    scenario = fixtures["resolved"]
    stop, = scenario.stops
    snapshot = resolved_rejoin_snapshot(scenario, stop, layout_version="test-loop", length_m=900.)
    state = next(frame.state for frame in reversed(scenario.run.trace) if frame.event.at_us == stop.merge_us)
    script = OvertakeAttempt("merge", state.simulation_id, 63, 4, "test-loop", stop.merge_us,
                             stop.merge_us, stop.merge_us + 35_000_000, 8, "S2_RESOLVED")
    result = run(state, snapshot, (script,), pace={63: 60., 4: 92.})
    assert result.results[0].status is OvertakeStatus.COMPLETED
    coarse = fixtures["coarse"]
    assert resolved_rejoin_snapshot(coarse, coarse.stops[0], layout_version="test-loop", length_m=900.) is None


def test_flag_change_cancels_pending_pass_without_retroactively_cancelling_completed_pass():
    state, physical = field()
    early = Event(7_500_000, EventKind.TRACK_STATE_CHANGE, track_state=TrackState.VSC)
    assert run(state, physical, (attempt(),), environment=(early,)).results[0].status is OvertakeStatus.CANCELLED
    late = replace(early, at_us=7_500_001)
    assert run(state, physical, (attempt(),), environment=(late,)).results[0].status is OvertakeStatus.COMPLETED


def test_red_flag_holds_checkpoint_progress_until_resumption():
    state, physical = field()
    red = Event(5_000_000, EventKind.TRACK_STATE_CHANGE, track_state=TrackState.RED)
    green = replace(red, at_us=15_000_000, track_state=TrackState.GREEN)
    result = run(state, physical, (attempt(),), environment=(red, green))
    assert result.results[0].status is OvertakeStatus.CANCELLED
    assert [r.crossing_us for r in result.traversals if r.checkpoint == 10] == [20_000_000, 20_100_000]


def test_live_s2_itinerary_removes_and_reinserts_pitting_car():
    scenario = _synthetic()["resolved"]
    stop, = scenario.stops
    clock = 155_000_000
    current = next(f.state for f in reversed(scenario.run.trace) if f.event.at_us <= clock)
    state = replace(current, race_clock_us=clock)
    positions = tuple(TrackPosition(c.driver_number, PhysicalRoute.RACING, c.completed_laps, c.grid_position,
                                    900. * (clock / 1e6 / (90 if c.driver_number == 63 else 92) % 1),
                                    0., quality=PositionQuality.EXACT_SCENARIO) for c in state.cars)
    physical = PhysicalSnapshot(1, state.race_start_utc + timedelta(microseconds=clock), "test-loop", 900., positions)
    environment = tuple(f.event for f in scenario.run.trace if f.event.driver_number == 63
                        and stop.route_entry_us <= f.event.at_us <= stop.merge_us
                        and (f.event.kind not in {EventKind.CHECKPOINT_CROSSING, EventKind.LAP_COMPLETED}
                             or f.event.source in {"PIT_ROUTE_LINE", "COMMON_MERGE"}))
    script = OvertakeAttempt("after-merge", state.simulation_id, 63, 4, "test-loop", stop.merge_us,
                             stop.merge_us, stop.merge_us + 50_000_000, 8, "S2_RESOLVED")
    result = simulate_scripted_overtaking(state, physical, {63: 60., 4: 92.}, (script,),
                                         laps=2, environment=environment)
    assert any(r["phase"] == "RESOLVED_REJOIN" for r in result.interactions)
    assert [f.event.at_us for f in result.run.trace if f.event.kind is EventKind.PIT_SERVICE_END] == [stop.service_end_us]
    assert result.results[0].status is OvertakeStatus.COMPLETED


def test_coarse_return_stops_precision_crossings_without_fabricating_merge():
    state, physical = field()
    replacement = TyreState("HARD", 0, 2)
    environment = (
        Event(10_000_000, EventKind.COARSE_PIT_TRANSITION, 1, tyres=replacement),
        Event(40_000_000, EventKind.LAP_COMPLETED, 1, 12, source="COARSE_PIT_ACCOUNTING"),
    )
    script = replace(attempt(checkpoint=13), proposed_us=42_000_000, window_start_us=42_000_000, window_end_us=60_000_000)
    result = run(state, physical, (script,), environment=environment)
    assert result.results[0].reason == "PAIR_NOT_ON_RACING_ROUTE"
    assert all(r.crossing_us < 40_000_000 for r in result.traversals)
    assert any(r["phase"] == "COARSE_REJOIN_POSITION_UNRESOLVED" for r in result.interactions)
