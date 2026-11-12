"""Run the preregistered Stage 5.3 continuation on its initial race cohort."""
from __future__ import annotations

from collections import Counter
from contextlib import nullcontext
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

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
from f1ri.validation.traffic_exposures import build_traffic_exposures, lap_opportunities
from f1ri.validation.traffic_localization import localization_summary, localize_window
from f1ri.validation.traffic_reference import (
    clean_pace_references,
    residual_research,
    residual_summary,
    traffic_readiness,
)


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


def _collect_race(root, warehouse, race, cohort, config, output, client, bundle):
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
    planned = select_traffic_windows(key, inputs["laps"], inputs["track"],
                                     pd.Timestamp(scheduled_reference["end"]), sampling, config["seed"])
    windows = [window for window in planned if window.telemetry.start_utc >= geometry.available_at_utc]
    if not windows:
        raise ValueError(f"No supported green windows were found for session {key}")
    positions, neighbors, quality = [], [], []
    checks = {"control_passages": [], "dropped_fixes": [], "markers": []}
    used_drivers = set()
    window_manifest = []
    off_track = {row["driver_number"]: row for row in race.get("known_off_track", [])}
    for window in windows:
        frames, coverage = acquire_window(root, window.telemetry, sampling["endpoints"], drivers, client)
        pos, nbr, details, diagnostics = localize_window(
            geometry, window, frames["location"], frames["car_data"], inputs, markers, config["reconstruction"],
            sampling["sample_period_seconds"], reference["driver"], used_drivers, off_track,
        )
        positions.extend(pos)
        neighbors.extend(nbr)
        quality.append(details)
        for name, rows in diagnostics.items():
            checks[name].extend(rows)
        window_manifest.append(asdict(window) | {"name": window.name, "sources": coverage})
    summary = localization_summary(quality, checks, len(drivers))
    period = sampling["sample_period_seconds"]
    episodes, segments = build_traffic_exposures(neighbors, config["exposure"], period)
    features, outcomes, opportunity_counts = lap_opportunities(neighbors, segments, inputs["laps"],
                                                                config, cohort["split"])
    references = clean_pace_references(warehouse, cohort["scheduled_laps"], features, bundle,
                                       config["reference"]) if bundle is not None else []
    expected = sampling["windows_per_race"]
    actual = Counter(window.kind for window in windows)
    timing = summary["timing_markers"]
    threshold = config["reconstruction"]["maximum_timing_marker_mae_s"]
    summary |= {
        "name": cohort["name"], "session_key": key, "split": cohort["split"],
        "geometry_version": geometry.version, "reference_end_gap_m": geometry.reference_lap_end_gap_m,
        "geometry_available_utc": geometry.available_at_utc.isoformat(),
        "reference_lap": reference["lap"], "rejected_reference_laps": rejected_references,
        "windows_before_geometry_available": len(planned) - len(windows),
        "field_window_seconds": sum((w.telemetry.end_utc - w.telemetry.start_utc).total_seconds() for w in windows),
        "window_counts": dict(actual), "all_window_strata_acquired": actual == expected,
        "speed_samples": sum(source["usable_rows"] for w in window_manifest for source in w["sources"]
                             if source["endpoint"] == "car_data"),
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
    return summary, quality, references, outcomes, archive


def run_traffic_continuation(
    root: Path, warehouse: Path, config_path: Path, output: Path, *, ingest: bool = False,
    stage3_root: Path | None = None,
) -> dict:
    config, protocol_hash = load_traffic_protocol(config_path)
    cohorts = {row.get("session_key"): row for row in config["cohorts"]}
    source = load_config(root)["data"]
    manager = (OpenF1Client(source["openf1_base_url"], source["min_seconds_between_requests"])
               if ingest else nullcontext())
    bundle = load_pace_bundle(root, stage3_root) if stage3_root is not None else None
    races, quality, references, outcomes, archive = [], {}, [], [], []
    with manager as client:
        for race in config["initial_acquisition"]:
            key = race["session_key"]
            summary, windows, pace_rows, lap_outcomes, inputs = _collect_race(
                root, warehouse, race, cohorts[key], config, output / str(key), client, bundle,
            )
            races.append(summary)
            quality[key] = windows
            references.extend(pace_rows)
            outcomes.extend(lap_outcomes)
            archive.append(inputs)
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
    return result
