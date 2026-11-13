"""Execute the frozen cache-only short-horizon feasibility protocol."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from statistics import median

import numpy as np
import yaml

from f1ri.simulator.pace_adapter import load_pace_bundle
from f1ri.validation.proximity_pilot import _json, _parquet
from f1ri.validation.traffic_acquisition import race_inputs
from f1ri.validation.traffic_continuation import load_traffic_protocol
from f1ri.validation.traffic_history_audit import parquet_rows
from f1ri.validation.traffic_history_study import _sha
from f1ri.validation.traffic_segment_reference import validate_and_match
from f1ri.validation.traffic_short_horizon import build_race


def group_segments(rows):
    groups = defaultdict(list)
    for row in rows:
        stint = row["outcome"]["retrospective_stint_number"]
        if row["outcome"]["following_label"] and stint is not None:
            key = (row["session_key"], row["driver_number"], row["physical_leader"], stint)
            groups[key].append(row)
    result = []
    for (session, driver, leader, stint), group in sorted(groups.items()):
        ordered = sorted(group, key=lambda r: (r["target_start_utc"], r["opportunity_id"]))
        overlaps, latest = 0, None
        for row in ordered:
            overlaps += latest is not None and row["target_start_utc"] < latest
            latest = max(latest, row["outcome"]["end_utc"]) if latest else row["outcome"]["end_utc"]
        result.append({"session_key": session, "driver_number": driver, "leader": leader, "stint_number": stint,
                       "segments": len(group), "overlaps_with_earlier": overlaps,
                       "episode_fragments": sorted({r["outcome"]["episode_id"] for r in group if r["outcome"]["episode_id"]}),
                       "opportunity_ids": [r["opportunity_id"] for r in ordered]})
    return result


def funnel(rows, traffic, settings):
    keep = list(rows)
    stages = {"prospective": len(rows)}
    predicates = (
        ("initial_supported", lambda r: r["cutoff_group"] != "PHYSICAL_STATE_UNRESOLVED"),
        ("neighbor_supported", lambda r: r["physical_leader"] is not None),
        ("completed", lambda r: r["outcome"]["completed"]),
        ("timing", lambda r: r["outcome"]["timing_pass"]),
        ("route", lambda r: r["support"]["route_continuous"]),
        ("no_events", lambda r: r["outcome"]["green_dry"] and r["outcome"]["no_disqualifying_event"]),
        ("physical_support", lambda r: r["support"]["support_pass"]),
        ("physical_following", lambda r: r["outcome"]["following_label"]),
    )
    for name, predicate in predicates:
        keep = [r for r in keep if predicate(r)]
        stages[name] = len(keep)
    ids = {r["opportunity_id"] for r in keep}
    prediction = [r for r in traffic if r["opportunity_id"] in ids]
    validated = {r["opportunity_id"] for r in prediction if r["validated_reference"]}
    targets = {r["opportunity_id"] for r in prediction if r["signed_proxy_s"] is not None}
    stages.update(validated_reference=len(validated), final_proxy=len(targets))
    groups = group_segments(keep)
    target_groups = group_segments([r for r in keep if r["opportunity_id"] in targets])
    errors = [r["timing"]["timing_error_s"] for r in rows if r["timing"].get("timing_error_s") is not None
              and r["outcome"]["timing_pass"]]
    completed = [r for r in rows if r["outcome"]["completed"]]
    physical_pass = [r for r in completed if r["support"]["support_pass"]]
    close = sum(r["cutoff_group"] == "CLOSE_SUPPORTED" for r in rows)
    frac = lambda a, b: a / b if b else None
    return {
        "funnel": stages, "first_failures": dict(Counter(r["first_failure"] or "PASS" for r in rows)),
        "cutoff_groups": dict(Counter(r["cutoff_group"] for r in rows)),
        "timing_pass_fraction": frac(sum(r["outcome"]["timing_pass"] for r in completed), len(completed)),
        "timing_error_p90_s": float(np.quantile(np.abs(errors), .9)) if errors else None,
        "timing_reconciled_n": len(errors), "physical_pass_fraction": frac(len(physical_pass), len(completed)),
        "median_duration_s": median(r["outcome"]["observed_s"] for r in completed) if completed else None,
        "following_conversion": frac(len(keep), close),
        "clean_controls": sum(r["outcome"]["clean_control"] for r in rows),
        "independent_groups": len(groups), "driver_stints": len({(g["driver_number"], g["stint_number"]) for g in groups}),
        "drivers": len({r["driver_number"] for r in keep}), "races": int(bool(keep)),
        "raw_fragments": len({e for g in groups for e in g["episode_fragments"]}),
        "overlaps": sum(g["overlaps_with_earlier"] for g in groups),
        "eligible_target_groups": len(target_groups),
        "same_lap_vs_lapped": dict(Counter(r["lap_relationship"] or "UNKNOWN" for r in keep)),
        "development_gate": len(target_groups) >= settings["minimum_independent_encounters_per_race"]
        and bool(close and len(keep) / close >= settings["minimum_following_conversion"])
        and bool(errors and np.quantile(np.abs(errors), .9) <= settings["timing_error_p90_max_s"])
        and bool(completed and sum(r["outcome"]["timing_pass"] for r in completed) / len(completed)
                 >= settings["minimum_timing_pass_fraction"]),
    }


def _cached_d8(census, transition, history, key):
    manifest = json.loads((census / "census.json").read_text(encoding="utf8"))
    path = census / f"candidates_{key}.json"
    if _sha(path) != manifest["races"][str(key)]["ledger_sha256"]:
        raise ValueError("Frozen census ledger differs")
    rows = json.loads(path.read_text(encoding="utf8"))
    result = {(r["driver_number"], r["lap_number"]): r for r in rows}
    for run in (history, transition):
        for row in parquet_rows(run / str(key) / "clean_pace_references.parquet"):
            _, driver, lap = row["opportunity_id"].split("/")
            base = result.setdefault((int(driver), int(lap)), {})
            if row["pace_status"] == "SUPPORTED":
                base.update(d8_status=row["pace_status"], expected_clean_s=row["expected_clean_s"],
                            clean_std_s=row["clean_std_s"], prior_clean=row["prior_clean_stint_laps"],
                            compound=row["compound"], tire_age=row["tyre_age"],
                            cutoff_utc=row["as_of_utc"], stint_number=row["stint_number"])
    return result


def run_short_horizon(root, warehouse, stage3, transition, history, census, encounter, output, plan_path):
    inputs = (transition, history, census, encounter)
    if any(p.resolve() in (output.resolve(), *output.resolve().parents) for p in inputs):
        raise ValueError("Short horizon study requires a separate output directory")
    plan = yaml.safe_load(plan_path.read_text(encoding="utf8"))
    protocol, protocol_hash = load_traffic_protocol(root / "config/experiments/stage5_s3_c_protocol.yaml")
    report_root = root / "artifacts/reports"
    files = {
        "transition": (transition / "transition_metrics.json", report_root / "stage5_s3_c_transition_metrics.json"),
        "history": (history / "history_study_metrics.json", report_root / "stage5_s3_c_history_metrics.json"),
        "encounter": (encounter / "metrics.json", report_root / "stage5_s3_c_encounter_metrics.json"),
        "census": (census / "census.json", report_root / "stage5_s3_c_transition_census.json"),
    }
    hashes = {name: _sha(src) for name, (src, _) in files.items()}
    if any(hashes[name] != _sha(frozen) for name, (_, frozen) in files.items()):
        raise ValueError("A frozen study differs from its committed reference")
    if hashes["encounter"] != plan["frozen_encounter_sha256"] or protocol_hash != plan["frozen_protocol_sha256"]:
        raise ValueError("Unexpected frozen experiment")
    bundle = load_pace_bundle(root, stage3)
    if bundle.artifact_sha256 != plan["frozen_d8_sha256"]:
        raise ValueError("Unexpected D8 model artifact")
    old = json.loads((encounter / "metrics.json").read_text(encoding="utf8"))
    warehouse_hash = _sha(warehouse)
    if warehouse_hash != old["inputs"]["warehouse"]:
        raise ValueError("Short-horizon replay needs the frozen warehouse")
    if plan["development_sessions"] != [9955, 9928] or plan["acquisition"] != "CACHE_ONLY":
        raise ValueError("C2 is limited to cached Austria and Hungary")
    output.mkdir(parents=True, exist_ok=True)
    (output / "plots").mkdir(exist_ok=True)
    (output / "diagnostics").mkdir(exist_ok=True)
    (output / "config.yaml").write_bytes(plan_path.read_bytes())
    rows, geometries, sources = [], {}, {}
    for cohort in protocol["cohorts"]:
        key = cohort.get("session_key")
        if key not in plan["development_sessions"]:
            continue
        config = plan | {"entry_upper_m": protocol["exposure"]["entry_upper_m"],
                         "scheduled_laps": cohort["scheduled_laps"],
                         "expected_seconds": {"H1": 30, "H2": 20} | {
                             f"H3_{seconds}": seconds for seconds in plan["sampling"]["diagnostic_seconds"]}}
        local, geometries[key], raw = build_race(root, transition, history, key, config, protocol,
                                                _cached_d8(census, transition, history, key), race_inputs(warehouse, key))
        rows.extend(local)
        sources.update(raw)
    validation, traffic, matches, ref_metrics, heldout = validate_and_match(rows, plan)
    buckets = defaultdict(list)
    for row in rows:
        buckets[row["session_key"], row["horizon_type"]].append(row)
    metrics = {f"{key}/{h}": funnel(local, traffic, plan["feasibility"])
               for (key, h), local in sorted(buckets.items())}
    h0 = json.loads((transition / "transition_metrics.json").read_text(encoding="utf8"))
    for race in h0["races"]:
        key = race["session_key"]
        target = race["targets"]
        recovered = int(key == 9955 and old["recovery_target"]["signed_proxy_s"] is not None)
        cutoffs = parquet_rows(transition / str(key) / "lap_cutoff_cohort.parquet")
        outcomes = {r["opportunity_id"]: r for r in parquet_rows(transition / str(key) / "lap_eligibility_ledger.parquet")
                    if r["screen"] != "LEFT_TRUNCATED_WINDOW"}
        prefix = [r for r in cutoffs if r["cutoff_exposure"] != "UNSUPPORTED"]
        neighbor = [r for r in prefix if r["opponent_number"] is not None]
        complete = [outcomes[r["opportunity_id"]] for r in neighbor if outcomes[r["opportunity_id"]]["full_lap"]]
        timing = [r for r in complete if r["next_line_utc"] is not None]
        events = [r for r in timing if r["physical_coverage"] is not None]
        metrics[f"{key}/H0"] = {
            "funnel": {"prospective": target["cutoff_cohort"], "initial_supported": len(prefix),
                       "neighbor_supported": len(neighbor), "completed": len(complete), "timing": len(timing),
                       "route": None, "no_events": len(events),
                       "physical_support": target["physically_screened"]["unique_laps"],
                       "physical_following": target["following"]["unique_laps"],
                       "validated_reference": target["supported_following"]["unique_laps"], "final_proxy": recovered},
            "independent_groups": race["following_independence"]["independent_encounters"],
            "eligible_target_groups": recovered, "development_gate": False,
            "timing_gate": race["timing_marker_gate"], "frozen_full_laps": target["full_laps"],
            "recovered_proxy_s": old["recovery_target"]["signed_proxy_s"] if recovered else None,
        }
    result = {"version": plan["version"], "frozen_inputs": hashes | {"warehouse": warehouse_hash,
              "D8": bundle.artifact_sha256, "plan": _sha(plan_path), "lap_protocol": protocol_hash},
              "archive_availability": plan["archive_availability"], "horizons": metrics,
              "references": ref_metrics, "traffic_model_status": "RESEARCH_ONLY",
              "operational_baseline": "T1_DETERMINISTIC_NO_PASS", "new_requests": 0,
              "empirical_fit_status": "GATED"}
    fields = ("timing", "support", "outcome", "first_failure")
    _parquet(output / "candidate_opportunities.parquet", [{k: v for k, v in r.items() if k not in fields} for r in rows])
    _parquet(output / "physical_support.parquet", [
        {k: r[k] for k in ("opportunity_id", "session_key", "driver_number", "horizon_type", "as_of_utc", "first_failure")}
        | r["timing"] | r["support"] | r["outcome"] for r in rows])
    _parquet(output / "encounter_groups.parquet", group_segments(rows))
    _parquet(output / "clean_reference_validation.parquet", validation)
    _parquet(output / "control_matching.parquet", matches)
    _parquet(output / "traffic_segments.parquet", traffic)
    for name, value in (
        ("metrics", result), ("target_feasibility", metrics), ("protocol", plan),
        ("geometry_manifest", geometries),
        ("data_manifest", {"warehouse_sha256": warehouse_hash, "cached_raw_payloads_sha256": sources,
                           "new_requests": 0, "availability": plan["archive_availability"]}),
        ("split_manifest", {"cohorts": protocol["cohorts"], "development": [9955, 9928],
                            "held_out_drivers": heldout, "global_models_train_on_held_out_drivers": False,
                            "R2_uses_only_prior_same_stint_observations": True}),
        ("pace_reference_manifest", {"D8": bundle.artifact_sha256,
                                     "trained_through_utc": bundle.trained_through_utc,
                                     "calibrated_through_utc": bundle.calibrated_through_utc,
                                     "models": {"R0": "earlier circuit/checkpoint median",
                                                "R1": "D8 times median earlier clean traversal/full-lap ratio",
                                                "R2": "earlier context-matched same-stint median",
                                                "R3": "context median shrunk toward circuit/checkpoint median"}}),
    ):
        _json(output / f"{name}.json", value)
    _json(output / "diagnostics" / "support_and_validation.json", {
        "timing_checks": {"H1": "mapped marker traversal minus official elapsed",
                          "H2": "paired disjoint-fix repeatability, not independent ground truth",
                          "H3": "observed progress within diagnostic time window"},
        "clean_negative_controls": ref_metrics,
        "retrospective_following_screens": {key: data["first_failures"] for key, data in metrics.items()
                                            if "first_failures" in data},
    })
    from f1ri.validation.traffic_short_horizon_plots import plot_short_horizon

    plot_short_horizon(rows, validation, traffic, result, plan, output / "plots" / "diagnostics.png")
    return result
