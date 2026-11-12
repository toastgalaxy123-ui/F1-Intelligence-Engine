import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from f1ri.features.physical_positions import LocalizationMode, estimate_location
from f1ri.features.track_geometry import LocationFix, TrackGeometry, forward_passages, map_fixes
from f1ri.ingestion.telemetry import TelemetryWindow, load_telemetry_window
from f1ri.simulator.following import effective_traversal_us, simulate_circular_following
from f1ri.simulator.proximity import (
    PhysicalRoute,
    PhysicalSnapshot,
    PositionQuality,
    TrackPosition,
    physical_neighborhoods,
)
from f1ri.simulator.state import CarState, CarStatus, Route, TyreState
from f1ri.validation.proximity_episodes import candidate_episodes
from f1ri.validation.proximity_pilot import _pit_route, synthetic_following
from f1ri.validation.proximity_timing_audit import retrospective_order

START = datetime(2025, 1, 1, tzinfo=UTC)


def time(seconds):
    return START + timedelta(seconds=seconds)


def track_position(driver, progress, laps, rank, spread=0):
    return TrackPosition(driver, PhysicalRoute.RACING, laps, rank, progress, spread, 50.,
                         PositionQuality.EXACT_SCENARIO)


def snapshot(*cars):
    return PhysicalSnapshot(1, START, "test", 1000., cars)


@pytest.mark.parametrize(("target_laps", "advantage", "encounter"),
                         [(9, 2, "LAPPING"), (10, 1, "LAPPING"), (11, 0, "SAME_LAP"),
                          (12, -1, "BEING_LAPPED"), (None, None, "LAP_RELATION_UNKNOWN")])
def test_wraparound_lap_advantage_is_independent_of_classification(target_laps, advantage, encounter):
    subject = track_position(63, 980, 10, 1)
    target = track_position(31, 20, target_laps, 19)
    other = track_position(4, 900, 10, 2)
    order = physical_neighborhoods(snapshot(other, target, subject))
    ahead = order[63].ahead
    assert (ahead.driver_number, ahead.gap_m, ahead.lap_advantage, ahead.encounter) == (
        31, 40, advantage, encounter,
    )
    assert ahead.approximate_gap_s == 0.8
    assert order[63].behind.driver_number == 4
    assert order[31].behind.lap_advantage == advantage
    assert order == physical_neighborhoods(snapshot(subject, other, target))


def test_route_changes_preserve_field_and_recompute_neighbors():
    a, b, c = (track_position(driver, progress, 2, driver)
               for driver, progress in ((1, 100), (2, 200), (3, 350)))
    pit = TrackPosition(2, PhysicalRoute.PIT, 2, 2)
    unknown = replace(pit, route=PhysicalRoute.UNKNOWN, reason="UNRESOLVED_REJOIN")
    assert physical_neighborhoods(snapshot(a, b, c))[1].ahead.driver_number == 2
    after_entry = physical_neighborhoods(snapshot(a, pit, c))
    assert set(after_entry) == {1, 2, 3}
    assert after_entry[1].ahead.driver_number == 3
    assert after_entry[2].ahead is None
    assert after_entry[1].coverage == "COMPLETE"
    coarse = physical_neighborhoods(snapshot(a, unknown, c))
    assert coarse[1].coverage == "SUBSET"
    assert coarse[2].ahead_reason == "UNRESOLVED_REJOIN"


def test_uncertainty_can_remove_physical_order_or_neighbor_identity():
    a = track_position(1, 995, 2, 1, 8)
    b = track_position(2, 4, 3, 2, 5)
    c = track_position(3, 60, 3, 3, 5)
    ambiguous = physical_neighborhoods(snapshot(a, b, c))[1]
    assert ambiguous.ahead is None
    assert ambiguous.ahead_reason == "CIRCULAR_ORDER_OVERLAP"
    near = replace(b, progress_m=50)
    assert physical_neighborhoods(snapshot(a, near, c))[1].ahead_reason == "NEAREST_NEIGHBOR_OVERLAP"
    known = physical_neighborhoods(snapshot(a, near, replace(c, progress_m=150)))[1].ahead
    assert (known.lower_m, known.gap_m, known.upper_m) == (42, 55, 68)


def test_crossing_geometry_uses_continuity_or_reports_an_ambiguous_branch():
    shape = ((0, 0), (200, 200), (0, 200), (200, 0), (0, 0))
    geometry = TrackGeometry("crossing", 1000., START, shape, 0.)
    crossing = LocationFix(time(1), 100, 100)
    assert map_fixes(geometry, (crossing,))[0].reason == "AMBIGUOUS_SEGMENT"
    path = map_fixes(geometry, (LocationFix(time(.5), 65, 65), crossing))
    assert path[0].progress_m < path[1].progress_m < 200


def test_passage_interpolates_across_the_seam():
    shape = ((0, 0), (100, 0), (100, 100), (0, 100), (0, 0))
    geometry = TrackGeometry("square", 400., START, shape, 0.)
    trace = map_fixes(geometry, (LocationFix(START, 0, 10), LocationFix(time(.5), 10, 0)))
    assert forward_passages(trace, 0, 400.) == (time(.25),)


def test_point_in_time_ignores_future_samples_and_late_arrivals():
    shape = ((0, 0), (100, 0), (100, 100), (0, 100), (0, 0))
    geometry = TrackGeometry("square", 400., START, shape, 0.)
    prefix = (LocationFix(START, 30, 0), LocationFix(time(.25), 40, 0))
    later = LocationFix(time(.5), 48, 0)
    delayed = LocationFix(time(.3), 44, 0, available_at_utc=time(.55))
    cutoff = time(.4)
    causal = LocalizationMode.POINT_IN_TIME
    expected = estimate_location(geometry, prefix, cutoff, causal)
    assert expected.progress_m == 46.
    assert expected.quality is PositionQuality.EXTRAPOLATED
    assert estimate_location(geometry, (*prefix, delayed, later), cutoff, causal) == expected
    assert estimate_location(geometry, (*prefix, replace(later, x=95)), cutoff, causal) == expected
    retro = estimate_location(geometry, (*prefix, later), cutoff, LocalizationMode.RETROSPECTIVE)
    assert retro.progress_m == pytest.approx(44.8)
    assert estimate_location(geometry, prefix, time(2), causal).reason == "STALE_LOCATION"
    assert estimate_location(geometry, (), cutoff, causal).reason == "MISSING_LOCATION_PREFIX"
    assert estimate_location(replace(geometry, available_at_utc=time(2)),
                             prefix, cutoff, causal).reason == "GEOMETRY_NOT_AVAILABLE_AT_CUTOFF"


def test_future_pit_report_only_changes_retrospective_route():
    from types import SimpleNamespace

    pit = SimpleNamespace(date=time(25), lane_duration=18)
    assert _pit_route(time(17), LocalizationMode.POINT_IN_TIME, (pit,))[0] is PhysicalRoute.RACING
    assert _pit_route(time(17), LocalizationMode.RETROSPECTIVE, (pit,))[0] is PhysicalRoute.PIT
    assert _pit_route(time(24), LocalizationMode.RETROSPECTIVE, (pit,))[0] is PhysicalRoute.UNKNOWN


def test_queue_conserves_each_traversal_and_holds_crossing_headway():
    physical, following = synthetic_following()
    assert following.run.capability == "S3"
    assert len(following.traversals) == len(following.run.trace) == 18
    ledger = {(row.driver_number, row.checkpoint): row for row in following.traversals}
    assert any(row.traffic_delay_us > 0 and row.driver_number == 63 for row in ledger.values())
    assert any(row.traffic_delay_us > 0 and row.driver_number == 4 for row in ledger.values())
    assert not any(row.traffic_delay_us for row in ledger.values() if row.driver_number == 31)
    for row in ledger.values():
        prior = ledger.get((row.driver_number, row.checkpoint - 1))
        assert row.crossing_us - (prior.crossing_us if prior else 0) == row.effective_us
        assert row.effective_us == row.clean_us + row.traffic_delay_us
        if row.predecessor_checkpoint is not None:
            ahead = ledger[row.predecessor_driver, row.predecessor_checkpoint]
            assert row.crossing_us - ahead.crossing_us >= 200_000
    assert physical_neighborhoods(physical)[63].ahead.encounter == "LAPPING"
    assert effective_traversal_us(18_000_000, 300_000, 18_500_000) == 18_500_000


def test_equal_pace_keeps_headway_and_missing_pace_keeps_the_field():
    physical, following = synthetic_following()
    paces = {63: 90., 31: 90., 4: 90.}
    unchanged = simulate_circular_following(following.initial, physical, paces, laps=2)
    assert all(row.traffic_delay_us == 0 for row in unchanged.traversals)
    assert paces == {63: 90., 31: 90., 4: 90.}
    unknown = simulate_circular_following(following.initial, physical, {63: 90., 4: 90.}, laps=2)
    assert unknown.run is None
    assert unknown.initial == following.initial
    assert unknown.unsupported == {31: "PACE_UNSUPPORTED"}


def test_reconciliation_applies_elapsed_penalties_and_classified_retirement():
    tyres = TyreState("HARD", 20)
    first = CarState(1, 1, tyres, completed_checkpoints=216, last_crossing_us=100_000_000)
    penalized = replace(first, driver_number=2, grid_position=2, last_crossing_us=99_000_000)
    retired = replace(first, driver_number=3, grid_position=3, status=CarStatus.RETIRED,
                      route=Route.OFF_TRACK, completed_checkpoints=192)
    below = replace(retired, driver_number=4, grid_position=4, completed_checkpoints=191)
    assert retrospective_order((below, retired, penalized, first), 72, {2: 15}) == {1: 1, 2: 2, 3: 3}


def test_episode_is_split_at_quality_gap_and_keeps_only_complete_green_exposure():
    base = {"window": "pilot", "mode": "POINT_IN_TIME", "driver_number": 63,
            "ahead": 18, "encounter": "LAPPING", "layout_version": "test", "session_key": 1,
            "coverage": "COMPLETE", "track_state": "GREEN", "gap_lower_m": 40, "gap_upper_m": 80}
    points = [base | {"as_of_utc": time(i / 2)} for i in range(10)]
    points[4]["coverage"] = "SUBSET"
    result, = candidate_episodes(points, maximum_gap_upper_m=120, minimum_duration_s=2)
    assert (result["start_utc"], result["duration_s"], result["samples"]) == (time(2.5), 2., 5)
    assert result["label_status"] == "DESCRIPTIVE_PILOT_ONLY"


def test_bounded_telemetry_removes_conflicts_and_validates_before_caching(tmp_path):
    window = TelemetryWindow(10, 63, START, time(2))

    class Provider:
        def fetch(self, endpoint, params):
            assert endpoint == "location" and params["driver_number"] == 63
            a = {"session_key": 10, "driver_number": 63, "date": time(.5).isoformat(),
                 "x": 10, "y": 20, "z": 0}
            b = a | {"date": time(1).isoformat()}
            return json.dumps([a, a, b, b | {"x": 11}]).encode(), "https://example.com/telemetry"

    frame, provenance = load_telemetry_window(tmp_path, window, "location", client=Provider())
    assert list(frame.x) == [10]
    assert provenance["conflicting_rows"] == 2
    assert load_telemetry_window(tmp_path, window, "location")[1] == provenance

    class IncorrectProvider(Provider):
        def fetch(self, endpoint, params):
            payload, url = super().fetch(endpoint, params)
            return payload.replace(b'"driver_number": 63', b'"driver_number": 44'), url

    invalid_root = tmp_path / "invalid"
    with pytest.raises(ValueError, match="outside the declared identity"):
        load_telemetry_window(invalid_root, window, "location", client=IncorrectProvider())
    assert not invalid_root.exists()
    with pytest.raises(ValueError, match="up to 180 seconds"):
        TelemetryWindow(10, 63, START, time(181))
