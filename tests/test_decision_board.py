"""Regression controls for the research board's decision and evidence boundary."""

from dataclasses import replace
from pathlib import Path
from zipfile import ZipFile

import pytest

from f1ri.decision_board import (
    MODES,
    PRESETS,
    Provenance,
    Settings,
    execute,
    export_run,
    load_assets,
    prepare,
    replay,
)
from f1ri.simulator.strategy_optimization import optimize

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def assets():
    return load_assets(ROOT)


@pytest.fixture(scope="module")
def neutralization(assets):
    settings = Settings(preset="neutralization", mode=MODES[1], conditional_future="SC", sets=("soft", "used-soft"),
                        stop_options=1)
    return settings, execute(ROOT, settings, assets)


def test_archived_cutoff_has_real_tyres_and_missing_physical_state(assets):
    snapshot, request, errors = prepare(ROOT, Settings(), assets)
    assert not errors
    assert snapshot.session.value == 9896
    assert snapshot.cutoff_utc.value.isoformat() == "2025-10-05T12:52:29.272000+00:00"
    assert snapshot.weather.at_utc <= snapshot.cutoff_utc.value
    assert snapshot.drivers[0]["fitted_tyre"].provenance == Provenance.OBSERVED
    for key in ("inventory", "route", "progress", "physical_predecessor"):
        assert snapshot.drivers[0][key].value is None
        assert snapshot.drivers[0][key].provenance == Provenance.UNAVAILABLE
    assert all(t <= snapshot.cutoff_utc.value for f in request.features for _, t in f.evidence_as_of)


@pytest.mark.parametrize(("settings", "code"), [
    (Settings(preset="unsupported"), "FULL_FIELD_PHYSICAL_STATE_UNAVAILABLE"),
    (Settings(mode="LIVE"), "LIVE_UNSUPPORTED"),
    (Settings(preset="two-car"), "DECLARED_SCENARIO_REQUIRES_COUNTERFACTUAL_OR_HINDSIGHT"),
    (Settings(cutoff_utc="2025-10-05T12:53:00Z"), "CUTOFF_NOT_IN_VERIFIED_CATALOG"),
])
def test_unsupported_context_emits_no_forecast_or_strategy(assets, settings, code):
    result = execute(ROOT, settings, assets)
    assert code in result["unsupported"]
    assert not result["forecasts"]
    assert result["strategy"] is None
    assert result["recommendation"]["unique_action"] is None


def test_invalid_inventory_and_unqualified_weights():
    with pytest.raises(ValueError, match="research bounds"):
        Settings(sets=("inferred-race-allocation",))
    with pytest.raises(ValueError, match="one explicit positive weight"):
        Settings(objective="DECLARED_WEIGHTED_DELTA")


def test_point_in_time_and_hindsight_are_separate(assets, neutralization):
    settings, result = neutralization
    conditional = result["strategy"]["conditional"]
    assert conditional["decision_basis"] == "DECISION_KNOWN_EVENTS"
    _, request, _ = prepare(ROOT, settings, assets)
    decision = optimize(replace(request, scenario=replace(request.scenario, scenario_id="SC")), assets[0])
    assert conditional["decision"] == decision["decision"]
    assert conditional["decision"]["rows"] != conditional["conditional"]["rows"]
    retrospection = execute(ROOT, replace(settings, mode=MODES[2]), assets)
    assert retrospection["recommendation"]["unique_action"] is None
    assert retrospection["strategy"]["conditional"]["information_mode"] == "PERFECT_FORESIGHT"


def test_replacements_and_common_marker(neutralization):
    _, result = neutralization
    assert len(result["forecasts"]) == 5
    for row in result["forecasts"]:
        for lap in row["mean"]:
            assert set(lap) == {"lap", "age", "stint", "compound", "seconds", "cumulative_seconds", "support"}
        if row["plan"].startswith("used-soft"):
            assert row["request"]["tyre_age_at_start"] == 3
            assert row["request"]["observed_stint_clean_times_s"] == ()
    assert result["snapshot"]["drivers"][1]["pace_history"]["provenance"] == Provenance.DECLARED
    decision = result["strategy"]["conditional"]["decision"]
    assert any(row["following_us"] > 0 for row in decision["rows"])
    assert all(row["comparison"] == "EXACT_SIMULATION_TIE" for row in decision["rows"]
               if row["candidate"] in decision["best"])
    assert len({tuple(r["markers"]) for r in decision["rows"]}) == 1


def test_search_withholds_action_when_incomplete(assets, neutralization):
    settings, _ = neutralization
    result = execute(ROOT, replace(settings, budget=2, robust_budget=4), assets)
    assert result["recommendation"]["incomplete"]
    assert result["recommendation"]["unique_action"] is None
    assert result["strategy"]["conditional"]["unevaluated"]
    assert result["strategy"]["robust"]["search"]["unevaluated_policies"]


def test_driver_horizon_inventory_and_clock_edits_change_identity(assets, neutralization):
    settings, baseline = neutralization
    for delta in ({"driver": 4}, {"horizon": 3}, {"sets": ("soft",)}, {"sc_at_s": 28}):
        snapshot, _, _ = prepare(ROOT, replace(settings, **delta), assets)
        assert snapshot.snapshot_id != baseline["snapshot"]["snapshot_id"]
    norris = execute(ROOT, Settings(driver=4, horizon=10), assets)
    start = norris["forecasts"][0]
    assert start["request"]["driver_number"] == 4
    assert len(start["mean"]) == 10


def test_exports_reproduce_and_reject_corruption(assets, neutralization, tmp_path):
    _, result = neutralization
    raw = export_run(result)
    assert export_run(replay(ROOT, raw, assets)) == raw
    path = tmp_path / "unknown.zip"
    with ZipFile(path, "w") as archive:
        archive.writestr("../manifest.json", b"{}")
    with pytest.raises(ValueError, match="Invalid decision-board export"):
        replay(ROOT, path.read_bytes(), assets)


def test_frozen_timing_preserves_cancellation_and_scheduled_execution(assets):
    timings = execute(ROOT, Settings(preset="timing", mode=MODES[1]), assets)["timing"]
    assert timings["case"]["sc-63-soft"]["regret_us"] == 46_754_185
    assert timings["case"]["p2-green-stay-out"]["visited_pits"] == []
    assert timings["case"]["p2-green-replan"]["visited_pits"][0]["target_lap"] == 33
    assert timings["case"]["p5-scheduled-conditional"]["visited_pits"][0]["target_lap"] == 31


def test_streamlit_invalidates_on_preset_edit():
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(ROOT / "src/f1ri/dashboard/app.py"), default_timeout=30).run()
    assert not app.exception
    assert app.title[0].value == "Race Intelligence Decision Board"
    next(button for button in app.button if button.label == "Run research analysis").click().run()
    assert any(x.value == "No unique recommendation." for x in app.warning)
    next(box for box in app.selectbox if box.label == "Snapshot preset").select("unsupported").run()
    assert not app.exception
    assert any("FULL_FIELD_PHYSICAL_STATE_UNAVAILABLE" in x.value for x in app.error)
    assert not any(x.value == "No unique recommendation." for x in app.warning)
    assert len(PRESETS) == 6
