from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from f1ri.validation.splits import build_split_manifest
from f1ri.validation.stage3_r_population import ready_splits, validate_examples
from f1ri.validation.stage3_r_release import REPORT, load_successor
from f1ri.validation.stage3_r_sources import census, digest, prepare_reference, study_inputs
from f1ri.validation.stage3_r_validation import D8, MODELS, cutoff_request, gates, summaries

ROOT = Path(__file__).resolve().parents[1]


def test_original_cohort_roles_and_minimum_support(tmp_path):
    plan, cohort = study_inputs(ROOT)
    splits = build_split_manifest(cohort)
    assert tuple(map(len, (splits.train, splits.val, splits.test, splits.transfer_2026))) == (17, 4, 6, 5)
    assert splits.untouched_test_keys == [9955, 9928, 9888, 9839]
    status = [{"session_key": key, "status": "READY"} for key in splits.val_keys + splits.test_keys]
    groups, gaps = ready_splits(plan, splits, status)
    assert groups["calibration"] == splits.val_keys
    assert gaps == {"train": {"available": 0, "required": 12}}
    assert len(census(ROOT, tmp_path, cohort)) == 32


def test_reconstruction_requires_an_isolated_cache():
    with pytest.raises(ValueError, match="isolated"):
        prepare_reference(ROOT, ROOT)


def test_cutoff_uses_fresh_age_instead_of_target_age():
    request = cutoff_request({
        "session_key": 42, "driver_number": 63, "cutoff_lap_number": 10, "planned_start_lap": 12,
        "forecast_as_of_utc": datetime(2025, 9, 1, tzinfo=UTC), "tyre_age_at_forecast_start": 0,
        "tyre_age_at_lap_start": 4, "requested_horizon": 5,
    })
    assert request.tyre_age_at_start == 0
    assert request.planned_start_lap == 12


@pytest.mark.parametrize(("field", "value", "message"), [
    ("planned_start_lap", 2, "Forecast start"),
    ("temperature_observation_age_s", -0.25, "Future weather"),
    ("tyre_age_at_forecast_start", -1, "tyre age"),
])
def test_feature_clock_and_age_validation(field, value, message):
    row = {"session_key": 42, "driver_number": 63, "cutoff_lap_number": 2, "planned_start_lap": 3,
           "forecast_as_of_utc": datetime(2025, 9, 1, tzinfo=UTC), "scenario_type": "continuation",
           "requested_horizon": 1, "temperature_observation_age_s": 9, "tyre_age_at_forecast_start": 2}
    assert validate_examples(pd.DataFrame([row]))
    with pytest.raises(ValueError, match=message):
        validate_examples(pd.DataFrame([row | {field: value}]))
    with pytest.raises(ValueError, match="Duplicate"):
        validate_examples(pd.DataFrame([row, row]))


def test_uncertainty_and_mean_gates_are_independent():
    config, _ = study_inputs(ROOT)
    scores = {}
    for name in (*MODELS[:2], D8):
        profile = {
            "endpoint": {1: {"race_weighted_mae_s": .45 if name == D8 else .5}},
            "cumulative": {h: {"cum_race_weighted_mae_s": 2.5, "cum_coverage_50": .51,
                               "cum_coverage_90": .75 if h == 15 else .88} for h in (5, 10, 15, 20)},
        }
        scores[name] = {"all_test": profile, "historical_uninspected_test": profile}
    outcome = gates(scores, config["validation"], replacement_ok=True, strategy_ok=True,
                    serialization_ok=True, reproducible=True)
    assert outcome["G2_mean_quality"]
    assert not outcome["G3_uncertainty_quality"]


def test_unmatched_predictions_are_rejected():
    rows = [{"model_id": "a", "session_key": 42, "driver_number": 1, "cutoff_lap_number": 1,
             "scenario_type": "continue", "requested_horizon": 1}]*2
    with pytest.raises(ValueError, match="Unmatched"):
        summaries(pd.DataFrame(rows), {}, [])


def test_dashboard_unsupported_release_blocks_both_modes(tmp_path):
    path = tmp_path/REPORT
    path.parent.mkdir(parents=True)
    assert load_successor(tmp_path, correlated=False)[0] is None
    path.write_text('{"release":"UNSUPPORTED","reason":"undercoverage"}', encoding="utf-8")
    assert len(digest(path)) == 64
    assert load_successor(tmp_path, correlated=False) == (None, "undercoverage")
    assert load_successor(tmp_path, correlated=True) == (None, "undercoverage")
