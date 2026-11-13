"""Run the C3 sector reference experiment using frozen development evidence."""
from __future__ import annotations

import json
from collections import Counter
from statistics import median
from zipfile import ZipFile

import yaml

from f1ri.features.clean_sector_evidence import cached_h1, evidence_rows
from f1ri.models.latent_sector_pace import stint_key
from f1ri.simulator.pace_adapter import load_pace_bundle
from f1ri.validation.proximity_pilot import _json, _parquet
from f1ri.validation.sector_reference_backtest import replay_sector_references, summarize
from f1ri.validation.traffic_acquisition import race_inputs
from f1ri.validation.traffic_continuation import load_traffic_protocol
from f1ri.validation.traffic_history_study import _sha


def calibration_gate(stats, plan):
    c = plan["calibration"]
    if stats["interval_stints"] < c["minimum_evaluation_stints"]:
        return "INSUFFICIENT_CALIBRATION_STINTS"
    if not (c["acceptable_50_coverage"][0] <= stats["coverage_50"] <= c["acceptable_50_coverage"][1]
            and c["acceptable_90_coverage"][0] <= stats["coverage_90"] <= c["acceptable_90_coverage"][1]
            and stats["median_half_width_90_s"] <= c["maximum_median_90_half_width_s"]):
        return "COVERAGE_OR_WIDTH_FAILED"
    return "PASSED"


def run_sector_study(root, warehouse, stage3, c2, output, plan_path):
    plan = yaml.safe_load(plan_path.read_text(encoding="utf8"))
    if plan["development_sessions"] != [9955, 9928] or plan["acquisition"] != "CACHE_ONLY":
        raise ValueError("C3 is limited to cached Austria and Hungary development evidence")
    if c2.resolve() in (output.resolve(), *output.resolve().parents):
        raise ValueError("C3 requires a separate output directory")
    bundle = load_pace_bundle(root, stage3)
    reports = root / "artifacts/reports"
    committed = reports / "stage5_s3_c2_short_horizon_metrics.json"
    if _sha(c2 / "metrics.json") != _sha(committed) or _sha(committed) != plan["c2_sha256"]:
        raise ValueError("C2 metrics differ from the frozen benchmark")
    frozen = json.loads(committed.read_text(encoding="utf8"))
    if _sha(warehouse) != frozen["frozen_inputs"]["warehouse"] or bundle.artifact_sha256 != plan["d8_sha256"]:
        raise ValueError("Unexpected warehouse or D8 artifact")
    archive = reports / "stage5_s3_c2_short_horizon_ledgers.zip"
    inputs = ("candidate_opportunities.parquet", "physical_support.parquet", "data_manifest.json", "geometry_manifest.json")
    with ZipFile(archive) as z:
        if any((c2 / name).read_bytes() != z.read(name) for name in inputs):
            raise ValueError("C2 ledgers differ from the frozen artifacts")
    raw_manifest = json.loads((c2 / "data_manifest.json").read_text(encoding="utf8"))
    for relative, digest in raw_manifest["cached_raw_payloads_sha256"].items():
        if _sha(root / relative) != digest:
            raise ValueError(f"Frozen C2 telemetry differs: {relative}")
    baseline = yaml.safe_load((root / "config/experiments/stage5_s3_c2_short_horizon.yaml").read_text(encoding="utf8"))
    protocol, _ = load_traffic_protocol(root / "config/experiments/stage5_s3_c_protocol.yaml")
    cohorts = {r["session_key"]: r for r in protocol["cohorts"] if r["split"] == "development"}
    cached = cached_h1(c2)
    output.mkdir(parents=True, exist_ok=True)
    (output / "config.yaml").write_bytes(plan_path.read_bytes())
    evidence, availability, predictions, traces, following, model_artifacts = [], [], [], [], [], {}
    metrics = {}
    for key in plan["development_sessions"]:
        local = list(evidence_rows(warehouse, key, cohorts[key]["scheduled_laps"], race_inputs(warehouse, key), cached,
                                   plan["official_boundary_tolerance_s"]))
        evidence.extend(local)
        frozen_race = frozen["horizons"][f"{key}/H1"]
        if sum(r["following_label"] for r in local) != frozen_race["funnel"]["physical_following"]:
            raise ValueError("C3 does not reproduce the frozen H1 following cohort")
        predicted, prefix, traffic, models, folds, controls, calibration = replay_sector_references(
            local, plan, baseline["reference"])
        predictions.extend(predicted)
        availability.extend(prefix)
        traces.extend(calibration)
        model_artifacts.update(models)
        candidate = plan["confirmatory_candidate"]
        score = summarize(predicted, prefix, plan["feasibility"])
        chosen = score["B"][candidate]
        calibration_status = calibration_gate(chosen, plan)
        validated = chosen["point_gate_pass"] and calibration_status == "PASSED"
        traffic_index = {r["opportunity_id"]: r for r in traffic if r["model"] == candidate}
        groups, proxies = set(), 0
        for row in local:
            if not row["following_label"]:
                continue
            identity = row["opportunity_id"]
            forecast = traffic_index.get(identity)
            matched = controls.get((identity, candidate), [])
            proxy = (forecast["signed_residual_s"] - median(r["signed_residual_s"] for r in matched)
                     if validated and forecast and forecast["half_width_90_s"] is not None
                     and len(matched) >= baseline["reference"]["minimum_matched_controls"] else None)
            if proxy is not None:
                proxies += 1
                groups.add((row["driver_number"], row["physical_leader"], row["archived_stint_number"]))
            following.append({
                "opportunity_id": identity, "session_key": key, "driver_number": row["driver_number"],
                "leader": row["physical_leader"], "stint_number": row["archived_stint_number"],
                "checkpoint": row["checkpoint"], "as_of_utc": row["as_of_utc"], "episode_id": row["episode_id"],
                "frozen_timing_pass": row["physical_timing_pass"], "frozen_physical_support": row["physical_support_fraction"],
                "observed_s": row["sector_elapsed_s"], "r4_supported": forecast is not None,
                "exploratory_r4_s": forecast["predicted_s"] if forecast else None, "validated_reference": validated,
                "half_width_90_s": forecast["half_width_90_s"] if forecast else None,
                "matched_controls": len(matched), "matched_control_ids": [r["opportunity_id"] for r in matched],
                "signed_proxy_s": proxy,
                "status": ("ELIGIBLE" if proxy is not None else "REFERENCE_GATED" if not validated else
                           "REFERENCE_UNSUPPORTED" if forecast is None else
                           "CUTOFF_CALIBRATION_UNAVAILABLE" if forecast["half_width_90_s"] is None else "CONTROL_SHORTFALL"),
            })
        metrics[key] = {
            "evidence": {str(sector + 1): _population([r for r in local if r["checkpoint"] == sector]) for sector in range(3)},
            "totals": _population(local), "folds": folds, "reference": score,
            "r4_b_uncertainty_gate": calibration_status, "reference_validated": validated,
            "frozen_h1_following": frozen_race["funnel"]["physical_following"],
            "frozen_h1_groups": frozen_race["independent_groups"], "exploratory_following_predictions": len(traffic_index),
            "following_with_three_controls": sum(len(v) >= baseline["reference"]["minimum_matched_controls"]
                                                 for (_, m), v in controls.items() if m == candidate),
            "eligible_h1_proxies": proxies, "eligible_h1_groups": len(groups),
            "timing_p90_s": frozen_race["timing_error_p90_s"],
            "reference_and_timing_scale": {
                "reference_p90_s": chosen["absolute_error_p90_s"],
                "timing_p90_s": frozen_race["timing_error_p90_s"],
                "sum_of_component_p90_scales_s": chosen["absolute_error_p90_s"] + frozen_race["timing_error_p90_s"]
                if chosen["absolute_error_p90_s"] is not None else None,
                "combined_quantile": None,
                "reason": "DEPENDENCE_UNKNOWN_AND_OFFICIAL_TIMING_ALREADY_IN_RESIDUALS",
            },
            "development_gate": validated and len(groups) >= plan["feasibility"]["minimum_h1_groups"]
            and proxies / frozen_race["funnel"]["physical_following"] >= plan["feasibility"]["minimum_h1_conversion"],
        }
    model_file = output / "r4_model_snapshots.json"
    _json(model_file, {"version": plan["version"], "family": "R4_LATENT_SECTOR_PACE", "snapshots": model_artifacts})
    for name, data in (("clean_sector_evidence", evidence), ("historical_availability", availability),
                       ("held_out_predictions", predictions), ("calibration_residuals", traces), ("h1_readiness", following),
                       ("online_latent_state", [r for r in predictions + traces if r.get("adaptation_ids") is not None])):
        _parquet(output / f"{name}.parquet", data)
    manifest = {"c2": _sha(committed), "c2_ledgers": _sha(archive), "c2_sources": {n: _sha(c2 / n) for n in inputs},
                "verified_raw_payloads": len(raw_manifest["cached_raw_payloads_sha256"]),
                "d8": bundle.artifact_sha256, "d8_trained_through_utc": bundle.trained_through_utc,
                "d8_calibrated_through_utc": bundle.calibrated_through_utc, "warehouse": _sha(warehouse),
                "config": _sha(plan_path), "r4_snapshot_artifact": _sha(model_file)}
    result = {"version": plan["version"], "inputs": manifest, "races": metrics,
              "h0_austria_proxy_s": frozen["horizons"]["9955/H0"]["recovered_proxy_s"],
              "cross_race_evaluation": "UNAVAILABLE_DISTINCT_LAYOUTS_AND_NO_APPROVED_TRANSFER_COHORT",
              "r4_c_status": "DEFERRED_INSUFFICIENT_VALIDATION_SUPPORT",
              "r4_d_status": "CHRONOLOGICAL_CALIBRATION_UNSUPPORTED",
              "crps_status": "UNAVAILABLE_WITHOUT_CALIBRATED_PREDICTIVE_DISTRIBUTION",
              "traffic_model_status": "RESEARCH_ONLY", "empirical_fit_status": "GATED",
              "operational_baseline": "T1_DETERMINISTIC_NO_PASS", "new_requests": 0}
    for name, value in (("metrics", result), ("protocol", plan), ("source_and_model_manifest", manifest),
                        ("split_manifest", {"race_roles": protocol["cohorts"],
                                            "evaluation_A": "chronological within race, focal driver excluded",
                                            "evaluation_B": "rotating driver folds with separate calibration fold",
                                            "evaluation_C": result["cross_race_evaluation"],
                                            "driver_folds": {key: data["folds"] for key, data in metrics.items()}})):
        _json(output / f"{name}.json", value)
    from f1ri.validation.sector_reference_plots import plot_sector_reference

    plot_sector_reference(predictions, result, output / "diagnostics.png")
    return result


def _population(rows):
    return {"candidates": len(rows), "tiers": dict(Counter(r["tier"] for r in rows)),
            "reasons": dict(Counter(r["label_reason"] for r in rows)),
            "clean_by_tier": {tier: {"drivers": len({r["driver_number"] for r in rows if r["tier"] == tier}),
                                     "stints": len({stint_key(r) for r in rows if r["tier"] == tier
                                                    and r["stint_number"] is not None}),
                                     "race_weekends": int(any(r["tier"] == tier for r in rows))}
                              for tier in ("A", "B")}}
