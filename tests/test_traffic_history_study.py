from datetime import UTC, datetime, timedelta

import pandas as pd

from f1ri.features.race_state import _same_stint_history
from f1ri.validation.traffic_history_audit import physical_history_check
from f1ri.validation.traffic_history_plan import choose_blocks
from f1ri.validation.traffic_reference import (
    INDEPENDENCE_RULE,
    control_pool,
    encounter_units,
    traffic_readiness,
)

START = datetime(2025, 6, 29, tzinfo=UTC)


def at(seconds):
    return START + timedelta(seconds=seconds)


def test_same_stint_audit_retains_excluded_laps_and_requires_visible_next_line():
    base = {"compound": "HARD", "observed_lap_time_s": 10., "exclusion_reason": "TRAFFIC",
            "is_clean_green_pace_lap": False, "position_at_lap_start": 6, "gap_ahead_at_start_raw": "1.2",
            "gap_ahead_at_start_s": 1.2, "track_gap_ahead_at_start_s": 1.1,
            "track_gap_ahead_at_end_s": 1.0, "is_clean_air_at_start": False,
            "had_in_lap_on_track_pass": False, "is_blue_flagged_lap": False, "track_status": "GREEN",
            "is_pit_in_lap": False, "is_pit_out_lap": False}
    laps = pd.DataFrame([
        base | {"lap_number": n, "stint_number": 1 if n == 1 else 2,
                "lap_start_utc": at(start), "lap_end_estimate_utc": at(end)}
        for n, start, end in ((1, 0, 10), (2, 10, 20), (3, 20, 29), (4, 30, 40))
    ])
    current = laps.iloc[-1]
    assert [r["lap_number"] for r in _same_stint_history(laps, current, at(29.9))] == [2]
    exact = _same_stint_history(laps, current, at(30))
    assert [r["lap_number"] for r in exact] == [2, 3]
    assert exact[-1]["available_by_utc"] == at(30)
    assert exact[-1]["stage1_exclusions"] == "TRAFFIC"


def test_prior_physical_screen_requires_complete_window_and_route_evidence():
    config = {"sampling": {"sample_period_seconds": .5},
              "reference": {"label_minimum_coverage": .9, "minimum_clean_control_fraction": .9},
              "exposure": {"entry_upper_m": 120, "clean_lower_m": 225}}
    base = {"window": "arm", "track_state": "GREEN", "coverage": "COMPLETE", "route": "RACING",
            "encounter": "SAME_LAP", "gap_upper_m": 280., "gap_lower_m": 240., "route_mask_ok": True}
    rows = [base | {"as_of_utc": at(n / 2)} for n in range(5)]
    assert physical_history_check(rows, START, at(2), config)["physical_history"] == "CLEAN"
    assert physical_history_check(rows[1:], START, at(2), config)["physical_reason"] == "NO_FULL_WINDOW"
    rows[1]["route_mask_ok"] = False
    assert physical_history_check(rows, START, at(2), config)["physical_reason"] == "PHYSICAL_SUPPORT_BELOW_MINIMUM"


def test_control_attrition_excludes_later_races_and_late_evidence_before_matching():
    row = {"opportunity_id": "austria", "as_of_utc": at(100), "compound": "HARD", "tyre_age": 12,
           "race_progress": .5}
    base = {"opportunity_id": "ok", "completed_utc": at(80), "outcome_available_by_utc": at(90),
            "split": "development", "compound": "HARD", "tyre_age": 8, "race_progress": .4}
    controls = [
        base, base | {"opportunity_id": "hungary", "completed_utc": at(200)},
        base | {"opportunity_id": "exact", "outcome_available_by_utc": at(100)},
        base | {"opportunity_id": "monza", "split": "validation"},
        base | {"opportunity_id": "wrong_compound", "compound": "MEDIUM"},
        base | {"opportunity_id": "too_old", "tyre_age": 18},
        base | {"opportunity_id": "phase", "race_progress": .1},
        base | {"opportunity_id": "austria"},
    ]
    eligible, counts = control_pool(row, controls, {"control_max_tyre_age_delta": 5,
                                                   "control_max_progress_delta": .2})
    assert [c["opportunity_id"] for c in eligible] == ["ok"]
    assert list(counts.values()) == [8, 7, 6, 5, 4, 3, 2, 1]


def test_readiness_counts_repeated_laps_and_fragments_as_one_encounter():
    race = {"session_key": 9955, "split": "development", "timing_marker_gate": True}
    base = race | {"driver_number": 4, "cutoff_opponent": 81, "stint_number": 2, "signed_proxy_s": 1.}
    following = [
        base | {"outcome_group": "FOLLOWING", "opportunity_id": f"lap-{n}",
                "cutoff_episode_id": f"fragment-{n}", "episode_ids": [f"fragment-{n}", f"other-{n}"]}
        for n in range(5)
    ]
    controls = [base | {"outcome_group": "CLEAN_CONTROL", "opportunity_id": f"control-{n}"} for n in range(5)]
    counts = encounter_units(following)
    assert counts == {"unique_laps": 5, "cutoff_encounters": 5, "fragments": 10,
                      "driver_leader_stints": 1, "independent_encounters": 1}
    limits = {"minimum_train_races": 2, "minimum_validation_races": 2, "minimum_reserved_test_races": 3,
              "minimum_following_laps_per_train_race": 5, "minimum_control_laps_per_train_race": 5}
    result = traffic_readiness([race], following + controls, limits, independence_rule=INDEPENDENCE_RULE)
    assert result["eligible_development_races"] == []
    assert result["qualifying_encounters_by_race"][9955]["independent_encounters"] == 1


def test_qualified_blocks_are_disjoint_and_exclude_non_green_spans():
    plan = {"seed": 9, "cutoff_population": {"clean_interval_min_s": 3.},
            "acquisition": {"window_seconds": 160, "maximum_chunk_seconds": 180,
                            "boundary_overlap_seconds": 1, "arms": {"early_control": 1, "history_close": 1,
                                                                     "random_green": 1},
                            "maximum_new_field_seconds_per_race": 500, "maximum_field_requests_per_race": 8}}
    rows = [{"block_start_utc": at(seconds), "driver_number": 4, "lap_number": n + 8,
             "phase": "early", "selection_gap_s": gap, "qualified": qualified}
            for n, (seconds, gap, qualified) in enumerate(
                ((0, 4., False), (130, 1., True), (200, 1., True), (400, 2., False)))]
    track = pd.DataFrame({"start_utc": [START, at(190)], "end_utc": [at(160), at(580)],
                          "status": ["GREEN", "GREEN"], "confidence": ["HIGH", "HIGH"]})
    windows, anchors, budget = choose_blocks(1, rows, rows[1:3], plan, (), track)
    assert [anchor["block_start_utc"] for anchor in anchors] == [START, at(200), at(400)]
    assert len(windows) == 3 and budget["field_requests"] == 6
