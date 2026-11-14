"""Run and seal the declared Stage 6-A study."""

from dataclasses import asdict, replace
from datetime import timedelta
from itertools import combinations, product
from json import loads
from pathlib import Path
from time import perf_counter

import duckdb
import pandas as pd
import yaml

from f1ri.simulator.strategy import StrategyInputError, compare_strategies, fingerprint
from f1ri.simulator.strategy_constraints import CheckpointConstraints
from f1ri.simulator.strategy_optimization import (
    MODEL_SHA256,
    REFERENCE,
    _evaluation,
    _MeanAdapter,
    optimize,
)
from f1ri.simulator.strategy_policy import Policy, PolicyKind, Stop
from f1ri.validation.optimization_scenarios import (
    control_perturbation,
    fixture_request,
    perturbations,
    scenario_requests,
)
from f1ri.validation.proximity_pilot import _json
from f1ri.validation.stage3_r_release import BUNDLE, REPORT, load_successor
from f1ri.validation.stage3_r_sources import digest
from f1ri.validation.strategy_study import _invariants

ARCHIVE = "artifacts/reports/stage3_r1_pace_reconstruction.zip"
FROZEN = {
    BUNDLE: MODEL_SHA256,
    ARCHIVE: "7f4debe12cd1e798c521f797e71df28e8e2c92deaeed262647ec12a02963637d",
    **{f"artifacts/reports/{file}_metrics.json": value for file, value in {
        "stage5_s5_c1_model_strategy_integration": "500b99c186e144cf727515ad0db3c9f473c182dc45b2cc7ef186f35550e4a823",
        "stage5_s5_c_strategy_scenarios": "669312321029a882336ffdee8f39cb7d0ddb78eeea10df22febed1d2ae5795c1",
        "stage5_s5_b_historical_neutralization_validation": "a49ce8809f8169d8eacfe8f640f4e406b52780a3d37aeaa2b326794772d8b9df",
        "stage5_s5_a1_pit_restart_stabilization": "1d77585f99578084160eb47332caa988ed6934aed508459dbc5753e0acc655d3",
        "stage5_s5_a_neutralization_mechanics": "c345b7e78f96168c7b72f367362bece02534b675942ba44861d759a4b5cfe54e",
    }.items()},
}
PROTOCOL = "config/experiments/stage6_s6_a.yaml"


def verified_optimizer(root: Path):
    hashes = {p: digest(root/p) if (root/p).is_file() else None for p in FROZEN}
    if hashes != FROZEN:
        failed = [p for p, expected in FROZEN.items() if hashes[p] != expected]
        raise StrategyInputError("FROZEN_INTEGRITY", "Missing or mismatched frozen inputs: "+", ".join(failed))
    manifest = loads((root/REPORT).read_bytes())
    if manifest.get("evidence_archive_sha256") != hashes[ARCHIVE] or manifest.get("published_model") != BUNDLE:
        raise StrategyInputError("MODEL_UNVERIFIED", "The release manifest must identify the pinned model and evidence.")
    bundle, release = load_successor(root, correlated=False)
    if bundle is None or release != "MEAN_ONLY_VALIDATED" or bundle.artifact_sha256 != MODEL_SHA256:
        raise StrategyInputError("MODEL_UNVERIFIED", "The pinned D8R1 mean-only release is required.")
    return bundle, hashes


def _suite(root):
    fixtures = scenario_requests(root)
    def make(name, **params):
        return fixture_request(root, name, fixtures=fixtures, **params)

    cases = {name: make(name) for name in
             ("clean-air", "two-car", "retirement", "wider-gap", "original-opponent", "opponent-pits",
              "scripted-pass", "vsc", "sc", "late-vsc")}
    cases.update({
        "three-lap": make("clean-air", horizon=3), "ten-lap": make("clean-air", horizon=10),
        "two-stop": make("clean-air", max_stops=2, sets=("hard", "medium", "soft"), budget=128),
        "shared-box": make("shared-box", horizon=5, max_stops=2, sets=("hard", "soft"), stop_options=2, budget=128),
        "budget-limited": make("clean-air", budget=4),
        "unavailable-set": make("clean-air", sets=("hard", "not-allocated"), stop_options=2),
        "vsc-oracle": make("vsc", information="PERFECT_FORESIGHT"),
        "sc-oracle": make("sc", information="PERFECT_FORESIGHT"),
    })
    for objective, key in (("TEAM_WEIGHTED", "team-weighted"), ("LEAD_DRIVER", "team-lead")):
        cases[key] = replace(cases["shared-box"], objective=objective)
    cases.update(perturbations(cases["two-car"]))
    cases["vsc-speed-minus-10pct"] = control_perturbation(cases["vsc"], "vsc-speed-minus-10pct")
    cases["restart-speed-plus-10pct"] = control_perturbation(cases["sc"], "restart-speed-plus-10pct")
    return {name: replace(req, request_id=name) for name, req in cases.items()}


def _parity(root, bundle):
    """Independent small cases use manually specified S5 policies and individual runs."""
    checks = {}
    fixtures = scenario_requests(root)
    for name in ("clean-air", "two-car", "vsc", "sc", "shared-box"):
        team = name == "shared-box"
        request = fixture_request(root, name, sets=("hard", "soft"), stop_options=2,
                                  max_stops=2 if team else 1, fixtures=fixtures, budget=128)
        result = optimize(request, bundle)
        rows = {r["candidate"]: r for r in result["conditional"]["rows"]}
        paired = result["conditional"]["paired"]
        reference = Policy(REFERENCE, PolicyKind.CONTINUE)
        fixed = [[Stop(w.driver, t, lap) for lap, t in product(
            (w.first_lap, w.last_lap), ("hard", "soft"))] for w in request.windows]
        plans = [(s,) for stops in fixed for s in stops]
        if team:
            plans.extend(tuple(sorted(pair, key=lambda s: (s.lap, s.driver, s.set_id))) for pair in product(*fixed))
        weights = result["weights"]
        reversed_policies = [reference]
        matched = {}
        for stops in plans:
            key = "__".join(f"{s.driver}-L{s.lap}-{s.set_id}" for s in stops)
            manual = Policy(key, PolicyKind.TARGET if len(stops) == 1 else PolicyKind.TEAM, stops)
            reversed_policies.append(manual)
            single = compare_strategies(replace(request.scenario, policies=(reference, manual)), REFERENCE,
                                        recorder_factory=CheckpointConstraints,
                                        forecast_adapter=_MeanAdapter(request, bundle))
            run = single["results"][key]
            evaluated = _evaluation(run, single["pairs"][key], manual, dict(request.scenario.targets),
                                    weights, rows[key]["near_margin_us"])
            matched[key] = {"same_objective": evaluated["objective_us"] == rows[key]["objective_us"],
                            "same_events": run == paired["results"][key]}
        reordered = compare_strategies(replace(request.scenario, policies=tuple(reversed(reversed_policies))),
                                       REFERENCE, recorder_factory=CheckpointConstraints,
                                       forecast_adapter=_MeanAdapter(request, bundle))
        order_parity = all(run == paired["results"][key] for key, run in reordered["results"].items())
        fixed_rows = [rows[key]["objective_us"] for key in matched if rows[key]["objective_us"] is not None]
        if rows[REFERENCE]["objective_us"] is not None:
            fixed_rows.append(rows[REFERENCE]["objective_us"])
        legal_fixed = {p["strategy_id"] for p in result["candidates"] if all(s["lap"] is not None for s in p["stops"])}
        checks[name] = {"manual_fixed_candidates": len(plans)+1, "fixed_space_equal": legal_fixed == {REFERENCE, *matched},
                        "matched": matched, "reordered_subset_equal": order_parity,
                        "best_fixed_objective_us": min(fixed_rows) if fixed_rows else None,
                        "exhausted": result["completion"] == "EXHAUSTED_LEGAL_SPACE"}
    req = fixture_request(root, "vsc", sets=("hard", "soft"), stop_options=2, fixtures=fixtures)
    cached, uncached = optimize(req, bundle), optimize(replace(req, cache_forecasts=False), bundle)
    checks["cache"] = {"on_off_candidate_results_equal": all(cached[k] == uncached[k] for k in ("decision", "conditional")),
                       "cache_hits": cached["search"]["forecast_cache_hits"],
                       "calls_with_cache": cached["search"]["forecast_calls"],
                       "calls_without_cache": uncached["search"]["forecast_calls"]}
    return checks


def _parquet(path, rows):
    # Ledger JSON carries variable nested provenance, Parquet columns retain filterable keys.
    with duckdb.connect() as db:
        db.register("ledger", pd.DataFrame(rows))
        db.execute("COPY ledger TO ? (FORMAT PARQUET, COMPRESSION ZSTD)", [str(path)])


def _forecast_rows(request, stage, candidate, run, manifest):
    for f in run.get("forecast_requests", []):
        r = f["request"]
        if "forecast" not in f:
            yield {"scenario": request, "evaluation": stage, "candidate": candidate, "driver": r["driver_number"],
                   "model_hash": manifest["model_sha256"], "status": "UNSUPPORTED", "reason": f["reason"]}
            continue
        for lap in f["forecast"]["lap_forecasts"]:
            yield {"scenario": request, "evaluation": stage, "candidate": candidate, "driver": r["driver_number"],
                   "session": r["session_key"], "circuit": r["circuit_short_name"], "model": manifest["model_version"],
                   "model_hash": f["model_sha256"], "feature_schema_hash": manifest["feature_schema_sha256"],
                   "feature_source_hash": f["feature_sha256"], "training_cutoff": manifest["trained_through_utc"],
                   "calibration_cutoff": manifest["calibrated_through_utc"], "as_of": r["as_of_utc"],
                   "request_hash": f["request_sha256"], "fit_clock_us": f["fit_clock_us"],
                   "compound": r["compound_relative"], "absolute_compound": r["compound_absolute"],
                   "start_age": r["tyre_age_at_start"], "horizon": r["horizon_laps"],
                   "start_lap": r["planned_start_lap"], "stint": r["stint_number"],
                   "lap": lap["target_lap_number"], "age": lap["projected_tyre_age"], "status": lap["support_status"],
                   "extrapolation": lap["extrapolation_warning"], "model_mean_lap_s": lap["expected_lap_time_s"],
                   "applied_mean_lap_s": lap["scenario_lap_time_s"]}


def _scenario_summary(req, result):
    scene = req.scenario
    return {"request": req.request_id, "session": scene.initial.session_key,
            "model": "PaceV2_D8R1", "model_hash": MODEL_SHA256, "field_support": scene.field_support,
            "layout": scene.physical.layout_version, "rule_profile": scene.profile.identifier,
            "decision_cutoff": scene.physical.as_of_utc, "horizon": scene.horizon_laps, "targets": scene.targets,
            "information_mode": req.information_mode, "decision_basis": result["decision_basis"],
            "best_decision": result["decision"]["best"], "best_declared": result["conditional"]["best"],
            "near_declared": result["conditional"]["near"], "scope": result["scope"],
            "completion": result["completion"], "search": result["search"],
            "top_declared": [r for key in result["conditional"]["ranked"][:8]
                             for r in result["conditional"]["rows"] if r["candidate"] == key],
            "pace_mode": "D8R1_MEAN_ONLY_VALIDATED", "passing": result["conditional"]["paired"]["overtaking"],
            "benchmark_aliases": result["benchmark_aliases"]}


def _fixture_status(results, parity):
    c = {k: {r["candidate"]: r for r in v["conditional"]["rows"]} for k, v in results.items()}
    medium, soft = "63-L31-medium", "63-L31-soft"
    small = all(x["fixed_space_equal"] and x["reordered_subset_equal"] and x["exhausted"]
                and all(all(m.values()) for m in x["matched"].values())
                for key, x in parity.items() if key != "cache")
    return {
        "O1_no_pit_needed": REFERENCE in results["clean-air"]["conditional"]["best"],
        "O2_immediate": soft in results["vsc"]["conditional"]["best"],
        "O3_delayed": "63-L32-soft" in results["late-vsc"]["conditional"]["best"],
        "O4_compound": c["clean-air"][medium]["objective_us"] != c["clean-air"][soft]["objective_us"],
        "O5_exact_tie": len(results["two-car"]["conditional"]["best"]) > 1,
        "O6_following": c["two-car"][medium]["objective_us"] == c["two-car"][soft]["objective_us"]
                        and c["two-car"][soft]["following_us"] > 0,
        "O7_blocker_removed": c["retirement"][medium]["objective_us"] != c["retirement"][soft]["objective_us"],
        "O8_vsc": "vsc-63-soft" in results["vsc"]["conditional"]["best"],
        "O9_sc": "sc-63-soft" in results["sc"]["conditional"]["best"],
        "O10_double_stack": any(r["service_wait_us"] > 0 for r in c["shared-box"].values()),
        "O11_two_stop": any(r["pit_visits"] == 2 for r in c["two-stop"].values()),
        "O12_unavailable": any(r["reason"] == "TYRE_SET_UNAVAILABLE" for r in results["unavailable-set"]["rejected"]),
        "O14_budget": results["budget-limited"]["completion"] == "BOUNDED_SEARCH_INCOMPLETE",
        "O15_small_case_and_order": small,
        "O17_no_pass": all(not v["unplanned_passes"] for k, v in results.items() if k != "scripted-pass"),
        "cache_parity": parity["cache"]["on_off_candidate_results_equal"],
    }


def rejected_requests(req, bundle):
    inputs = {
        "O13_unsupported_horizon": (replace(req, scenario=replace(req.scenario, horizon_laps=20)), "HORIZON_UNSUPPORTED"),
        "O16_historical_field_unsupported": (
            replace(req, scenario=replace(req.scenario, field_support="EXACT_HISTORICAL")), "PHYSICAL_STATE_UNSUPPORTED"),
        "O18_unverified_model": (replace(req, model_sha256="0"*64), "MODEL_UNVERIFIED"),
    }
    failed = {}
    for name, (request, code) in inputs.items():
        try:
            optimize(request, bundle)
        except StrategyInputError as error:
            failed[name] = {"passed": error.code == code, "code": error.code, "reason": str(error)}
        else:
            failed[name] = {"passed": False, "reason": "Request unexpectedly accepted"}
    return failed


def run_study(root, out):
    if out.exists():
        raise FileExistsError(out)
    bundle, hashes = verified_optimizer(root)
    protocol = yaml.safe_load((root/PROTOCOL).read_bytes())
    manifest = yaml.safe_load((root/REPORT).read_bytes())
    out.mkdir(parents=True)
    requests = _suite(root)
    results, seconds, summaries, invariants = {}, {}, {}, {}
    evaluations, legal, rejected, constraints, progress, pit, forecasts, trace = ([] for _ in range(8))
    for name, request in requests.items():
        print(f"S6-A {name}", flush=True)
        start = perf_counter()
        result = optimize(request, bundle)
        seconds[name] = round(perf_counter()-start, 4)
        summaries[name] = _scenario_summary(request, result)
        results[name] = {
            "conditional": {k: v for k, v in result["conditional"].items() if k != "paired"},
            "search": result["search"], "rejected": result["rejected"], "completion": result["completion"],
            "benchmark_aliases": result["benchmark_aliases"], "unevaluated": result["unevaluated"],
            "unplanned_passes": any(r.get("pass_results") for r in result["conditional"]["paired"]["results"].values()),
        }
        legal.extend({"scenario": name, **p} for p in result["candidates"])
        rejected.extend({"scenario": name, **row} for row in result["rejected"])
        branches = [("declared", result["conditional"])]
        if result["decision"] is not result["conditional"]:
            branches.append(("decision-known", result["decision"]))
        for label, branch in branches:
            scene = request.scenario
            paired = branch["paired"]
            if label == "decision-known":
                scene = replace(scene, controls=tuple(e for e in scene.controls if e.at_us <= scene.initial.race_clock_us),
                                retirements=(), passes=())
            invariants[f"{name}/{label}"] = _invariants(scene, paired)
            evaluations.extend({"scenario": name, "evaluation": label, **r} for r in branch["rows"])
            for key, run in paired["results"].items():
                context = {"scenario": name, "evaluation": label, "candidate": key}
                trace.append(context | {"matched": paired["matched"], "status": run["status"],
                                        "decisions": run["decisions"], "trace_sha256": fingerprint(run),
                                        "race_control": run.get("race_control", []),
                                        "pit_events": run.get("pit_events", []),
                                        "passes": run.get("pass_results", []),
                                        "restart_permissions": run.get("restart_permissions", [])})
                constraints.extend(context | row for row in run.get("checkpoint_constraints", []))
                progress.extend(context | {k: row[k] for k in ("clock_us", "driver", "checkpoint", "route",
                                "track_state", "progress_m", "physical_predecessor", "event", "classification")}
                                | {"compound": row["tyres"]["compound"], "age": row["tyres"]["age_laps"],
                                   "stint": row["tyres"]["stint_number"], "set_id": row["tyres"]["set_id"]}
                                for row in run["trajectory"])
                pit.extend(context | row for row in run["pit"])
                forecasts.extend(_forecast_rows(name, label, key, run, manifest))
    parity = _parity(root, bundle)
    special = rejected_requests(requests["clean-air"], bundle)
    flags = _fixture_status(results, parity) | {k: row["passed"] for k, row in special.items()}
    fail = [{"scenario": key, "candidate": candidate, "invariant": field}
            for key, branches in invariants.items() for candidate, fields in branches.items()
            for field, ok in fields.items() if not ok]
    sensitivity = _sensitivity(summaries, evaluations)
    metrics = {
        "stage": protocol["version"], "baseline": 348, "scenarios": len(results), "fixture_checks": flags,
        "invariant_failures": fail, "model_hash": MODEL_SHA256,
        "simulator_evaluations": sum(r["search"]["simulator_evaluations"] for r in results.values()),
        "rankable_scenarios": sum(bool(r["conditional"]["best"]) for r in results.values()),
        "decision": "PATH_A_CONDITIONAL_OPTIMIZATION_VALIDATED" if all(flags.values()) and not fail
        else "PATH_B_OPTIMIZER_MECHANICS_REQUIRE_STABILIZATION",
        "scope": "BEST_WITHIN_EVALUATED_CANDIDATES", "release": "MEAN_ONLY_VALIDATED",
    }
    for name, table in {
        "candidate_evaluations": evaluations, "checkpoint_constraints": constraints, "field_progress": progress,
        "pit_execution": pit, "tire_forecasts": forecasts,
    }.items():
        _parquet(out/f"{name}.parquet", table)
    for name, data in {
        "optimization_protocol": protocol, "initial_states": {k: {"initial": asdict(v.scenario.initial),
            "physical": asdict(v.scenario.physical), "cutoff": v.scenario.initial.race_start_utc+
            timedelta(microseconds=v.scenario.initial.race_clock_us)} for k, v in requests.items()},
        "model_manifest": manifest, "scenario_configuration": {k: asdict(v) for k, v in requests.items()},
        "search_space": {"legal": legal, "benchmarks": {k: v["benchmark_aliases"] for k, v in results.items()}},
        "candidate_rejections": {"candidates": rejected, "requests": special}, "optimizer_trace": trace,
        "search_budget": {k: v["search"] | {"unevaluated": v["unevaluated"], "completion": v["completion"]}
                          for k, v in results.items()},
        "top_candidates": summaries, "scenario_sensitivity": sensitivity, "cache_diagnostics": parity["cache"],
        "regression_parity": {"verified_frozen_sha256": hashes, "reference_regression": parity},
        "invariant_checks": invariants, "metrics": metrics, "performance_observations": seconds,
    }.items():
        _json(out/f"{name}.json", data)
    from f1ri.validation.optimization_report import finish_report

    finish_report(out, metrics, summaries, evaluations, rejected, pit, progress, constraints, sensitivity)
    return out/"metrics.json"


def _sensitivity(summaries, evaluations):
    groups = {
        "two-car": ("retirement", "wider-gap", "original-opponent", "opponent-pits", "scripted-pass",
                    "mean-pace-plus-0.3s", "soft-plus-0.3s", "degradation-plus-0.06s-per-age-lap", "pit-travel-plus-2s",
                    "service-plus-1s"),
        "vsc": ("vsc-speed-minus-10pct", "late-vsc"), "sc": ("restart-speed-plus-10pct",),
        "clean-air": ("three-lap", "ten-lap"),
    }
    times = {}
    for row in evaluations:
        if row["evaluation"] == "declared" and row["objective_us"] is not None:
            times.setdefault(row["scenario"], {})[row["candidate"]] = row["objective_us"]
    return [{"reference": base, "scenario": change,
             "reference_best": summaries[base]["best_declared"], "varied_best": summaries[change]["best_declared"],
             "no_shared_winner": not set(summaries[base]["best_declared"]) & set(summaries[change]["best_declared"]),
             "winner_set_changed": summaries[base]["best_declared"] != summaries[change]["best_declared"],
             "pairwise_rank_reversals": sum((times[base][a]-times[base][b])*(times[change][a]-times[change][b]) < 0
                                            for a, b in combinations(
                                                sorted(times.get(base, {}).keys() & times.get(change, {}).keys()), 2)),
             "near": summaries[change]["near_declared"], "unsupported": summaries[change]["search"]["outcomes"].get(
                 "UNSUPPORTED", 0)} for base, scenarios in groups.items() for change in scenarios]
