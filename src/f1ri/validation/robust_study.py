"""Execute and publish the declared S6-B scenario matrix and audit ledgers."""

from dataclasses import asdict, replace
from json import dumps, loads
from pathlib import Path

import yaml

from f1ri.simulator.strategy import StrategyInputError, fingerprint
from f1ri.simulator.strategy_optimization import optimize
from f1ri.simulator.strategy_regret import analyze_matrix
from f1ri.simulator.strategy_robust import INITIAL, evaluate_family, policy_library
from f1ri.validation.optimization_study import _parquet, verified_optimizer
from f1ri.validation.proximity_pilot import _json
from f1ri.validation.robust_scenarios import _study, variant
from f1ri.validation.stage3_r_release import REPORT
from f1ri.validation.stage3_r_sources import digest
from f1ri.validation.strategy_study import _invariants

S6_A = {
    "artifacts/reports/stage6_s6_a_metrics.json": "c86ecbb21718e8fd1878ccdefffe2e7893faaf3428de56cda167ecdf29368c11",
    "artifacts/reports/stage6_s6_a_ledgers.zip": "80e969d085a04f6fe11d421d21225223442f0716f7ba430bec5936df378a46c7",
}
PROTOCOL = "config/experiments/stage6_s6_b.yaml"


def verified_robust_model(root):
    hashes = {p: digest(root/p) if (root/p).is_file() else None for p in S6_A}
    if hashes != S6_A:
        raise StrategyInputError("FROZEN_INTEGRITY", "The published S6-A metrics or evidence archive differ.")
    bundle, prior = verified_optimizer(root)
    return bundle, hashes | prior


def _by_pair(result, a, b):
    return next(r for r in result["pairs"] if {r["a"], r["b"]} == {a, b})


def _summary(family, result):
    return {"family": family.family_id, "horizon": family.initial.scenario.horizon_laps, "objective": family.objective,
            "team_objective": family.initial.objective, "comparator": result["comparator"], "status": result["status"],
            "selected": result["selected"], "presentation": result["presentation"], "near": result["near"],
            "near_margin_us": result["near_margin_us"], "search": result["search"],
            "weights_mode": result["analysis"]["weights_mode"], "weights": dict(family.weights),
            "scenarios": {v.scenario_id: {"category": v.category, "changed_assumptions": v.changed_assumptions,
                           "provenance": v.provenance,
                           "best_realized_policies": [p for p, by in result["analysis"]["regret"].items()
                                                      if by[v.scenario_id] == 0],
                           "controls": [asdict(e) for e in v.request.scenario.controls]}
                          for v in family.scenarios},
            "results": result["analysis"]["rows"], "other_objectives": result["analysis"]["winners"]}


def _ledger_tables(family, result, tables):
    executions, outcomes, regrets, physical = tables
    by_variant = {v.scenario_id: v for v in family.scenarios}
    for name, case in result["cases"].items():
        scene = by_variant[name].request.scenario
        for policy, row in case["rows"].items():
            context = {"family": family.family_id, "scenario": name, "policy": policy}
            eligible = policy in result["policies"]
            entry = row | {"role": "EXECUTABLE_POLICY" if eligible else "ORACLE_ONLY"}
            outcomes.append(context | {k: val for k, val in entry.items() if k not in {
                "committed_actions", "visited_pits", "markers", "objective_delta_us"}})
            if eligible:
                regrets.append(context | {
                    "elapsed_us": row["objective_us"], "delta_us": result["analysis"]["deltas"][policy][name],
                    "best_realized_regret_us": result["analysis"]["regret"][policy][name],
                    "perfect_information_regret_us": result["analysis"]["perfect_information_regret"][policy][name],
                    "status": row["status"], "oracle_complete": result["oracles"][name]["complete"]})
            if case["paired"] is None:
                continue
            run = case["paired"]["results"][policy]
            executions.append(context | {
                "initial_sha256": case["paired"]["matched"]["initial_state_hash"],
                "assumptions_sha256": case["paired"]["matched"]["exogenous_hash"],
                "policy_sha256": fingerprint(result["policies"].get(policy, run["policy"])),
                "execution_sha256": fingerprint(run),
                "actual_actions": row["committed_actions"], "pit": run["pit"], "decisions": run["decisions"],
                "arrival_order": [p["driver"] for p in sorted(
                    (p for p in run["pit"] if p["arrival_us"] is not None), key=lambda p: p["arrival_us"])],
                "service_order": [p["driver"] for p in sorted(
                    (p for p in run["pit"] if p["service_start_us"] is not None), key=lambda p: p["service_start_us"])],
                "control": run.get("race_control", []), "pit_events": run.get("pit_events", []),
                "progress": run.get("trajectory", []), "field_motion": run.get("physical_progress", []),
                "forecast_requests": run.get("forecast_requests", []), "passes": run.get("pass_results", []),
                "restart_permissions": run.get("restart_permissions", []), "markers": row["markers"]})
            physical.extend(context | {"horizon": scene.horizon_laps, **constraint}
                            for constraint in run.get("checkpoint_constraints", []))


def _write_table(path, rows):
    _parquet(path, [
        {k: dumps(v, default=str, sort_keys=True) if isinstance(v, (dict, list, tuple)) else v for k, v in row.items()}
        for row in rows
    ])


def _evidence_for(family, result, evidence):
    if family.family_id == "primary":
        cases = result["cases"]
        ref = "reference"

        early = cases["early-vsc"]["rows"]["vsc-63-soft"]["committed_actions"]
        sc = cases["early-sc"]["rows"]["sc-63-soft"]["committed_actions"]
        vsc_clock = next(v.request.scenario.controls[0].at_us for v in family.scenarios if v.scenario_id == "early-vsc")
        sc_clock = next(v.request.scenario.controls[0].at_us for v in family.scenarios if v.scenario_id == "early-sc")
        green_actions = cases["green"]["rows"]["vsc-63-soft"]["committed_actions"]
        eq = _by_pair(result, "vsc-63-soft", ref)
        evidence.update({
            "B03_green_sc": bool(sc) and not cases["green"]["rows"]["sc-63-soft"]["committed_actions"],
            "B05_vsc_observed_only": not green_actions and bool(early) and all(x["at_us"] >= vsc_clock for x in early),
            "B06_sc_observed_only": bool(sc) and all(x["at_us"] >= sc_clock for x in sc),
            "B07_oracle_separate": any(row["complete"] and row["oracle_only_evaluated"] > 0
                                       for row in result["oracles"].values())
            and not any(k.startswith("oracle-") for k in result["policies"]),
            "B09_minimax_regret": min(r["worst_regret_us"] for r in result["analysis"]["rows"]
                                      if r["comparable"]) == next(r["worst_regret_us"] for r in result["analysis"]["rows"]
                                                                 if r["policy"] in result["selected"]),
            "B13_exact_ties": len(result["selected"]) > 1,
            "B14_trigger_equivalence_subset": eq["equivalence"] == "EQUIVALENT_IN_SUBSET"
            and eq["cases"]["green"]["same_actions"] and not eq["cases"]["early-vsc"]["same_actions"],
        })
    if family.family_id == "separate-clean-field":
        late = _by_pair(result, "63-L32-soft", "reference")
        evidence["B04_late_vsc_near_equal"] = late["cases"]["clean-late-vsc"]["classification"] == "NEAR_EQUAL_OUTCOME"
        cases = result["cases"]
        evidence["B02_green_vsc_exchange"] = (
            cases["clean-air"]["rows"]["63-L31-soft"]["objective_delta_us"] > 0
            and cases["clean-early-vsc"]["rows"]["63-L31-soft"]["objective_delta_us"] < 0)
    if family.family_id == "initial-gap":
        comparison = _by_pair(result, "63-L31-hard", "63-L31-soft")["cases"]
        evidence["B15_following_limited"] = (comparison["close-gap"]["delta_us"] == 0
                                             and any(v > 0 for v in comparison["close-gap"]["following_us"]))
        evidence["B16_initial_gap_changes_ranking"] = comparison["wider-gap"]["delta_us"] != 0
    if family.family_id == "pit-entry-boundary":
        row = {s: case["rows"]["63-L31-soft"]["visited_pits"] for s, case in result["cases"].items()}
        clock = family.initial.scenario.initial.race_clock_us+29_967_958
        evidence["B17_pit_entry_boundary"] = all(len(p) == 1 for p in row.values()) and (
            row["before"][0]["entry_us"] >= clock-1 and row["after"][0]["entry_us"] < clock+1)
    if family.family_id == "team-combined":
        evidence["B18_shared_box_queue"] = any((p["wait_us"] or 0) > 0 for case in result["cases"].values()
                                               for r in case["rows"].values() for p in r["visited_pits"])
    if family.family_id == "budget-limited":
        evidence["B21_incomplete_withholds_result"] = result["status"] == "ROBUST_SEARCH_INCOMPLETE" and (
            not result["selected"] and not any(v == 0 for r in result["analysis"]["regret"].values() for v in r.values()))


def _parity_checks(family, bundle):
    base = family.initial
    small = replace(base, windows=(replace(base.windows[0], last_lap=base.windows[0].first_lap+1,
                                          set_ids=("hard", "soft")),))
    cases = tuple(variant(small, key, req.changed_assumptions, controls=req.request.scenario.controls)
                  for key, req in ((v.scenario_id, v) for v in family.scenarios[:3]))
    compact = replace(family, initial=small, scenarios=cases, include_oracle=False, contingent=False)
    full_policies = policy_library(small, contingent=False)[0]
    one_case = replace(compact, scenarios=cases[:1])
    identical = replace(compact, scenarios=(cases[0], replace(cases[0], scenario_id="same")))
    single = evaluate_family(one_case, bundle)
    duplicate = evaluate_family(identical, bundle)
    expected = optimize(cases[0].request, bundle)["conditional"]
    same = single["cases"][cases[0].scenario_id]["paired"]
    full = evaluate_family(compact, bundle)
    reverse = evaluate_family(replace(compact, scenarios=tuple(reversed(cases))), bundle,
                              library=tuple(reversed(full_policies)))
    subset = evaluate_family(replace(compact, scenarios=cases[:2]), bundle, library=full_policies[:5])
    uncached = evaluate_family(replace(compact, initial=replace(small, cache_forecasts=False), scenarios=tuple(
        replace(v, request=replace(v.request, cache_forecasts=False)) for v in cases)), bundle)
    uncached_equal = all(row["paired"]["results"] == uncached["cases"][s]["paired"]["results"]
                         for s, row in full["cases"].items())
    full_equal = all(case["rows"] == reverse["cases"][s]["rows"] for s, case in full["cases"].items())
    addition = all(run == full["cases"][s]["paired"]["results"][k]
                   for s, case in subset["cases"].items() for k, run in case["paired"]["results"].items())
    malformed = replace(one_case, scenarios=(replace(cases[0], request=replace(
        cases[0].request, scenario=replace(cases[0].request.scenario, horizon_laps=10))),))
    mismatch = False
    try:
        evaluate_family(malformed, bundle)
    except StrategyInputError as error:
        mismatch = error.code == "HORIZON_MISMATCH"
    unsupported_base = replace(small, features=())
    unsupported = evaluate_family(replace(one_case, initial=unsupported_base, scenarios=(
        variant(unsupported_base, "unavailable", "Missing forecast data"),)), bundle)
    matrix = {"first": {"reference": 20, "a": 12, "b": 18, "worse": 25},
              "second": {"reference": 10, "a": 13, "b": 9, "worse": 20}}
    hand = analyze_matrix(matrix, {}, complete=True, oracle_complete={})
    return {
        "B01_identical_rank": single["selected"] == duplicate["selected"] == expected["best"],
        "B08_hand_regret": hand["regret"]["a"] == {"first": 0, "second": 4} and
        hand["regret"]["b"] == {"first": 6, "second": 0},
        "B10_relative_loss_can_differ": hand["winners"]["MINIMAX_REGRET"] == ["a"]
        and hand["winners"]["WORST_CASE_RELATIVE_LOSS"] == ["b"],
        "B11_dominance": hand["dominance"]["worse"] == ["a", "b", "reference"],
        "B12_pareto": hand["pareto"] == ["a", "b"],
        "B19_horizon_mismatch": mismatch,
        "B20_missing_model_inputs": unsupported["status"] == "NO_FULLY_SUPPORTED_POLICY",
        "B22_order_and_additions": full_equal and addition,
        "B23_cache_parity": uncached_equal,
        "B25_single_scenario_s6_a": same["results"] == expected["paired"]["results"]
        and same["pairs"] == expected["paired"]["pairs"],
    }, {"scope": "SCENARIO_AND_POLICY_WITH_SECOND_LEVEL_TYPED_FORECAST_CACHE",
        "cached_calls": sum(r["forecast_calls"] for r in full["cases"].values()),
        "uncached_calls": sum(r["forecast_calls"] for r in uncached["cases"].values()),
        "shared_typed_hits": sum(r["cache_hits"] for r in full["cases"].values())}


def run_robust_study(root: Path, out: Path):
    if out.exists():
        raise FileExistsError(out)
    bundle, hashes = verified_robust_model(root)
    protocol = yaml.safe_load((root/PROTOCOL).read_bytes())
    families = tuple(_study(root))
    invariants, summaries, audit, libraries, comparators, equivalence, checks = ({} for _ in range(7))
    executions, outcomes, regrets, physical = ([] for _ in range(4))
    for family in families:
        print(f"S6-B {family.family_id}", flush=True)
        result = evaluate_family(family, bundle)
        summaries[family.family_id] = _summary(family, result)
        libraries[family.family_id] = {"policies": result["policies"], "rejections": result["rejected"]}
        comparators[family.family_id] = result["oracles"]
        equivalence[family.family_id] = result["pairs"]
        audit[family.family_id] = {
            "parent_config_sha256": fingerprint(asdict(family.initial)),
            "initial_state_sha256": fingerprint((asdict(family.initial.scenario.initial),
                                                 asdict(family.initial.scenario.physical))),
            "variants": {v.scenario_id: {
                "category": v.category, "common_initial_state": v.category != INITIAL or
                v.request.scenario.physical == family.initial.scenario.physical,
                "field_support": v.request.scenario.field_support,
                "scenario_validation": "EVALUATED" if result["cases"][v.scenario_id]["paired"] else "UNSUPPORTED",
                "initial_sha256": fingerprint((asdict(v.request.scenario.initial), asdict(v.request.scenario.physical))),
                "provenance": v.provenance, "changes": v.changed_assumptions, "decision_cutoff": v.request.scenario.physical.as_of_utc,
                "targets": v.request.scenario.targets, "horizon": v.request.scenario.horizon_laps}
                for v in family.scenarios}}
        _evidence_for(family, result, checks)
        _ledger_tables(family, result, (executions, outcomes, regrets, physical))
        for v in family.scenarios:
            paired = result["cases"][v.scenario_id]["paired"]
            if paired:
                invariants[f"{family.family_id}/{v.scenario_id}"] = _invariants(v.request.scenario, paired)
    regression, cache = _parity_checks(families[0], bundle)
    checks |= regression
    violations = [{"case": case, "policy": policy, "failure": name} for case, rows in invariants.items()
                  for policy, flags in rows.items() for name, ok in flags.items() if not ok]
    checks["B24_no_unscripted_pass"] = not any(v["passes"] for v in executions if v["scenario"] != "scripted-pass")
    primary = summaries["primary"]
    completed_families = sum(row["status"] == "ROBUST_SEARCH_COMPLETE" for row in summaries.values())
    decision = ("PATH_B_ROBUST_INTEGRATION_NEEDS_STABILIZATION" if violations or not all(checks.values()) else
                "PATH_A_ROBUST_OPTIMIZATION_VALIDATED" if primary["selected"] else
                "PATH_C_ROBUST_RECOMMENDATION_NOT_IDENTIFIABLE")
    metrics = {"stage": protocol["version"], "decision": decision, "release": "MEAN_ONLY_VALIDATED",
               "probabilities": "SCENARIO_PROBABILITIES_NOT_ESTIMATED", "baseline_tests": 363,
               "families": len(families), "completed_families": completed_families,
               "evaluations": sum(s["search"]["simulator_evaluations"] for s in summaries.values()),
               "supported_cells": sum(s["search"]["supported_cells"] for s in summaries.values()),
               "primary": {"selected": primary["selected"], "presentation": primary["presentation"],
                           "near": primary["near"], "search": primary["search"]},
               "checks": checks, "invariant_failures": violations}
    out.mkdir(parents=True)
    for name, data in {
        "protocol": protocol, "scenario_families": {f.family_id: asdict(f) for f in families},
        "initial_state_manifest": audit, "model_manifest": loads((root/REPORT).read_bytes()), "policy_library": libraries,
        "candidate_support": {k: [{field: r[field] for field in ("policy", "comparable", "first_failure",
                              "unsupported_scenarios", "valid_scenarios", "evaluated_scenarios",
                              "reaching_marker_scenarios")} for r in v["results"]] + [
                                  {"policy": key, "comparable": False, "evaluated_scenarios": 0, "valid_scenarios": 0,
                                   "reaching_marker_scenarios": 0, "unsupported_scenarios": 0,
                                   "first_failure": {"reasons": ["NOT_EVALUATED_BUDGET"]}}
                                  for key in v["search"]["unevaluated_policies"]]
                              for k, v in summaries.items()},
        "robust_rankings": summaries, "scenario_oracles": comparators, "policy_equivalence": equivalence,
        "dominance": {k: {r["policy"]: r["dominated_by"] for r in v["results"] if r["comparable"]} for k, v in summaries.items()},
        "pareto_frontier": {k: v["other_objectives"]["PARETO"] for k, v in summaries.items()},
        "near_equal_candidates": {k: {"near": v["near"], "margin_us": v["near_margin_us"]} for k, v in summaries.items()},
        "neutralization_sensitivity": {k: summaries[k] for k in ("neutralization-timing", "pit-entry-boundary")},
        "traffic_sensitivity": {k: summaries[k] for k in ("initial-gap", "future-traffic", "separate-clean-field")},
        "tire_sensitivity": summaries["pace-and-route-sensitivity"],
        "horizon_stability": {k: summaries[k] for k in ("horizon-3", "horizon-5", "horizon-10")},
        "search_completeness": {k: v["search"] for k, v in summaries.items()},
        "cache_diagnostics": cache, "regression_parity": {"verified_frozen_sha256": hashes, "checks": regression},
        "invariant_checks": invariants, "metrics": metrics,
    }.items():
        _json(out/f"{name}.json", data)
    for name, rows in {
        "policy_execution_ledger": executions, "scenario_outcomes": outcomes,
        "scenario_relative_deltas": [{k: r[k] for k in ("family", "scenario", "policy", "delta_us", "status")} for r in regrets],
        "regret_matrix": regrets, "physical_constraints": physical,
    }.items():
        _write_table(out/f"{name}.parquet", rows)
    from f1ri.validation.robust_report import generate_report

    generate_report(out, metrics, summaries, comparators, regrets, outcomes, executions, equivalence, cache)
    return out/"metrics.json"
