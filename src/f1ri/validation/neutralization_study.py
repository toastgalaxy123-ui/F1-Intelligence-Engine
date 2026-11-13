"""Run the S5-A fixtures, frozen regression checks and archived status replays."""
from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import timedelta
from itertools import pairwise
from statistics import median
from zipfile import ZipFile

import yaml

from f1ri.simulator.pace_adapter import load_pace_bundle
from f1ri.simulator.race_control import ControlCommand, RaceControlProfile
from f1ri.validation.neutralization_history import historical_cases
from f1ri.validation.neutralization_scenarios import run_synthetic
from f1ri.validation.overtake_historical_study import _load_json
from f1ri.validation.overtake_study import _trace
from f1ri.validation.proximity_pilot import _json, _parquet
from f1ri.validation.traffic_history_study import _sha


def _frozen(root, stage3, replay, config):
    reports = root / "artifacts/reports"
    names = {"s4_b1": "stage5_s4_b1_evidence_stabilization", "s4_b": "stage5_s4_b_historical_overtakes",
             "s4_a": "stage5_s4_a_overtaking_mechanics", "c4": "stage5_s3_c4_clean_reference_validation"}
    hashes = {k: _sha(reports / f"{name}_metrics.json") for k, name in names.items()}
    hashes["d8"] = load_pace_bundle(root, stage3).artifact_sha256
    if hashes != config["frozen"] or _sha(replay / "metrics.json") != hashes["s4_a"]:
        raise ValueError("A frozen S4 or D8 hash differs")
    with ZipFile(reports / f"{names['s4_a']}_ledgers.zip") as archive:
        files = [p for p in sorted(replay.iterdir()) if p.name in archive.namelist()]
        for path in files:
            if archive.read(path.name) != path.read_bytes():
                raise ValueError(f"S4-A replay differs: {path.name}")
    if len(files) != 12:
        raise ValueError("S4-A generated replay file set incomplete")
    return {"hashes": hashes, "s4_a_reproduced_files": [p.name for p in files],
            "s1_s2_s3_and_t1": _load_json(replay / "metrics.json")["parity"]}


def _checks(result):
    run = result.execution.run
    if run is None:
        return {"field_supported": False}
    crossings = [f for f in run.trace if f.event.kind.name in (
        "CHECKPOINT_CROSSING", "LAP_COMPLETED", "ON_TRACK_PASS")]
    by_driver = {c.driver_number: [] for c in result.execution.initial.cars}
    for f in crossings:
        by_driver[f.event.driver_number].append(f.event)
    positions = {}
    monotonic = True
    for f in result.progress:
        for d, p in f["positions_m"].items():
            monotonic &= p >= positions.get(d, p) - 1e-6
            positions[d] = p
    return {
        "field_supported": True,
        "ordered_event_clock": all(a.event.at_us <= b.event.at_us for a, b in pairwise(run.trace)),
        "contiguous_unique_crossings": all(b.at_us > a.at_us and b.checkpoint == a.checkpoint+1
                                          for rows in by_driver.values() for a, b in pairwise(rows)),
        "racing_distance_monotonic": monotonic,
        "racing_order_preserved": all(g >= -1e-6 for r in result.progress for g in r["gaps_m"].values()),
        "no_pass_under_neutralization": all(f.state.track_state == "GREEN" for f in crossings
                                            if f.event.kind.name == "ON_TRACK_PASS"),
        "no_crossing_under_red": all(f.state.track_state != "RED" for f in crossings),
        "clock_conservation": all(r["wall_elapsed_us"] == r["active_elapsed_us"] + r["suspended_us"]
                                  and r["active_elapsed_us"] >= 0 for r in result.progress),
        "classification_field_identity": all(set(f.state.timing_order) == set(by_driver) for f in run.trace),
        "elapsed_traversal_accounting": all(t.effective_us == t.clean_us + t.traffic_delay_us
                                            and t.traffic_delay_us >= 0 for t in result.execution.traversals),
        "provenance_and_rejections": all(r["event"]["provenance"] and (r["outcome"] == "COMMITTED" or r["reason"])
                                         for r in result.controls),
    }


def _scenario_metrics(result):
    progress = result.progress
    run = result.execution.run
    passages = {(f.event.driver_number, f.event.checkpoint, f.event.at_us): f.state.track_state
                for f in run.trace if f.event.kind.name in (
        "LAP_COMPLETED", "ON_TRACK_PASS", "CHECKPOINT_CROSSING")}
    train = [r for r in progress if r["kind"] == "TRAIN_FORMED"]
    reschedules = [r for r in result.execution.interactions if r["phase"] == "RESCHEDULED"]
    return {
        "deployed_and_released": [r for r in result.controls if r["event"]["command"] in (
            ControlCommand.VSC, ControlCommand.SC, ControlCommand.RED, ControlCommand.GREEN)],
        "pass_outcomes": dict(Counter(r.status for r in result.execution.results)),
        "laps_by_state": dict(Counter(f.state.track_state for f in run.trace if f.event.checkpoint
                                      and f.event.checkpoint % 3 == 0)),
        "suspension_us": progress[-1]["suspended_us"] if progress else 0,
        "queue_formed_us": [r["at_us"] for r in train],
        "compression_at_train_m": min((sum(g for d, g in progress[0]["gaps_m"].items() if d != r["pacer"])
                                       - sum(g for d, g in r["gaps_m"].items() if d != r["pacer"])
                                       for r in train), default=None),
        "invalidated_crossings": sum(r.get("invalidated_crossings", 0) for r in reschedules),
        "regenerated_crossings": sum(r.get("regenerated_crossings", 0) for r in reschedules),
        "added_checkpoint_traversal_us": sum(t.traffic_delay_us for t in result.execution.traversals),
        "added_traversal_us_by_crossing_state": {
            status: sum(t.traffic_delay_us for t in result.execution.traversals
                        if passages.get((t.driver_number, t.checkpoint, t.crossing_us)) == status)
            for status in sorted(set(passages.values()))
        },
        "final_classification": run.final_state.timing_order,
        "final_phase": run.final_state.phase,
        "finish_us": run.final_state.chequered_us,
        "unsupported_controls": sum(r["outcome"] == "UNSUPPORTED" for r in result.controls),
    }


def _compare_archive(result, evidence):
    run = result.execution.run
    epoch = result.execution.initial.race_start_utc
    observed = {(r["driver"], r["completed_laps"]): r["utc"] for r in evidence["observed_lines"]}
    comparisons = []
    for f in run.trace:
        e = f.event
        if e.kind.name == "LAP_COMPLETED" and (e.driver_number, e.checkpoint//3) in observed:
            recorded = observed[e.driver_number, e.checkpoint//3]
            simulated = epoch + timedelta(microseconds=e.at_us)
            comparisons.append({"driver": e.driver_number, "completed_laps": e.checkpoint//3,
                                "recorded_utc": recorded, "simulated_utc": simulated,
                                "error_s": (simulated-recorded).total_seconds()})
    errors = [abs(r["error_s"]) for r in comparisons]
    shifts = [r for r in result.controls if r["outcome"] == "COMMITTED"
              and r["event"]["command"] in (ControlCommand.VSC, ControlCommand.SC, ControlCommand.GREEN)]
    return {"compared_lines": comparisons, "median_absolute_line_error_s": median(errors) if errors else None,
            "command_timing": [{"command": r["event"]["command"], "requested_us": r["event"]["at_us"],
                                "effective_us": r["clock_us"], "difference_us": r["clock_us"]-r["event"]["at_us"]}
                               for r in shifts]}


def _encode(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_encode(v) for v in value]
    return value


def run_study(root, warehouse, stage3, replay, output, plan):
    config = yaml.safe_load(plan.read_bytes())
    if output.exists():
        raise FileExistsError("Use a new S5-A output directory")
    if config["acquisition"] != "CACHE_ONLY" or config["historical"] != {9896: "frozen_pilot", 9987: "reconstruction_only"}:
        raise ValueError("Only approved 2025 cache roles are supported")
    parity = _frozen(root, stage3, replay, config)
    profile = RaceControlProfile(**config["profile"])
    synthetic, runs, pit = run_synthetic(profile)
    archived, replay_runs, evidence = historical_cases(root, warehouse, profile)
    results = runs | replay_runs
    invariant = {name: _checks(r) for name, r in results.items()}
    history = {name: _compare_archive(r, evidence[name]) for name, r in replay_runs.items()}
    checks = {name: all(categories.values()) for name, categories in invariant.items()}
    metrics = {
        "version": config["version"], "synthetic_cases": len(runs), "historical_cases": len(replay_runs),
        "scenario_pass": checks, "scenarios": {name: _scenario_metrics(r) for name, r in results.items()},
        "next_stage": "PATH_B_PIT_ENTRY_RETIMING_AND_HISTORICAL_RESTART_SUPPORT",
        "historical": {name: {k: v for k, v in row.items() if k != "compared_lines"} for name, row in history.items()},
        "pit_comparisons": pit, "frozen": parity, "new_openf1_requests": 0, "random_draws": 0,
        "operational_baseline": "T1_DETERMINISTIC_NO_PASS", "traffic_v1": "RESEARCH_ONLY",
    }
    output.mkdir(parents=True)
    (output / "config.yaml").write_bytes(plan.read_bytes())
    for filename, obj in {
        "metrics": metrics, "regression_parity": parity, "invariant_results": invariant,
        "protocol": {"revision": config["baseline_revision"], "seed": None, "mode": "SCRIPTED_2025_MECHANICAL",
                     "progress": "CHECKPOINT_BOUNDED_PIECEWISE_LINEAR", "historical_roles": config["historical"]},
        "scenario_manifest": {"synthetic": _encode(synthetic), "archived": _encode(archived)},
        "source_manifest": {"warehouse_sha256": _sha(warehouse), "config_sha256": _sha(plan),
                            "sources": ["gold.track_status_intervals", "silver.race_control", "S0_TIMING_REPLAY"],
                            "historical": evidence},
        "race_control_rules_manifest": profile.manifest(),
        "historical_comparison": history,
        "event_traces": {name: _trace(r.execution.run) for name, r in results.items()},
        "pass_attempts": {name: [asdict(p) for p in r.execution.results] for name, r in results.items()},
        "scheduler_interactions": {name: r.execution.interactions for name, r in results.items()},
    }.items():
        _json(output / f"{filename}.json", obj)
    control_rows = [{"scenario": name, **c, **c["event"]} for name, r in results.items() for c in r.controls]
    _parquet(output / "race_control_events.parquet", [{k: v for k, v in c.items() if k != "event"}
                                                       for c in control_rows])
    _parquet(output / "neutralization_state_transitions.parquet", [
        {"scenario": name, "at_us": r["at_us"], "phase": r["phase"], "track_state": r["track_state"],
         "wall_elapsed_us": r["wall_elapsed_us"], "active_elapsed_us": r["active_elapsed_us"],
         "suspended_us": r["suspended_us"], "kind": r["kind"]}
        for name, result in results.items() for r in result.progress])
    _parquet(output / "field_progress.parquet", [
        {"scenario": name, "at_us": r["at_us"], "driver": d, "progress_m": p, "phase": r["phase"],
         "checkpoint": r["checkpoint_counts"][d], "virtual_sc_m": r["virtual_sc_m"],
         "route": r["routes"][d], "position_support": "CONDITIONAL_CHECKPOINT_INTERPOLATION"}
        for name, result in results.items() for r in result.progress for d, p in r["positions_m"].items()])
    _parquet(output / "queue_state.parquet", [
        {"scenario": name, "at_us": r["at_us"], "driver": d, "predecessor": leader,
         "gap_m": r["gaps_m"].get(d), "pacer": r["pacer"], "phase": r["phase"]}
        for name, result in results.items() for r in result.progress for d, leader in r["neighbors"].items()])
    _parquet(output / "pit_interactions.parquet", pit)
    _parquet(output / "restart_events.parquet", [{k: v for k, v in c.items() if k != "event"}
                                                 for c in control_rows if c["command"] in (
        ControlCommand.SC_IN, ControlCommand.SC_WITHDRAWN, ControlCommand.ROLLING, ControlCommand.GREEN)])
    _parquet(output / "classification_reconciliation.parquet", [
        {"scenario": name, "at_us": f.event.at_us, "driver": c.driver_number, "status": c.status,
         "checkpoint": c.completed_checkpoints, "completed_laps": c.completed_laps,
         "rank": f.state.timing_order.index(c.driver_number)+1}
        for name, r in results.items() for f in r.execution.run.trace for c in f.state.cars
        if f.event.kind.name in ("RACE_CONTROL", "LAP_COMPLETED", "SCENARIO_WINDOW_END")])
    from f1ri.validation.neutralization_plots import plot_study

    figures = output / "diagnostics"
    figures.mkdir()
    plot_study(figures, results, metrics, invariant)
    return output / "metrics.json"
