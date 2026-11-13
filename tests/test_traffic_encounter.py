from datetime import UTC, datetime, timedelta

import pytest

from f1ri.validation.traffic_encounter_audit import (
    close_runs,
    encounter_states,
    support_sample,
    support_summary,
)
from f1ri.validation.traffic_encounter_study import selector, yield_bounds

START = datetime(2025, 6, 29, tzinfo=UTC)
EXPOSURE = {"entry_upper_m": 120, "clean_lower_m": 225}
PLAN = {
    "seed": 10,
    "selector": {"maximum_age_s": .5, "replay_prefix_seconds": 8, "replay_suffix_seconds": 110,
                 "priority": ["SUPPORTED_CLOSE_AT_LAP_START",
                                                  "PHYSICAL_APPROACH_PRIORITY", "COARSE_ACQUISITION_PRIORITY"]},
}


@pytest.mark.parametrize(("states", "leaders", "durations"), [
    (["INTERMEDIATE"] * 3, [1] * 3, []),
    (["INTERMEDIATE", "CLOSE", "INTERMEDIATE"], [1] * 3, [.5]),
    (["INTERMEDIATE", "CLOSE", "CLOSE", "CLOSE"], [1] * 4, [1.5]),
    (["CLOSE", "CLOSE", "CLOSE"], [1, 2, 1], [.5, .5, .5]),
    (["CLOSE", "UNSUPPORTED", "CLOSE"], [1] * 3, [.5, .5]),
])
def test_close_timeline_keeps_entry_separation_and_observation_gaps(states, leaders, durations):
    samples = [{"state": state, "leader": leader, "as_of_utc": START + timedelta(seconds=i / 2)}
               for i, (state, leader) in enumerate(zip(states, leaders, strict=True))]
    assert [r["duration_s"] for r in close_runs(samples, .5)] == durations


@pytest.mark.parametrize(("own", "other", "route", "reason"), [
    (False, True, "RACING", "FOLLOWER_STALE_LOCATION"),
    (True, False, "RACING", "LEADER_STALE_LOCATION"),
    (True, True, "PIT", "RETROSPECTIVE_ROUTE_MASK"),
    (True, True, "UNKNOWN", "RETROSPECTIVE_ROUTE_MASK"),
])
def test_target_support_is_stricter_than_field_complete(own, other, route, reason):
    neighbor = {"as_of_utc": START, "ahead": 63, "gap_m": 100, "gap_upper_m": 110, "gap_lower_m": 90,
                "encounter": "SAME_LAP", "coverage": "COMPLETE", "route": "RACING", "track_state": "GREEN"}
    follower = {"localized": own, "failure": "STALE_LOCATION"}
    leader = {"localized": other, "failure": "STALE_LOCATION"}
    sample = support_sample(neighbor, follower, leader, {"route": "RACING"}, {"route": route},
                            {"complete": True, "missing_required": []}, EXPOSURE)
    assert reason in sample["reasons"]
    good = sample | {"target_supported": True}
    rows = [good, sample | {"target_supported": False, "as_of_utc": START + timedelta(seconds=.5)},
            good | {"as_of_utc": START + timedelta(seconds=1)}]
    stats = support_summary(rows, START, START + timedelta(seconds=1.5), .5)
    assert stats["fractions"]["field_complete"] == 1
    assert stats["frozen_fraction"] == pytest.approx(2 / 3)
    assert stats["longest_gap_s"] == .5


def test_cutoff_selector_prefers_current_close_and_ignores_future_close():
    first = {"opportunity_id": "9955/6/47", "session_key": 9955, "driver_number": 6, "lap_number": 47,
             "cutoff_utc": START, "d8_status": "SUPPORTED", "state_eligible": True,
             "matched_control_ids": [], "approach": "PHYSICAL_APPROACH"}
    second = first | {"opportunity_id": "9955/63/47", "driver_number": 63}
    sample = {"physical_as_of_utc": START, "cutoff_exposure": "CLOSE", "opponent_number": 10, "window": "sample"}
    physical = {first["opportunity_id"]: [sample | {"physical_as_of_utc": START + timedelta(milliseconds=1)}],
                second["opportunity_id"]: [sample]}
    rows = selector([first, second], physical, PLAN)
    assert rows == selector([second, first], physical, PLAN)
    assert [r["trigger"] for r in rows] == ["SUPPORTED_CLOSE_AT_LAP_START", "PHYSICAL_APPROACH_PRIORITY"]


def test_online_label_does_not_backfill_episode_start_after_future_samples():
    samples = [{"as_of_utc": START + timedelta(seconds=i / 2), "state": "CLOSE", "leader": 63,
                "gap_lower_m": 90, "gap_upper_m": 110} for i in range(5)]
    prefix = [dict(samples[0])]
    encounter_states(prefix, START, 120, .5, 2)
    encounter_states(samples, START, 120, .5, 2)
    assert samples[0] == prefix[0]
    assert samples[0]["online_diagnostic_state"] == "CLOSE_SUPPORTED"
    assert samples[-1]["online_diagnostic_state"] == "FOLLOWING_EPISODE_ACTIVE"


def test_physical_recovery_bound_excludes_immutable_failures_and_collapses_known_groups():
    base = {"driver_number": 6, "stint_number": 3, "d8_status": "SUPPORTED", "history_status": "QUALIFIED",
            "approach": "PHYSICAL_APPROACH"}
    census = [base | {"opportunity_id": str(i)} for i in range(5)]
    close = {"cutoff_exposure": "CLOSE", "opponent_number": 63}
    screens = {"2": {"ARCHIVE_PASS_INDICATION_CENSORED"}}
    physical = {"0": [close], "1": [close], "3": [close | {"cutoff_exposure": "CLEAR"}]}
    controls = dict.fromkeys(("0", "1", "2", "3"), 5)
    result = yield_bounds(census, screens, physical, controls, [])
    assert result["B"]["laps_upper_bound"] == 2
    assert result["B"]["independent_upper_bound"] == 1
    assert result["C"]["laps_upper_bound"] == 0
