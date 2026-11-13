"""C4 cached clean-reference expansion, calibration and readiness."""
import json
from collections import Counter
from datetime import timedelta
from statistics import median
from zipfile import ZipFile

import duckdb
import pandas as pd
import yaml

from f1ri.features.clean_sector_evidence import cached_h1, evidence_rows
from f1ri.models.latent_sector_pace import stint_key
from f1ri.simulator.pace_adapter import load_pace_bundle
from f1ri.validation.proximity_pilot import _json, _parquet
from f1ri.validation.sector_reference_backtest import replay_sector_references, summarize
from f1ri.validation.sector_reference_study import _population, calibration_gate
from f1ri.validation.sector_validation_audit import (
    calibration_census,
    control_census,
    interval_metrics,
)
from f1ri.validation.traffic_acquisition import ReplaySpan, race_inputs
from f1ri.validation.traffic_continuation import load_traffic_protocol
from f1ri.validation.traffic_coverage_pilot import uncovered_requests
from f1ri.validation.traffic_history_plan import _load_cached_slices
from f1ri.validation.traffic_history_study import _sha
from f1ri.validation.traffic_segment_reference import diagnostics
from f1ri.validation.traffic_short_horizon import build_race
from f1ri.validation.traffic_short_horizon_study import _cached_d8


def repeat_layout_inventory(warehouse, bundle, root):
    with duckdb.connect(str(warehouse), read_only=True) as con:
        con.execute("SET TimeZone = 'UTC'")
        rows = con.execute("""
            SELECT s.country_name, s.session_key, s.circuit_key, s.date_start, s.date_end,
                   count(l.lap_number) AS lap_rows, count(*) FILTER (
                       WHERE l.duration_sector_1 > 0 AND l.duration_sector_2 > 0 AND l.duration_sector_3 > 0
                   ) AS complete_sector_rows,
                   (SELECT count(*) FROM silver.stints t WHERE t.session_key = s.session_key
                    AND t.compound IN ('SOFT', 'MEDIUM', 'HARD') AND t.tyre_age_at_start IS NOT NULL) AS dry_stint_rows
            FROM silver.sessions s LEFT JOIN silver.laps l USING (session_key)
            WHERE s.year = 2024 AND s.session_name = 'Race' AND s.country_name IN ('Austria', 'Hungary')
            GROUP BY ALL ORDER BY s.date_start
        """).fetchall()
    found = {r[0]: r[1:] for r in rows}
    result = {}
    for name in ("Austria", "Hungary"):
        if name not in found:
            result[name] = {"status": "SESSION_ABSENT_FROM_FROZEN_WAREHOUSE"}
            continue
        key, circuit, start, end, laps, complete, stints = found[name]
        result[name] = {
            "status": "CROSS_RACE_PROVENANCE_UNRESOLVED", "role": "UNAPPROVED_METADATA_ONLY",
            "session_key": key, "circuit_key": circuit, "start_utc": start, "end_utc": end, "lap_rows": laps,
            "complete_timing_lap_rows": complete, "dry_stint_rows": stints,
            "cached_physical": (root / "data/telemetry/proximity" / str(key)).is_dir(),
            "d8_training_member": key in bundle.training_sessions, "d8_calibration_member": key in bundle.calibration_sessions,
            "layout_and_marker_equivalence": "UNVERIFIED", "regulation_era": "2023_2025",
            "sector_outcomes_used": 0,
        }
    return result


def _new_clean_sources(root, paths, transition, history, census, key, c2, protocol, cohort, inputs, frozen_cache):
    if len(paths) != 2:
        raise ValueError("The locked expansion requires the original and extension caches")
    config = c2 | {"entry_upper_m": protocol["exposure"]["entry_upper_m"], "scheduled_laps": cohort["scheduled_laps"],
                   "expected_seconds": {"H1": 30}}
    d8 = _cached_d8(census, transition, history, key)
    additional, geometry, hashes = build_race(root, *paths, key, config, protocol, d8, inputs, sectors_only=True)
    cache, sources = dict(frozen_cache), []
    for row in additional:
        identity = row["opportunity_id"]
        observed = row | row["outcome"] | row["support"] | row["timing"]
        previous = cache.get(identity)
        if previous is not None and (previous["following_label"] or previous["clean_control"]):
            continue
        if previous is None or (
            previous["cutoff_group"] == "PHYSICAL_STATE_UNRESOLVED" and observed["cutoff_group"] != previous["cutoff_group"]
        ) or (
            not previous["completed"] and observed["completed"]
            and previous["cutoff_group"] not in ("CLOSE_SUPPORTED", "CLOSE_UNCERTAIN")
        ):
            observed["following_label"] = False
            cache[identity] = observed
            sources.append({"opportunity_id": identity, "window": row["window"],
                            "prior_c2_present": previous is not None, "clean_control": observed["clean_control"]})
    return cache, sources, geometry, hashes


def _dimensions(rows, ids):
    local = [r for r in rows if r["opportunity_id"] in ids]
    groups = {(r["session_key"], r["driver_number"], r["physical_leader"], r["archived_stint_number"])
              for r in local if r["physical_leader"] is not None and r["archived_stint_number"] is not None}
    return {"sectors": len(local), "groups": len(groups), "drivers": len({r["driver_number"] for r in local}),
            "stints": len({stint_key(r) for r in local if r["stint_number"] is not None}),
            "races": len({r["session_key"] for r in local})}


def run_c4(root, warehouse, stage3, c3, c2, transition, history, census, baseline, output, plan_path):
    plan = yaml.safe_load(plan_path.read_text(encoding="utf8"))
    if (plan["development_sessions"], plan["approved_transfer_sessions"], plan["acquisition"],
        plan["maximum_new_requests"], plan["expansion_sources"]) != ([9955, 9928], [], "CACHE_ONLY", 0, [
            {"session_key": 9928, "run": "baseline", "role": "development_clean_reconstruction"},
            {"session_key": 9928, "run": "coverage", "role": "development_clean_reconstruction"},
        ]):
        raise ValueError("Unexpected C4 race roles or cache-only scope")
    if any(p.resolve() in (output.resolve(), *output.resolve().parents)
           for p in (c3, c2, transition, history, census, baseline)):
        raise ValueError("C4 requires a separate output directory")
    original = json.loads((c3 / "metrics.json").read_text(encoding="utf8"))
    if _sha(c3 / "metrics.json") != plan["c3_sha256"] or _sha(c3 / "r4_model_snapshots.json") != plan["r4_sha256"]:
        raise ValueError("Frozen C3 replay differs")
    with ZipFile(root / "artifacts/reports/stage5_s3_c3_latent_sector_pace_ledgers.zip") as z:
        checked = ("held_out_predictions.parquet", "clean_sector_evidence.parquet", "calibration_residuals.parquet")
        if any(z.read(n) != (c3 / n).read_bytes() for n in checked):
            raise ValueError("Frozen C3 evidence differs")
    bundle = load_pace_bundle(root, stage3)
    if (bundle.artifact_sha256 != original["inputs"]["d8"] or _sha(c2 / "metrics.json") != original["inputs"]["c2"]
            or _sha(warehouse) != original["inputs"]["warehouse"]):
        raise ValueError("Unexpected frozen inputs")
    if any(_sha(c2 / n) != h for n, h in original["inputs"]["c2_sources"].items()):
        raise ValueError("C2 evidence differs")
    c3_path = root / "config/experiments/stage5_s3_c3_latent_sector_pace.yaml"
    if _sha(c3_path) != original["inputs"]["config"]:
        raise ValueError("C4 requires the frozen R4-B specification")
    c3_config = yaml.safe_load(c3_path.read_text(encoding="utf8"))
    c2_config = yaml.safe_load((root / "config/experiments/stage5_s3_c2_short_horizon.yaml").read_text(encoding="utf8"))
    c2_metrics = json.loads((c2 / "metrics.json").read_text(encoding="utf8"))
    protocol, protocol_hash = load_traffic_protocol(root / "config/experiments/stage5_s3_c_protocol.yaml")
    if protocol_hash != c2_metrics["frozen_inputs"]["lap_protocol"]:
        raise ValueError("Frozen lap protocol differs")
    for path, name in ((baseline / "metrics.json", "stage5_s3_c_metrics.json"),
                       (baseline / "coverage_diagnostics.json", "stage5_s3_c_coverage_diagnostics.json")):
        if _sha(path) != _sha(root / "artifacts/reports" / name):
            raise ValueError(f"Frozen clean-expansion input differs: {name}")
    cohorts = {r["session_key"]: r for r in protocol["cohorts"] if r["split"] == "development"}
    original_cache = cached_h1(c2)
    for relative, digest in json.loads((c2 / "data_manifest.json").read_text(encoding="utf8"))[
        "cached_raw_payloads_sha256"
    ].items():
        if _sha(root / relative) != digest:
            raise ValueError(f"Frozen C2 telemetry differs: {relative}")
    output.mkdir(parents=True, exist_ok=True)
    (output / "config.yaml").write_bytes(plan_path.read_bytes())
    calibration_rows, support, predictions, residuals, control_rows, control_summary = [], [], [], [], [], []
    clean, expansion, proposed = [], [], []
    races, provenance, snapshots, interval_predictions = {}, {}, {}, []
    for key in plan["development_sessions"]:
        inputs = race_inputs(warehouse, key)
        cohort = cohorts[key]
        cache = original_cache
        if key == 9928:
            cache, sources, geometry, hashes = _new_clean_sources(
                root, (baseline, baseline / "coverage_pilot"), transition, history, census,
                key, c2_config, protocol, cohort, inputs, original_cache)
            expansion.extend(sources)
            provenance[str(key)] = {"geometry": geometry, "cached_payloads_sha256": hashes}
            c2_geometry = json.loads((c2 / "geometry_manifest.json").read_text(encoding="utf8"))
            if geometry["sha256"] != c2_geometry[str(key)]["sha256"]:
                raise ValueError("The expansion changed frozen geometry")
        evidence = list(evidence_rows(warehouse, key, cohort["scheduled_laps"], inputs, cache,
                                      c3_config["official_boundary_tolerance_s"]))
        clean.extend(evidence)
        index = {r["opportunity_id"]: r for r in evidence}
        pred, prefix, traffic, models, folds, _, cal = replay_sector_references(
            evidence, c3_config, c2_config["reference"], sensitivity_weights=plan["tier_b_sensitivity_weights"])
        snapshots.update(models)
        scored = summarize(pred, prefix, c3_config["feasibility"])
        context_slices = {}
        for model in scored["B"]:
            local = [r for r in pred if r["evaluation"] == "B" and r["model"] == model]
            context_slices[model] = {
                "sector": {str(i + 1): diagnostics([r for r in local if r["checkpoint"] == i]) for i in range(3)},
                "race_progress_quarter": {str(i + 1): diagnostics(
                    [r for r in local if min(3, int(r["race_progress"] * 4)) == i]) for i in range(4)},
            }
        reference = scored["B"]["R4-B"]
        following = [r for r in evidence if r["following_label"]]
        original_race = original["races"][str(key)]
        if len(following) != original_race["frozen_h1_following"]:
            raise ValueError("The expansion changed frozen H1 following labels")
        prior = [r for r in cal if r["model"] == "R4-B"]
        residuals.extend(cal)
        prefix_index = {r["opportunity_id"]: r for r in prefix if r["evaluation"] == "B"}
        test = {r["opportunity_id"]: r for r in pred + traffic if r["evaluation"] == "B" and r["model"] == "R4-B"}
        for row in prior:
            source = index[row["opportunity_id"]]
            fitted = models[row["model_id"]]["training_ids"]
            if (source["tier"] != "A" or row["opportunity_id"] in fitted
                    or row["latest_training_availability_utc"] >= row["as_of_utc"]):
                raise ValueError("Calibration residual lacks independent chronological provenance")
        calibrated = {m: [] for m in plan["calibration_methods"]}
        support_by_id = {}
        for target in evidence:
            if target["tier"] != "A" and not target["following_label"]:
                continue
            identity = target["opportunity_id"]
            forecast = test.get(identity)
            rows, summary = calibration_census(target, forecast, prior, models, index, folds, c3_config["calibration"])
            prefix_row = prefix_index[identity]
            summary.update({name: prefix_row[name] for name in (
                "available_tier_a", "available_tier_b", "available_training_stints", "prior_clean_sectors", "test_fold",
            )})
            summary["point_support_status"] = prefix_row["model_status"]["R4-B"]
            summary["available_training_races"] = int(bool(prefix_row["available_tier_a"]))
            calibration_rows.extend(rows)
            support.append(summary)
            support_by_id[identity] = summary
            if forecast and target["tier"] == "A":
                for method, (w50, w90) in summary["intervals"].items():
                    calibrated[method].append(forecast | {"half_width_50_s": w50, "half_width_90_s": w90,
                                                          "calibration_method": method})
        intervals = {}
        for method, rows in calibrated.items():
            interval_predictions.extend(rows)
            stats = interval_metrics(rows, c3_config["feasibility"]["planning_effect_s"]) | {
                "interval_stints": len({stint_key(r) for r in rows})}
            intervals[method] = stats | {"gate": calibration_gate(stats | {"median_half_width_90_s":
                                                                            stats["median_90_half_width_s"]}, c3_config)}
        validated = reference["point_gate_pass"] and intervals["U1"]["gate"] == "PASSED"
        minimum = c2_config["reference"]["minimum_matched_controls"]
        scenario = {label: set() for label in ("A", "B", "C")}
        bounds_rows = []
        control_counts = Counter()
        for target in following:
            identity = target["opportunity_id"]
            config = c2_config["reference"] | {"folds": c3_config["folds"]}
            summary, audit = control_census(target, evidence, prior, config, folds[target["driver_number"]], folds)
            control_rows.extend(audit)
            for stage, n in summary["counts"].items():
                control_counts[stage] += n
            observed = len(summary["matched_control_ids"]) >= minimum
            optimistic = summary["recoverable_stint_upper_bound"] >= minimum
            forecast = test.get(identity)
            calibrated_target = bool(support_by_id[identity]["intervals"])
            proxy = None
            if forecast and observed:
                scenario["B"].add(identity)
                if calibrated_target and validated:
                    scenario["A"].add(identity)
                    matched_ids = set(summary["matched_control_ids"])
                    same_fold = [r["signed_residual_s"] for r in prior if r["test_fold"] == folds[target["driver_number"]]
                                 and r["opportunity_id"] in matched_ids]
                    proxy = forecast["signed_residual_s"] - median(same_fold)
            if forecast and (optimistic or observed):
                scenario["C"].add(identity)
            summary.update(r4_supported=forecast is not None, calibrated=calibrated_target, signed_proxy_s=proxy,
                           as_of_utc=target["as_of_utc"], checkpoint=target["checkpoint"], driver_number=target["driver_number"],
                           physical_leader=target["physical_leader"], episode_id=target["episode_id"],
                           physical_support_fraction=target["physical_support_fraction"], timing_pass=target["physical_timing_pass"],
                           archived_stint_number=target["archived_stint_number"], observed_s=target["sector_elapsed_s"],
                           model_id=forecast["model_id"] if forecast else None,
                           signed_residual_s=forecast["signed_residual_s"] if forecast else None)
            control_summary.append(summary)
            if forecast and (optimistic or observed):
                bounds_rows.extend(summary["recoverable_control_seed_ids"])
        predictions.extend(pred)
        follow_ids = {r["opportunity_id"] for r in following}
        original_a = original_race["totals"]["tiers"]["A"]
        additions = _population(evidence)
        races[str(key)] = {
            "population": additions, "original_tier_a": original_a,
            "added_tier_a_net": additions["tiers"]["A"] - original_a,
            "added_tier_a_stints_net": additions["clean_by_tier"]["A"]["stints"] - original_race["totals"]["clean_by_tier"]["A"]["stints"],
            "by_sector": {str(i): _population([r for r in evidence if r["checkpoint"] == i]) for i in range(3)},
            "c3_r4_b": original_race["reference"]["B"]["R4-B"], "references": scored, "intervals": intervals,
            "context_slices": context_slices,
            "max_prior_calibration_stints": max(r["independent_prior_stints"] for r in support_by_id.values()),
            "calibration_failure_totals": dict(sum((Counter(r["reasons"]) for r in support_by_id.values()), Counter())),
            "timing_disagreement_p90_s": original_race["timing_p90_s"], "joint_uncertainty_quantile_s": None,
            "reference_validated": validated, "control_attrition": dict(control_counts),
            "frozen_following": _dimensions(following, follow_ids),
            "following_with_r4": _dimensions(following, follow_ids & test.keys()),
            "following_with_calibration": _dimensions(following, {i for i in follow_ids if support_by_id[i]["intervals"]}),
            "bounds": {k: _dimensions(following, ids) for k, ids in scenario.items()},
            "development_gate": validated and len(scenario["A"]) / len(following) >= c3_config["feasibility"]["minimum_h1_conversion"]
            and _dimensions(following, scenario["A"])["groups"] >= c3_config["feasibility"]["minimum_h1_groups"],
        }
        if bounds_rows:
            layout = json.loads((c2 / "geometry_manifest.json").read_text(encoding="utf8"))[str(key)]["version"]
            proposed.extend(_proposal(root, key, index, bounds_rows, (transition, history, baseline, baseline / "coverage_pilot"),
                                      layout))
    transfer = repeat_layout_inventory(warehouse, bundle, root)
    result = {"version": plan["version"], "races": races, "transfer": transfer, "new_requests": 0,
              "decision": ("A" if all(r["development_gate"] for r in races.values()) else
                           "B" if all(r["reference_validated"] for r in races.values()) else "C"),
              "c3_sha256": plan["c3_sha256"], "frozen_r4_sha256": plan["r4_sha256"], "d8_sha256": bundle.artifact_sha256,
              "c2_sha256": original["inputs"]["c2"], "h0_austria_proxy_s": original["h0_austria_proxy_s"],
              "traffic_v1": "RESEARCH_ONLY", "operational_baseline": "T1_DETERMINISTIC_NO_PASS"}
    for name, rows in (("clean_sector_inventory", clean), ("calibration_eligibility", calibration_rows),
                       ("calibration_residuals", residuals), ("calibration_support", support),
                       ("held_out_predictions", predictions), ("h1_control_eligibility", control_rows),
                       ("h1_control_attrition", control_summary), ("cached_expansion", expansion)):
        _parquet(output / f"{name}.parquet", rows)
    _json(output / "r4_expanded_prefixes.json", snapshots)
    _json(output / "calibrated_interval_predictions.json", interval_predictions)
    for name, data in (
        ("metrics", result), ("protocol", plan), ("race_roles", {"frozen": protocol["cohorts"], "metadata_only": transfer}),
        ("transfer_report", transfer), ("h1_bounds", {k: v["bounds"] for k, v in races.items()}),
        ("acquisition_manifest", {"new_requests": 0, "maximum_new_requests": plan["maximum_new_requests"],
                                  "stop": "REPLAY_DECLARED_CACHE_AND_REPORT_CONTROL_BOUND",
                                  "cached_expansion": provenance, "unapproved_candidate_windows": proposed}),
        ("provenance", {"warehouse": _sha(warehouse), "plan": _sha(plan_path), "c3_model_spec": _sha(c3_path),
                        "expanded_prefixes": _sha(output / "r4_expanded_prefixes.json"), "frozen": original["inputs"]}),
    ):
        _json(output / f"{name}.json", data)
    from f1ri.validation.sector_validation_plots import plot_c4

    plot_c4(result, support, predictions, output / "diagnostics.png")
    return result


def _proposal(root, key, index, seeds, paths, layout):
    archives = []
    for run in paths:
        folder = run / str(key)
        if (folder / "acquisition.json").is_file():
            archives.append(json.loads((folder / "acquisition.json").read_text(encoding="utf8")) | {"session_key": key})
    cached = _load_cached_slices(root, archives, key)
    rows = []
    for identity in sorted(set(seeds)):
        row = index[identity]
        if row["tier"] != "B":
            continue
        start = pd.Timestamp(row["as_of_utc"]).floor("s") - timedelta(seconds=3)
        end = pd.Timestamp(row["target_end_utc"]).ceil("s") + timedelta(seconds=3)
        gaps = uncovered_requests(ReplaySpan(key, start, end), cached, timedelta(seconds=178), timedelta(seconds=1))
        rows.append({"opportunity_id": identity, "session_key": key, "role": "CANDIDATE_CLEAN_CONTROL_ONLY",
                     "layout_version": layout,
                     "historical_available_by_utc": row["available_by_utc"], "projected_stints": 1,
                     "cache_full_window": not gaps, "window_start_utc": start, "window_end_utc": end,
                     "missing_query_spans": [w.bounds() for w in gaps], "endpoints": ["location", "car_data"],
                     "request_cost": 2 * len(gaps), "reason": "CONTEXT_MATCHED_STAGE1_CLEAN_REQUIRES_PHYSICAL_PROOF"})
    return rows
