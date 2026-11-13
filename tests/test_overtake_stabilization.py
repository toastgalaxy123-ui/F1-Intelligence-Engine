from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from f1ri.validation.overtake_evidence import (
    audit_event,
    continuous_components,
    first_loss,
    position_failure,
    sample_support,
    union_duration,
)
from f1ri.validation.overtake_gap_audit import acquisition_assessment
from f1ri.validation.overtake_stabilization import risk_record

START = datetime(2025, 8, 3, 13, tzinfo=UTC)


def at(s):
    return START + timedelta(seconds=s)


def car(progress, **kwargs):
    return {"route": "RACING", "progress_m": progress, "uncertainty_m": 6, "lap_aligned": True,
            "completed_laps": 12, **kwargs}


@pytest.mark.parametrize(("reason", "expected"), [
    ("MISSING_LOCATION_PREFIX", "MISSING_LOCATION"),
    ("STALE_LOCATION", "STALE_OBSERVATION"),
    ("NO_PLAUSIBLE_SEGMENT", "GEOMETRY_PROJECTION_FAILURE"),
    ("AMBIGUOUS_SEGMENT", "GEOMETRY_PROJECTION_FAILURE"),
    ("MISSING_VALID_BRACKET", "INTERPOLATION_SUPPORT_FAILURE"),
    ("LAP_EVIDENCE_MISSING_OR_STALE", "MISSING_OR_STALE_TIMING"),
])
def test_position_failures_remain_distinct(reason, expected):
    assert position_failure(car(None, reason=reason)) == expected


def test_pair_reports_missing_cars_and_neighbor_overlap():
    frames = {at(0): {1: car(100), 2: car(150)}}
    neighbors = {at(0): {1: {"ahead": None, "reason": "NEAREST_NEIGHBOR_OVERLAP"}}}
    flags, _ = sample_support(at(0), (1, 2), frames, neighbors, 1000)
    assert flags == {"pair": "AMBIGUOUS_NEAREST_NEIGHBOR"}
    frames[at(0)].pop(2)
    assert sample_support(at(0), (1, 2), frames, neighbors, 1000)[0] == {"target": "MISSING_POSITION_ROW"}
    frames[at(0)] = {2: car(150)}
    assert sample_support(at(0), (1, 2), frames, neighbors, 1000)[0] == {"attacker": "MISSING_POSITION_ROW"}


def test_bad_clock_and_route_are_auditable():
    frames = {at(0): {1: car(100, observation_utc=at(1)), 2: car(150, route="UNKNOWN")}}
    assert sample_support(at(0), (1, 2), frames, {}, 1000)[0] == {
        "target": "ROUTE_AMBIGUITY", "clock": "INCONSISTENT_SOURCE_TIMESTAMP"}


def test_followup_stops_at_first_loss_and_keeps_partial_time():
    frames = {at(s): {1: car(100), 2: car(150)} for s in range(5)}
    neighbors = {tick: {1: {"ahead": 2}} for tick in frames}
    frames[at(3)][1] = car(None, reason="MISSING_VALID_BRACKET")
    lo, hi, flags, _ = first_loss(at(0), at(4), (1, 2), frames, neighbors, sorted(frames), 1, 1000)
    assert (lo, hi, flags) == (at(2), at(3), {"attacker": "INTERPOLATION_SUPPORT_FAILURE"})
    row = {"cutoff_utc": at(0), "opportunity_id": "o", "encounter_id": "e", "session": 1,
           "attacker": 1, "target": 2, "source": "archive", "window": "w", "lap_relation": "SAME_LAP",
           "gap_m": 50, "gap_lower_m": 38, "gap_upper_m": 62, "field_coverage": "COMPLETE",
           "latest_feature_observation_utc": at(0)}
    result = risk_record(row, {"outcome": "CENSORED_LOCALIZATION", "observed_until_utc": at(3),
                               "planned_end_utc": at(4), "pass_event_id": None},
                         {"last_supported_utc": lo, "first_unsupported_utc": hi})
    assert result["at_risk_s"] == 2 and result["termination"] == "CENSORED_LOCALIZATION"
    assert first_loss(at(0), at(5), (1, 2), {at(0): frames[at(0)]}, neighbors, [at(0)], 1, 1000)[2] == {
        "grid": "OBSERVATION_WINDOW_TERMINATION"}


@pytest.mark.parametrize(("rank_after", "width", "flat", "decision"), [
    ([2, 1, 2], 1., False, "TIER_A_CONFIRMED"),
    ([2, 2, 1], 1., False, "TIER_B_SUPPORTED"),
    ([2, 1, 2], 4., False, "TIER_B_SUPPORTED"),
    ([2, 2, 1], 1., True, "TIER_C_AMBIGUOUS"),
])
def test_tier_requires_independent_support_and_preserves_bracket(rank_after, width, flat, decision):
    cfg = {"official_pairs": {"1/1-2": [[1, 2, 1], rank_after]},
           "official_lap_charts": {1: "chart"}, "official_pit_summaries": {1: "pits"}}
    e = {"session": 1, "attacker": 1, "target": 2, "event_time_lower_utc": at(10),
         "event_time_upper_utc": at(10+width), "event_time_bracket_s": width, "source_period_s": .5,
         "field_coverage": ["COMPLETE", "COMPLETE"], "exclusions": [],
         "before": {"target": car(250)}, "after": {"target": car(250 if flat else 270)},
         "evidence": {"timing_pair_order": [
             {"lap": 13, "marker": 1 if flat else 0, "attacker_first": False, "attacker_utc": at(8), "target_utc": at(7)},
             {"lap": 13, "marker": 2, "attacker_first": True, "attacker_utc": at(15), "target_utc": at(16)},
         ]}}
    history = SimpleNamespace(pits={1: [], 2: []}, interruptions=lambda *args: [])
    manifest = {"payloads": [], "files": {"geometry.json": "hash"}, "role": "development",
                "circuit": "test", "sector_markers_m": [0, 320, 650]}
    audited = audit_event(e, cfg, manifest, history, Path("."))
    assert audited["decision"] == decision
    assert audited["event_time_lower_utc"] == e["event_time_lower_utc"]
    assert audited["event_time_upper_utc"] == e["event_time_upper_utc"]


def test_union_keeps_one_pair_exposure_and_contact_breaks():
    assert union_duration([(at(0), at(3)), (at(1), at(5)), (at(7), at(8))]) == 6
    assert continuous_components([(at(0), 1), (at(1), 1), (at(3), 1)], 1.51) == 2


def test_acquisition_is_zero_budget_and_has_no_fetch_path():
    assert acquisition_assessment([], {"api_request_budget": 0})["requests_made"] == 0
    with pytest.raises(ValueError, match="zero-request"):
        acquisition_assessment([], {"api_request_budget": 1})
