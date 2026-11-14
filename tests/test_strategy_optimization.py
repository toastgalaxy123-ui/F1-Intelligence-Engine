from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from f1ri.simulator.strategy import StrategyInputError
from f1ri.simulator.strategy_optimization import REFERENCE, optimize
from f1ri.simulator.strategy_policy import Stop
from f1ri.validation.optimization_scenarios import fixture_request, scenario_requests
from f1ri.validation.optimization_study import _parity, verified_optimizer

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def study():
    bundle, _ = verified_optimizer(ROOT)
    return bundle, scenario_requests(ROOT)


def request(study, scene, **kwargs):
    return fixture_request(ROOT, scene, fixtures=study[1], **kwargs)


def evaluated(result, name, stage="conditional"):
    return next(r for r in result[stage]["rows"] if r["candidate"] == name)


def test_exhaustive_parity_and_reordered_subset(study):
    parity = _parity(ROOT, study[0])
    for key, rows in parity.items():
        if key != "cache":
            assert rows["fixed_space_equal"] and rows["exhausted"]
            assert rows["reordered_subset_equal"]
            assert all(all(comparison.values()) for comparison in rows["matched"].values())
    assert parity["cache"]["on_off_candidate_results_equal"]
    assert parity["cache"]["calls_with_cache"] < parity["cache"]["calls_without_cache"]


def test_future_vsc_cannot_change_prospective_selection_or_budget(study):
    req = request(study, "late-vsc", budget=8)
    future = optimize(req, study[0])
    green = optimize(replace(req, scenario=replace(req.scenario, controls=())), study[0])
    assert green["decision"] == future["decision"]
    assert green["unevaluated"] == future["unevaluated"]
    assert green["search"]["simulator_evaluations"] == 4
    assert future["search"]["simulator_evaluations"] == 8
    assert future["completion"] == "BOUNDED_SEARCH_INCOMPLETE"


@pytest.mark.parametrize(("scene", "prefix", "command"), [("vsc", "vsc", "VSC_DEPLOY"), ("sc", "sc", "SC_DEPLOY")])
def test_reactive_pit_uses_committed_announcement(study, scene, prefix, command):
    req = request(study, scene)
    result = optimize(req, study[0])
    candidate = f"{prefix}-63-soft"
    branch = result["conditional"]["paired"]["results"][candidate]
    committed = next(r["event"]["at_us"] for r in branch["race_control"]
                     if r["event"]["command"] == command and r["outcome"] == "COMMITTED")
    assert branch["pit"][0]["decision_us"] >= committed
    assert result["decision"]["paired"]["results"][candidate]["pit"] == []
    assert any(k == candidate for k in result["conditional"]["best"])
    assert not any(c["strategy_id"].startswith("oracle") for c in result["candidates"])
    oracle = optimize(replace(req, information_mode="PERFECT_FORESIGHT"), study[0])
    assert any(c["strategy_id"].startswith("oracle") for c in oracle["candidates"])


def test_following_keeps_tie_and_removal_restores_difference(study):
    hold = optimize(request(study, "two-car"), study[0])
    removed = optimize(request(study, "retirement"), study[0])
    medium, soft = "63-L31-medium", "63-L31-soft"
    assert evaluated(hold, medium)["objective_us"] == evaluated(hold, soft)["objective_us"]
    assert evaluated(hold, soft)["following_us"] > 0
    assert evaluated(removed, medium)["objective_us"] != evaluated(removed, soft)["objective_us"]
    assert REFERENCE in hold["conditional"]["best"]
    assert all(not branch["pass_results"] for branch in hold["conditional"]["paired"]["results"].values())


def test_two_stops_have_distinct_resources_and_cutoff_known_forecasts(study):
    req = request(study, "clean-air", sets=("medium", "used-soft"), max_stops=2, budget=128)
    result = optimize(req, study[0])
    row = result["conditional"]["paired"]["results"]["63-L31-medium__63-L33-used-soft"]
    assert [p["set_id"] for p in row["pit"]] == ["medium", "used-soft"]
    assert row["pit"][0]["merge_us"] < row["pit"][1]["entry_us"]
    requests = [f["request"] for f in row["forecast_requests"]]
    assert len({r["stint_number"] for r in requests}) == 3
    assert len({r["as_of_utc"] for r in requests}) == 1
    assert requests[-1]["tyre_age_at_start"] == 3
    assert not requests[-1]["observed_stint_clean_times_s"]
    assert {r["reason"] for r in result["rejected"]} >= {"TYRE_SET_REUSED", "SECOND_STOP_BEFORE_SEPARATE_STINT"}


def test_inventory_and_existing_plan(study):
    req = request(study, "clean-air", sets=("medium", "unavailable"), max_stops=2)
    out = optimize(req, study[0])
    assert any(r["reason"] == "TYRE_SET_UNAVAILABLE" for r in out["rejected"])
    with pytest.raises(StrategyInputError, match="Existing plans failed"):
        optimize(replace(req, planned_stops=(Stop(63, "unavailable", 31),)), study[0])
    fixed = optimize(replace(req, planned_stops=(Stop(63, "medium", 31),)), study[0])
    assert all(c["stops"][0]["set_id"] == "medium" for c in fixed["candidates"])
    assert evaluated(fixed, REFERENCE)["pit_visits"] == 1


def test_team_preserves_each_driver_time_and_common_marker(study):
    req = request(study, "shared-box", sets=("hard", "soft"), stop_options=2, max_stops=2, budget=128)
    combined = optimize(req, study[0])
    weighted = optimize(replace(req, objective="TEAM_WEIGHTED"), study[0])
    key = "4-L30-hard__63-L31-hard"
    row = evaluated(combined, key)
    other = evaluated(weighted, key)
    assert row["objective_us"] == sum(row["driver_elapsed_us"].values())
    assert other["objective_us"] == row["driver_elapsed_us"][63]*2+row["driver_elapsed_us"][4]
    assert other["driver_elapsed_us"] == row["driver_elapsed_us"]
    assert len({r["checkpoint"] for r in other["markers"].values()}) == 1
    assert row["service_wait_us"] > 0


@pytest.mark.parametrize(("change", "code"), [
    ("horizon", "HORIZON_UNSUPPORTED"),
    ("model", "MODEL_UNVERIFIED"),
    ("field", "PHYSICAL_STATE_UNSUPPORTED"),
    ("forecast-clock", "REQUEST_UNSUPPORTED"),
])
def test_unsupported_requests_rejected_before_simulation(study, change, code):
    req = request(study, "clean-air")
    if change == "horizon":
        req = replace(req, scenario=replace(req.scenario, horizon_laps=20))
    if change == "model":
        req = replace(req, model_sha256="0"*64)
    if change == "field":
        req = replace(req, scenario=replace(req.scenario, field_support="EXACT_HISTORICAL"))
    if change == "forecast-clock":
        feature, = req.features
        req = replace(req, features=(replace(feature, evidence_as_of=
                      (("identity", feature.request.as_of_utc+timedelta(seconds=1)),)),))
    with pytest.raises(StrategyInputError) as issue:
        optimize(req, study[0])
    assert issue.value.code == code


def test_unavailable_control_timestamp_cannot_reach_the_policy(study):
    req = request(study, "vsc")
    event, *others = req.scenario.controls
    available = req.scenario.initial.race_start_utc + timedelta(microseconds=event.at_us+1)
    req = replace(req, scenario=replace(req.scenario, controls=(replace(event, available_utc=available), *others)))
    with pytest.raises(StrategyInputError) as issue:
        optimize(req, study[0])
    assert issue.value.code == "CONTROL_AVAILABILITY"


def test_optimizer_forecasts_expose_mean_ledger_only(study):
    result = optimize(request(study, "vsc"), study[0])
    for row in result["conditional"]["paired"]["results"]["vsc-63-soft"]["forecast_requests"]:
        assert row["sample_index"] is None
        assert row["selected_lap_sample_s"] is None
        for lap in row["forecast"]["lap_forecasts"]:
            assert set(lap) == {"target_lap_number", "projected_tyre_age", "support_status", "extrapolation_warning",
                                "expected_lap_time_s", "scenario_lap_time_s"}


def test_nonreached_markers_have_no_ranking_or_time(study):
    req = request(study, "clean-air")
    early = replace(req.scenario, end_us=req.scenario.initial.race_clock_us+1)
    result = optimize(replace(req, scenario=early), study[0])
    assert not result["conditional"]["best"]
    assert all(r["objective_us"] is None for r in result["conditional"]["rows"])
