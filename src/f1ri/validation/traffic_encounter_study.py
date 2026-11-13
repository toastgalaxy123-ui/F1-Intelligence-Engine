"""Run the bounded S3-C conversion and recovery experiment."""
from __future__ import annotations

import json
from bisect import bisect_left
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pandas as pd
import yaml

from f1ri.config import load_config
from f1ri.ingestion.openf1 import OpenF1Client
from f1ri.simulator.pace_adapter import load_pace_bundle
from f1ri.validation.proximity_pilot import _json
from f1ri.validation.traffic_acquisition import race_inputs
from f1ri.validation.traffic_continuation import load_traffic_protocol
from f1ri.validation.traffic_control_recovery import (
    TARGET,
    evaluate_controls,
    frozen_control_pool,
    recovery_sources,
    recovery_windows,
    target_control_candidates,
)
from f1ri.validation.traffic_encounter_audit import anchor_audits, window_rows
from f1ri.validation.traffic_history_audit import parquet_rows
from f1ri.validation.traffic_history_plan import _rank
from f1ri.validation.traffic_history_study import _sha
from f1ri.validation.traffic_reference import control_pool, encounter_units, residual_research


def selector(rows, physical, plan):
    """Rank lap-start candidates using cutoff-visible observations only."""
    candidates = []
    for row in rows:
        if row["d8_status"] != "SUPPORTED" or not row["state_eligible"]:
            continue
        identity = row["opportunity_id"]
        cutoff = pd.Timestamp(row["cutoff_utc"])
        samples = [r for r in physical.get(identity, ()) if r["physical_as_of_utc"] <= cutoff
                   and cutoff - r["physical_as_of_utc"] <= timedelta(seconds=plan["selector"]["maximum_age_s"])]
        newest = max(samples, key=lambda r: (r["physical_as_of_utc"], r["window"]), default=None)
        trigger = None
        if newest and newest["cutoff_exposure"] == "CLOSE" and newest["opponent_number"] is not None:
            trigger = "SUPPORTED_CLOSE_AT_LAP_START"
        elif row["approach"] == "PHYSICAL_APPROACH":
            trigger = "PHYSICAL_APPROACH_PRIORITY"
        elif row["approach"] == "COARSE_INTERVAL_APPROACH" and (
                newest is None or newest["cutoff_exposure"] == "UNSUPPORTED"):
            trigger = "COARSE_ACQUISITION_PRIORITY"
        if trigger:
            candidates.append(row | {"trigger": trigger, "cutoff_observation": newest,
                                     "evaluation_window_start_utc": cutoff.floor("s") - timedelta(
                                         seconds=plan["selector"]["replay_prefix_seconds"]),
                                     "evaluation_window_end_utc": cutoff.ceil("s") + timedelta(
                                         seconds=plan["selector"]["replay_suffix_seconds"]),
                                     "frozen_control_count": row.get("available_control_count",
                                                                     len(row["matched_control_ids"] or []))})
    return sorted(candidates, key=lambda r: (
        plan["selector"]["priority"].index(r["trigger"]), -r["frozen_control_count"],
        _rank(r, plan["seed"], r["session_key"], "conversion"),
    ))


def yield_bounds(census, screens, cutoff_rows, control_rows, frozen_following):
    """Bound each one-factor scenario within the audited population."""
    immutable = {"PIT_OR_STINT_TRANSITION", "ARCHIVE_PASS_INDICATION_CENSORED",
                 "GREEN_DRY_OUTCOME_UNSUPPORTED", "TIMING_BOUNDARY_UNSUPPORTED", "MISSING_OBSERVED_ELAPSED"}
    supported = [r for r in census if r["d8_status"] == "SUPPORTED"]
    following = [r for r in frozen_following if r["pace_status"] == "SUPPORTED"]
    actual = [r for r in following if r["signed_proxy_s"] is not None]
    b = []
    for row in supported:
        identity = row["opportunity_id"]
        screen = screens.get(identity, set())
        if screen & immutable or screen & {"CLEAN_CONTROL", "INTERMEDIATE", "FOLLOWING"}:
            continue
        cutoff = cutoff_rows.get(identity, [])
        known = [r for r in cutoff if r["cutoff_exposure"] != "UNSUPPORTED"]
        if known and all(r["cutoff_exposure"] != "CLOSE" for r in known):
            continue
        if control_rows.get(identity, 0) < 5:
            continue
        leaders = {r["opponent_number"] for r in known if r["cutoff_exposure"] == "CLOSE"}
        b.append({"opportunity_id": identity, "driver": row["driver_number"], "stint": row["stint_number"],
                  "known_leaders": sorted(leaders - {None}), "observed_screens": sorted(screen)})
    # One unknown group per unresolved lap is optimistic. Count a singleton
    # known leader group once, allowing distinct assignments for ambiguous rows.
    known = {(r["driver"], r["known_leaders"][0], r["stint"]) for r in b if len(r["known_leaders"]) == 1}
    existing = {(r["driver_number"], r["cutoff_opponent"], r["stint_number"]) for r in actual}
    return {
        "historical_cutoffs": len(census),
        "history_qualified": sum(r["history_status"] == "QUALIFIED" for r in census),
        "d8_supported": len(supported),
        "d8_supported_physical_approaches": sum(r["approach"] == "PHYSICAL_APPROACH" for r in supported),
        "d8_with_five_cached_controls": sum(control_rows.get(r["opportunity_id"], 0) >= 5 for r in supported),
        "d8_supported_observed_following": len(following),
        "A": {"laps": len(actual), "independent_upper_bound": len(existing)},
        "B": {"laps_upper_bound": len(actual) + len(b), "additional_possible_sustained_following": len(b),
              "independent_upper_bound": len(known | existing) + sum(len(r["known_leaders"]) != 1 for r in b),
              "unresolved_candidates": b},
        "C": {"laps_upper_bound": len(following), "additional_potential": len(following) - len(actual),
              "independent_upper_bound": encounter_units(following)["independent_encounters"]},
    }


def _evidence(run, key):
    folder = run / str(key)
    cuts, outcomes = {}, {}
    for r in parquet_rows(folder / "lap_cutoff_cohort.parquet"):
        cuts.setdefault(r["opportunity_id"], []).append(r)
    for r in parquet_rows(folder / "lap_eligibility_ledger.parquet"):
        if r["screen"] != "LEFT_TRUNCATED_WINDOW":
            outcomes[r["opportunity_id"]] = r
    return cuts, outcomes


def run_encounter_study(root: Path, warehouse: Path, stage3: Path, transition: Path, history: Path,
                        baseline: Path, census: Path, output: Path, plan_path: Path, *, lock_path=None, ingest=False):
    config, protocol_hash = load_traffic_protocol(root / "config/experiments/stage5_s3_c_protocol.yaml")
    plan = yaml.safe_load(plan_path.read_text(encoding="utf8"))
    reports = root / "artifacts/reports"
    checked = {}
    for artifact, expected in (
        (baseline / "metrics.json", "stage5_s3_c_metrics.json"),
        (baseline / "coverage_diagnostics.json", "stage5_s3_c_coverage_diagnostics.json"),
        (history / "history_study_metrics.json", "stage5_s3_c_history_metrics.json"),
        (transition / "transition_metrics.json", "stage5_s3_c_transition_metrics.json"),
        (census / "lock.json", "stage5_s3_c_transition_lock.json"),
        (census / "census.json", "stage5_s3_c_transition_census.json"),
    ):
        checked[expected] = _sha(artifact)
        if checked[expected] != _sha(reports / expected):
            raise ValueError(f"Frozen replay differs from {expected}")
    bundle = load_pace_bundle(root, stage3)
    if protocol_hash != plan["frozen_protocol_sha256"] or bundle.artifact_sha256 != plan["d8_sha256"]:
        raise ValueError("Unexpected protocol or D8 artifact")
    metrics = json.loads((transition / "transition_metrics.json").read_text(encoding="utf8"))
    if metrics["input_sha256"]["warehouse"] != _sha(warehouse):
        raise ValueError("Unexpected warehouse")
    if any(p.resolve() in (output.resolve(), *output.resolve().parents) for p in (transition, history, baseline, census)):
        raise ValueError("Encounter audit requires a separate output directory")
    output.mkdir(parents=True, exist_ok=True)
    locked = json.loads((census / "lock.json").read_text(encoding="utf8"))
    census_manifest = json.loads((census / "census.json").read_text(encoding="utf8"))
    cohorts = {r["session_key"]: r for r in config["cohorts"] if r["split"] == "development"}
    target = next(r for r in parquet_rows(transition / "9955/traffic_residuals.parquet") if r["opportunity_id"] == TARGET)
    refs, frozen_outcomes = frozen_control_pool(transition, history, baseline, target, config)
    clean_refs = {r["opportunity_id"]: r for r in refs if r["pace_status"] == "SUPPORTED"}
    frozen_controls = [r | clean_refs[r["opportunity_id"]] for r in frozen_outcomes
                       if r["outcome_group"] == "CLEAN_CONTROL" and r["opportunity_id"] in clean_refs]
    timelines, failures, alignment, bounds, selected, close_conversion = [], [], [], {}, {}, {}
    inputs = {key: race_inputs(warehouse, key) for key in cohorts}
    for key, data in inputs.items():
        folder = transition / str(key)
        acquisition = json.loads((folder / "acquisition.json").read_text(encoding="utf8"))
        by_id = {w["anchor"]: w for w in acquisition["windows"]}
        anchors = [w["anchor"] for w in locked["races"][str(key)]["windows"]]
        cuts, outcomes = _evidence(transition, key)
        timeline, failure = anchor_audits(folder, [by_id[r["opportunity_id"]] for r in anchors],
                                          anchors, data["laps"], config, outcomes)
        timelines.extend(timeline)
        failures.extend(failure)
        laps = {driver: tuple(frame.sort_values("lap_start_utc").lap_start_utc)
                for driver, frame in data["laps"].groupby("driver_number")}
        for row in window_rows(folder, "traffic_episodes", [w["name"] for w in acquisition["windows"]]):
            clock = laps[row["driver_number"]]
            index = bisect_left(clock, row["start_utc"])
            boundary = clock[index] if index < len(clock) else None
            following_boundary = clock[index + 1] if index + 1 < len(clock) else None
            alignment.append(row | {
                "next_boundary_utc": boundary,
                "lead_to_boundary_s": (boundary - row["start_utc"]).total_seconds() if boundary is not None else None,
                "crosses_boundary": boundary is not None and row["end_utc"] >= boundary,
                "covers_whole_lap": following_boundary is not None and row["end_utc"] >= following_boundary,
            })
        other_cuts, other_outcomes = _evidence(history, key)
        combined = {identity: cuts.get(identity, []) + other_cuts.get(identity, []) for identity in cuts.keys() | other_cuts.keys()}
        screens = {identity: {r["screen"] for r in (outcomes.get(identity), other_outcomes.get(identity)) if r}
                   for identity in outcomes.keys() | other_outcomes.keys()}
        lap_starts = {(int(r.driver_number), int(r.lap_number)): r.lap_start_utc for r in data["laps"].itertuples()}
        for lap in data["laps"].itertuples():
            identity = f"{key}/{lap.driver_number}/{lap.lap_number}"
            if pd.isna(lap.observed_lap_time_s):
                screens.setdefault(identity, set()).add("MISSING_OBSERVED_ELAPSED")
                continue
            line = lap_starts.get((lap.driver_number, lap.lap_number + 1))
            if line is None or abs((line - lap.lap_end_estimate_utc).total_seconds()) > 2:
                screens.setdefault(identity, set()).add("TIMING_BOUNDARY_UNSUPPORTED")
            if lap.track_status != "GREEN" or not lap.confirmed_green_lap or lap.rainfall:
                screens.setdefault(identity, set()).add("GREEN_DRY_OUTCOME_UNSUPPORTED")
            if lap.is_pit_lap or lap.stint_pit_mismatch:
                screens.setdefault(identity, set()).add("PIT_OR_STINT_TRANSITION")
            if lap.had_in_lap_on_track_pass:
                screens.setdefault(identity, set()).add("ARCHIVE_PASS_INDICATION_CENSORED")
        ledger = census / f"candidates_{key}.json"
        if _sha(ledger) != census_manifest["races"][str(key)]["ledger_sha256"]:
            raise ValueError("Frozen candidate ledger changed")
        population = json.loads(ledger.read_text(encoding="utf8"))
        frozen_control_rows = {r["opportunity_id"]: len(control_pool({
            "opportunity_id": r["opportunity_id"], "as_of_utc": pd.Timestamp(r["cutoff_utc"]),
            "compound": r["compound"], "tyre_age": r["tire_age"], "race_progress": r["lap_number"] / cohorts[key]["scheduled_laps"],
        }, frozen_controls, config["reference"])[0]) for r in population if r["d8_status"] == "SUPPORTED"}
        for row in population:
            row["available_control_count"] = frozen_control_rows.get(row["opportunity_id"])
        ranked = selector(population, combined, plan)
        selected[key] = ranked
        close_conversion[key] = dict(Counter(outcomes.get(r["opportunity_id"], {}).get("screen", "UNOBSERVED")
                                             for r in ranked if r["trigger"] == "SUPPORTED_CLOSE_AT_LAP_START"))
        following = [row | {"episode_ids": row["outcome_episode_ids"] or []}
                     for row in outcomes.values() if row["screen"] == "FOLLOWING"]
        bounds[key] = yield_bounds(population, screens, combined, frozen_control_rows, following)

    ledger, new_refs = target_control_candidates(warehouse, bundle, inputs[9955], cohorts[9955], target, config,
                                                  set(target["matched_control_ids"]))
    cached, cache_hashes = recovery_sources(root, transition, history)
    geometry = json.loads((transition / "9955/geometry.json").read_text(encoding="utf8"))
    at = pd.Timestamp(geometry["available_at_utc"])
    off_track = {row["driver_number"]: row for row in metrics["races"][0]["off_track_evidence"]}
    existing = {r["opportunity_id"] for r in frozen_outcomes if r["outcome_group"] == "CLEAN_CONTROL"}
    cached_windows = recovery_windows(ledger, cached, target, plan, at, inputs[9955]["track"])
    admitted, manifest = evaluate_controls(root, cached_windows, ledger, inputs[9955], geometry, config, off_track)
    added = [r for r in admitted if r["opportunity_id"] not in existing]
    if len(added) + target["matched_control_count"] < config["reference"]["minimum_matched_controls"]:
        planned = recovery_windows(ledger, cached, target, plan, at, inputs[9955]["track"], include_missing=True)
    else:
        planned = []
    lock = {"plan_sha256": _sha(plan_path), "transition_sha256": checked["stage5_s3_c_transition_metrics.json"],
            "target": TARGET, "windows": [{"opportunity_id": w.anchor, "start_utc": w.replay.telemetry.start_utc,
                                            "end_utc": w.replay.telemetry.end_utc,
                                            "reused": [r.bounds() for r in w.reused],
                                            "requested": [r.bounds() for r in w.requested]} for w in planned]}
    lock_output = output / "recovery_lock.json"
    if ingest and (lock_path is None or lock_path.resolve() == lock_output.resolve()):
        raise ValueError("Acquisition requires a preregistered recovery lock in a separate run")
    _json(lock_output, lock)
    if ingest:
        if lock_output.read_bytes() != lock_path.read_bytes():
            raise ValueError("Acquisition requires the byte-identical preregistered recovery lock")
        if planned:
            source = load_config(root)["data"]
            with OpenF1Client(source["openf1_base_url"], source["min_seconds_between_requests"]) as client:
                new_outcomes, extra = evaluate_controls(root, planned, ledger, inputs[9955], geometry, config, off_track, client)
            added.extend(r for r in new_outcomes if r["opportunity_id"] not in existing)
            manifest.extend(extra)
    added_ids = {r["opportunity_id"] for r in added}
    result = next(r for r in residual_research(
        [r for r in refs if r["opportunity_id"] not in added_ids] + [new_refs[k] for k in sorted(added_ids)],
        [r for r in frozen_outcomes if r["opportunity_id"] not in added_ids] + added,
        config["reference"], audit_controls=True,
    ) if r["opportunity_id"] == TARGET)
    recovered_following = result["signed_proxy_s"] is not None
    report = {
        "version": plan["version"], "inputs": checked | {"warehouse": _sha(warehouse), "d8": bundle.artifact_sha256,
                                                        "plan": _sha(plan_path), "protocol": protocol_hash},
        "archive_availability": "OBSERVATION_TIMESTAMP_PROXY", "target_bounds": bounds,
        "frozen_target": target, "recovery_target": result, "recovered_control_ids": sorted(added_ids),
        "observed_following_proxies_after_recovery": {9955: int(recovered_following), 9928: 0},
        "qualifying_independent_groups_after_recovery": {9955: int(recovered_following), 9928: 0},
        "control_screen_counts": dict(Counter(r["reason"] for r in ledger)),
        "episode_alignment": {key: {
            "fragments": len(rows := [r for r in alignment if r["session_key"] == key]),
            "began_mid_lap": sum((r["lead_to_boundary_s"] or 0) > 0 for r in rows),
            "began_within_five_seconds": sum(0 <= r["lead_to_boundary_s"] <= 5 for r in rows if r["lead_to_boundary_s"] is not None),
            "crosses_boundary": sum(r["crosses_boundary"] for r in rows),
            "covers_whole_lap": sum(r["covers_whole_lap"] for r in rows),
            "termination": dict(Counter(r["termination"] for r in rows)),
        } for key in cohorts},
        "readiness": metrics["readiness"], "traffic_model_status": "RESEARCH_ONLY",
        "selector_counts": {key: dict(Counter(r["trigger"] for r in rows)) for key, rows in selected.items()},
        "selector_close_conversion": close_conversion,
    }
    for name, data in (("encounter_timelines", timelines), ("physical_support_failures", failures),
                       ("control_recovery", ledger), ("selector", selected), ("episode_alignment", alignment),
                       ("recovery_manifest", {"cache_sha256": cache_hashes, "replays": manifest}),
                       ("metrics", report)):
        _json(output / f"{name}.json", data)
    from f1ri.validation.traffic_encounter_plots import plot_encounter_study

    plot_encounter_study(timelines, failures, alignment, report, output / "diagnostics.png")
    return report
