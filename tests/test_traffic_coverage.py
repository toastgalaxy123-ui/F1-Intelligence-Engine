from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

from f1ri.features.race_state import _clean_lap_evidence
from f1ri.features.track_geometry import TrackGeometry
from f1ri.ingestion.telemetry import TelemetryWindow
from f1ri.validation.traffic_acquisition import TrafficWindow
from f1ri.validation.traffic_coverage import (
    CURRENT,
    MATCHED_RETRO,
    NATIVE_RETRO,
    NO_SPEED,
    CoverageReplay,
)
from f1ri.validation.traffic_coverage_pilot import (
    ReplaySpan,
    merge_source_frames,
    plan_extensions,
    uncovered_requests,
)
from f1ri.validation.traffic_exposures import LapEligibilityAudit, lap_opportunities
from f1ri.validation.traffic_localization import localize_window
from f1ri.validation.traffic_reference import residual_research

START = datetime(2025, 1, 1, tzinfo=UTC)


def at(seconds):
    return START + timedelta(seconds=seconds)


def test_paired_status_mask_keeps_missing_car_until_retirement_available():
    geometry = TrackGeometry("square", 400., START,
                             ((0, 0), (100, 0), (100, 100), (0, 100), (0, 0)), 0.)
    window = TrafficWindow("random_green", TelemetryWindow(1, None, START, at(2)))
    inputs = {
        "drivers": pd.DataFrame({"driver_number": [1, 2]}),
        "laps": pd.DataFrame({"driver_number": [1, 2], "lap_number": [2, 2], "lap_start_utc": [START, START]}),
        "positions": pd.DataFrame(columns=["driver_number", "date", "position"]),
        "pits": pd.DataFrame({"driver_number": [2], "date": [at(4.5)], "lane_duration": [10.]}),
        "passes": pd.DataFrame(columns=["driver_number", "date_start", "lap_number"]),
    }
    location = pd.DataFrame({"driver_number": [1, 1, 1], "date": [at(.25), at(.6), at(1.1)],
                             "x": [20., 27., 37.], "y": [0., 0., 0.]})
    speed = pd.DataFrame({"driver_number": [1, 1], "date": [at(.3), at(1.3)], "speed": [72., 72.]})
    audit = CoverageReplay()
    localize_window(
        geometry, window, location, speed, inputs, [0., 130., 260.],
        {"speed_fallback_max_age_s": .45, "dropout_stride": 19, "timing_passage_gap_limit_s": 5,
         "maximum_timing_marker_match_s": 2, "independent_laps": 0},
        .5, 1, set(), {2: {"available_by_utc": at(1.0).isoformat(), "reason": "DATED_RETIREMENT",
                          "source": "https://example.test/status"}}, coverage_replay=audit,
    )
    first = {row["mode"]: row for row in audit.snapshots if row["as_of_utc"] == at(.5)}
    assert first[CURRENT]["required"] == first[NO_SPEED]["required"] == first[MATCHED_RETRO]["required"] == [1, 2]
    assert first[CURRENT]["supported"] == [1]
    assert first[CURRENT]["missing_required"] == [2]
    assert first[NO_SPEED]["missing_required"] == [1, 2]
    assert first[NATIVE_RETRO]["required"] == [1]
    later = next(row for row in audit.snapshots if row["as_of_utc"] == at(1.) and row["mode"] == CURRENT)
    assert later["evidence_off_track"] == [2]
    assert later["complete"]


def test_diagnostic_distinguishes_stale_speed_from_window_end_bracket():
    geometry = TrackGeometry("square", 400., START,
                             ((0, 0), (100, 0), (100, 100), (0, 100), (0, 0)), 0.)
    window = TrafficWindow("clean_seed", TelemetryWindow(1, None, START, at(1.5)))
    inputs = {
        "drivers": pd.DataFrame({"driver_number": [1]}),
        "laps": pd.DataFrame({"driver_number": [1], "lap_number": [2], "lap_start_utc": [START]}),
        "positions": pd.DataFrame(columns=["driver_number", "date", "position"]),
        "pits": pd.DataFrame(columns=["driver_number", "date", "lane_duration"]),
        "passes": pd.DataFrame(columns=["driver_number", "date_start", "lap_number"]),
    }
    location = pd.DataFrame({"driver_number": [1], "date": [at(.45)], "x": [22.], "y": [0.]})
    speed = pd.DataFrame({"driver_number": [1, 1], "date": [START, at(1.1)], "speed": [72., 72.]})
    audit = CoverageReplay()
    localize_window(
        geometry, window, location, speed, inputs, [0., 130., 260.],
        {"speed_fallback_max_age_s": .45, "dropout_stride": 19, "timing_passage_gap_limit_s": 5,
         "maximum_timing_marker_match_s": 2, "independent_laps": 0},
        .5, 1, set(), {}, coverage_replay=audit,
    )
    failures = {(row["as_of_utc"], row["mode"]): row["failure"] for row in audit.drivers}
    assert failures[at(.5), CURRENT] == "NO_RECENT_VELOCITY_SPEED_STALE"
    assert failures[at(1.), CURRENT] == "NO_RECENT_VELOCITY_SPEED_STALE"
    assert failures[at(1.), MATCHED_RETRO] == "WINDOW_END_NO_RIGHT_FIX"


def test_extension_reuses_cached_interior_and_deduplicates_source_seams():
    span = ReplaySpan(1, START, at(400))
    cached = (TelemetryWindow(1, None, at(100), at(220)),)
    slices = uncovered_requests(span, cached, timedelta(seconds=178), timedelta(seconds=1))
    assert [(s.start_utc, s.end_utc) for s in slices] == [
        (START, at(101)), (at(219), at(399)), (at(397), at(400)),
    ]
    row = {"session_key": 1, "driver_number": 1, "date": at(100), "speed": 72}
    other = row | {"driver_number": 2}
    frames = [pd.DataFrame([row, other]), pd.DataFrame([row, row | {"speed": 74}, other])]
    merged, counts = merge_source_frames(frames, span)
    assert merged.driver_number.tolist() == [2]
    assert counts == {"rows_in_span": 5, "duplicate_rows_removed": 2,
                      "conflicting_rows_excluded": 2, "usable_rows": 1}


def test_pilot_plan_requires_geometry_available_before_block(tmp_path):
    plan = {"session_key": 1, "split": "development", "frozen_protocol_sha256": "hash",
            "prefix_seconds": 20, "suffix_seconds": 10, "boundary_overlap_seconds": 1,
            "maximum_chunk_seconds": 180, "strata_priority": ["close_seed"], "anchors": ["close_seed_000030"],
            "maximum_new_source_seconds": 32, "maximum_new_requests": 4}
    path = tmp_path / "pilot.yaml"
    path.write_text(yaml.safe_dump(plan), encoding="utf8")
    originals = [TrafficWindow("close_seed", TelemetryWindow(1, None, at(30), at(90)))]
    track = pd.DataFrame({"start_utc": [START], "end_utc": [at(150)], "status": ["GREEN"], "confidence": ["HIGH"]})
    cohort = {"session_key": 1, "split": "development"}
    extensions, budget = plan_extensions(path, "hash", cohort, originals, track, at(10))
    assert extensions[0].replay.telemetry.start_utc == at(10)
    assert budget["bounded_requests"] == 4
    with pytest.raises(ValueError, match="anchors differ"):
        plan_extensions(path, "hash", cohort, originals, track, at(11))


def test_cutoff_cohort_keeps_truncation_and_future_episode_in_outcome_ledger():
    config = {"sampling": {"sample_period_seconds": .5},
              "exposure": {"entry_upper_m": 120, "clean_lower_m": 225},
              "reference": {"label_minimum_coverage": .9, "minimum_clean_control_fraction": .9}}
    lap = {"driver_number": 1, "stint_number": 1, "track_status": "GREEN", "confirmed_green_lap": True,
           "rainfall": 0, "is_pit_lap": False, "stint_pit_mismatch": False, "had_in_lap_on_track_pass": False}
    laps = pd.DataFrame([lap | {"lap_number": i, "lap_start_utc": at(i), "lap_end_estimate_utc": at(i + 1),
                                "observed_lap_time_s": 1.} for i in (1, 2, 3)])
    metadata = {"session_key": 1, "window": "example", "window_kind": "close_seed", "track_state": "GREEN",
                "driver_number": 1, "coverage": "COMPLETE", "route": "RACING", "encounter": "SAME_LAP",
                "ahead": 2, "gap_m": 70., "gap_lower_m": 60., "gap_upper_m": 80.}
    neighbors = []
    for seconds in (1.5, 2., 2.5, 3.):
        row = metadata | {"as_of_utc": at(seconds)}
        neighbors.extend([row | {"mode": CURRENT}, row | {"mode": NATIVE_RETRO},
                          row | {"mode": NATIVE_RETRO, "driver_number": 2}])
    segments = [{"window": "example", "driver_number": 1, "as_of_utc": at(2.5), "episode_id": "later"}]
    audit = LapEligibilityAudit()
    _, outcomes, _ = lap_opportunities(neighbors, segments, laps, config, "development", audit=audit)
    assert [row["screen"] for row in audit.outcomes] == ["LEFT_TRUNCATED_WINDOW", "INTERMEDIATE", "PARTIAL_WINDOW"]
    assert [row["opportunity_id"] for row in audit.cutoffs] == ["1/1/2", "1/1/3"]
    assert all("observed_lap_s" not in row and row["horizon_laps"] == 1 for row in audit.cutoffs)
    assert audit.outcomes[1]["later_or_different_episode"]
    assert not audit.outcomes[1]["cutoff_encounter_continued"]
    assert outcomes[0]["outcome_group"] == "INTERMEDIATE"


def test_same_stint_history_excludes_target_and_future_laps():
    laps = pd.DataFrame({
        "lap_number": [1, 2, 3, 4, 5], "lap_start_utc": [at(i * 10) for i in range(5)],
        "lap_end_estimate_utc": [at(i * 10) for i in range(1, 6)],
        "is_clean_green_pace_lap": [True] * 5, "compound": ["HARD"] * 5,
        "stint_number": [1, 2, 2, 2, 2], "observed_lap_time_s": [10.] * 5,
    })
    request = SimpleNamespace(planned_start_lap=4, as_of_utc=at(30), stint_number=2,
                              clean_laps_observed_current_stint=2)
    assert _clean_lap_evidence(laps, request) == [
        {"lap_number": 2, "available_by_utc": at(20)}, {"lap_number": 3, "available_by_utc": at(30)},
    ]
    request.as_of_utc = at(29)
    request.clean_laps_observed_current_stint = 1
    assert [row["lap_number"] for row in _clean_lap_evidence(laps, request)] == [2]


def test_control_evidence_must_arrive_before_cutoff_and_laps_are_unique():
    config = {"control_max_tyre_age_delta": 5, "control_max_progress_delta": .20, "minimum_matched_controls": 1}
    refs, outcomes = [], []
    for identifier, cutoff, completed, available, group in (
        ("prior", 1, 11, 12, "CLEAN_CONTROL"),
        ("late", 2, 13, 25, "CLEAN_CONTROL"),
        ("exact", 3, 14, 20, "CLEAN_CONTROL"),
        ("target", 20, 29, 31, "FOLLOWING"),
    ):
        refs.append({"opportunity_id": identifier, "as_of_utc": at(cutoff), "pace_status": "SUPPORTED",
                     "expected_clean_s": 10., "split": "development", "compound": "HARD",
                     "tyre_age": 8, "race_progress": .3})
        outcomes.append({"opportunity_id": identifier, "observed_lap_s": float(completed - cutoff),
                         "completed_utc": at(completed), "outcome_available_by_utc": at(available),
                         "outcome_group": group})
    rows = residual_research(refs, outcomes, config)
    assert rows[-1]["matched_control_ids"] == ["prior"]
    assert rows[-1]["signed_proxy_s"] == -1.
    with pytest.raises(ValueError, match="unique"):
        residual_research(refs, [*outcomes, outcomes[-1]], config)
