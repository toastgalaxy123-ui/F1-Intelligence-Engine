"""Compare stabilized pit and restart mechanics with the frozen S5-A run."""
from collections import Counter
from zipfile import ZipFile

import yaml

from f1ri.simulator.neutralization import simulate_neutralization
from f1ri.simulator.race_control import RaceControlProfile
from f1ri.validation.frozen_neutralization_inputs import archived_replay
from f1ri.validation.neutralization_history import historical_cases
from f1ri.validation.neutralization_study import _checks, _encode, _frozen
from f1ri.validation.overtake_historical_study import _load_json
from f1ri.validation.pit_restart_scenarios import fixtures, pit_accounting
from f1ri.validation.proximity_pilot import _json, _parquet
from f1ri.validation.restart_snapshot_audit import historical_comparison, snapshot_audit
from f1ri.validation.traffic_history_study import _sha

PREFIX = "stage5_s5_a_neutralization_mechanics"
STABILIZATION = "stage5_s5_a1_pit_restart_stabilization"


def run_stabilization(root, warehouse, stage3, baseline, s4_replay, imola, output, *, frozen_inputs=False):
    contract = root / f"config/experiments/{STABILIZATION}.yaml"
    protocol = yaml.safe_load(contract.read_bytes())
    plan = root / protocol["baseline_config"]
    config = yaml.safe_load(plan.read_bytes())
    report = root / "artifacts/reports" / f"{PREFIX}_metrics.json"
    frozen_hash = protocol["frozen_s5_a_sha256"]
    if _sha(report) != frozen_hash or _sha(baseline / "metrics.json") != frozen_hash:
        raise ValueError("Frozen S5-A metrics differ")
    with ZipFile(report.with_name(f"{PREFIX}_ledgers.zip")) as archive:
        files = sorted(name for name in archive.namelist() if name not in {"SHA256SUMS", "validation_report.md"})
        if len(files) != 20 or any(archive.read(f) != (baseline/f).read_bytes() for f in files):
            raise ValueError("Frozen S5-A generated files differ")
    parity = (_load_json(baseline / "regression_parity.json") if frozen_inputs
              else _frozen(root, stage3, s4_replay, config))
    names = {"s4_b1": "stage5_s4_b1_evidence_stabilization", "s4_b": "stage5_s4_b_historical_overtakes",
             "s4_a": "stage5_s4_a_overtaking_mechanics", "c4": "stage5_s3_c4_clean_reference_validation"}
    if any(_sha(report.with_name(f"{name}_metrics.json")) != config["frozen"][key] for key, name in names.items()):
        raise ValueError("Frozen research artifact differs")
    parity["d8_verification"] = "FROZEN_S5_A_REPLAY" if frozen_inputs else "ORIGINAL_BUNDLE"
    parity["s5_a_sha256"] = frozen_hash
    parity["s5_a_generated_files"] = len(files)
    if output.exists():
        raise FileExistsError(output)
    profile = RaceControlProfile(**config["profile"])
    specs = fixtures(profile)
    results = {name: simulate_neutralization(**spec) for name, spec in specs.items()}
    if frozen_inputs:
        historical, evidence = archived_replay(baseline, profile)
    else:
        _, historical, evidence = historical_cases(root, warehouse, profile, individual_restart=True)
    comparison = historical_comparison(historical, evidence, _load_json(baseline/"historical_comparison.json"))
    snapshot, coverage, sources = snapshot_audit(root, None if frozen_inputs else warehouse, imola,
                                                 evidence["imola_sc_4"])
    all_runs = results | historical
    checks = {name: _checks(run) for name, run in all_runs.items()}
    pit = pit_accounting(specs, results)
    restart = [{"scenario": name, **row} for name, run in all_runs.items() for row in run.restart_permissions]
    controls = [{"scenario": name, **r["event"], **{k: v for k, v in r.items() if k != "event"}}
                for name, run in all_runs.items() for r in run.controls]
    itinerary = [{"scenario": name, **row} for name, run in results.items() for row in run.pit_ledger]
    for name, run in results.items():
        entries = [r["decision_id"] for r in run.pit_ledger if r["status"] == "PIT_ENTRY_COMMITTED"]
        checks[name]["unique_pit_entry"] = len(entries) == len(set(entries))
        queues = [r for r in run.pit_ledger if r["status"] == "SERVICE_QUEUE"]
        checks[name]["box_exclusive"] = all(a["service_end_us"] <= b["service_start_us"]
                                             for i, a in enumerate(queues) for b in queues[i+1:]
                                             if a["box"] == b["box"])
    strategy = {row["scenario"]: row for row in pit}
    parity["green_pit_160_217"] = strategy["dynamic_green"]["entry_us"] == 160_000_000 and (
        strategy["dynamic_green"]["merge_us"] == 217_000_000
    )
    imola_timing = comparison["imola_sc_4"]
    imola_green = imola_timing["stabilized"]["command_timing"][-1]
    invariants = {k: all(v.values()) for k, v in checks.items()}
    work = {}
    for name, run in all_runs.items():
        reschedules = [r for r in run.execution.interactions if r["phase"] == "RESCHEDULED"]
        work[name] = {"committed_events": len(run.execution.run.trace),
                      "invalidated_crossings": sum(r.get("invalidated_crossings", 0) for r in reschedules),
                      "regenerated_crossings": sum(r.get("regenerated_crossings", 0) for r in reschedules)}
    metrics = {
        "version": protocol["version"], "baseline_revision": protocol["baseline_revision"],
        "synthetic_cases": len(results), "historical_cases": len(historical), "new_openf1_requests": 0,
        "pit_accounting": pit, "snapshot_coverage": coverage,
        "imola_sc": {k: v for k, v in imola_timing.items() if k not in ("frozen", "stabilized")},
        "imola_green": imola_green,
        "imola_frozen_green": imola_timing["frozen"]["command_timing"][-1],
        "physical_support_tier": "DECLARED_FIELD_ONLY" if not coverage["global_clear"]["supported"]
        else "PARTIAL_OR_FULL_LOCALIZATION_TO_AUDIT",
        "scenario_invariants": invariants,
        "scheduler_work": work,
        "sc_release_counts": dict(Counter(r["scenario"] for r in restart if r["status"] == "CROSSED_RESTART_LINE")),
        "sc_release_bounds_us": {name: [min(clocks), max(clocks)] for name in historical
                                 if (clocks := [r["at_us"] for r in restart if r["scenario"] == name
                                                and r["status"] == "CROSSED_RESTART_LINE"])},
        "pit_prediction_counts": dict(Counter(r["scenario"] for r in itinerary if r["status"] == "APPROACHING_PIT_ENTRY")),
        "next_stage": "PATH_A_BOUNDED_MECHANICS" if all(invariants.values()) and
        parity["green_pit_160_217"] and imola_green["difference_us"] == 0 else "PATH_B_NARROW_STABILIZATION",
    }
    output.mkdir(parents=True)
    (output/"config.yaml").write_bytes(contract.read_bytes())
    for name, value in {
        "protocol": protocol,
        "source_manifest": {"warehouse_sha256": _load_json(baseline/"source_manifest.json")["warehouse_sha256"]
                            if frozen_inputs else _sha(warehouse), "frozen_config_sha256": _sha(plan),
                            "stabilization_config_sha256": _sha(contract),
                            "input_mode": "VERIFIED_FROZEN_S5_A" if frozen_inputs else "WAREHOUSE",
                            "imola_snapshot_inputs": sources, "archived_evidence": evidence},
        "rule_profile_manifest": profile.manifest() | {"restart": "GLOBAL_SIGNAL_AND_INDIVIDUAL_RACING_LINE",
                                                        "pit_entry": "DECLARED_LAYOUT_PROGRESS_BOUNDARY"},
        "scenario_manifest": _encode(specs), "historical_timing_comparison": comparison,
        "scheduler_invalidation_trace": {name: run.execution.interactions for name, run in all_runs.items()},
        "invariant_results": checks, "regression_parity": parity, "metrics": metrics,
    }.items():
        _json(output/f"{name}.json", value)
    _parquet(output/"pit_decisions.parquet", _encode([
        {"scenario": name, "decision": d} for name, spec in specs.items() for d in spec.get("pit_decisions", ())]))
    for file, rows in {
        "pit_entry_crossings": [r for r in itinerary if r["status"] == "PIT_ENTRY_COMMITTED"],
        "pit_itinerary_events": [{k: v for k, v in r.items() if k != "neighbors"} for r in itinerary],
        "service_box_queue": [r for r in itinerary if r["status"] == "SERVICE_QUEUE"],
        "pit_rejoins": [{"predecessor": r["neighbors"].get(r["driver"]),
                         **{k: v for k, v in r.items() if k != "neighbors"}} for r in itinerary if r["status"] == "REJOINED"],
        "restart_global_events": controls, "driver_restart_permissions": restart, "historical_snapshot_support": snapshot,
    }.items():
        _parquet(output/f"{file}.parquet", rows)
    from f1ri.validation.pit_restart_plots import plot_study

    plot_study(output, metrics, itinerary, restart, comparison, parity)
    return output/"metrics.json"
