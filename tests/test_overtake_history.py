from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from f1ri.features.overtake_events import classify_reversal, pair_points, reversal_candidates
from f1ri.features.overtake_opportunities import (
    group_encounters,
    outcome,
    sample_opportunities,
    seeded_score,
)
from f1ri.validation.overtake_historical_study import (
    _deduplicate_events,
    _route_order_changes,
    ordering_audit,
)

START = datetime(2025, 8, 3, 13, tzinfo=UTC)
GEOMETRY = {"length_m": 1000., "sector_markers_m": [0., 320., 650.]}
CONFIG = {
    "event_max_bracket_s": 5, "event_max_speed_mps": 120, "event_max_fix_age_s": 1,
    "gap_thresholds_m": [40, 80, 120], "seed": 19, "random_reference_fraction": .2,
    "random_reference_max_m": 300,
}


def clock(seconds):
    return START + timedelta(seconds=seconds)


def position(at, progress, laps, *, error=4, route="RACING", rank=3):
    return {"progress_m": progress, "completed_laps": laps, "uncertainty_m": error,
            "route": route, "lap_aligned": True, "source_age_s": .2, "observation_utc": at,
            "layout_version": "test", "quality": "INTERPOLATED", "speed_mps": 50, "classification": rank}


def neighborhood(target, gap=30, relation="SAME_LAP"):
    return {"ahead": target, "coverage": "COMPLETE", "gap_m": gap, "gap_lower_m": gap-8,
            "gap_upper_m": gap+8, "encounter": relation}


def reversal(laps=(3, 3), error=4, jump=False, unresolved=False):
    frames = {
        clock(0): {1: position(clock(0), 940, laps[0], error=error),
                   2: position(clock(0), 970, laps[1], error=error)},
        clock(2): {1: position(clock(2), 60 if not jump else 500, laps[0]+1, error=error),
                   2: position(clock(2), 30 if not jump else 470, laps[1]+1, error=error)},
    }
    neighbors = {clock(0): {1: neighborhood(2)}, clock(2): {2: neighborhood(1 if not unresolved else None)}}
    return frames, neighbors


@pytest.mark.parametrize(("laps", "kind"), [
    ((3, 3), "SAME_LAP_RACING_PASS"), ((4, 3), "LAPPING_PASS"), ((2, 3), "UNLAPPING_PASS"),
])
def test_bracket_through_start_finish(laps, kind):
    points = pair_points(*reversal(laps), GEOMETRY, 120)
    event, = reversal_candidates(points, GEOMETRY, CONFIG)
    assert event["event_time_lower_utc"] == clock(0)
    assert event["event_time_upper_utc"] == clock(2)
    assert event["event_time_bracket_s"] == 2
    assert event["first_confirmed_ahead_utc"] == clock(2)
    assert {key for key in event if key.startswith("event_time")} == {
        "event_time_lower_utc", "event_time_upper_utc", "event_time_bracket_s", "event_time_quality"}
    assert classify_reversal(event, [], True, .5) == (kind, "A", [])


@pytest.mark.parametrize(("kwargs", "kind"), [
    ({"error": 17}, "UNCERTAIN_PHYSICAL_REVERSAL"),
    ({"unresolved": True}, "UNCERTAIN_PHYSICAL_REVERSAL"),
    ({"jump": True}, "ARTIFACT_OR_UNRESOLVED"),
])
def test_unsupported_sign_changes_are_retained_without_a_pass(kwargs, kind):
    event, = reversal_candidates(pair_points(*reversal(**kwargs), GEOMETRY, 120), GEOMETRY, CONFIG)
    assert classify_reversal(event, [], False, 1)[0] == kind


def test_classification_flip_and_cycle_rotation_cannot_create_pass():
    frames, neighbors = reversal()
    frames[clock(2)] = {1: position(clock(2), 30, 4, rank=1), 2: position(clock(2), 60, 4, rank=2)}
    neighbors[clock(2)] = {1: neighborhood(2)}
    assert not list(reversal_candidates(pair_points(frames, neighbors, GEOMETRY, 120), GEOMETRY, CONFIG))

    row = {"status": "COMPLETED", "attempt_id": "a", "classification_before": [2, 1],
           "classification_after": [1, 2], "physical_before": [1, 2], "physical_after": [1, 2],
           "pair_at_boundary_before": [2, 1], "pair_at_boundary_after": [1, 2]}
    trace = [{"classification": [2, 1], "event": {"kind": "OVERTAKE_ATTEMPT"}},
             {"classification": [1, 2],
              "event": {"kind": "ON_TRACK_PASS", "attempt_id": "a", "at_us": 1, "driver_number": 1}}]
    result, = ordering_audit({"test": [row]}, {"test": trace})
    assert result["attacker_anchored_cycle_pre"] == result["attacker_anchored_cycle_post"]
    assert result["checkpoint_pair_front_to_back_pre"] != result["checkpoint_pair_front_to_back_post"]


@pytest.mark.parametrize(("cause", "kind"), [
    ("CENSORED_PIT", "PIT_INDUCED_ORDER_CHANGE"),
    ("CENSORED_RETIREMENT", "RETIREMENT_INDUCED_CHANGE"),
    ("CENSORED_TRACK_STATE", "RACE_CONTROL_ORDER_CHANGE"),
])
def test_competing_event_cannot_be_a_competitive_pass(cause, kind):
    event, = reversal_candidates(pair_points(*reversal(), GEOMETRY, 120), GEOMETRY, CONFIG)
    assert classify_reversal(event, [(clock(1), cause)], True, 1) == (kind, "EXCLUDED", [cause])


def test_wider_bracket_and_duplicate_suppression():
    frames, neighbors = reversal()
    frames[clock(4)] = frames.pop(clock(2))
    neighbors[clock(4)] = neighbors.pop(clock(2))
    event, = reversal_candidates(pair_points(frames, neighbors, GEOMETRY, 120), GEOMETRY, CONFIG)
    assert event["event_time_bracket_s"] == 4
    event.update(session=1, window="w", source="s", tier="B", event_type="SAME_LAP_RACING_PASS")
    earlier = event | {"window": "v", "event_time_upper_utc": clock(3), "event_time_bracket_s": 3}
    single, = _deduplicate_events([event, earlier])
    assert single["window"] == "v"
    assert len(single["supporting_observations"]) == 2


class CutoffHistory:
    def green_at(self, at):
        return at != clock(2)

    def cutoff_context(self, d, target, at, laps):
        return {"context_observed_utc": at}

    def active_at(self, drivers, at):
        return True


def test_sampling_uses_prefix_and_source_clocks_only():
    frames = {clock(t): {1: position(clock(t), 100+t*60, 3), 2: position(clock(t), 135+t*50, 3)}
              for t in range(4)}
    frames[clock(3)][1]["observation_utc"] = clock(4)
    neighbors = {clock(t): {1: neighborhood(2, 35-t*10, "LAPPING" if t == 1 else "SAME_LAP")}
                 for t in range(4)}
    args = (GEOMETRY, CutoffHistory(), CONFIG)
    left = list(sample_opportunities(1, "w", {clock(0): frames[clock(0)]}, neighbors, *args, Counter()))
    right = list(sample_opportunities(1, "w", frames, neighbors, *args, Counter()))
    assert right[:1] == left
    assert [r["cutoff_utc"] for r in right] == [clock(0), clock(1)]
    assert "D_LAPPED" in right[1]["arms"]
    assert seeded_score(19, 1, clock(0)) == seeded_score(19, 1, clock(0))
    assert seeded_score(20, 1, clock(0)) != seeded_score(19, 1, clock(0))


def following_frames():
    frames = {clock(t): {1: position(clock(t), 100+t*50, 3), 2: position(clock(t), 130+t*50, 3)}
              for t in range(6)}
    neighbors = {at: {1: neighborhood(2)} for at in frames}
    return frames, neighbors


@pytest.mark.parametrize("cause", ["CENSORED_PIT", "CENSORED_RETIREMENT", "CENSORED_TRACK_STATE"])
def test_outcome_records_competing_event(cause):
    frames, neighbors = following_frames()
    row = {"opportunity_id": "o", "cutoff_utc": clock(0), "attacker": 1, "target": 2}
    result = outcome(row, clock(5), frames, neighbors, sorted(frames), [], [(clock(2.2), cause)], 1, 1000)
    assert result["outcome"] == cause
    assert result["observed_duration_s"] == 2.2
    assert not result["fully_observed"]


@pytest.mark.parametrize(("case", "expected"), [
    ("normal", "NO_PASS_OBSERVED"), ("short", "HORIZON_INCOMPLETE"),
    ("localization", "CENSORED_LOCALIZATION"), ("neighbor", "CENSORED_NEIGHBOR_CHANGE"),
    ("supported", "PASS_OBSERVED"), ("straddle", "AMBIGUOUS_OUTCOME"),
    ("overlap", "AMBIGUOUS_OUTCOME"),
])
def test_outcomes(case, expected):
    frames, neighbors = following_frames()
    row = {"opportunity_id": "o", "cutoff_utc": clock(0), "attacker": 1, "target": 2}
    events = []
    planned = clock(5)
    if case == "short":
        frames.pop(clock(5))
    if case == "localization":
        frames[clock(2)].pop(2)
    if case == "neighbor":
        neighbors[clock(2)][1] = neighborhood(3)
    if case in ("supported", "straddle"):
        if case == "straddle":
            planned = clock(2.5)
        events.append({"pass_event_id": "one-pass", "event_time_lower_utc": clock(2),
                       "event_time_upper_utc": clock(3), "event_type": "SAME_LAP_RACING_PASS"})
        frames[clock(3)][1]["progress_m"] += 60
        neighbors[clock(3)][2] = neighborhood(1)
    if case == "overlap":
        frames[clock(2)][1]["progress_m"] += 29
    result = outcome(row, planned, frames, neighbors, sorted(frames), events, [], 1, 1000)
    assert result["outcome"] == expected
    if events:
        assert result["pass_event_id"] == "one-pass"


def test_overlapping_outcomes_and_fragmented_encounters():
    frames, neighbors = following_frames()
    frames[clock(3)][1]["progress_m"] += 60
    neighbors[clock(3)][2] = neighborhood(1)
    rows = [{"opportunity_id": str(t), "cutoff_utc": clock(t), "attacker": 1, "target": 2, "session": 1}
            for t in (0, 1, 20, 70)]
    events = [{"pass_event_id": "pass", "event_time_lower_utc": clock(2),
               "event_time_upper_utc": clock(3), "event_type": "SAME_LAP_RACING_PASS"}]
    assert {outcome(r, clock(5), frames, neighbors, sorted(frames), events, [], 1, 1000)["pass_event_id"]
            for r in rows[:2]} == {"pass"}
    groups = group_encounters(rows, 30)
    assert [r["opportunities"] for r in groups] == [3, 1]
    assert group_encounters(list(reversed(rows)), 30) == groups


def test_other_archive_bracket_cannot_override_contradictory_future_sample():
    frames, neighbors = following_frames()
    row = {"opportunity_id": "o", "cutoff_utc": clock(0), "attacker": 1, "target": 2}
    event = {"pass_event_id": "external", "event_time_lower_utc": clock(2),
             "event_time_upper_utc": clock(3), "event_type": "SAME_LAP_RACING_PASS"}
    result = outcome(row, clock(5), frames, neighbors, sorted(frames), [event], [], 1, 1000)
    assert result["outcome"] == "AMBIGUOUS_OUTCOME"


def test_pit_neighbor_change_bridges_uncertain_route_samples():
    frames, neighbors = following_frames()
    frames[clock(3)][2]["route"] = "UNKNOWN"
    frames[clock(4)][2]["route"] = "PIT"
    neighbors[clock(4)][1] = neighborhood(3)

    class History:
        def __init__(self):
            self.pits = {2: [(clock(2.5), clock(5), clock(5))]}
            self.retirements = {}

        def interruptions(self, drivers, start, end):
            return [(clock(2.5), "CENSORED_PIT")]

    context = "s", 1, "w", GEOMETRY, {"RETROSPECTIVE": (frames, neighbors)}, 1
    change, = _route_order_changes(context, History())
    assert change["before_utc"] == clock(2)
    assert change["after_utc"] == clock(4)
    assert change["event_type"] == "PIT_INDUCED_ORDER_CHANGE"
    assert "pass_event_id" not in change
