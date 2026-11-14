"""Freeze the model recovery, matched strategy attribution and controls."""
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path
from zipfile import ZipFile

import yaml

from f1ri.simulator.neutralization import simulate_neutralization
from f1ri.simulator.strategy import StrategyInputError, compare_strategies
from f1ri.simulator.strategy_constraints import constraint_summary
from f1ri.simulator.strategy_pace import PaceMode
from f1ri.validation.model_strategy_recovery import (
    feature_audit,
    recover_bundle,
    saved_samples_summary,
)
from f1ri.validation.overtake_historical_study import _load_json
from f1ri.validation.pit_restart_scenarios import fixtures as frozen_fixtures
from f1ri.validation.proximity_pilot import _json
from f1ri.validation.strategy_discrimination import cases, discriminate, evaluate
from f1ri.validation.strategy_fixtures import PROFILE, sensitivity_families, strategy_families
from f1ri.validation.strategy_sources import audit_model_assets, frozen_pace_fixture
from f1ri.validation.strategy_study import _invariants
from f1ri.validation.traffic_history_study import _sha

PREFIX = "stage5_s5_c1_model_strategy_integration"
PROTOCOL = f"config/experiments/{PREFIX}.yaml"


def _protocol_reports(root, plan, frozen_reproduction):
    folder = root/"artifacts/reports"
    metric = folder/"stage5_s5_c_strategy_scenarios_metrics.json"
    expected = plan["s5_c_sha256"]
    if _sha(metric) != expected:
        raise ValueError("Frozen S5-C metrics differ")
    local = _sha(frozen_reproduction/"metrics.json") if frozen_reproduction else None
    if local is not None and local != expected:
        raise ValueError("S5-C reproduction differs")
    with ZipFile(folder/"stage5_s5_c_strategy_scenarios_ledgers.zip") as archive:
        if archive.read("metrics.json") != metric.read_bytes():
            raise ValueError("Archived S5-C metrics differ")
    return {"metrics_sha256": expected, "reproduced_sha256": local}


def _sensitivity():
    families = strategy_families()
    rows = []
    variants = list(sensitivity_families(families))
    sc = families["sc"]
    # S5-C varied service inside a fixed lane timetable. This companion
    # shifts downstream markers as well, so it actually delays the merge.
    extended = tuple(replace(r, route=replace(r.route, service_s=r.route.service_s+2,
                        lane_exit_s=r.route.lane_exit_s+2, merge_s=r.route.merge_s+2,
                        out_sector_2_s=r.route.out_sector_2_s+2, out_line_s=r.route.out_line_s+2))
                     for r in sc.resources)
    variants.append(("service-plus-route", replace(sc, scenario_id="sc-service-plus-route",
                                                   resources=extended), "react-sc"))
    for label, s, key in variants:
        base = families["green" if key == "pit-now-medium" else "sc"]
        current = evaluate(s, "continue", [key])
        control = evaluate(base, "continue", [key])
        a = control["results"][key]
        b = current["results"][key]
        fields = ("physical", "pace", "profile", "resources", "controls")
        changes = {f: {"reference": asdict(base)[f], "case": asdict(s)[f]} for f in fields
                   if getattr(base, f) != getattr(s, f)}
        d, cp = s.targets[0]
        delta = current["pairs"][key]["combined_delta_us"]
        initial_delta = control["pairs"][key]["combined_delta_us"]
        def activity(run, d, cp):
            racing = [r for r in run["checkpoint_constraints"] if r["driver"] == d and
                      r["checkpoint"] is not None and r["checkpoint"] <= cp and r["latent_us"] is not None]
            return {"requested": run["decisions"], "pits": run["pit"], "marker": run["targets"].get(d),
                    "final_predecessor": (run["targets"].get(d) or {}).get("physical_predecessor"),
                    "constraints": constraint_summary(racing, d, cp),
                    "racing_latent_us": sum(r["latent_us"] for r in racing),
                    "racing_effective_us": sum(r["effective_us"] for r in racing)}
        rows.append({"case": label, "candidate": key, "changes": changes, "reference_delta_us": initial_delta,
                     "case_delta_us": delta, "same_checkpoint": s.targets == base.targets,
                     "rank_reversed": delta is not None and initial_delta is not None and delta*initial_delta < 0,
                     "reference_activity": activity(a, d, cp), "case_activity": activity(b, d, cp),
                     "reference_stay_out": activity(control["results"]["continue"], d, cp),
                     "case_stay_out": activity(current["results"]["continue"], d, cp),
                     "same_supported_marker": delta is not None and initial_delta is not None,
                     "matched_run_ids": current["matched"]})
    return rows


def run_model_strategy_study(root, output, *, stage3=None, frozen_reproduction=None, backup=None):
    if output.exists():
        raise FileExistsError(output)
    plan = yaml.safe_load((root/PROTOCOL).read_bytes())
    stage3 = stage3 or Path(plan["stage3_root"])
    prior, frozen, _ = audit_model_assets(root, stage3)
    frozen["s5_c"] = _protocol_reports(root, plan, frozen_reproduction)
    recovery, _bundle = recover_bundle(root, stage3, backup)
    features = feature_audit(root, prior)
    saved = saved_samples_summary(root, prior, plan["continuation_realizations"], plan["forecast_horizons"])
    geometry = root/plan["geometry"]
    document = _load_json(geometry)
    definitions = cases(document["length_m"])
    comparisons = {key: evaluate(s, ref, choices) for key, (s, ref, choices) in definitions.items()}
    audited = {key: _invariants(definitions[key][0], result) for key, result in comparisons.items()}
    if any(not ok for group in audited.values() for row in group.values() for ok in row.values()):
        raise ValueError("Strategy discrimination invariant failed")
    discrimination = {}
    for key, result in comparisons.items():
        s = definitions[key][0]
        pair_names = [(result["reference"], name) for name in result["results"] if name != result["reference"]]
        requested = (("pit-now-medium", "pit-now-hard"), ("pit-now-medium", "pit-now-used-soft"),
                     ("pit-now-medium", "extend-one"), ("pit-now-medium", "two-stop"),
                     ("stack", "63-first"), ("stack", "stagger"), ("react-vsc", "after-green"),
                     ("react-sc", "after-green"))
        pair_names.extend((a, b) for a, b in requested if a in result["results"] and b in result["results"])
        discrimination[key] = [discriminate(result, s, a, b) for a, b in pair_names]
    sensitivity = _sensitivity()
    realizations = []
    if saved["status"] == "FROZEN_PACE_CONTINUATION":
        for index in plan["continuation_realizations"]:
            s = frozen_pace_fixture(root, prior, index)
            r = evaluate(s, "continue")
            realizations.append({"index": index, "stream": s.pace.stream_id,
                                 "identical_trajectory": r["results"]["repeat"]["trajectory"] ==
                                 r["results"]["continue"]["trajectory"],
                                 "delta_us": r["pairs"]["repeat"]["combined_delta_us"],
                                 "source": r["matched"]})
    s = definitions["green"][0]
    try:
        compare_strategies(replace(s, pace=replace(s.pace, mode=PaceMode.FULL_MODEL)), "continue")
    except StrategyInputError as error:
        gate = {"status": "REQUEST_UNSUPPORTED" if _bundle else "MODEL_UNAVAILABLE",
                "diagnostic": error.code, "numeric_ranking": None,
                "reason": "Historical cutoff field and replacement inputs are unverified" if _bundle
                else "Original fitted D8 model and run metadata unavailable"}
    else:
        raise ValueError("Unverified FULL_MODEL produced strategy results")
    original = simulate_neutralization(**frozen_fixtures(PROFILE)["dynamic_green"])
    checkpoints = [r["at_us"] for r in original.pit_ledger if r["status"] in ("PIT_ENTRY_COMMITTED", "REJOINED")]
    frozen["green_pit_reproduced"] = checkpoints == [160_000_000, 217_000_000]
    counts = Counter(r["category"] for result in comparisons.values() for run in result["results"].values()
                     for r in run["checkpoint_constraints"])
    metrics = {"version": plan["version"], "baseline": plan["baseline"], "model_recovery": recovery["status"],
               "full_model": gate, "replacement_forecasts_executed": 0,
               "fixture_count": len(comparisons), "branches": sum(len(x["results"]) for x in comparisons.values()),
               "sensitivity_cases": len(sensitivity), "invariant_failures": 0,
               "constraint_counts": dict(counts), "saved_realizations": realizations,
               "field_roster": [c.driver_number for c in definitions["multi-car"][0].initial.cars],
               "geometry_source": {"version": document["version"], "sha256": _sha(geometry), "position_source": "DECLARED"},
               "rankings": {key: result["ranking"] for key, result in comparisons.items()},
               "decision": "PATH_B_FORECAST_INPUTS_PARTIAL" if _bundle else "PATH_C_ORIGINAL_MODEL_UNAVAILABLE"}
    output.mkdir(parents=True)
    (output/"config.yaml").write_bytes((root/PROTOCOL).read_bytes())
    for name, value in {
        "model_recovery": recovery, "verified_model_metadata": recovery["metadata"],
        "historical_feature_availability": features, "forecast_requests_and_support": gate,
        "replacement_trajectories": {"status": gate["status"], "forecasts": [], "numeric_ranking": None},
        "saved_continuation": saved, "common_randomness": {"realizations": realizations,
            "replacement_pairing": "SAME_DRIVER_STINT_START_DRAW_SHAPE; DISTINCT_DRIVER_AND_STINT_SEEDS",
            "cross_driver_covariance": "NOT_MODELED_IN_S1"},
        "strategy_scenarios": {k: asdict(v[0]) for k, v in definitions.items()},
        "strategy_discrimination": discrimination, "comparison_and_checkpoint_traces": comparisons,
        "sensitivity": sensitivity, "regression_parity": frozen, "invariants": audited, "metrics": metrics,
    }.items():
        _json(output/f"{name}.json", value)
    from f1ri.validation.model_strategy_plots import plot_model_strategy

    plot_model_strategy(output, metrics, comparisons, discrimination, sensitivity, saved, recovery)
    return output/"metrics.json"
