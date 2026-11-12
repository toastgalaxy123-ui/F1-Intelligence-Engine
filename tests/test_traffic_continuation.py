import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

from f1ri.features.physical_positions import LocalizationMode, estimate_from_trace
from f1ri.features.track_geometry import LocationFix, TrackGeometry, map_fixes
from f1ri.ingestion.telemetry import TelemetryWindow, load_telemetry_window
from f1ri.simulator.proximity import PhysicalRoute, TrackPosition
from f1ri.validation.traffic_acquisition import select_traffic_windows
from f1ri.validation.traffic_continuation import load_traffic_protocol
from f1ri.validation.traffic_exposures import (
    build_traffic_exposures,
    exposure_state,
    lap_opportunities,
)
from f1ri.validation.traffic_localization import apply_off_track_evidence, recent_speed_at
from f1ri.validation.traffic_reference import residual_research, traffic_readiness

START = datetime(2025, 1, 1, tzinfo=UTC)
CONFIG = Path(__file__).resolve().parents[1] / "config/experiments/stage5_s3_c_protocol.yaml"


def clock(seconds):
    return START + timedelta(seconds=seconds)


def test_full_field_telemetry_preserves_same_timestamp_for_different_drivers(tmp_path):
    class Provider:
        def fetch(self, endpoint, params):
            assert endpoint == "location" and "driver_number" not in params
            item = {"session_key": 9896, "driver_number": 63, "date": clock(.5).isoformat(),
                    "x": 10, "y": 20, "z": 0}
            rows = [item | {"date": clock(.8).isoformat()}, item, item | {"driver_number": 44}, item]
            return json.dumps(rows).encode(), "https://example.test/location"

    window = TelemetryWindow(9896, None, START, clock(2))
    frame, manifest = load_telemetry_window(tmp_path, window, "location", client=Provider())
    assert set(frame.driver_number) == {44, 63}
    assert len(frame) == 3
    assert manifest["duplicates"] == manifest["out_of_order_rows"] == 1
    assert manifest["conflicting_rows"] == 0


def test_protocol_rejects_reserved_acquisition_before_selection(tmp_path):
    config, _ = load_traffic_protocol(CONFIG)
    config["cohorts"][-1]["session_key"] = 1234
    config["initial_acquisition"].append({"session_key": 1234})
    changed = tmp_path / "reserved.yaml"
    changed.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ValueError, match="must exclude audit, pilot and reserved"):
        load_traffic_protocol(changed)


def test_windows_are_green_reproducible_disjoint_and_leave_seed_categories_distinct():
    config, _ = load_traffic_protocol(CONFIG)
    sampling = config["sampling"] | {"window_seconds": 70,
                                     "windows_per_race": {"close_seed": 1, "clean_seed": 1, "random_green": 2}}
    laps = pd.DataFrame({
        "driver_number": [1, 2], "lap_number": [2, 4], "lap_start_utc": [clock(40), clock(150)],
        "track_gap_ahead_at_start_s": [1., 4.], "observed_lap_time_s": [52., 56.],
        "is_pit_lap": [False, False], "track_status": ["GREEN", "GREEN"], "rainfall": [0, 0],
        "is_clean_green_pace_lap": [False, True],
    })
    track = pd.DataFrame({"start_utc": [START, clock(300)], "end_utc": [clock(270), clock(650)],
                          "status": ["GREEN", "GREEN"], "confidence": ["HIGH", "HIGH"]})
    result = select_traffic_windows(1, laps, track, pd.Timestamp(clock(20)), sampling, 73)
    assert result == select_traffic_windows(1, laps, track, pd.Timestamp(clock(20)), sampling, 73)
    assert {w.kind: w.seed_driver for w in result} == {"close_seed": 1, "clean_seed": 2, "random_green": None}
    assert len(result) == 4
    assert all(w.telemetry.start_utc >= clock(20) for w in result)
    assert all(left.telemetry.end_utc <= right.telemetry.start_utc
               for left, right in pairwise(result))


def test_fallback_speed_grows_uncertainty_and_keeps_invalid_locations_unsupported():
    geometry = TrackGeometry("square", 400., START,
                             ((0, 0), (100, 0), (100, 100), (0, 100), (0, 0)), 0.)
    mapped = map_fixes(geometry, (LocationFix(START, 30, 0),))
    mode = LocalizationMode.POINT_IN_TIME
    no_speed = estimate_from_trace(geometry, mapped, clock(.3), mode)
    from_speed = estimate_from_trace(geometry, mapped, clock(.3), mode, fallback_speed_mps=40)
    assert no_speed.reason == "NO_RECENT_VELOCITY"
    assert from_speed.progress_m == 42
    assert from_speed.uncertainty_m > 15
    assert from_speed.velocity_source == "CAR_SPEED_FALLBACK"
    assert estimate_from_trace(geometry, mapped, clock(1), mode, fallback_speed_mps=40).reason == "STALE_LOCATION"
    assert estimate_from_trace(geometry, (), clock(.3), mode, fallback_speed_mps=40).progress_m is None


def test_retirement_evidence_cannot_change_an_earlier_physical_field():
    before = TrackPosition(27, PhysicalRoute.UNKNOWN, None, None, reason="LAP_EVIDENCE_MISSING_OR_STALE")
    report = {"available_by_utc": clock(10).isoformat(), "reason": "FORMATION_LAP_RETIREMENT"}
    assert apply_off_track_evidence(before, clock(9), report) == before
    after = apply_off_track_evidence(before, clock(10), report)
    assert after.route is PhysicalRoute.OFF_TRACK
    assert after.reason == report["reason"]


def test_fallback_speed_lookup_ignores_future_updates_and_rejects_staleness():
    visible = SimpleNamespace(date=clock(1), speed=144)
    later = SimpleNamespace(date=clock(1.5), speed=180)
    lookup = {63: ((visible.date, later.date), (visible, later))}
    assert recent_speed_at(lookup, 63, clock(1.2), .45) == (40, clock(1))
    later.speed = 90
    assert recent_speed_at(lookup, 63, clock(1.2), .45) == (40, clock(1))
    assert recent_speed_at(lookup, 63, clock(2), .45) == (None, None)


def _sample(seconds, upper, *, route="RACING", opponent=4, mode="POINT_IN_TIME"):
    return {"session_key": 1, "window": "green", "window_kind": "random_green", "layout_version": "test",
            "mode": mode, "track_state": "GREEN", "driver_number": 63, "as_of_utc": clock(seconds),
            "route": route, "coverage": "COMPLETE", "ahead": opponent, "encounter": "SAME_LAP",
            "gap_upper_m": upper, "gap_lower_m": upper - 25, "gap_m": upper - 12,
            "speed_mps": 50, "source_age_s": .1, "completed_laps": 10}


def test_exposure_hysteresis_censors_confirmed_separation_and_never_uses_retro():
    protocol, _ = load_traffic_protocol(CONFIG)
    config = protocol["exposure"]
    gaps = [100, 110, 165, 108, 103, 162, 163, 260]
    samples = [_sample(i / 2, gap) for i, gap in enumerate(gaps)]
    retro = [_sample(i / 2, 30, mode="RETROSPECTIVE") for i in range(len(gaps))]
    episodes, segments = build_traffic_exposures(samples + retro, config, .5)
    episode, = episodes
    assert episode["termination"] == "GAP_OPENED"
    assert episode["end_utc"] == clock(2)
    assert len(segments) == 5
    assert segments[1]["closing_mps"] == -20
    assert segments[0]["episode_id"] == episode["episode_id"]
    assert exposure_state(samples[-1], config) == "CLEAR"


def test_following_lap_requires_cutoff_episode_to_continue_inside_target():
    config, _ = load_traffic_protocol(CONFIG)
    laps = pd.DataFrame([{
        "driver_number": 63, "lap_number": 11, "lap_start_utc": clock(1.25), "observed_lap_time_s": 5.,
        "track_status": "GREEN", "confirmed_green_lap": True, "rainfall": 0, "is_pit_lap": False,
        "stint_pit_mismatch": False, "had_in_lap_on_track_pass": False,
    }, {"driver_number": 63, "lap_number": 12, "lap_start_utc": clock(6.25)}])
    neighbors = []
    for i in range(15):
        at = i / 2
        neighbors.extend((_sample(at, 100), _sample(at, 100, mode="RETROSPECTIVE"),
                          _sample(at, 220, mode="RETROSPECTIVE") | {"driver_number": 4}))
    cutoff = {"window": "green", "driver_number": 63, "as_of_utc": clock(1), "episode_id": "A"}
    later = cutoff | {"as_of_utc": clock(2), "episode_id": "B"}
    _, outcomes, _ = lap_opportunities(neighbors, [cutoff, later], laps, config, "development")
    assert outcomes[0]["outcome_group"] == "INTERMEDIATE"
    _, outcomes, _ = lap_opportunities(neighbors, [cutoff, later | {"episode_id": "A"}],
                                        laps, config, "development")
    assert outcomes[0]["outcome_group"] == "FOLLOWING"


def test_signed_proxy_uses_only_past_development_controls():
    config, _ = load_traffic_protocol(CONFIG)
    settings = config["reference"] | {"minimum_matched_controls": 1}
    refs, outcomes = [], []
    for identifier, cutoff, actual, group, split, key in [
        ("prior", 10, 81., "CLEAN_CONTROL", "development", 1),
        ("target", 100, 79., "FOLLOWING", "development", 1),
        ("validation", 100, 85., "CLEAN_CONTROL", "validation", 2),
        ("future", 200, 90., "CLEAN_CONTROL", "development", 1),
    ]:
        refs.append({"opportunity_id": identifier, "as_of_utc": clock(cutoff), "expected_clean_s": 80.,
                     "pace_status": "SUPPORTED", "compound": "HARD", "tyre_age": 6,
                     "race_progress": .3, "split": split, "session_key": key})
        outcomes.append({"opportunity_id": identifier, "completed_utc": clock(cutoff + actual),
                         "observed_lap_s": actual, "outcome_group": group, "episode_ids": [identifier]})
    research = residual_research(refs, outcomes, settings)
    target = next(row for row in research if row["opportunity_id"] == "target")
    assert target["matched_control_count"] == 1
    assert target["signed_residual_s"] == -1
    assert target["signed_proxy_s"] == -2
    gate = traffic_readiness([{"session_key": 1, "split": "development", "timing_marker_gate": True}],
                             research, config["evaluation"])
    assert gate["empirical_fit_status"] == "GATED"
    assert gate["reserved_races_evaluated"] == 0
