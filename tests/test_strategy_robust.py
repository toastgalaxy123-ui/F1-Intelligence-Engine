from dataclasses import replace
from pathlib import Path

import pytest

from f1ri.simulator.state import TrackState
from f1ri.simulator.strategy import StrategyInputError
from f1ri.simulator.strategy_policy import PolicyView
from f1ri.simulator.strategy_regret import PERFECT_INFORMATION, analyze_matrix
from f1ri.simulator.strategy_robust import (
    ObservedPolicy,
    ScenarioFamily,
    evaluate_family,
    policy_library,
)
from f1ri.validation.optimization_scenarios import fixture_request
from f1ri.validation.robust_scenarios import _study, neutralization, variant
from f1ri.validation.robust_study import _parity_checks, verified_robust_model

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def engine():
    bundle, _ = verified_robust_model(ROOT)
    return bundle


@pytest.fixture(scope="module")
def primary():
    return next(_study(ROOT))


@pytest.fixture
def matrix():
    return {"a": {"reference": 20, "aggressive": 12, "safer": 18, "dominated": 25},
            "b": {"reference": 10, "aggressive": 13, "safer": 9, "dominated": 20}}


def test_hand_calculated_regret_dominance_and_risk_criteria(matrix):
    result = analyze_matrix(matrix, {}, complete=True, oracle_complete={})
    assert result["regret"]["aggressive"] == {"a": 0, "b": 4}
    assert result["regret"]["safer"] == {"a": 6, "b": 0}
    assert result["winners"]["MINIMAX_REGRET"] == ["aggressive"]
    assert result["winners"]["WORST_CASE_RELATIVE_LOSS"] == ["safer"]
    assert result["pareto"] == ["aggressive", "safer"]
    assert result["dominance"]["dominated"] == ["aggressive", "reference", "safer"]


def test_oracle_regrets_are_separate_and_incomplete_not_zero(matrix):
    comparison = analyze_matrix(matrix, {"a": 11, "b": 8}, complete=True, oracle_complete={"a": True, "b": False},
                                comparator=PERFECT_INFORMATION)
    assert comparison["perfect_information_regret"]["aggressive"] == {"a": 1, "b": None}
    assert comparison["winners"]["MINIMAX_REGRET"] == []
    assert comparison["regret"]["aggressive"]["a"] == 0


def test_declared_weights_use_exact_fraction_and_support(matrix):
    matrix["b"]["dominated"] = None
    out = analyze_matrix(matrix, {}, complete=True, oracle_complete={}, weights=(("a", 2), ("b", 1)))
    assert out["weights_mode"] == "USER_DECLARED_SCENARIO_WEIGHTS"
    row = next(r for r in out["rows"] if r["policy"] == "aggressive")
    assert row["weighted_delta_exact"] == "-13/3"
    assert out["winners"]["DECLARED_WEIGHTED_DELTA"] == ["aggressive"]
    assert "dominated" not in out["dominance"]
    assert next(r for r in out["rows"] if r["policy"] == "dominated")["worst_delta_us"] is None
    with pytest.raises(ValueError, match="nonnegative weight"):
        analyze_matrix(matrix, {}, complete=True, oracle_complete={}, weights=(("a", 3),))


def test_incomplete_budget_never_invents_regret(engine, primary):
    result = evaluate_family(replace(primary, total_budget=12), engine)
    assert result["status"] == "ROBUST_SEARCH_INCOMPLETE"
    assert result["search"]["evaluated_policy_scenario"] == 12
    assert result["search"]["simulator_evaluations"] == 12
    assert result["selected"] == []
    assert all(v is None for r in result["analysis"]["regret"].values() for v in r.values())
    assert result["analysis"]["frontier_scope"] == "EVALUATED_SUBSET_ONLY"


def test_single_scenario_baseline_reordering_and_cache_parity(engine, primary):
    checks, cache = _parity_checks(primary, engine)
    assert all(checks.values())
    assert cache["shared_typed_hits"] > 0
    assert cache["cached_calls"] < cache["uncached_calls"]


def test_contingent_policy_uses_commits_and_preserves_different_actions(engine, primary):
    result = evaluate_family(primary, engine)
    assert result["status"] == "ROBUST_SEARCH_COMPLETE"
    winners = result["selected"]
    assert len(winners) == 3
    p = "adaptive-63-medium-or-soft"
    assert p in winners
    green = result["cases"]["green"]["rows"][p]
    assert [v["set_id"] for v in green["visited_pits"]] == ["medium"]
    for name in ("early-vsc", "late-vsc", "early-sc"):
        case = next(v for v in primary.scenarios if v.scenario_id == name)
        row = result["cases"][name]["rows"][p]
        assert row["committed_actions"][0]["at_us"] >= case.request.scenario.controls[0].at_us
        assert [v["set_id"] for v in row["visited_pits"]] == ["soft"]
        assert not any(k.startswith("oracle-") for k in result["policies"])
    pair = next(x for x in result["pairs"] if {x["a"], x["b"]} == {p, "adaptive-63-hard-or-soft"})
    assert pair["cases"]["green"]["classification"] == "EQUAL_OUTCOME_DIFFERENT_ACTIONS"
    assert pair["cases"]["early-vsc"]["same_actions"]


def test_policy_view_cannot_trigger_from_flag_without_commit_or_after_deadline(primary):
    policies, _ = policy_library(primary.initial)
    p = next(k for k in policies if k.strategy_id == "adaptive-63-medium-or-soft")
    assert isinstance(p, ObservedPolicy)
    stop, = p.stops
    scene = primary.initial.scenario
    car = next(c for c in scene.initial.cars if c.driver_number == 63)
    view = PolicyView(scene.initial.race_clock_us+1, TrackState.VSC, (), car, 2500, 31, 4, 1, 24.6,
                      ("medium", "soft"), None)
    assert p.resolve(stop, view) is None
    view = replace(view, committed_signals=("VSC_DEPLOY",))
    assert p.resolve(stop, view).set_id == "soft"
    assert p.resolve(stop, replace(view, clock_us=p.deadline_us+1)) is None
    fallback = replace(view, track_state=TrackState.GREEN, next_pit_lap=33)
    assert p.resolve(stop, fallback).set_id == "medium"


def test_horizon_uses_same_definition_and_omits_action_after_marker(engine):
    long = fixture_request(ROOT, "clean-air", horizon=10, sets=("soft",), max_stops=2)
    short = fixture_request(ROOT, "clean-air", horizon=3, sets=("soft",), max_stops=2)
    family = ScenarioFamily("prefix", short, (variant(short, "green", "GREEN"),), library_reference=long)
    result = evaluate_family(family, engine)
    row = result["cases"]["green"]["rows"]["63-L33-soft"]
    assert row["marker_note"] == "PLANNED_ACTION_BEYOND_MARKER"
    assert row["objective_delta_us"] == 0
    assert row["visited_pits"] == []
    assert result["policies"]["63-L33-soft"]["stops"][0]["lap"] == 33


def test_timing_assumptions_cannot_change_initial_horizon_or_inventory(engine, primary):
    case = primary.scenarios[0]
    scene = case.request.scenario
    change = replace(case, request=replace(case.request, scenario=replace(scene, horizon_laps=10)))
    with pytest.raises(StrategyInputError) as error:
        evaluate_family(replace(primary, scenarios=(change,)), engine)
    assert error.value.code == "HORIZON_MISMATCH"
    inventory = tuple(replace(r, tyres=r.tyres[1:]) for r in scene.resources)
    change = replace(case, request=replace(case.request, scenario=replace(scene, resources=inventory)))
    with pytest.raises(StrategyInputError) as error:
        evaluate_family(replace(primary, scenarios=(change,)), engine)
    assert error.value.code == "FAMILY_INITIAL_STATE"


def test_perfect_information_oracle_budget_is_separate(engine, primary):
    req = fixture_request(ROOT, "two-car", sets=("hard", "soft"), stop_options=1, budget=16)
    cases = neutralization(req, vsc=(5,), sc=(), restart=False, longer=False)
    family = ScenarioFamily("budgeted-oracle", req, cases, contingent=False, comparator=PERFECT_INFORMATION,
                            total_budget=2*9)
    result = evaluate_family(family, engine)
    assert result["search"]["fixed_space_exhausted"]
    assert not result["search"]["perfect_information_complete"]
    assert result["status"] == "ROBUST_SEARCH_INCOMPLETE"
    assert result["selected"] == []
