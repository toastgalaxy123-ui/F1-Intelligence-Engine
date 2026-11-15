from dataclasses import replace
from pathlib import Path

import pytest

from f1ri.simulator.strategy_commitment import (
    ACTIVE,
    FIXED,
    ORIGINAL,
    REPLAN,
    REVALIDATE,
    alternatives,
)
from f1ri.simulator.strategy_robust import evaluate_family
from f1ri.validation.commitment_study import (
    boundary_controls,
    recommendation_support,
    reproduce,
    verified_baseline,
)
from f1ri.validation.robust_scenarios import _study

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def validation():
    bundle, frozen, _, checked = verified_baseline(ROOT)
    assert len(checked) == 29
    return bundle, frozen, tuple(_study(ROOT))


@pytest.fixture(scope="module")
def boundary(validation):
    bundle, _, families = validation
    case, timings = boundary_controls(families[0].initial, bundle)
    result = evaluate_family(case, bundle, extra_policies=alternatives(case.initial))
    return result, timings


@pytest.fixture(scope="module")
def revised(validation):
    bundle, _, families = validation
    stress = families[1]
    pivot = next(v for v in stress.scenarios if v.scenario_id == "sc-deploy-58")
    small = replace(stress, scenarios=(pivot,))
    return evaluate_family(small, bundle, extra_policies=alternatives(stress.initial))


def test_exact_frozen_regrets_and_original_stop(revised, validation):
    bundle, old, families = validation
    _, replay = reproduce(families, bundle, old)
    assert replay["regret_us"] == 46_754_185
    assert replay["fixed_worst_regret_us"] == 13_518_744
    assert replay["marker_gap"][-1]["checkpoint"] == 106
    old_run = replay["outcomes"][ORIGINAL]
    assert old_run["pit"][0]["entry_us"] == 3_109_301_035
    case = revised["cases"]["sc-deploy-58"]["paired"]["results"]
    assert case[ORIGINAL]["pit"] == case[ACTIVE]["pit"]
    assert revised["analysis"]["regret"][ORIGINAL]["sc-deploy-58"] >= 0


@pytest.mark.parametrize("flag", ("sc", "vsc"))
def test_green_release_revision_requires_strict_spatial_margin(boundary, flag):
    result, meta = boundary
    clock = meta[flag.upper()]["prefix_entry_us"]
    for tag, shift in (("before", -1), ("equal", 0), ("after", 1)):
        rows = result["cases"][f"{flag}-release-{tag}"]["paired"]["results"]
        new, original = rows[REVALIDATE], rows[ORIGINAL]
        control = next(r for r in new["race_control"] if r["event"]["command"] == "GREEN_RELEASE")
        assert control["event"]["at_us"] == clock+shift
        revisions = [r for r in new["decisions"] if r["outcome"] == "REVISED_CANCEL"]
        assert original["pit"][0]["entry_us"] == clock
        if shift < 0:
            assert not new["pit"] and [r["at_us"] for r in revisions] == [clock-1]
            assert "soft" in new["remaining_sets"][63]
        else:
            assert not revisions and len(new["pit"]) == 1
            assert new["pit"][0]["entry_us"] == clock
            assert new["pit"][0]["track_state_at_entry"] == ("GREEN" if shift == 0 else flag.upper())


def test_sc_deployment_boundary_no_retroactive_entry(boundary):
    result, _ = boundary
    outcomes = result["cases"]
    soft_laps = [outcomes[f"sc-deployment-{k}"]["rows"][ORIGINAL]["visited_pits"][0]["target_lap"]
                 for k in ("before", "equal", "after")]
    assert soft_laps == [31, 32, 32]
    for k in ("before", "equal", "after"):
        run = outcomes[f"sc-deployment-{k}"]["paired"]["results"][FIXED]
        assert len(run["pit"]) == 1
        assert run["pit"][0]["wait_us"] >= 0
        assert not run["pass_results"]


def test_replan_only_after_observed_green_and_consumes_one_set(revised):
    case = revised["cases"]["sc-deploy-58"]
    run = case["paired"]["results"][REPLAN]
    refs = [r for r in run["decisions"] if r["outcome"] in ("REQUESTED", "REVISED_CANCEL")]
    assert [r["outcome"] for r in refs] == ["REQUESTED", "REVISED_CANCEL", "REQUESTED"]
    assert [r["at_us"] for r in refs] == [2_995_075_000, 3_087_075_000, 3_119_123_927]
    assert refs[0]["decision_id"] == refs[1]["decision_id"] != refs[2]["decision_id"]
    assert [r["target_lap"] for r in run["pit"]] == [33]
    assert [r["set_id"] for r in run["pit"]] == ["medium"]
    assert len([r for r in run["pit_events"] if r["status"] == "SERVICE_COMPLETE"]) == 1
    assert "soft" in run["remaining_sets"][63] and "medium" not in run["remaining_sets"][63]
    assert case["rows"][REPLAN]["status"] == "MARKERS_REACHED"


def test_scope_labels_prevent_false_unique_recommendation():
    row = {"following_us": 1400, "visited_pits": [1], "delta_us": 0}
    primary = {"selected": ["a","b"], "outcomes": {"green": {FIXED: row, ORIGINAL: row|{"visited_pits": [2]}}},
               "rankings": [], "near": [], "status": "ROBUST_SEARCH_COMPLETE"}
    support = recommendation_support({"A-primary": primary, "B-stress": primary|{"selected": ["c"],
               "status": "ROBUST_SEARCH_INCOMPLETE"}}, [{"flag":"SC"}])
    assert support["unique_action"] is None
    assert {"FAMILY_SENSITIVE", "BOUNDARY_SENSITIVE", "SEARCH_INCOMPLETE",
            "CONSTRAINT_DOMINATED_IDENTIFIABILITY", "NO_UNIQUE_RECOMMENDATION"} <= set(support["statuses"])


def test_revised_cache_and_order_parity(validation):
    bundle, _, families = validation
    f = families[0]
    subset = replace(f, scenarios=f.scenarios[:2])
    first = evaluate_family(subset, bundle, extra_policies=alternatives(f.initial))
    initial = replace(f.initial, cache_forecasts=False)
    other = replace(subset, initial=initial, scenarios=tuple(
        replace(v, request=replace(v.request, cache_forecasts=False)) for v in reversed(subset.scenarios)))
    second = evaluate_family(other, bundle, extra_policies=tuple(reversed(alternatives(initial))))
    assert first["selected"] == second["selected"]
    assert first["analysis"] == second["analysis"]
    assert all(c["paired"]["results"] == second["cases"][s]["paired"]["results"] for s,c in first["cases"].items())
