"""Freeze the deterministic strategy validation and its measured envelopes."""
from collections import Counter
from dataclasses import asdict, replace
from itertools import pairwise

from f1ri.simulator.neutralization import simulate_neutralization
from f1ri.simulator.strategy import compare_strategies
from f1ri.validation.historical_neutralization_sources import read_release
from f1ri.validation.pit_restart_scenarios import fixtures as frozen_fixtures
from f1ri.validation.proximity_pilot import _json
from f1ri.validation.strategy_fixtures import PROFILE, sensitivity_families, strategy_families
from f1ri.validation.strategy_sources import PLAN, audit_model_assets, frozen_pace_fixture


def _invariants(s, result):
    roster = {c.driver_number for c in s.initial.cars}
    expected = [c.event_id for c in s.controls if c.at_us <= s.end_us]
    checks = {}
    for name, run in result["results"].items():
        ids = {}
        for row in run["trajectory"]:
            ids.setdefault(row["event_id"], []).append(row["driver"])
        events = [r for r in run["trajectory"] if r["event_driver"] == r["driver"] and r["event"] in
                  ("LAP_COMPLETED", "CHECKPOINT_CROSSING", "ON_TRACK_PASS")]
        orders = all(len(ds) == len(roster) and set(ds) == roster for ds in ids.values())
        counts = {(r["driver"], r["checkpoint"]) for r in events}
        controls = [r["event"]["event_id"] for r in run.get("race_control", []) if r["outcome"] == "COMMITTED"]
        boxes = {}
        for pit in run["pit"]:
            if pit["service_start_us"] is not None:
                boxes.setdefault(pit["box"], []).append((pit["service_start_us"], pit["service_end_us"]))
        check_boxes = all(a[1] <= b[0] for intervals in boxes.values() for a, b in pairwise(sorted(intervals)))
        monotone = all(a["clock_us"] <= b["clock_us"] for a, b in pairwise(run["trajectory"]))
        checks[name] = {"full_field_identity": orders, "unique_crossings": len(events) == len(counts),
                        "exclusive_boxes": check_boxes, "matched_control_commands": controls == expected,
                        "ordered_trace_clock": monotone,
                        "no_crossing_under_red": all(r["track_state"] != "RED" for r in events),
                        "no_spontaneous_passes": bool(s.passes) or all(r["event"] != "ON_TRACK_PASS" for r in events),
                        "nonnegative_elapsed": all(p["active_elapsed_us"] + p["suspended_us"] == p["wall_elapsed_us"]
                                                   for p in run.get("physical_progress", [])),
                        "physical_predecessor_gaps": all(g >= -1e-6 for p in run.get("physical_progress", [])
                                                         for g in p["gaps_m"].values())}
    return checks


def run_strategy_study(root, output, stage3=None):
    if output.exists():
        raise FileExistsError(output)
    spec, parity, assets = audit_model_assets(root, stage3)
    families = strategy_families()
    if assets["frozen_pace_replay"]["status"] == "VERIFIED_CONTINUATION_ONLY":
        families["frozen-pace"] = frozen_pace_fixture(root, spec, spec["frozen_pace"]["study_sample"])
    runs = {key: compare_strategies(s, "continue") for key, s in families.items()}
    checks = {k: _invariants(families[k], v) for k, v in runs.items()}
    green = families["green"]
    partial = compare_strategies(green, "continue", ["pit-now-medium"])
    reversed_config = replace(green, policies=tuple(reversed(green.policies)))
    reverse = compare_strategies(reversed_config, "continue")
    a1 = read_release(root, "s5_a1")
    old = a1["metrics.json"]
    classic = frozen_fixtures(PROFILE)["dynamic_green"]
    green_replay = simulate_neutralization(**classic)
    pit_clock = [x["at_us"] for x in green_replay.pit_ledger if x["status"] in ("PIT_ENTRY_COMMITTED", "REJOINED")]
    parity["regressions"] = {
        "frozen_green_pit_reexecuted": pit_clock == [160_000_000, 217_000_000],
        "frozen_imola_green_zero_delay": old["imola_green"]["difference_us"] == 0,
        "frozen_imola_vsc_sc_and_singapore": all(old["scenario_invariants"][k] for k in (
            "imola_vsc_1", "imola_sc_4", "singapore_double_yellow_0", "singapore_yellow_2")),
        "frozen_mechanics_invariants": all(old["scenario_invariants"].values()),
        "identical_reference": runs["green"]["pairs"]["continue"]["combined_delta_us"] == 0,
        "additional_policy_independence": partial["results"]["pit-now-medium"] ==
        runs["green"]["results"]["pit-now-medium"],
        "execution_order_independence": reverse["results"] == runs["green"]["results"],
        "frozen_draw_parity": (runs["frozen-pace"]["pairs"]["repeat"]["combined_delta_us"] == 0
                               if "frozen-pace" in runs else None),
    }
    replay = root / "artifacts/runs/stage5_s5_c_s5_a1_regression/metrics.json"
    if replay.is_file():
        from f1ri.validation.traffic_history_study import _sha

        parity["s5_a1_reproduced_metrics_sha256"] = _sha(replay)
        parity["regressions"]["s5_a1_metrics_replayed"] = (
            parity["s5_a1_reproduced_metrics_sha256"] == spec["frozen"]["s5_a1"])
    sensitivity = []
    for label, scenario, candidate in sensitivity_families(families):
        comparison = compare_strategies(scenario, "continue", [candidate])
        result = comparison["pairs"][candidate]
        sensitivity.append({"case": label, "configuration": asdict(scenario), "candidate": candidate,
                            "matched": comparison["matched"], "delta_us": result["combined_delta_us"],
                            "regret_us": {x: r["combined_delta_us"]-comparison["ranking"][0][1]
                                          for x, r in comparison["pairs"].items()
                                          if r["combined_delta_us"] is not None},
                            "delta_rank": result["drivers"][0]["delta_classification"],
                            "status": comparison["results"][candidate]["status"],
                            "reason": comparison["results"][candidate]["unsupported"]})
    summaries = {}
    for label in ("react-sc", "pit-now-medium"):
        values = [r["delta_us"] for r in sensitivity if r["candidate"] == label and r["delta_us"] is not None]
        summaries[label] = {"range_us": [min(values), max(values)] if values else None,
                            "ranking_reverses": bool(values and min(values) < 0 < max(values))}
    basic = {k: {key: val for key, val in v.items() if key not in ("results", "pace")} for k, v in runs.items()}
    failures = [(family, policy, name) for family, strategies in checks.items() for policy, chk in strategies.items()
                for name, ok in chk.items() if not ok]
    metrics = {"version": spec["version"], "baseline": spec["baseline"], "families": len(families),
               "strategy_runs": sum(len(r["results"]) for r in runs.values()),
               "sensitivity_cases": len(sensitivity), "sensitivity_summary": summaries,
               "strategy_statuses": dict(Counter(r["status"] for v in runs.values() for r in v["results"].values())),
               "invariant_failures": failures, "regressions": parity["regressions"], "model_modes": assets,
               "comparisons": basic,
               "decision": "PATH_A_BOUNDED_DETERMINISTIC_INTEGRATION" if not failures and
               all(x is True or x is None for x in parity["regressions"].values()) else "PATH_B_STABILIZATION",
               "full_model_strategy": "PATH_C_REQUIRED_D8_AND_REPLACEMENT_REQUEST_CONTRACT_UNAVAILABLE"}
    output.mkdir(parents=True)
    (output/"config.yaml").write_bytes((root/PLAN).read_bytes())
    for name, doc in {
        "metrics": metrics, "initial_state_manifests": {k: asdict(s) for k, s in families.items()},
        "pace_source_manifest": {k: asdict(s.pace) for k, s in families.items()}, "model_availability": assets,
        "regression_parity": parity, "invariants": checks, "paired_comparisons": basic,
        "sensitivity": sensitivity, "driver_strategy_ledgers": {k: r["results"] for k, r in runs.items()},
        "common_randomness": {k: r["matched"] for k, r in runs.items()},
        "support": {"scope": "MODEL_CONDITIONAL_SCENARIO", "s5_b_physical_calibration": "PARTIAL_EVIDENCE",
                    "full_model": assets["full_model"], "synthetic": "ILLUSTRATIVE_CURVES",
                    "frozen_pace": assets["frozen_pace_replay"],
                    "no_empirical_probabilities": True},
    }.items():
        _json(output/f"{name}.json", doc)
    from f1ri.validation.strategy_plots import plot_strategy_study

    plot_strategy_study(output, families, runs, sensitivity, metrics)
    return output/"metrics.json"
