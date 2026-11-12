"""Bounded Singapore reconstruction and synthetic circular-following diagnostics."""
from __future__ import annotations

import hashlib
import itertools
import json
from bisect import bisect_right
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml

from f1ri.config import load_config
from f1ri.features.physical_positions import LocalizationMode, estimate_from_trace
from f1ri.features.track_geometry import (
    LocationFix,
    build_racing_loop,
    forward_passages,
    map_fixes,
    signed_loop_delta,
)
from f1ri.ingestion.openf1 import OpenF1Client
from f1ri.ingestion.telemetry import TelemetryWindow, load_telemetry_window
from f1ri.simulator.following import simulate_circular_following
from f1ri.simulator.proximity import (
    PhysicalRoute,
    PhysicalSnapshot,
    PositionQuality,
    TrackPosition,
    physical_neighborhoods,
)
from f1ri.simulator.state import (
    CarState,
    RacePhase,
    RaceRuleset,
    RaceState,
    SimulationMode,
    TrackState,
    TyreState,
)
from f1ri.validation.proximity_episodes import candidate_episodes
from f1ri.validation.proximity_timing_audit import audit_race_timing


def _json(path: Path, value) -> None:
    encoded = json.dumps(value, indent=2, default=str, allow_nan=False) + "\n"
    path.write_text(encoded, encoding="utf-8", newline="\n")


def _parquet(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.unlink(missing_ok=True)
        return
    with duckdb.connect() as con:
        con.register("s3_rows", pd.DataFrame(rows))
        con.execute("COPY s3_rows TO ? (FORMAT PARQUET)", [str(path)])


def _date(value: str) -> datetime:
    at = datetime.fromisoformat(value)
    if at.utcoffset() is None:
        raise ValueError("Pilot windows must declare a timezone")
    return at.astimezone(UTC)


def _fetch(root, key, block, driver, endpoint, client, provenance):
    window = TelemetryWindow(key, driver, _date(block["start"]), _date(block["end"]))
    frame, manifest = load_telemetry_window(root, window, endpoint, client=client)
    provenance.append(manifest)
    return frame


def _fixes(frame) -> tuple[LocationFix, ...]:
    return tuple(LocationFix(row.date.to_pydatetime(), row.x, row.y)
                 for row in frame.dropna(subset=["x", "y"]).itertuples())


def _evidence(warehouse: Path, key: int) -> dict:
    tables = {
        "laps": """SELECT driver_number, lap_number, lap_start_utc, sector_1_s, sector_2_s,
            is_clean_green_pace_lap, track_gap_ahead_at_start_s, gap_ahead_at_start_raw,
            is_pit_lap, track_status FROM gold.lap_context
            WHERE session_key = ? AND lap_start_utc IS NOT NULL ORDER BY lap_start_utc""",
        "positions": """SELECT driver_number, date, position FROM silver.position
            WHERE session_key = ? AND date IS NOT NULL ORDER BY date, driver_number, position""",
        "pits": """SELECT driver_number, date, lane_duration FROM silver.pit
            WHERE session_key = ? AND date IS NOT NULL ORDER BY date""",
        "blue": """SELECT driver_number, date, lap_number FROM silver.race_control
            WHERE session_key = ? AND flag = 'BLUE' ORDER BY date""",
        "track": """SELECT start_utc, end_utc, status FROM gold.track_status_intervals
            WHERE session_key = ? ORDER BY start_utc""",
    }
    with duckdb.connect(str(warehouse), read_only=True) as con:
        con.execute("SET TimeZone = 'UTC'")
        return {name: con.execute(sql, [key]).df() for name, sql in tables.items()}


def _lap(evidence, block, number):
    rows = evidence["laps"]
    result = rows[(rows.driver_number == block["driver"]) & (rows.lap_number == number)]
    if len(result) != 1:
        raise ValueError("Reference or validation lap is missing from the warehouse")
    return result.iloc[0]


def _geometry(root, cfg, evidence, client, provenance):
    key, block = cfg["session_key"], cfg["geometry"]
    first, following = _lap(evidence, block, block["lap"]), _lap(evidence, block, block["lap"] + 1)
    if not first.is_clean_green_pace_lap:
        raise ValueError("Geometry input must be a screened clean green lap")
    fixes = _fixes(_fetch(root, key, block, block["driver"], "location", client, provenance))
    # Supporting speed samples are retained for independent local-speed checks.
    _fetch(root, key, block, block["driver"], "car_data", client, provenance)
    geometry = build_racing_loop(
        fixes, start=first.lap_start_utc.to_pydatetime(), end=following.lap_start_utc.to_pydatetime(),
        version=cfg["layout_version"], length_m=cfg["loop_length_m"],
        available_at_utc=_date(block["end"]),
    )
    trace = map_fixes(geometry, fixes)
    markers = [0.]
    for delay in (first.sector_1_s, first.sector_1_s + first.sector_2_s):
        at = first.lap_start_utc.to_pydatetime() + timedelta(seconds=delay)
        est = estimate_from_trace(geometry, trace, at, LocalizationMode.RETROSPECTIVE)
        if est.progress_m is None:
            raise ValueError("Reference sector has no consistent location bracket")
        markers.append(est.progress_m)
    if not 0 < markers[1] < markers[2] < geometry.length_m:
        raise ValueError("Reference sector markers are not in racing order")
    return geometry, markers


def _independent_check(root, cfg, evidence, geometry, markers, client, provenance):
    key, block = cfg["session_key"], cfg["check"]
    start = _lap(evidence, block, block["lap"])
    end = _lap(evidence, block, block["lap"] + 1).lap_start_utc.to_pydatetime()
    if not start.is_clean_green_pace_lap:
        raise ValueError("Independent validation lap is not a screened clean green lap")
    times = [start.lap_start_utc.to_pydatetime()]
    times.extend(times[0] + timedelta(seconds=value)
                 for value in (start.sector_1_s, start.sector_1_s + start.sector_2_s))
    times.append(end)
    location = _fetch(root, key, block, block["driver"], "location", client, provenance)
    speed = _fetch(root, key, block, block["driver"], "car_data", client, provenance)
    trace = map_fixes(geometry, _fixes(location))
    checks = []
    for label, marker, timing in zip(("line_start", "sector_1", "sector_2", "line_end"),
                                      (*markers, 0.), times, strict=True):
        crossings = forward_passages(trace, marker, geometry.length_m)
        crossing = min(crossings, key=lambda at: abs(at - timing), default=None)
        checks.append({"marker": label, "progress_m": marker, "timing_utc": timing.isoformat(),
                       "projection_utc": crossing.isoformat() if crossing else None,
                       "delta_s": (crossing - timing).total_seconds() if crossing else None})
    speeds = []
    for left, right in itertools.pairwise(trace):
        if left.progress_m is None or right.progress_m is None:
            continue
        dt = (right.fix.at_utc - left.fix.at_utc).total_seconds()
        if 0 < dt <= 0.8:
            delta = signed_loop_delta(left.progress_m, right.progress_m, geometry.length_m)
            if 0 <= delta <= 105 * dt:
                speeds.append({"date": right.fix.at_utc, "projected_mps": delta / dt})
    if speeds and not speed.empty:
        velocity = pd.DataFrame(speeds)
        velocity["date"] = pd.to_datetime(velocity.date, utc=True)
        speed["date"] = pd.to_datetime(speed.date, utc=True).astype(velocity.date.dtype)
        paired = pd.merge_asof(velocity, speed[["date", "speed"]], on="date",
                               tolerance=pd.Timedelta(seconds=0.35), direction="nearest").dropna()
        errors = (paired.projected_mps - paired.speed / 3.6).abs()
    else:
        errors = pd.Series(dtype=float)
    offsets = [abs(row["delta_s"]) for row in checks if row["delta_s"] is not None]
    return {"driver": block["driver"], "lap": block["lap"], "checks": checks,
            "location_samples": len(trace), "matched": sum(x.progress_m is not None for x in trace),
            "speed_pairs": len(errors), "median_speed_abs_error_mps": (
                float(errors.median()) if len(errors) else None),
            "all_markers_within_1s": len(offsets) == len(checks) and max(offsets) <= 1.0}


def _driver_rows(frame, timestamp):
    return {int(driver): (tuple(group[timestamp]), tuple(group.itertuples()))
            for driver, group in frame.groupby("driver_number")}


def _asof(lookup, driver, at):
    times, rows = lookup.get(driver, ((), ()))
    index = bisect_right(times, at) - 1
    return rows[index] if index >= 0 else None


def _physical_row(geometry, trace, at, driver, mode, laps, reported, pits, *, fallback_speed_mps=None):
    lap, position = _asof(laps, driver, at), _asof(reported, driver, at)
    lap_count = int(lap.lap_number) - 1 if lap else None
    classification = int(position.position) if position and pd.notna(position.position) else None
    if lap is None or (at - lap.lap_start_utc).total_seconds() > 160:
        return TrackPosition(driver, PhysicalRoute.UNKNOWN, lap_count, classification,
                             reason="LAP_EVIDENCE_MISSING_OR_STALE")
    route, route_reason = _pit_route(at, mode, pits)
    if route is not PhysicalRoute.RACING:
        return TrackPosition(driver, route, lap_count, classification, reason=route_reason)
    if mode is LocalizationMode.POINT_IN_TIME:
        # The pilot archive uses fix timestamps as availability. With an actual
        # separate availability clock use estimate_location on the raw prefix.
        trace = tuple(point for point in trace if point.fix.availability <= at)
    est = estimate_from_trace(geometry, trace, at, mode, fallback_speed_mps=fallback_speed_mps)
    aligned = est.progress_m is not None
    if aligned:
        elapsed = (at - lap.lap_start_utc).total_seconds()
        aligned = est.progress_m <= 105 * elapsed + 35
    reason = est.reason or ("CONTROL_LINE_ALIGNMENT_UNCERTAIN" if not aligned else est.velocity_source)
    return TrackPosition(driver, PhysicalRoute.RACING, lap_count, classification, est.progress_m,
                         est.uncertainty_m, est.speed_mps, est.quality,
                         est.latest_observation_utc, aligned, reason)


def _pit_route(at, mode, pits):
    for pit in pits:
        if mode is LocalizationMode.POINT_IN_TIME and pit.date > at:
            continue
        if pd.isna(pit.lane_duration) or pit.lane_duration <= 0:
            if abs((pit.date - at).total_seconds()) <= 2:
                return PhysicalRoute.UNKNOWN, "PIT_BOUNDARY_UNCERTAIN"
            continue
        start = pit.date - timedelta(seconds=pit.lane_duration)
        if start + timedelta(seconds=2) <= at < pit.date - timedelta(seconds=2):
            return PhysicalRoute.PIT, "RETROSPECTIVE_APPROXIMATE_PIT_WINDOW"
        if start - timedelta(seconds=2) <= at <= pit.date + timedelta(seconds=2):
            return PhysicalRoute.UNKNOWN, "PIT_BOUNDARY_UNCERTAIN"
    return PhysicalRoute.RACING, None


def _encounter(root, cfg, block, evidence, geometry, client, provenance):
    key, drivers = cfg["session_key"], cfg["field_drivers"]
    start, end = _date(block["start"]), _date(block["end"])
    traces = {driver: map_fixes(geometry, _fixes(
        _fetch(root, key, block, driver, "location", client, provenance)
    )) for driver in drivers}
    laps = _driver_rows(evidence["laps"], "lap_start_utc")
    positions = _driver_rows(evidence["positions"], "date")
    pits = _driver_rows(evidence["pits"], "date")
    focus = cfg["geometry"]["driver"]
    scope = set(block["drivers"])
    track = evidence["track"][(evidence["track"].start_utc < end)
                              & (evidence["track"].end_utc > start)]
    rows, neighbors = [], []
    metrics = Counter()
    gaps = []
    at = start + timedelta(seconds=1)
    while at <= end - timedelta(seconds=1):
        statuses = set(track.loc[(track.start_utc <= at) & (track.end_utc > at), "status"])
        track_state = next(iter(statuses)) if len(statuses) == 1 else "UNKNOWN"
        for mode in LocalizationMode:
            cars = tuple(_physical_row(geometry, trace, at, driver, mode, laps, positions,
                                       pits.get(driver, ((), ()))[1])
                         for driver, trace in traces.items())
            snapshot = PhysicalSnapshot(key, at, geometry.version, geometry.length_m, cars)
            subset = PhysicalSnapshot(key, at, geometry.version, geometry.length_m, tuple(
                car for car in cars if car.driver_number in scope
            ))
            groups = physical_neighborhoods(snapshot)
            sampled_neighbor = physical_neighborhoods(subset)[focus].ahead
            target = groups[focus]
            metric = mode.value.lower()
            metrics[f"{metric}_snapshots"] += 1
            metrics[f"{metric}_complete"] += snapshot.complete
            metrics[f"{metric}_focus_ahead_resolved"] += target.ahead is not None
            if sampled_neighbor is not None and target.ahead is not None:
                metrics[f"{metric}_paired_neighbor"] += 1
                metrics[f"{metric}_different_from_subset"] += (
                    sampled_neighbor.driver_number != target.ahead.driver_number
                )
            if mode is LocalizationMode.RETROSPECTIVE and target.ahead is not None:
                gaps.append(target.ahead.gap_m)
            for car in cars:
                row = asdict(car)
                rows.append({**row, "window": block["name"], "as_of_utc": at,
                             "mode": mode.value, "layout_version": geometry.version,
                             "session_key": key, "track_state": track_state,
                             "unwrapped_m": car.unwrapped_m(geometry.length_m),
                             "source_age_s": ((at - car.observed_at_utc).total_seconds()
                                              if car.observed_at_utc else None)})
                group = groups[car.driver_number]
                neighbors.append({
                    "window": block["name"], "as_of_utc": at, "mode": mode.value,
                    "session_key": key, "layout_version": geometry.version,
                    "track_state": track_state,
                    "driver_number": car.driver_number, "coverage": group.coverage,
                    "ahead": group.ahead.driver_number if group.ahead else None,
                    "behind": group.behind.driver_number if group.behind else None,
                    "ahead_reason": group.ahead_reason, "behind_reason": group.behind_reason,
                    "gap_m": group.ahead.gap_m if group.ahead else None,
                    "gap_lower_m": group.ahead.lower_m if group.ahead else None,
                    "gap_upper_m": group.ahead.upper_m if group.ahead else None,
                    "approximate_gap_s": group.ahead.approximate_gap_s if group.ahead else None,
                    "lap_advantage": group.ahead.lap_advantage if group.ahead else None,
                    "encounter": group.ahead.encounter if group.ahead else None,
                    "subset_ahead": (sampled_neighbor.driver_number
                                     if sampled_neighbor and car.driver_number == focus else None),
                })
        at += timedelta(seconds=0.5)
    line = {
        driver: [at for at in forward_passages(trace, 0, geometry.length_m)
                 if _pit_route(at, LocalizationMode.RETROSPECTIVE, pits.get(driver, ((), ()))[1])[0]
                 is PhysicalRoute.RACING]
        for driver, trace in traces.items()
    }
    stage1 = []
    for crossing in line[focus]:
        previous = [(t, driver) for driver, passes in line.items() if driver != focus
                    for t in passes if 0 < (crossing - t).total_seconds() <= 10]
        match = max(previous, default=None)
        records = [row for row in laps[focus][1]
                   if abs((crossing - row.lap_start_utc).total_seconds()) <= 2]
        if match and records:
            record = min(records, key=lambda row: abs((crossing - row.lap_start_utc).total_seconds()))
            stage1.append({
                "leader_lap": int(record.lap_number), "other_driver": match[1],
                "telemetry_control_line_gap_s": (crossing - match[0]).total_seconds(),
                "stage1_physical_gap_s": (float(record.track_gap_ahead_at_start_s)
                                          if pd.notna(record.track_gap_ahead_at_start_s) else None),
                "stage1_classification_gap_raw": (str(record.gap_ahead_at_start_raw)
                                                   if pd.notna(record.gap_ahead_at_start_raw) else None),
            })
    blue = evidence["blue"][evidence["blue"].date.between(start, end)]
    pairs = {}
    for row in neighbors:
        if row["driver_number"] == focus and row["coverage"] == "COMPLETE" and row["ahead"] is not None:
            pairs.setdefault(row["as_of_utc"], []).append(row)
    paired = [rows for rows in pairs.values() if len(rows) == 2]
    gap_diff = [abs(a["gap_m"] - b["gap_m"]) for a, b in paired if a["ahead"] == b["ahead"]]
    result = {"name": block["name"], "start_utc": start.isoformat(), "end_utc": end.isoformat(),
              "telemetry_rows": sum(map(len, traces.values())),
              "map_matched": sum(point.progress_m is not None for trace in traces.values()
                                 for point in trace),
              "field_drivers_requested": len(drivers), "snapshot_counts": dict(metrics),
              "complete_resolved_in_both_modes": len(paired),
              "same_neighbor_in_both_modes": len(gap_diff),
              "same_neighbor_median_gap_difference_m": float(np.median(gap_diff)) if gap_diff else None,
              "median_retrospective_gap_m": float(np.median(gaps)) if gaps else None,
              "stage1_comparison": stage1,
              "blue_flag_recipients": sorted(int(n) for n in blue.driver_number.dropna().unique()),
              "track_states": sorted(set(track.status))}
    return result, rows, neighbors


def synthetic_following():
    start = datetime(2025, 1, 1, tzinfo=UTC)
    length = 900.
    locations = (TrackPosition(63, PhysicalRoute.RACING, 10, 1, 885., 0.,
                               quality=PositionQuality.EXACT_SCENARIO),
                 TrackPosition(31, PhysicalRoute.RACING, 9, 19, 20., 0.,
                               quality=PositionQuality.EXACT_SCENARIO),
                 TrackPosition(4, PhysicalRoute.RACING, 10, 2, 820., 0.,
                               quality=PositionQuality.EXACT_SCENARIO))
    physical = PhysicalSnapshot(1, start, "synthetic-circle", length, locations)
    field = RaceState(1, start, RaceRuleset(2025, 20), SimulationMode.CONDITIONAL_SIMULATION,
                      "s3-lapped-chain", tuple(CarState(
                          pos.driver_number, i, TyreState("MEDIUM", 1),
                          completed_checkpoints=pos.completed_laps * 3 + int(pos.progress_m // 300),
                          last_crossing_us=0,
                      ) for i, pos in enumerate(locations, 1)),
                      phase=RacePhase.RUNNING, track_state=TrackState.GREEN)
    following = simulate_circular_following(field, physical, {63: 80., 31: 105., 4: 79.},
                                             laps=2, minimum_gap_s=0.2)
    if following.run is None:
        raise AssertionError(f"Known synthetic following field was rejected: {following.unsupported}")
    return physical, following


def run_stage5_s3(
    root: Path, warehouse: Path, audit_warehouse: Path, config_path: Path, output: Path,
    *, ingest: bool = False,
) -> dict:
    config_bytes = config_path.read_bytes()
    cfg = yaml.safe_load(config_bytes)
    evidence = _evidence(warehouse, cfg["session_key"])
    provenance = []
    source = load_config(root)["data"]
    client = OpenF1Client(source["openf1_base_url"], source["min_seconds_between_requests"]) if ingest else None
    try:
        geometry, markers = _geometry(root, cfg, evidence, client, provenance)
        check = _independent_check(root, cfg, evidence, geometry, markers, client, provenance)
        windows, positions, neighborhoods = [], [], []
        for block in cfg["encounters"]:
            summary, rows, neighbors = _encounter(root, cfg, block, evidence, geometry, client, provenance)
            windows.append(summary)
            positions.extend(rows)
            neighborhoods.extend(neighbors)
    finally:
        if client is not None:
            client.client.close()
    audits = [audit_race_timing(root, audit_warehouse, item) for item in cfg["reconciliation"]]
    physical, following = synthetic_following()
    output.mkdir(parents=True, exist_ok=True)
    (output / "config.yaml").write_bytes(config_bytes)
    _json(output / "geometry.json", {"layout_version": geometry.version, "length_m": geometry.length_m,
                                    "available_at_utc": geometry.available_at_utc.isoformat(),
                                    "reference_end_gap_m": geometry.reference_lap_end_gap_m,
                                    "length_source": cfg["length_source"], "sector_markers_m": markers,
                                    "points_xy": geometry.points, "quality": "SAMPLED_RACING_LINE"})
    _json(output / "telemetry_manifest.json", provenance)
    _json(output / "timing_reconciliation.json", audits)
    _parquet(output / "physical_positions.parquet", positions)
    _parquet(output / "neighborhoods.parquet", neighborhoods)
    episodes = list(candidate_episodes(neighborhoods, **cfg["candidate_exposure"]))
    _parquet(output / "candidate_exposures.parquet", episodes)
    _parquet(output / "following_traversals.parquet", [asdict(row) for row in following.traversals])
    report = {
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(), "geometry_version": geometry.version,
        "geometry_sha256": hashlib.sha256((output / "geometry.json").read_bytes()).hexdigest(),
        "geometry_reference_end_gap_m": geometry.reference_lap_end_gap_m,
        "independent_lap": check, "windows": windows,
        "candidate_exposures_by_mode": dict(Counter(row["mode"] for row in episodes)),
        "classification_audits": [{key: row[key] for key in (
            "label", "baseline_position_matches", "audited_position_matches", "official_classified",
        )} | {"excluded_local_windows": len(row["local_quality_mask"])} for row in audits],
        "synthetic_following": {
            "physical_ahead": {driver: group.ahead.driver_number
                               for driver, group in physical_neighborhoods(physical).items()},
            "traversals": len(following.traversals),
            "traffic_delay_s": {driver: sum(row.traffic_delay_us for row in following.traversals
                                            if row.driver_number == driver) / 1e6
                                for driver in (63, 31, 4)},
        },
        "traffic_model_status": "INSUFFICIENT_INDEPENDENT_LABELS_AND_RACES",
    }
    _json(output / "metrics.json", report)
    from f1ri.validation.proximity_plots import plot_proximity

    plot_proximity(geometry, cfg, positions, neighborhoods, following, report, output)
    return report
