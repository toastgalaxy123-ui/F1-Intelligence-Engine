"""Run the locked development study and retain cutoff and control audit ledgers."""
from __future__ import annotations

import json
from collections import Counter
from functools import partial
from hashlib import sha256
from pathlib import Path

import pandas as pd
import yaml

from f1ri.config import load_config
from f1ri.ingestion.openf1 import OpenF1Client
from f1ri.simulator.pace_adapter import load_pace_bundle
from f1ri.validation.proximity_pilot import _json, _parquet
from f1ri.validation.traffic_continuation import _collect_race, load_traffic_protocol
from f1ri.validation.traffic_history_audit import (
    audit_hungary_history,
    audit_hungary_marker,
    parquet_rows,
)
from f1ri.validation.traffic_history_plan import plan_development_race
from f1ri.validation.traffic_reference import (
    INDEPENDENCE_RULE,
    encounter_units,
    residual_research,
    traffic_readiness,
)
from f1ri.validation.traffic_target_audit import SCREENED, target_summary


def _sha(path):
    return sha256(path.read_bytes()).hexdigest()


def _frozen_rows(folder):
    refs = parquet_rows(folder / "clean_pace_references.parquet")
    outcomes = parquet_rows(folder / "lap_outcomes.parquet")
    ledger = {row["opportunity_id"]: row for row in parquet_rows(folder / "lap_eligibility_ledger.parquet")
              if row["screen"] in SCREENED}
    return refs, [row | _label_fields(ledger[row["opportunity_id"]]) for row in outcomes]


def _label_fields(row):
    return {name: row.get(name) for name in (
        "cutoff_episode_id", "cutoff_opponent", "stint_number", "outcome_available_by_utc",
    )}


def _control_audit(refs, outcomes, config, *, fixed_validation=None):
    residuals = residual_research(refs, outcomes, config, audit_controls=True)
    index = {row["opportunity_id"]: row for row in residuals}
    rows = [ref | {"match_attempted": ref["opportunity_id"] in index}
            | {name: index.get(ref["opportunity_id"], {}).get(name) for name in (
                "control_pool_attrition", "matched_control_count", "matched_control_ids",
                "signed_residual_s", "signed_proxy_s",
            )} for ref in refs]
    if fixed_validation is not None:
        fixed_controls, fixed_residuals = fixed_validation
        rows = [fixed_controls.get(row["opportunity_id"], row) for row in rows]
        residuals = [fixed_residuals.get(row["opportunity_id"], row) for row in residuals]
    return rows, residuals


def _audit_summary(controls):
    attempted = [row for row in controls if row["match_attempted"]]
    return {"attempted": len(attempted), "unattempted": len(controls) - len(attempted),
            "zero_matches": sum(row["matched_control_count"] == 0 for row in attempted),
            "maximum_matches": max((row["matched_control_count"] for row in attempted), default=0),
            "proxies": sum(row["signed_proxy_s"] is not None for row in attempted),
            "filter_totals": dict(sum((Counter(row["control_pool_attrition"]) for row in attempted), Counter()))}


def _control_json_rows(rows):
    fields = ("opportunity_id", "session_key", "as_of_utc", "pace_status", "match_attempted",
              "control_pool_attrition", "matched_control_count", "matched_control_ids",
              "signed_residual_s", "signed_proxy_s")
    return [{name: row.get(name) for name in fields} for row in rows]


def _baseline_evidence(root, warehouse, baseline, output, config):
    committed = root / "artifacts/reports"
    for saved, reference in (("metrics.json", "stage5_s3_c_metrics.json"),
                             ("coverage_diagnostics.json", "stage5_s3_c_coverage_diagnostics.json")):
        if _sha(baseline / saved) != _sha(committed / reference):
            raise ValueError(f"Frozen study input differs from {reference}")
    manifest = json.loads((baseline / "acquisition_manifest.json").read_text(encoding="utf-8"))
    diagnostics = json.loads((baseline / "coverage_diagnostics.json").read_text(encoding="utf-8"))
    extension = diagnostics["development_pilot"]
    folders = [baseline / "9928", baseline / "coverage_pilot/9928", baseline / "9912", baseline / "9987"]
    frozen = [_frozen_rows(folder) for folder in folders]
    original_pool = [frozen[i] for i in (0, 2, 3)]
    spans = [(pd.Timestamp(window["telemetry"]["start_utc"]), pd.Timestamp(window["telemetry"]["end_utc"]))
             for window in extension["sources"]["windows"]]
    original_refs, original_outcomes = frozen[0]
    clocks = {row["opportunity_id"]: row["as_of_utc"] for row in original_refs}
    retained = [row for row in original_outcomes if not any(
        start <= clocks[row["opportunity_id"]] < end for start, end in spans
    )]
    retained_ids = {row["opportunity_id"] for row in retained}
    frozen[0] = ([row for row in original_refs if row["opportunity_id"] in retained_ids], retained)
    cohort = next(row for row in config["cohorts"] if row.get("session_key") == 9928)
    prior = audit_hungary_history(warehouse, cohort, folders[1], config)
    marker = audit_hungary_marker(root, warehouse, folders[1], config)
    _json(output / "hungary_prior_laps.json", prior)
    _json(output / "hungary_sector_marker.json", marker)
    _parquet(output / "hungary_prior_laps.parquet", [
        row | {"opportunity_id": target["opportunity_id"], "cutoff_utc": target["cutoff_utc"]}
        for target in prior for row in target["prior_laps"]
    ])
    refs, outcomes = _deduplicate(frozen)
    controls, _ = _control_audit(refs, outcomes, config["reference"])
    original_controls, fixed_residuals = _control_audit(*_deduplicate(original_pool), config["reference"])
    fixed = ({row["opportunity_id"]: row for row in original_controls if row["session_key"] == 9912},
             {row["opportunity_id"]: row for row in fixed_residuals if row["session_key"] == 9912})
    controls = [fixed[0].get(row["opportunity_id"], row) for row in controls]
    _parquet(output / "frozen_control_attrition.parquet", controls)
    _json(output / "frozen_control_attrition.json", _control_json_rows(controls))
    return (manifest["races"] + [extension["sources"]], diagnostics, frozen, prior, marker, controls,
            len(original_outcomes) - len(retained), fixed)


def _deduplicate(pairs):
    """Later paired evidence takes precedence for each observed lap."""
    refs, outcomes = {}, {}
    for local_refs, local_outcomes in pairs:
        for row in local_outcomes:
            key = row["opportunity_id"]
            if key in outcomes and outcomes[key]["completed_utc"] != row["completed_utc"]:
                raise ValueError(f"Conflicting elapsed evidence for {key}")
            outcomes[key] = row
        refs.update((row["opportunity_id"], row) for row in local_refs)
    return list(refs.values()), list(outcomes.values())


def _before_acquisition(output, inputs, geometry_available, *, root, warehouse, cohort, plan, config, archives):
    windows, summary, history = plan_development_race(
        root, warehouse, cohort, inputs, geometry_available, plan, config, archives,
    )
    _json(output / "selection_manifest.json", summary)
    _parquet(output / "history_qualification.parquet", history)
    return windows, summary


def _arm_summary(summary, archive, detail, refs, residuals, folder):
    audit = detail["audit"]
    targets = target_summary(audit, detail["episodes"], detail["segments"], refs, residuals, folder)
    arms = {}
    for name, limit in detail["plan"]["planned_arm_counts"].items():
        row = targets["by_stratum"].get(name)
        if row is None:
            arms[name] = {"planned_blocks": limit, "acquired_blocks": 0, "acquired_seconds": 0,
                          "status": "NO_FEASIBLE_ANCHOR"}
            continue
        windows = detail["coverage"]["modes"]["POINT_IN_TIME"]["strata"][name]
        local = [lap for lap in audit.outcomes if lap["window_kind"] == name]
        sources = [window for window in archive["windows"] if window["kind"] == name]
        row["planned_blocks"], row["acquired_blocks"] = limit, len(sources)
        row["coverage"] = windows
        row["acquired_seconds"] = sum((item["telemetry"]["end_utc"] - item["telemetry"]["start_utc"]).total_seconds()
                                      for item in sources)
        row["source_rows"] = {endpoint: sum(item["merge"][endpoint]["usable_rows"] for item in sources)
                              for endpoint in ("location", "car_data")}
        row["following_independence"] = encounter_units([
            lap | {"episode_ids": lap.get("outcome_episode_ids", ())} for lap in local
        ])
        row["supported_control_laps"] = sum(lap["screen"] == "CLEAN_CONTROL" and lap.get("pace_status") == "SUPPORTED"
                                             for lap in local)
        row["control_proxies"] = sum(lap["screen"] == "CLEAN_CONTROL" and lap.get("signed_proxy_s") is not None
                                     for lap in local)
        by_lap = {lap["opportunity_id"]: lap for lap in local if lap["screen"] != "LEFT_TRUNCATED_WINDOW"}
        row["anchor_outcomes"] = []
        for item in sources:
            anchor = by_lap.get(item["anchor"], {})
            row["anchor_outcomes"].append({
                "opportunity_id": item["anchor"], "screen": anchor.get("screen", "CUTOFF_NOT_IN_LEDGER"),
                "pace_status": anchor.get("pace_status"), "matched_control_count": anchor.get("matched_control_count"),
            })
        arms[name] = row
    markers = parquet_rows(folder / "markers.parquet")
    for name, arm in arms.items():
        names = {w["name"] for w in archive["windows"] if w["kind"] == name}
        checks = [row for row in markers if row["window"] in names]
        arm["timing_markers"] = {"requested": len(checks), "resolved": sum(row["error_s"] is not None for row in checks)}
    return summary | {"selection": detail["plan"], "sources": archive, "targets": targets, "arms": arms}


def run_history_study(root: Path, warehouse: Path, protocol: Path, pilot: Path, baseline: Path,
                      stage3_root: Path, output: Path, *, ingest: bool = False,
                      transition_lock: Path | None = None, transition_history: Path | None = None) -> dict:
    config, protocol_hash = load_traffic_protocol(protocol)
    text = pilot.read_text(encoding="utf-8")
    plan = yaml.safe_load(text)
    protected_inputs = [baseline]
    if transition_history is not None:
        protected_inputs.append(transition_history)
    if transition_lock is not None:
        protected_inputs.append(transition_lock.parent)
    if any(path.resolve() in (output.resolve(), *output.resolve().parents) for path in protected_inputs):
        raise ValueError("The candidate study requires a separate run directory")
    locked = None
    if transition_lock is not None:
        from f1ri.validation.traffic_transition import lock_transition_windows, locked_extensions

        locked = json.loads(transition_lock.read_text(encoding="utf8"))
        if transition_history is None:
            raise ValueError("Transition study requires its frozen history run")
        output.mkdir(parents=True, exist_ok=True)
        replay = output / "selection_verification.json"
        lock_transition_windows(root, warehouse, transition_history, transition_lock.parent, plan, replay)
        if replay.read_bytes() != transition_lock.read_bytes():
            raise ValueError("Transition lock differs from deterministic census selection")
    if plan["frozen_protocol_sha256"] != protocol_hash or plan["independence_rule"] != INDEPENDENCE_RULE:
        raise ValueError("History study requires its frozen protocol and independence rule")
    if [row["session_key"] for row in plan["cohorts"]] != [9955, 9928]:
        raise ValueError("History study is limited to Austria and Hungary in chronological order")
    bundle = load_pace_bundle(root, stage3_root)
    stage3 = stage3_root / "artifacts/runs/stage3_degradation_v1"
    splits = json.loads((stage3 / "splits.json").read_text(encoding="utf-8"))
    labels = {row["session_key"]: name for name, rows in splits.items() for row in rows}
    if ({row["session_key"] for row in splits["train"]} != set(bundle.training_sessions)
            or {row["session_key"] for row in splits["val"]} != set(bundle.calibration_sessions)):
        raise ValueError("Stage 3 input split differs from the frozen D8 manifest")
    if any(labels.get(key) != "test" or key in bundle.training_sessions + bundle.calibration_sessions
           for key in (9955, 9928)):
        raise ValueError("Development race was not held out from frozen D8")
    geometry_budget = plan["acquisition"]["maximum_geometry_requests_per_race"]
    if geometry_budget < 2 or any(2 * (1 + len(race.get("fallback_geometry_laps", ()))) > geometry_budget
                                 for race in plan["cohorts"]):
        raise ValueError("Configured geometry attempts exceed the frozen request budget")
    output.mkdir(parents=True, exist_ok=True)
    (archives, diagnostics, frozen, prior, marker, frozen_controls,
     frozen_overlap, fixed_validation) = _baseline_evidence(root, warehouse, baseline, output, config)
    if diagnostics["development_pilot"]["plan"]["plan_sha256"] != plan["frozen_coverage_plan_sha256"]:
        raise ValueError("Hungary history baseline used a different coverage plan")
    cohorts = {row.get("session_key"): row for row in config["cohorts"]}
    geometries = {row["session_key"]: row for row in config["initial_acquisition"]}
    source = load_config(root)["data"]
    candidate = []
    with OpenF1Client(source["openf1_base_url"], source["min_seconds_between_requests"]) as openf1:
        for spec in plan["cohorts"]:
            key = spec["session_key"]
            cohort, folder = cohorts[key], output / str(key)
            race = geometries[key] if spec.get("reuse_frozen_geometry") else spec
            folder.mkdir(parents=True, exist_ok=True)
            planner = partial(_before_acquisition, folder, root=root, warehouse=warehouse,
                              cohort=cohort, plan=plan, config=config, archives=archives)
            if locked is not None:
                def planner(inputs, geometry_available, key=key):
                    windows, summary = locked_extensions(key, locked, plan)
                    if any(window.replay.telemetry.start_utc < geometry_available for window in windows):
                        raise ValueError("Locked transition window predates geometry availability")
                    return windows, summary
            result = _collect_race(root, warehouse, race, cohort, config, folder, openf1 if ingest else None, bundle,
                                   coverage_diagnostics=True, development_planner=planner)
            summary, _, refs, _, archive, detail = result
            if spec.get("reuse_frozen_geometry"):
                prior_geometry = next(row["geometry_sha256"] for row in archives if row["session_key"] == key)
                if archive["geometry_sha256"] != prior_geometry:
                    raise ValueError("Hungary geometry changed")
            if locked is not None:
                prior_geometry = json.loads((transition_history / str(key) / "geometry.json").read_text(encoding="utf8"))
                if json.loads((folder / "geometry.json").read_text(encoding="utf8")) != prior_geometry:
                    raise ValueError("Transition study geometry differs from its frozen history run")
            if len(archive["geometry_sources"]) > geometry_budget:
                raise ValueError("History study exceeded geometry budget")
            ledger = {row["opportunity_id"]: row for row in detail["audit"].outcomes if row["screen"] in SCREENED}
            evidence = [row | _label_fields(ledger[row["opportunity_id"]]) for row in detail["audited_outcomes"]]
            candidate.append((summary, refs, archive, detail, folder, evidence))
    # Original and extended Hungary can contribute earlier controls. The new
    # study supplies its own target labels and takes precedence at shared laps.
    covered = {lap["opportunity_id"] for row in candidate for lap in row[3]["audit"].cutoffs}
    if locked is not None:
        for key in (9955, 9928):
            folder = transition_history / str(key)
            covered.update(row["opportunity_id"] for row in parquet_rows(folder / "lap_cutoff_cohort.parquet"))
        old_history = [_frozen_rows(transition_history / str(key)) for key in (9955, 9928)]
        study_covered = {lap["opportunity_id"] for row in candidate for lap in row[3]["audit"].cutoffs}
    else:
        old_history, study_covered = [], covered
    retained = [([ref for ref in refs if ref["opportunity_id"] not in covered],
                 [outcome for outcome in outcomes if outcome["opportunity_id"] not in covered])
                for refs, outcomes in frozen]
    history_retained = [([ref for ref in refs if ref["opportunity_id"] not in study_covered],
                         [outcome for outcome in outcomes if outcome["opportunity_id"] not in study_covered])
                        for refs, outcomes in old_history]
    refs, outcomes = _deduplicate(retained + history_retained + [(row[1], row[5]) for row in candidate])
    controls, residuals = _control_audit(refs, outcomes, config["reference"], fixed_validation=fixed_validation)
    _parquet(output / "control_pool_attrition.parquet", controls)
    _json(output / "control_pool_attrition.json", _control_json_rows(controls))
    results, candidate_ids = [], set()
    for summary, local_refs, archive, detail, folder, evidence in candidate:
        ids = {row["opportunity_id"] for row in evidence}
        candidate_ids.update(ids)
        local = [row for row in residuals if row["opportunity_id"] in ids]
        _parquet(folder / "traffic_residuals.parquet", local)
        results.append(_arm_summary(summary, archive, detail, local_refs, local, folder))
    # Monza stays fixed. Development gates use only the new study outcomes.
    study_rows = [row for row in residuals if row["opportunity_id"] in candidate_ids or row["session_key"] == 9912]
    fixed = next(row for row in diagnostics["races"] if row["session_key"] == 9912)
    baseline_metrics = json.loads((baseline / "metrics.json").read_text(encoding="utf-8"))
    fixed_timing = next(row["timing_marker_gate"] for row in baseline_metrics["races"] if row["session_key"] == 9912)
    readiness = traffic_readiness(
        [*results, {"session_key": 9912, "split": "validation", "timing_marker_gate": fixed_timing}], study_rows,
        config["evaluation"], independence_rule=plan["independence_rule"],
    )
    ledger = []
    for result in results:
        ledger.extend(parquet_rows(output / str(result["session_key"]) / "lap_eligibility_ledger.parquet"))
    _parquet(output / "development_eligibility.parquet", ledger)
    brief = [row for row in ledger if row["screen"] in ("FOLLOWING", "CLEAN_CONTROL")]
    attrition = {row["opportunity_id"]: row for row in controls}
    _json(output / "target_eligibility.json", [row | {
        "control_pool_attrition": attrition.get(row["opportunity_id"], {}).get("control_pool_attrition"),
        "control_match_attempted": attrition.get(row["opportunity_id"], {}).get("match_attempted", False),
    } for row in brief])
    result = {
        "plan_version": plan["version"], "plan_sha256": sha256(text.encode()).hexdigest(),
        "protocol_sha256": protocol_hash, "archive_availability": "OBSERVATION_TIMESTAMP_PROXY",
        "input_sha256": {"warehouse": _sha(warehouse), "frozen_metrics": _sha(baseline / "metrics.json"),
                         "frozen_coverage_diagnostics": _sha(baseline / "coverage_diagnostics.json")},
        "traffic_model_status": "RESEARCH_ONLY", "readiness": readiness, "races": results,
        "fixed_monza": {"timing_marker_gate": fixed_timing, "targets": fixed["targets"]["following_evidence"],
                        "following_d8": fixed["targets"]["supported_following"]["unique_laps"],
                        "following_proxies": fixed["targets"]["following_with_proxy"]["unique_laps"]},
        "frozen_control_audit": _audit_summary(frozen_controls),
        "control_pool_audit": _audit_summary(controls), "historical_and_study_duplicate_lap_rows_removed": (
            frozen_overlap + sum(len(pair[1]) for pair in frozen + old_history)
            + sum(len(row[5]) for row in candidate) - len(outcomes)
        ),
        "original_hungary_rows_superseded_by_frozen_extension": frozen_overlap,
        "frozen_rows_superseded_on_study_grid": sum(len(old[1]) - len(kept[1])
                                                   for old, kept in zip(frozen, retained, strict=True)),
        "frozen_hungary_history": [{name: value for name, value in row.items() if name != "prior_laps"} for row in prior],
        "frozen_hungary_marker": marker,
        "d8": {"sha256": bundle.artifact_sha256, "stage3_splits_sha256": _sha(stage3 / "splits.json"),
               "trained_through_utc": bundle.trained_through_utc,
               "selected_and_calibrated_through_utc": bundle.calibrated_through_utc,
               "training_sessions": bundle.training_sessions, "selection_sessions": bundle.calibration_sessions,
               "calibration_sessions": bundle.calibration_sessions, "study_races_stage3_split": {
                   key: labels[key] for key in (9955, 9928)
               }},
    }
    _json(output / "history_study_metrics.json", result)
    from f1ri.validation.traffic_plots import plot_history_study

    plot_history_study(result, output / "stage5_s3_c_history_study.png")
    if locked is not None:
        from f1ri.validation.traffic_transition import anchor_conversion
        from f1ri.validation.traffic_transition_plots import plot_transition

        result["transition_lock_sha256"] = _sha(transition_lock)
        result["prior_history_outcomes_reused"] = sum(len(pair[1]) for pair in history_retained)
        result["anchor_conversion"] = anchor_conversion(results, locked, output, config["reference"])
        for race in results:
            folder = output / str(race["session_key"])
            ledger = parquet_rows(folder / "lap_eligibility_ledger.parquet")
            race["following_independence"] = encounter_units([
                row | {"episode_ids": row["outcome_episode_ids"] or []}
                for row in ledger if row["screen"] == "FOLLOWING"
            ])
            residuals_path = folder / "traffic_residuals.parquet"
            pace = ({row["opportunity_id"]: row for row in parquet_rows(residuals_path)}
                    if residuals_path.is_file() else {})
            for row in race["targets"]["following_evidence"]:
                residual = pace.get(row["opportunity_id"], {})
                row.update({name: residual.get(name) for name in ("expected_clean_s", "observed_lap_s")})
        census = json.loads((transition_lock.parent / "census.json").read_text(encoding="utf8"))
        control_counts = {}
        for key in (9955, 9928):
            rows = parquet_rows(transition_lock.parent / f"candidates_{key}.parquet")
            control_counts[key] = [(row["lap_number"], len(row["matched_control_ids"]))
                                   for row in rows if row["matched_control_ids"] is not None]
        plot_transition(census, result, control_counts, output / "stage5_s3_c_transition.png")
        _json(output / "transition_metrics.json", result)
    return result
