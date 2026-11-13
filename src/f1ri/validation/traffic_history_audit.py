"""Audit frozen prefix history, retrospective physical evidence and marker failures."""
from __future__ import annotations

import json
from collections import Counter
from datetime import timedelta
from hashlib import sha256
from itertools import pairwise
from pathlib import Path

import duckdb
import pandas as pd

from f1ri.features.physical_positions import LocalizationMode
from f1ri.features.race_state import build_field_snapshot
from f1ri.features.track_geometry import TrackGeometry, forward_passages, map_fixes
from f1ri.ingestion.telemetry import TelemetryWindow, load_telemetry_window
from f1ri.validation.proximity_pilot import _fixes, _pit_route
from f1ri.validation.traffic_acquisition import ReplaySpan, race_inputs
from f1ri.validation.traffic_coverage_pilot import merge_source_frames
from f1ri.validation.traffic_exposures import exposure_state


def parquet_rows(path: Path) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing frozen diagnostic ledger: {path}")
    with duckdb.connect() as con:
        con.execute("SET TimeZone = 'UTC'")
        cursor = con.execute("SELECT * FROM read_parquet(?)", [str(path)])
        fields = [column[0] for column in cursor.description]
        return [dict(zip(fields, row, strict=True)) for row in cursor.fetchall()]


def visible_history(warehouse, cohort, driver, lap, cutoff, *, audit=False):
    full = {driver: []} if audit else None
    field = build_field_snapshot(warehouse, cohort["session_key"], cutoff, cohort["scheduled_laps"], 1,
                                 same_stint_history_audit=full)
    car = next(row for row in field.drivers if row.driver_number == driver)
    request = car.request
    if request is None or request.planned_start_lap != lap:
        return None, full[driver] if full is not None else [], car.support_reason or "PACE_LAP_ALIGNMENT_UNSUPPORTED"
    return request, full[driver] if full is not None else [], None


def physical_history_check(rows, start, end, config):
    """Apply the frozen physical clean-air screen to a covered prior lap."""
    period = timedelta(seconds=config["sampling"]["sample_period_seconds"])
    expected = (end - start).total_seconds()
    if expected <= 0:
        return {"physical_history": "INSUFFICIENT_EVIDENCE", "physical_reason": "INVALID_ELAPSED"}
    groups = {}
    for row in rows:
        groups.setdefault(row["window"], []).append(row)
    best = {"physical_history": "INSUFFICIENT_EVIDENCE", "physical_reason": "NO_FULL_WINDOW",
            "physical_supported_fraction": None}
    for name, group in groups.items():
        ordered = sorted(group, key=lambda row: row["as_of_utc"])
        if not ordered[0]["as_of_utc"] <= start or not end <= ordered[-1]["as_of_utc"] + period:
            continue
        cutoff = next(row for row in reversed(ordered) if row["as_of_utc"] <= start)
        usable = [row for row in ordered if start <= row["as_of_utc"] < end and row["route_mask_ok"]
                  and exposure_state(row, config["exposure"]) != "UNSUPPORTED"]
        support = min(1., len(usable) * period.total_seconds() / expected)
        clear = (sum(exposure_state(row, config["exposure"]) == "CLEAR" for row in usable) / len(usable)
                 if usable else None)
        evidence = {"physical_window": name, "physical_supported_fraction": support, "physical_clear_fraction": clear}
        state = exposure_state(cutoff, config["exposure"])
        if support >= config["reference"]["label_minimum_coverage"] and state != "UNSUPPORTED":
            clean = state == "CLEAR" and clear >= config["reference"]["minimum_clean_control_fraction"]
            return evidence | {"physical_history": "CLEAN" if clean else "NON_CLEAN", "physical_reason": None}
        if best["physical_supported_fraction"] is None or support > best["physical_supported_fraction"]:
            best |= evidence | {"physical_reason": "PHYSICAL_SUPPORT_BELOW_MINIMUM"}
    return best


def _physical_history_rows(path: Path, driver: int) -> list[dict]:
    with duckdb.connect() as con:
        con.execute("SET TimeZone = 'UTC'")
        cursor = con.execute("""
            WITH source AS (SELECT * FROM read_parquet(?)),
            retrospect AS (SELECT * FROM source WHERE mode = 'RETROSPECTIVE')
            SELECT p.*, coalesce(own.route = 'RACING' AND other.route = 'RACING', false) AS route_mask_ok
            FROM source p
            LEFT JOIN retrospect own ON own."window" = p."window" AND own.as_of_utc = p.as_of_utc
                AND own.driver_number = p.driver_number
            LEFT JOIN retrospect other ON other."window" = p."window" AND other.as_of_utc = p.as_of_utc
                AND other.driver_number = p.ahead
            WHERE p.mode = 'POINT_IN_TIME' AND p.driver_number = ?
            ORDER BY p."window", p.as_of_utc
        """, [str(path), driver])
        fields = [column[0] for column in cursor.description]
        return [dict(zip(fields, row, strict=True)) for row in cursor.fetchall()]


def audit_hungary_history(warehouse, cohort, folder, config):
    rows = []
    ledger = parquet_rows(folder / "lap_eligibility_ledger.parquet")
    for target in (row for row in ledger if row["screen"] == "FOLLOWING"):
        driver, lap, cutoff = target["driver_number"], target["lap_number"], target["as_of_utc"]
        request, history, reason = visible_history(warehouse, cohort, driver, lap, cutoff, audit=True)
        candidates = _physical_history_rows(folder / "physical_neighbors.parquet", driver)
        audited = []
        for prior in history:
            physical = physical_history_check(candidates, prior["started_utc"], prior["completed_utc"], config)
            traffic = "TRAFFIC" in (prior["stage1_exclusions"] or "").split("|")
            state = physical["physical_history"]
            agreement = ("INSUFFICIENT_EVIDENCE" if state == "INSUFFICIENT_EVIDENCE" else
                         "AGREEMENT" if (traffic and state == "NON_CLEAN") or (not traffic and state == "CLEAN")
                         else "DISAGREEMENT")
            audited.append(prior | physical | {"stage1_traffic": traffic, "agreement": agreement})
        rows.append({
            "opportunity_id": target["opportunity_id"], "cutoff_utc": cutoff, "driver_number": driver,
            "target_lap": lap, "stint_number": request.stint_number if request else None,
            "pace_request_status": reason or "AVAILABLE", "prior_completed_laps": len(audited),
            "prior_clean_laps": request.clean_laps_observed_current_stint if request else None,
            "stage1_traffic_laps": sum(row["stage1_traffic"] for row in audited),
            "physical_comparison": dict(Counter(row["agreement"] for row in audited)), "prior_laps": audited,
        })
    return rows


def audit_hungary_marker(root, warehouse, folder, config):
    acquisition = json.loads((folder / "acquisition.json").read_text(encoding="utf-8"))
    selected = next(row for row in acquisition["windows"] if row["name"] == "random_green_134157")
    raw = json.loads((folder / "geometry.json").read_text(encoding="utf-8"))
    geometry = TrackGeometry(raw["version"], raw["length_m"], pd.Timestamp(raw["available_at_utc"]),
                             tuple(map(tuple, raw["points_xy"])), raw["reference_end_gap_m"])
    clocks = selected["telemetry"]
    span = ReplaySpan(9928, pd.Timestamp(clocks["start_utc"]), pd.Timestamp(clocks["end_utc"]))
    frames, source_hashes = [], []
    for row in selected["sources"]:
        if row["endpoint"] != "location":
            continue
        if sha256((root / row["cache_path"]).read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("Hungary marker input hash mismatch")
        frame, _ = load_telemetry_window(root, TelemetryWindow(
            9928, None, pd.Timestamp(row["start_utc"]), pd.Timestamp(row["end_utc"]),
        ), "location")
        frames.append(frame[frame.driver_number.eq(5)])
        source_hashes.append(row["sha256"])
    fixes, _ = merge_source_frames(frames, span)
    trace = map_fixes(geometry, _fixes(fixes))
    inputs = race_inputs(warehouse, 9928)
    with duckdb.connect(str(warehouse), read_only=True) as con:
        con.execute("SET TimeZone = 'UTC'")
        timing = con.execute("""
            SELECT DISTINCT date_start, duration_sector_1, duration_sector_2
            FROM silver.laps WHERE session_key = 9928 AND driver_number = 5 AND lap_number = 29
        """).fetchall()
    if len(timing) != 1 or any(value is None for value in timing[0]):
        raise ValueError("Hungary marker needs a unique independent timing row")
    start, s1, s2 = timing[0]
    expected = start + timedelta(seconds=s1 + s2)
    marker = raw["sector_markers_m"][2]
    tolerance = config["reconstruction"]["maximum_timing_marker_match_s"]
    passages = forward_passages(trace, marker, geometry.length_m)
    nearby = [point for point in trace if abs((point.fix.at_utc - expected).total_seconds()) <= tolerance]
    left = next(point for point in reversed(nearby) if point.progress_m is not None and point.progress_m < marker)
    right = next(point for point in nearby if point.progress_m is not None and point.progress_m > marker)
    span_s = (right.fix.at_utc - left.fix.at_utc).total_seconds()
    estimate = left.fix.at_utc + (right.fix.at_utc - left.fix.at_utc) * (
        (marker - left.progress_m) / (right.progress_m - left.progress_m)
    )
    pit = tuple(inputs["pits"][inputs["pits"].driver_number.eq(5)].itertuples())
    matched = any(abs((event - expected).total_seconds()) <= tolerance for event in passages)
    consecutive = any(a is left and b is right for a, b in pairwise(trace))
    rejected_gap = consecutive and span_s > 1.0 and not matched
    return {
        "session_key": 9928, "driver_number": 5, "lap_number": 29, "sector": 2, "window": selected["name"],
        "layout": raw["version"], "geometry_sha256": sha256((folder / "geometry.json").read_bytes()).hexdigest(),
        "location_source_sha256": source_hashes, "timing_source": "silver.laps",
        "lap_start_utc": start, "sector_1_s": s1, "sector_2_s": s2,
        "expected_utc": expected, "marker_m": marker, "match_tolerance_s": tolerance,
        "route_at_marker": _pit_route(expected, LocalizationMode.RETROSPECTIVE, pit)[0].value,
        "fixes": [{"date": p.fix.at_utc, "x": p.fix.x, "y": p.fix.y, "progress_m": p.progress_m,
                   "projection_error_m": p.projection_error_m, "reason": p.reason} for p in nearby],
        "left_utc": left.fix.at_utc, "right_utc": right.fix.at_utc, "bracket_s": span_s,
        "maximum_bracket_s": 1.0, "counted_as_match": matched,
        "reason": "SOURCE_GAP_EXCEEDS_PASSAGE_BRACKET" if rejected_gap else "UNRESOLVED",
        "diagnostic_only_interpolation_utc": estimate,
        "diagnostic_only_error_s": (estimate - expected).total_seconds(),
    }
