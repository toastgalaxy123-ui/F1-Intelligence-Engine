"""Run the preregistered Stage 5.3 continuation on its initial race cohort."""
from __future__ import annotations

import json
from collections import Counter
from contextlib import nullcontext
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

import duckdb
import pandas as pd
import yaml

from f1ri.config import load_config
from f1ri.ingestion.openf1 import OpenF1Client
from f1ri.simulator.pace_adapter import load_pace_bundle
from f1ri.validation.proximity_pilot import _geometry, _json, _parquet
from f1ri.validation.traffic_acquisition import (
    acquire_window,
    geometry_window,
    race_inputs,
    select_traffic_windows,
)
from f1ri.validation.traffic_coverage import CURRENT, CoverageReplay
from f1ri.validation.traffic_coverage_pilot import acquire_extension, plan_extensions
from f1ri.validation.traffic_exposures import (
    LapEligibilityAudit,
    build_traffic_exposures,
    lap_opportunities,
)
from f1ri.validation.traffic_localization import localization_summary, localize_window
from f1ri.validation.traffic_reference import (
    clean_pace_references,
    residual_research,
    residual_summary,
    traffic_readiness,
)
from f1ri.validation.traffic_target_audit import audited_outcomes, target_summary


def load_traffic_protocol(path: Path) -> tuple[dict, str]:
    content = path.read_text(encoding="utf-8")
    config = yaml.safe_load(content)
    cohorts = config["cohorts"]
    dates = [pd.Timestamp(row["date"]) for row in cohorts]
    if dates != sorted(set(dates)) or len({row["name"] for row in cohorts}) != len(cohorts):
        raise ValueError("Traffic protocol requires uniquely dated, chronological race cohorts")
    keys = [row["session_key"] for row in cohorts if "session_key" in row]
    if len(keys) != len(set(keys)):
        raise ValueError("A race can only belong to one traffic cohort")
    phases = {"development": 0, "validation": 1, "reserved": 2}
    order = [phases[row["split"]] for row in cohorts if row["split"] in phases]
    if order != sorted(order):
        raise ValueError("Traffic development, validation and reserved races must be chronological")
    allowed = {row.get("session_key") for row in cohorts
               if row["split"] in {"reconstruction_only", "development", "validation"}}
    selected = [row["session_key"] for row in config["initial_acquisition"]]
    if not set(selected) <= allowed or len(set(selected)) != len(selected):
        raise ValueError("Initial acquisition must exclude audit, pilot and reserved races")
    sampling = config["sampling"]
    if not 0 < sampling["sample_period_seconds"] < 1:
        raise ValueError("Invalid traffic sampling period")
    if set(sampling["endpoints"]) != {"location", "car_data"}:
        raise ValueError("Traffic acquisition requires both location and speed")
    if any(value <= 0 for value in sampling["windows_per_race"].values()):
        raise ValueError("Every sampled traffic stratum requires a positive window count")
    return config, sha256(content.encode()).hexdigest()


def _collect_race(root, warehouse, race, cohort, config, output, client, bundle,
                  *, coverage_diagnostics=False, pilot_path=None, protocol_hash=None,
                  development_planner=None):
    key = race["session_key"]
    inputs = race_inputs(warehouse, key)
    drivers = tuple(int(driver) for driver in inputs["drivers"].driver_number)
    if not drivers:
        raise ValueError(f"Missing race inputs for session {key}")
    scheduled_reference = geometry_window(race, inputs["laps"])
    provenance, rejected_references = [], []
    for number in (race["geometry_lap"], *race.get("fallback_geometry_laps", ())):
        reference = geometry_window(race | {"geometry_lap": number}, inputs["laps"])
        try:
            geometry, markers = _geometry(root, race | {"geometry": reference}, inputs, client, provenance)
        except ValueError as exc:
            rejected_references.append({"lap": number, "reason": str(exc)})
        else:
            break
    else:
        raise ValueError(f"No declared reference geometry passed for session {key}: {rejected_references}")
    sampling = config["sampling"]
    if development_planner is None:
        planned = select_traffic_windows(key, inputs["laps"], inputs["track"],
                                         pd.Timestamp(scheduled_reference["end"]), sampling, config["seed"])
        windows = [window for window in planned if window.telemetry.start_utc >= geometry.available_at_utc]
        discarded = len(planned) - len(windows)
    else:
        windows, discarded = [], 0
    extensions = {}
    pilot_plan = None
    if development_planner is not None:
        extended, pilot_plan = development_planner(inputs, geometry.available_at_utc)
        extensions = {item.replay.name: item for item in extended}
        windows = [item.replay for item in extended]
    if pilot_path is not None:
        extended, pilot_plan = plan_extensions(
            pilot_path, protocol_hash, cohort, windows, inputs["track"], geometry.available_at_utc,
        )
        extensions = {item.replay.name: item for item in extended}
        windows = [item.replay for item in extended]
    if not windows:
        raise ValueError(f"No supported green windows were found for session {key}")
    positions, neighbors, quality = [], [], []
    checks = {"control_passages": [], "dropped_fixes": [], "markers": []}
    used_drivers = set()
    window_manifest = []
    off_track = {row["driver_number"]: row for row in race.get("known_off_track", [])}
    coverage = CoverageReplay() if coverage_diagnostics else None
    audit = LapEligibilityAudit() if coverage_diagnostics else None
    for index, window in enumerate(windows):
        if extension := extensions.get(window.name):
            frames, sources = acquire_extension(root, extension, sampling["endpoints"], drivers, client)
        else:
            frames, provenance_rows = acquire_window(root, window.telemetry, sampling["endpoints"], drivers, client)
            sources = {"sources": provenance_rows}
        pos, nbr, details, diagnostics = localize_window(
            geometry, window, frames["location"], frames["car_data"], inputs, markers, config["reconstruction"],
            sampling["sample_period_seconds"], reference["driver"], used_drivers, off_track,
            coverage_replay=coverage,
            marker_laps_per_window=(config["reconstruction"]["independent_laps"]
                                    if pilot_plan is not None and index == len(windows) - 1 else 1),
        )
        positions.extend(pos)
        neighbors.extend(nbr)
        quality.append(details)
        for name, rows in diagnostics.items():
            checks[name].extend(rows)
        window_manifest.append(asdict(window) | {"name": window.name} | sources)
    summary = localization_summary(quality, checks, len(drivers))
    period = sampling["sample_period_seconds"]
    episodes, segments = build_traffic_exposures(neighbors, config["exposure"], period)
    features, outcomes, opportunity_counts = lap_opportunities(
        neighbors, segments, inputs["laps"], config, cohort["split"], audit=audit,
    )
    references = clean_pace_references(warehouse, cohort["scheduled_laps"], features, bundle,
                                       config["reference"], audit_history=coverage_diagnostics) if bundle is not None else []
    expected = sampling["windows_per_race"]
    if pilot_path is not None:
        expected = dict.fromkeys(pilot_plan["strata_priority"], 1)
    if development_planner is not None:
        expected = pilot_plan["planned_arm_counts"]
    actual = Counter(window.kind for window in windows)
    timing = summary["timing_markers"]
    threshold = config["reconstruction"]["maximum_timing_marker_mae_s"]
    summary |= {
        "name": cohort["name"], "session_key": key, "split": cohort["split"],
        "geometry_version": geometry.version, "reference_end_gap_m": geometry.reference_lap_end_gap_m,
        "geometry_available_utc": geometry.available_at_utc.isoformat(),
        "reference_lap": reference["lap"], "rejected_reference_laps": rejected_references,
        "windows_before_geometry_available": discarded,
        "field_window_seconds": sum((w.telemetry.end_utc - w.telemetry.start_utc).total_seconds() for w in windows),
        "window_counts": dict(actual), "all_window_strata_acquired": actual == expected,
        "speed_samples": (
            sum(w["merge"]["car_data"]["usable_rows"] for w in window_manifest) if development_planner is not None
            else sum(source["usable_rows"] for w in window_manifest for source in w["sources"]
                     if source["endpoint"] == "car_data")
        ),
        "timing_marker_gate": (
            timing["n"] == config["reconstruction"]["independent_laps"] * 4 and timing["mae"] <= threshold
        ),
        "archive_availability": "OBSERVATION_TIMESTAMP_PROXY",
        "off_track_evidence": list(off_track.values()),
        "episodes": len(episodes), "episode_segments": len(segments),
        "episode_encounters": dict(Counter(row["encounter"] for row in episodes)),
        "episode_terminations": dict(Counter(row["termination"] for row in episodes)),
        "lap_opportunities": opportunity_counts,
        "pace_reference_status": dict(Counter(row["pace_status"] for row in references)),
    }
    output.mkdir(parents=True, exist_ok=True)
    _json(output / "geometry.json", {
        "version": geometry.version, "length_m": geometry.length_m,
        "available_at_utc": geometry.available_at_utc, "reference": reference,
        "reference_end_gap_m": geometry.reference_lap_end_gap_m, "points_xy": geometry.points,
        "sector_markers_m": markers, "length_source": race["length_source"],
    })
    _json(output / "acquisition.json", {"geometry_sources": provenance, "windows": window_manifest})
    for name, rows in checks.items():
        _parquet(output / f"{name}.parquet", rows)
    _parquet(output / "physical_positions.parquet", positions)
    _parquet(output / "physical_neighbors.parquet", neighbors)
    _parquet(output / "traffic_episodes.parquet", episodes)
    _parquet(output / "traffic_segments.parquet", segments)
    _parquet(output / "lap_cutoff_features.parquet", features)
    _parquet(output / "lap_outcomes.parquet", outcomes)
    _parquet(output / "clean_pace_references.parquet", references)
    by_id = {row["opportunity_id"]: row["outcome_group"] for row in outcomes}
    summary["pace_reference_by_outcome"] = {
        group: dict(Counter(row["pace_status"] for row in references if by_id[row["opportunity_id"]] == group))
        for group in ("FOLLOWING", "CLEAN_CONTROL", "INTERMEDIATE")
    }
    archive = {
        "session_key": key,
        "geometry_sha256": sha256((output / "geometry.json").read_bytes()).hexdigest(),
        "geometry_sources": provenance,
        "windows": [
            row | {"sources": [{k: value for k, value in source.items() if k != "drivers"}
                               for source in row["sources"]]} for row in window_manifest
        ],
    }
    diagnostics = None
    if coverage is not None:
        diagnostics = {
            "coverage": coverage.finish(output, config["exposure"]),
            "snapshots": [row for row in coverage.snapshots if row["mode"] == CURRENT],
            "audit": audit, "episodes": episodes, "segments": segments,
            "audited_outcomes": audited_outcomes(outcomes, audit, inputs),
            "plan": pilot_plan,
        }
    return summary, quality, references, outcomes, archive, diagnostics


def run_traffic_continuation(
    root: Path, warehouse: Path, config_path: Path, output: Path, *, ingest: bool = False,
    stage3_root: Path | None = None,
    coverage_diagnostics: bool = False, coverage_pilot: Path | None = None,
) -> dict:
    config, protocol_hash = load_traffic_protocol(config_path)
    cohorts = {row.get("session_key"): row for row in config["cohorts"]}
    source = load_config(root)["data"]
    manager = (OpenF1Client(source["openf1_base_url"], source["min_seconds_between_requests"])
               if ingest else nullcontext())
    bundle = load_pace_bundle(root, stage3_root) if stage3_root is not None else None
    coverage_diagnostics |= coverage_pilot is not None
    pilot_key = (yaml.safe_load(coverage_pilot.read_text(encoding="utf-8"))["session_key"]
                 if coverage_pilot is not None else None)
    if pilot_key is not None and pilot_key not in {item["session_key"] for item in config["initial_acquisition"]}:
        raise ValueError("Coverage pilot requires an original race cohort")
    races, quality, references, outcomes, archive = [], {}, [], [], []
    diagnostic_races = {}
    pilot = None
    with manager as client:
        for race in config["initial_acquisition"]:
            key = race["session_key"]
            summary, windows, pace_rows, lap_outcomes, inputs, diagnostics = _collect_race(
                root, warehouse, race, cohorts[key], config, output / str(key), client, bundle,
                coverage_diagnostics=coverage_diagnostics,
            )
            races.append(summary)
            quality[key] = windows
            references.extend(pace_rows)
            outcomes.extend(lap_outcomes)
            archive.append(inputs)
            if diagnostics is not None:
                diagnostic_races[key] = diagnostics
            if key == pilot_key:
                pilot = _collect_race(
                    root, warehouse, race, cohorts[key], config, output / "coverage_pilot" / str(key), client, bundle,
                    coverage_diagnostics=True, pilot_path=coverage_pilot, protocol_hash=protocol_hash,
                )
    research_rows = residual_research(references, outcomes, config["reference"])
    for race in races:
        key = race["session_key"]
        local_residuals = [row for row in research_rows if row["session_key"] == key]
        race["signed_residual_diagnostics"] = residual_summary(local_residuals)
        _json(output / str(key) / "localization.json", {"summary": race, "windows": quality[key]})
        _parquet(output / str(key) / "traffic_residuals.parquet", local_residuals)
    result = {"protocol": config["version"], "protocol_sha256": protocol_hash, "races": races,
              "pace_bundle_status": "VERIFIED" if bundle is not None else "NOT_REQUESTED",
              "pace_bundle_sha256": bundle.artifact_sha256 if bundle is not None else None,
              "research_readiness": traffic_readiness(races, research_rows, config["evaluation"]),
              "traffic_model_status": "RESEARCH_ONLY"}
    _json(output / "metrics.json", result)
    _json(output / "acquisition_manifest.json", {"protocol_sha256": protocol_hash, "races": archive})
    from f1ri.validation.traffic_plots import plot_traffic_research

    plot_traffic_research(races, output / "stage5_s3_c_cross_circuit.png",
                          config["reconstruction"]["minimum_full_field_fraction_target"])
    if coverage_diagnostics:
        _write_coverage_report(output, races, diagnostic_races, references, pilot, config, stage3_root,
                               result["pace_bundle_sha256"])
    return result


def _write_coverage_report(output, races, diagnostics, references, pilot, config, stage3_root, pace_hash):
    audited = [row for race in diagnostics.values() for row in race["audited_outcomes"]]
    residuals = residual_research(references, audited, config["reference"])
    splits = {}
    if stage3_root is not None:
        frozen = json.loads((stage3_root / "artifacts/runs/stage3_degradation_v1/splits.json").read_text(encoding="utf-8"))
        splits = {row["session_key"]: name for name, rows in frozen.items() for row in rows}
    result = {
        "availability": "OBSERVATION_TIMESTAMP_PROXY",
        "pace_bundle_sha256": pace_hash,
        "coverage_target": config["reconstruction"]["minimum_full_field_fraction_target"],
        "traffic_model_status": "RESEARCH_ONLY",
        "original_readiness": traffic_readiness(races, residuals, config["evaluation"]),
        "races": [],
    }
    for race in races:
        key = race["session_key"]
        detail = diagnostics[key]
        ref = [row for row in references if row["session_key"] == key]
        local = [row for row in residuals if row["session_key"] == key]
        targets = target_summary(detail["audit"], detail["episodes"], detail["segments"], ref, local, output / str(key))
        result["races"].append({
            "session_key": key, "name": race["name"], "split": race["split"], "stage3_split": splits.get(key),
            "field_window_seconds": race["field_window_seconds"], "window_counts": race["window_counts"],
            "shared_timing_marker_check": race["timing_markers"], "shared_control_passage_check": race["control_order"],
            "coverage": detail["coverage"], "targets": targets,
        })
    if pilot is not None:
        race, quality, ref, _, archive, detail = pilot
        key = race["session_key"]
        folder = output / "coverage_pilot" / str(key)
        spans = [(row["telemetry"]["start_utc"], row["telemetry"]["end_utc"]) for row in archive["windows"]]
        baseline_cutoffs = {row["opportunity_id"]: row["as_of_utc"] for row in diagnostics[key]["audit"].cutoffs}
        historical = [row for row in audited if row["session_key"] != key or not any(
            start <= baseline_cutoffs[row["opportunity_id"]] < end for start, end in spans
        )]
        allowed = {row["opportunity_id"] for row in historical}
        pilot_residuals = residual_research(
            [row for row in references if row["opportunity_id"] in allowed] + ref,
            historical + detail["audited_outcomes"], config["reference"],
        )
        pilot_ids = {row["opportunity_id"] for row in detail["audited_outcomes"]}
        local = [row for row in pilot_residuals if row["opportunity_id"] in pilot_ids]
        targets = target_summary(detail["audit"], detail["episodes"], detail["segments"], ref, local, folder)
        _parquet(folder / "traffic_residuals.parquet", local)
        _json(folder / "localization.json", {"summary": race, "windows": quality})
        original = next(row for row in result["races"] if row["session_key"] == key)
        original_archive = json.loads((output / "acquisition_manifest.json").read_text(encoding="utf-8"))["races"]
        frozen_hash = next(row["geometry_sha256"] for row in original_archive if row["session_key"] == key)
        if archive["geometry_sha256"] != frozen_hash:
            raise ValueError("Coverage pilot must reuse the original geometry")
        paired = _paired_extension_coverage(diagnostics[key]["snapshots"], detail["snapshots"])
        paired["complete_field_neighbor_pairs"] = _paired_extension_neighbors(output / str(key), folder)
        result["development_pilot"] = {
            "session_key": key, "name": race["name"], "split": race["split"], "stage3_split": splits.get(key),
            "plan": detail["plan"], "field_window_seconds": race["field_window_seconds"],
            "window_counts": race["window_counts"], "sources": archive,
            "coverage": detail["coverage"], "targets": targets, "shared_grid_comparison": paired,
            "anchors": [
                {"original_window": window["anchor"], "extended_window": window["name"],
                 "original": original["targets"]["by_window"][window["anchor"]],
                 "extended": targets["by_window"][window["name"]]} for window in archive["windows"]
            ],
            "shared_timing_marker_check": race["timing_markers"], "shared_control_passage_check": race["control_order"],
            "readiness": traffic_readiness(
                [row for row in races if row["session_key"] != key] + [race], pilot_residuals, config["evaluation"],
            ),
        }
    _json(output / "coverage_diagnostics.json", result)
    from f1ri.validation.traffic_plots import plot_coverage_diagnostics

    plot_coverage_diagnostics(result, output / "stage5_s3_c_coverage_diagnostics.png")


def _paired_extension_coverage(original, extended):
    by_clock = {row["as_of_utc"]: row for row in original}
    counts = Counter()
    for row in extended:
        baseline = by_clock.get(row["as_of_utc"])
        if baseline is None:
            continue
        if baseline["required"] != row["required"]:
            raise ValueError("Original and extended point-in-time route masks differ")
        counts["shared_snapshots"] += 1
        counts["original_complete"] += baseline["complete"]
        counts["extension_complete"] += row["complete"]
        counts["extension_only_complete"] += row["complete"] and not baseline["complete"]
        counts["original_only_complete"] += baseline["complete"] and not row["complete"]
    return dict(counts)


def _paired_extension_neighbors(original, extended):
    with duckdb.connect() as con:
        pair = con.execute("""
            SELECT count(*) AS both_resolved,
                   count(*) FILTER (WHERE a.ahead <> b.ahead) AS different_identity,
                   quantile_cont(abs(a.gap_m - b.gap_m), 0.90)
                       FILTER (WHERE a.ahead = b.ahead) AS same_identity_gap_difference_p90_m
            FROM read_parquet(?) a JOIN read_parquet(?) b
                 USING (driver_number, as_of_utc, mode)
            WHERE a.mode = 'POINT_IN_TIME' AND a.complete AND b.complete
              AND a.ahead IS NOT NULL AND b.ahead IS NOT NULL
        """, [str(path / "coverage_driver_snapshots.parquet") for path in (original, extended)])
        return dict(zip((column[0] for column in pair.description), pair.fetchone(), strict=True))
