"""Retrospective classification reconciliation and local S3 timing masks."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

import duckdb
import pandas as pd

from f1ri.simulator.replay import replay_session
from f1ri.simulator.state import CarStatus, elapsed_us
from f1ri.validation.pit_integration_backtest import _events


def retrospective_order(cars, leader_laps: int, added_seconds: dict[int, float]) -> dict[int, int]:
    """Apply the 2025 90%-rounded-down threshold and declared elapsed penalties."""
    if leader_laps <= 0:
        raise ValueError("Classification requires a positive winner lap count")
    minimum = leader_laps * 9 // 10
    eligible = [car for car in cars if car.completed_laps >= minimum
                and car.last_crossing_us is not None and car.status is not CarStatus.DNS]
    eligible.sort(key=lambda car: (
        -car.completed_laps, car.last_crossing_us + elapsed_us(added_seconds.get(car.driver_number, 0)),
        car.grid_key,
    ))
    return {car.driver_number: rank for rank, car in enumerate(eligible, 1)}


def audit_race_timing(root: Path, warehouse: Path, config: dict) -> dict:
    key = config["session_key"]
    replay = replay_session(warehouse, key, config["scheduled_laps"], annotations_root=root)
    state = replay.run.final_state
    additions = config["elapsed_additions_s"]
    adjusted = retrospective_order(state.cars, state.leader_completed_laps, additions)
    source = config["source"]
    with duckdb.connect(str(warehouse), read_only=True) as con:
        con.execute("SET TimeZone = 'UTC'")
        laps = con.execute("""
            SELECT driver_number, lap_number, lap_start_utc, lap_end_estimate_utc,
                   position_at_lap_start, gap_ahead_at_start_raw, track_status
            FROM gold.lap_context WHERE session_key = ?
        """, [key]).df()
        intervals = con.execute("""
            SELECT driver_number, date, interval_raw FROM silver.intervals
            WHERE session_key = ? ORDER BY date, driver_number
        """, [key]).df()
        flags = con.execute("""
            SELECT date, message FROM silver.race_control
            WHERE session_key = ? AND flag = 'CHEQUERED' ORDER BY date
        """, [key]).df()
        visits, _ = _events(con, key, root)
    lap_rows = {(row.driver_number, row.lap_number): row for row in laps.itertuples()}
    last_interval = intervals.drop_duplicates("driver_number", keep="last").set_index(
        "driver_number"
    ).to_dict("index")
    local = defaultdict(list)
    windows = []
    for issue in replay.issues:
        if issue.code not in {
            "LAP_BOUNDARY_DISAGREEMENT", "SECTOR_SUM_DISAGREEMENT", "UNUSABLE_SECTOR_CROSSING",
            "MISSING_OR_UNEXPECTED_LAP_BOUNDARY", "MISSING_OR_INVALID_LAP_DURATION",
            "NONMONOTONIC_LAP_CROSSING",
        }:
            continue
        row = lap_rows.get((issue.driver_number, issue.lap_number))
        if row is None or pd.isna(row.lap_start_utc):
            continue
        next_row = lap_rows.get((issue.driver_number, issue.lap_number + 1))
        end = next_row.lap_end_estimate_utc if next_row is not None else row.lap_end_estimate_utc
        if pd.isna(end):
            end = row.lap_end_estimate_utc
        if pd.isna(end) or end <= row.lap_start_utc:
            continue
        local[issue.driver_number].append(issue.code)
        windows.append({
            "driver_number": issue.driver_number, "start_utc": row.lap_start_utc.isoformat(),
            "end_utc": end.isoformat(), "reason": issue.code, "detail": issue.detail,
            "track_status": row.track_status, "position_at_lap_start": row.position_at_lap_start,
            "confidence": "EXCLUDE_PHYSICAL_LABEL",
        })
    for visit in visits.itertuples():
        if visit.timing_quality != "C" or pd.isna(visit.estimated_lane_entry_utc):
            continue
        local[visit.driver_number].append("PIT_BOUNDARY_AMBIGUOUS")
        windows.append({
            "driver_number": visit.driver_number, "start_utc": visit.estimated_lane_entry_utc.isoformat(),
            "end_utc": visit.estimated_lane_exit_utc.isoformat(), "reason": "PIT_BOUNDARY_AMBIGUOUS",
            "detail": visit.quality_flags_json, "track_status": visit.transition_track_status,
            "confidence": "EXCLUDE_PHYSICAL_LABEL",
        })
    driver_rows = []
    for driver in replay.drivers:
        number = driver.driver_number
        official, before, after = driver.official_position, driver.classified_position, adjusted.get(number)
        if after != official:
            reason = "UNRESOLVED_CONFLICT"
        elif number in additions:
            reason = "SOURCED_ELAPSED_PENALTY"
        elif before is None and after is not None and driver.status is CarStatus.RETIRED:
            reason = "CLASSIFIED_RETIREMENT_90_PERCENT"
        elif before != after:
            reason = "REORDERED_AFTER_SOURCED_PENALTY"
        else:
            reason = "CONSISTENT_CLASSIFICATION"
        driver_rows.append({
            **asdict(driver), "audited_position": after, "classification_resolution": reason,
            "penalty_s": additions.get(number, 0), "timing_flags": sorted(set(local[number])),
            "last_interval": last_interval.get(number, {}).get("interval_raw"),
        })
    return {
        "session_key": key, "label": config["label"], "official_source": source,
        "baseline_position_matches": sum(row.official_position is not None
                                         and row.classified_position == row.official_position
                                         for row in replay.drivers),
        "audited_position_matches": sum(row["official_position"] is not None
                                        and row["audited_position"] == row["official_position"]
                                        for row in driver_rows),
        "official_classified": sum(row.official_position is not None for row in replay.drivers),
        "chequered_issues": [asdict(issue) for issue in replay.issues
                             if "CHEQUERED" in issue.code],
        "recorded_chequered_utc": [value.isoformat() for value in flags.date],
        "driver_comparisons": driver_rows, "local_quality_mask": windows,
    }
