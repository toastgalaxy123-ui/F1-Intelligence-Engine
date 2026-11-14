import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from f1ri.simulator.neutralization import simulate_neutralization
from f1ri.simulator.proximity import PositionQuality
from f1ri.simulator.strategy import (
    StrategyInputError,
    compare_strategies,
    run_strategy,
    validate_scenario,
)
from f1ri.simulator.strategy_pace import PaceMode
from f1ri.simulator.strategy_policy import Policy, PolicyKind, Stop
from f1ri.validation.strategy_fixtures import sensitivity_families, strategy_families
from f1ri.validation.strategy_io import scenario_from_document
from f1ri.validation.strategy_sources import audit_model_assets, frozen_pace_fixture
from f1ri.validation.strategy_study import _invariants

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def families():
    return strategy_families()


@pytest.fixture(scope="module")
def study(families):
    return {key: compare_strategies(s, "continue") for key, s in families.items()}


def test_identical_candidates_and_candidate_independence(families, study):
    s = families["green"]
    equal = replace(s, policies=(s.policies[0], Policy("copy", PolicyKind.CONTINUE)))
    assert compare_strategies(equal, "continue")["pairs"]["copy"]["combined_delta_us"] == 0
    actual = study["green"]["results"]["pit-now-medium"]
    assert run_strategy(s, s.policies[1]) == actual
    assert compare_strategies(replace(s, policies=tuple(reversed(s.policies))), "continue",
                              ["pit-now-medium"])["results"]["pit-now-medium"] == actual


def test_green_tyre_replacement_and_matched_route_loss(study):
    demo = study["green"]
    r = demo["results"]["pit-now-medium"]
    stop, = r["pit"]
    assert stop["decision_us"] < stop["entry_us"] < stop["merge_us"]
    assert stop["pit_elapsed_us"] == 57_000_000
    assert stop["pit_elapsed_us"] > stop["lane_motion_us"] + stop["service_us"]
    mounted = next(t for t in r["trajectory"] if t["event"] == "PIT_SERVICE_END" and
                   t["driver"] == t["event_driver"] == 63)
    assert (mounted["tyres"]["compound"], mounted["tyres"]["age_laps"]) == ("MEDIUM", 0)
    assert "medium" not in r["remaining_sets"][63]
    assert demo["pairs"]["pit-now-medium"]["pit_accounting"][0]["paired_clock_gap_at_merge_us"] is not None
    assert demo["results"]["extend-one"]["pit"][0]["entry_us"] > stop["entry_us"]
    assert len(demo["results"]["two-stop"]["pit"]) == 2
    assert r["finish_order"] is None


@pytest.mark.parametrize(("family", "policy", "control"), [("vsc", "react-vsc", "VSC"), ("sc", "react-sc", "SC")])
def test_reactive_policy_observes_only_committed_deployment(family, policy, control, study):
    results = study[family]["results"]
    r = results[policy]
    assert r["decisions"][0]["at_us"] == 156_000_000
    assert r["decisions"][0]["track_state"] == control
    assert r["pit"][0]["entry_us"] > 156_000_000
    assert not results["continue"]["pit"]
    assert results["after-green"]["pit"][0]["entry_us"] > r["pit"][0]["entry_us"]


def test_shared_box_and_red_walltime(study):
    runs = study["shared-box"]["results"]
    first, second = runs["stack"]["pit"]
    assert second["service_start_us"] == first["service_end_us"]
    assert second["wait_us"] > 0
    assert not any(p["wait_us"] for p in runs["63-first"]["pit"])
    red = study["red"]["results"]["pit-now-medium"]
    assert red["suspended_us"] == 28_000_000
    assert red["pit"][0]["entry_us"] > 184_000_000


def test_scripted_pass_is_the_only_pass(study):
    no_pass = study["traffic-no-pass"]["results"]["continue"]
    scripted = study["traffic-scripted-pass"]["results"]["continue"]
    assert not no_pass["pass_results"]
    assert [r["status"] for r in scripted["pass_results"]] == ["COMPLETED"]
    assert no_pass["targets"][2]["clock_us"] > scripted["targets"][2]["clock_us"]


@pytest.mark.parametrize(("action", "status", "outcome"), [
    (Stop(63, "unknown"), "UNSUPPORTED", "UNSUPPORTED"),
    (Stop(63, "medium", lap=2, review_us=180_000_000), "MARKERS_REACHED", "MISSED_ENTRY"),
    (Stop(63, "medium", cancel_us=160_000_000), "MARKERS_REACHED", "CANCELLED"),
])
def test_invalid_late_and_cancelled_stops(families, action, status, outcome):
    policy = Policy("stop", PolicyKind.TARGET, (action,))
    s = replace(families["green"], policies=(policy,))
    output = run_strategy(s, policy)
    assert output["status"] == status
    assert output["decisions"][-1]["outcome"] == outcome
    assert output["pit"] == []


def test_cancelled_set_can_be_used_in_a_later_stop(families):
    policy = Policy("two-decisions", PolicyKind.SEQUENCE,
                    (Stop(63, "medium", lap=2, cancel_us=160_000_000), Stop(63, "medium", lap=3)))
    s = replace(families["green"], policies=(policy,))
    output = run_strategy(s, policy)
    stop, = output["pit"]
    assert stop["target_lap"] == 3


def test_end_window_and_model_modes(families):
    s = families["green"]
    early_end = replace(s, end_us=s.initial.race_clock_us+10_000_000)
    out = compare_strategies(early_end, "continue", ["pit-now-medium"])
    assert not out["ranking"]
    assert all(r["status"] == "INCOMPLETE" and r["finish_order"] is None for r in out["results"].values())
    with pytest.raises(StrategyInputError, match="verified D8"):
        validate_scenario(replace(s, pace=replace(s.pace, mode=PaceMode.FULL_MODEL)))


def test_full_finish_and_dry_compound_scope(families):
    green = families["green"]
    state = replace(green.initial, ruleset=replace(green.initial.ruleset, scheduled_laps=4))
    resources = tuple(replace(r, prior_dry_compounds=("MEDIUM",)) for r in green.resources)
    s = replace(green, initial=state, resources=resources, targets=((63, 12),), policies=green.policies[:2],
                dry_race_rule=True, mandatory_compounds=("HARD",))
    result = compare_strategies(s, "continue", ["pit-now-medium"])
    assert result["results"]["continue"]["finish_order"]
    assert result["pairs"]["pit-now-medium"]["combined_delta_us"] is not None
    violation = replace(s, resources=tuple(replace(r, prior_dry_compounds=()) for r in resources))
    run = run_strategy(violation, violation.policies[0])
    assert run["status"] == "UNSUPPORTED" and run["finish_order"] is None
    assert "DRY_COMPOUND_RULE:63" in run["unsupported"]


def test_no_intervention_constant_pace_matches_the_frozen_engine(families, study):
    s = families["green"]
    old = simulate_neutralization(s.initial, s.physical, {63: 90., 4: 92.}, (),
                                  laps=s.horizon_laps, end_us=s.end_us)
    markers = {(f.event.driver_number, f.event.checkpoint): f.event.at_us for f in old.execution.run.trace
               if f.event.checkpoint is not None}
    assert study["green"]["results"]["continue"]["targets"][63]["clock_us"] == markers[63, 17]


def test_unresolved_initial_order_and_red_active_pit_are_unsupported(families):
    s = families["green"]
    poor = replace(s.physical.cars[0], uncertainty_m=5, quality=PositionQuality.OBSERVED_ESTIMATE)
    with pytest.raises(StrategyInputError, match="exact declared field"):
        validate_scenario(replace(s, physical=replace(s.physical, cars=(poor, *s.physical.cars[1:]))))
    red = families["red"]
    controls = (replace(red.controls[0], at_us=170_000_000), *red.controls[1:])
    result = compare_strategies(replace(red, controls=controls), "continue", ["pit-now-medium"])
    assert result["results"]["pit-now-medium"]["status"] == "UNSUPPORTED"
    assert "RED_WITH_ACTIVE_PIT_ITINERARY" in result["results"]["pit-now-medium"]["unsupported"]
    assert result["pairs"]["pit-now-medium"]["combined_delta_us"] is None


def test_unresolved_merge_cannot_receive_a_rank(families):
    green = families["green"]
    left, right = green.resources
    route = replace(left.route, merge_s=left.route.merge_s - 4.833333)
    s = replace(green, resources=(replace(left, route=route), right))
    out = compare_strategies(s, "continue", ["pit-now-medium"])
    assert "SIMULTANEOUS_MERGE_UNRESOLVED" in out["results"]["pit-now-medium"]["unsupported"]
    assert out["pairs"]["pit-now-medium"]["combined_delta_us"] is None


def test_round_trip_and_all_declared_invariants(families, study):
    for key, s in families.items():
        doc = json.loads(json.dumps(asdict(s), default=str))
        assert scenario_from_document(doc) == s
        assert all(all(row.values()) for row in _invariants(s, study[key]).values())


def test_sensitivity_reverses_sc_ranking(families):
    deltas = []
    for label, scenario, candidate in sensitivity_families(families):
        if label in ("faster-sc", "longer-sc"):
            r = compare_strategies(scenario, "continue", [candidate])
            deltas.append(r["pairs"][candidate]["combined_delta_us"])
    assert min(deltas) < 0 < max(deltas)


def test_frozen_pace_samples_are_paired_if_available():
    spec, _, assets = audit_model_assets(ROOT)
    if assets["frozen_pace_replay"]["status"] == "UNAVAILABLE":
        pytest.skip("Ignored S1 trajectory sample artifact is not installed.")
    a = frozen_pace_fixture(ROOT, spec, 0)
    b = frozen_pace_fixture(ROOT, spec, 1)
    r = compare_strategies(a, "continue")
    assert a.pace.stream_id != b.pace.stream_id
    assert r["pairs"]["repeat"]["combined_delta_us"] == 0
    assert r["results"]["repeat"]["trajectory"] == r["results"]["continue"]["trajectory"]
    document = json.loads(json.dumps(asdict(a), default=str))
    assert scenario_from_document(document, ROOT) == a
    document["pace"]["curves"][0]["lap_seconds"][0] += 1
    with pytest.raises(StrategyInputError, match="pinned fixture"):
        scenario_from_document(document, ROOT)
