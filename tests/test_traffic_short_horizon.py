from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from f1ri.features.track_geometry import LocationFix, MappedFix, forward_passages
from f1ri.validation.traffic_segment_reference import earlier, predict, split_drivers
from f1ri.validation.traffic_short_horizon import (
    crossings,
    cutoff_state,
    screen_support,
    sector_boundaries,
    timing_rows,
)
from f1ri.validation.traffic_short_horizon_study import group_segments

START = datetime(2025, 6, 29, tzinfo=UTC)


def at(seconds):
    return START + timedelta(seconds=seconds)


def test_sector_boundaries_are_cumulative_and_stop_at_missing_duration():
    assert sector_boundaries(START, [19, 30, 21]) == [START, at(19), at(49), at(70)]
    assert sector_boundaries(START, [19, None, 21]) == [START, at(19)]


def test_wraparound_passage_keeps_bracket_and_delayed_availability():
    trace = [MappedFix(LocationFix(at(0), 0, 0), 990, 7, 0),
             MappedFix(LocationFix(at(.5), 1, 0, at(.7)), 10, 7, 0)]
    assert forward_passages(trace, 0, 1000) == (at(.25),)
    assert crossings(trace, 1000, 1000) == [{"at": at(.25), "available": at(.7), "bracket_s": .5}]
    late = MappedFix(LocationFix(at(1.001), 1, 0), 10, 7, 0)
    assert crossings([trace[0], late], 0, 1000) == []


def test_fixed_distance_cutoff_waits_for_crossing_confirmation_and_censors_missing_exit():
    lap = SimpleNamespace(lap_start_utc=START, sector_1_s=20, sector_2_s=25, sector_3_s=25)
    next_lap = SimpleNamespace(lap_start_utc=at(70))
    event = lambda seconds: {"at": at(seconds), "available": at(seconds + .2), "bracket_s": .4}
    index = {0: [event(0), event(70)], 250: [event(16)], 500: [], 750: [event(54)],
             300: [event(20)], 600: [event(45)], 1000: [event(0), event(70)]}
    out = []
    timing_rows(lap, next_lap, [0, 300, 600, 0], [0, 250, 500, 750, 1000], index,
                {"sampling": {"maximum_sector_marker_disagreement_seconds": 1}}, lambda *r: out.append(r))
    assert ("H2", 0, START, at(.2), at(16), at(16.2)) == out[3][:6]
    assert out[4][0:2] == ("H2", 1)
    assert out[4][4] is None and not out[4][-1]["valid"]


def test_cutoff_selection_uses_visible_prefix():
    rows = [{"state": "CLEAR", "ahead": 63, "gap_m": 250},
            {"state": "CLOSE", "ahead": 63, "gap_m": 80}]
    config = {"sampling": {"maximum_cutoff_sample_age_seconds": .5}, "entry_upper_m": 120}
    assert cutoff_state(rows, [at(0), at(.5)], at(.25), config)[1] == "CLEAR"
    assert cutoff_state(rows, [at(0), at(.5)], at(.5), config)[1] == "CLOSE_SUPPORTED"
    assert cutoff_state(rows, [at(0), at(.5)], at(1.1), config)[1] == "PHYSICAL_STATE_UNRESOLVED"


@pytest.mark.parametrize(("fault", "value", "expected_route", "expected_support"), [
    ("follower_retro_route", "PIT", False, False),
    ("leader_retro_route", "PIT", False, False),
    ("state", "UNSUPPORTED", True, False),
    ("ahead", 55, True, True),
])
def test_support_survival_records_route_gaps_and_neighbor_changes(fault, value, expected_route, expected_support):
    clocks = [at(i / 2) for i in range(10)]
    rows = [{"as_of_utc": clock, "state": "CLOSE", "ahead": 63, "coverage": "COMPLETE", "gap_upper_m": 95,
             "follower_retro_route": "RACING", "leader_retro_route": "RACING", "target_supported": True}
            for clock in clocks]
    for i in range(3, 8):
        rows[i][fault] = value
        rows[i]["target_supported"] = fault == "ahead"
    stats = screen_support(rows, clocks, at(0), at(5), at(0), rows[0], "CLOSE_SUPPORTED", .5,
                           {"minimum_support_fraction": .9, "maximum_unsupported_gap_seconds": 2.})
    assert stats["route_continuous"] is expected_route
    assert stats["support_pass"] is expected_support
    assert stats["neighbor_changed"] is (fault == "ahead")
    assert stats["same_leader_close_at_start_s"] == 1.5


def test_reference_never_uses_target_or_evidence_available_at_cutoff():
    def row(identity, available):
        return {"opportunity_id": identity, "driver_number": 6, "stint_number": 3, "tyre_age": 7,
                "compound": "HARD", "race_progress": .3, "track_temperature_c": 32.,
                "d8_lap_s": 70., "d8_lap_std_s": .3, "as_of_utc": at(30),
                "outcome": {"available_by_utc": available, "observed_s": 20.,
                            "full_lap_observed_s": 70., "stage1_clean_lap": True}}
    target = row("target", at(25))
    train = earlier(target, [target, row("early", at(29)), row("equal", at(30)), row("future", at(31))])
    assert [r["opportunity_id"] for r in train] == ["early"]
    config = {"minimum_pool": 1, "minimum_same_stint": 1, "hierarchical_shrinkage": 6,
              "maximum_tyre_age_delta": 5, "maximum_progress_delta": .2, "maximum_track_temperature_delta_c": 5}
    result = predict(target, train, train, config)
    assert set(result) == {"R0", "R2", "R3"}
    assert result["R2"][0] == 20
    pair = train + [row("earlier", at(28))]
    assert predict(target, pair, pair, config)["R1"][0] == 20


def test_fragmented_overlapping_segments_share_one_conservative_group():
    rows = [{"session_key": 9955, "driver_number": 6, "physical_leader": 63, "target_start_utc": at(i),
             "opportunity_id": str(i), "outcome": {"following_label": True, "retrospective_stint_number": 3,
                                                   "episode_id": str(i), "end_utc": at(i + 10)}}
            for i in range(3)]
    group, = group_segments(rows)
    assert group["overlaps_with_earlier"] == 2
    assert group["episode_fragments"] == ["0", "1", "2"]
    split = {"seed": 21, "validation_driver_fraction": .4}
    drivers = [{"driver_number": i} for i in range(10)]
    assert split_drivers(drivers, split) == split_drivers(list(reversed(drivers)), split)
