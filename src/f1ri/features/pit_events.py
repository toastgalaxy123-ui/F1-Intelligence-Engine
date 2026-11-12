"""Evidence-based pit-visit reconstruction; no duration-based incident classification."""
from __future__ import annotations

import json
import math
from datetime import timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from f1ri.features.pit_policy import apply_pit_eligibility


def number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def integer(value: Any) -> int | None:
    result = number(value)
    return int(result) if result is not None else None


def utc(value: Any) -> pd.Timestamp | None:
    result = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(result) else result


def replace_session_frame(
    con: duckdb.DuckDBPyConnection, table: str, frame: pd.DataFrame,
    schema: dict[str, str], session_key: int,
) -> None:
    """Typed, idempotent per-session replacement, also supporting empty inputs."""
    con.execute(f"CREATE TABLE IF NOT EXISTS {table} (" + ", ".join(
        f'"{k}" {v}' for k, v in schema.items()) + ")")
    for name, dtype in schema.items():
        con.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "{name}" {dtype}')
    con.execute(f"DELETE FROM {table} WHERE session_key = ?", [session_key])
    if frame.empty:
        return
    frame = frame.reindex(columns=list(schema))
    con.register("_pit_incoming", frame)
    try:
        columns = ", ".join(f'"{k}"' for k in schema)
        casts = ", ".join(f'CAST("{k}" AS {v})' for k, v in schema.items())
        con.execute(f"INSERT INTO {table} ({columns}) SELECT {casts} FROM _pit_incoming")
    finally:
        con.unregister("_pit_incoming")


def status_at(intervals: pd.DataFrame, when: Any) -> str:
    ts = utc(when)
    if intervals.empty or ts is None:
        return "UNKNOWN"
    starts = pd.to_datetime(intervals.start_utc, utc=True)
    ends = pd.to_datetime(intervals.end_utc, utc=True)
    hit = intervals[(starts <= ts) & (ends > ts)]
    return str(hit.iloc[0].status) if len(hit) == 1 else "UNKNOWN"


def status_overlap(intervals: pd.DataFrame, start: Any, end: Any) -> tuple[str, str]:
    """Require full interval coverage. Unknown/gaps never become GREEN."""
    start, end = utc(start), utc(end)
    if start is None or end is None or end <= start or intervals.empty:
        return "UNKNOWN", "{}"
    durations: dict[str, float] = {}
    cursor = start
    for row in intervals.sort_values("start_utc").to_dict("records"):
        a, b = max(start, utc(row["start_utc"])), min(end, utc(row["end_utc"]))
        if b <= a:
            continue
        if a != cursor:
            return "UNKNOWN", json.dumps(durations, sort_keys=True)
        durations[str(row["status"])] = durations.get(str(row["status"]), 0) + (b-a).total_seconds()
        cursor = b
    if cursor != end:
        return "UNKNOWN", json.dumps(durations, sort_keys=True)
    state = next(iter(durations)) if len(durations) == 1 else "MIXED"
    return state, json.dumps(durations, sort_keys=True)


def reconstruct_pit_events(
    pits: pd.DataFrame, laps: pd.DataFrame, stints: pd.DataFrame,
    session: dict, intervals: pd.DataFrame, drivers: pd.DataFrame | None = None,
    annotations: pd.DataFrame | None = None,
    race_control: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Reconcile raw pit lap ±1 against raw out marker AND both stint boundaries.

    The pit timestamp is a provider report time, not a legal entry loop. Current
    OpenF1 ingestion timestamps PitStop/PitLaneTimeCollection feed messages.
    A backwards lane window ending at that report is explicitly approximate;
    its boundaries may be shifted by feed latency and never become quality A.
    Exact duplicate payloads are retained as a count on one canonical visit.
    Conflicting records at the same time remain flagged and ineligible.
    """
    if pits.empty:
        return pd.DataFrame()
    result = []
    pits = pits.copy()
    pits["_date"] = pd.to_datetime(pits["date"], utc=True, errors="coerce", format="ISO8601")
    pits["_sort"] = pits.apply(lambda r: json.dumps(
        {k: str(v) for k, v in r.items() if k != "_sort"}, sort_keys=True), axis=1)
    drivers = drivers if drivers is not None else pd.DataFrame()
    annotations = annotations if annotations is not None else pd.DataFrame()
    sid = int(session["session_key"])
    for driver, visits in pits.groupby("driver_number", sort=True):
        dlaps = laps[laps.driver_number == driver].sort_values("lap_number")
        dstints = stints[stints.driver_number == driver].sort_values("stint_number")
        visits = visits.sort_values(["_date", "lap_number", "_sort"], na_position="last")
        duplicates = visits.groupby("_sort").size().to_dict()
        visits = visits.drop_duplicates("_sort")
        by_lap = {int(r["lap_number"]): r for r in dlaps.to_dict("records")}
        by_stint = dstints.to_dict("records")
        drv = drivers[drivers.driver_number == driver] if not drivers.empty else drivers
        for sequence, pit in enumerate(visits.to_dict("records"), 1):
            flags: list[str] = []
            event_id = f"{sid}:{int(driver)}:{sequence}"
            rawlap = integer(pit.get("lap_number"))
            timestamp = utc(pit.get("date"))
            lane = number(pit.get("lane_duration"))
            if lane is None:
                lane = number(pit.get("pit_duration"))
            stop = number(pit.get("stop_duration"))
            if timestamp is None:
                flags.append("MISSING_PIT_TIMESTAMP")
            elif sum(utc(v["date"]) == timestamp for v in visits.to_dict("records")) > 1:
                flags.append("CONFLICTING_PIT_RECORDS")
            if lane is None or lane <= 0:
                flags.append("INVALID_LANE_DURATION")
            if stop is not None and (stop < 0 or lane is None or stop > lane):
                flags.append("INVALID_STATIONARY_DURATION")
            candidates = []
            for old, new in pairwise(by_stint):
                outlap = integer(new.get("lap_start"))
                inlap = integer(old.get("lap_end"))
                if outlap is None or inlap is None or outlap != inlap + 1:
                    continue
                if rawlap is None or min(abs(rawlap-inlap), abs(rawlap-outlap)) > 1:
                    continue
                ilap, olap = by_lap.get(inlap), by_lap.get(outlap)
                if ilap is None or olap is None:
                    continue
                if olap.get("is_pit_out_lap") not in (True, 1):
                    continue
                begin = utc(ilap.get("date_start"))
                out_begin = utc(olap.get("date_start"))
                out_t = number(olap.get("lap_duration"))
                finish = out_begin + timedelta(seconds=out_t) if out_begin is not None and out_t else None
                if (timestamp is not None and begin is not None and finish is not None
                        and not begin - timedelta(seconds=2) <= timestamp <= finish + timedelta(seconds=2)):
                    continue
                candidates.append((old, new, ilap, olap, begin, finish))
            if len(candidates) != 1:
                flags.append("AMBIGUOUS_PIT_LAP_MAPPING" if candidates else "MISSING_STINT_OR_OUT_MARKER")
            old = new = ilap = olap = {}
            begin = finish = None
            if len(candidates) == 1:
                old, new, ilap, olap, begin, finish = candidates[0]
                if rawlap not in {integer(ilap.get("lap_number")), integer(olap.get("lap_number"))}:
                    flags.append("PIT_RECORD_LAP_DISAGREEMENT")
                if begin is None or finish is None:
                    flags.append("MISSING_LAP_BOUNDARIES")
                in_time, out_time = number(ilap.get("lap_duration")), number(olap.get("lap_duration"))
                out_start = utc(olap.get("date_start"))
                if in_time is None or out_time is None or min(in_time, out_time) <= 0:
                    flags.append("INVALID_TRANSITION_LAP_DURATION")
                if (begin is not None and in_time and out_start is not None
                        and abs((out_start-begin).total_seconds()-in_time) > 2.0):
                    flags.append("LAP_BOUNDARY_DISAGREEMENT")
                if (timestamp is not None and lane and finish is not None and begin is not None
                        and (timestamp > finish + timedelta(seconds=2)
                             or timestamp-timedelta(seconds=lane) < begin-timedelta(seconds=2))):
                    flags.append("LANE_WINDOW_OUTSIDE_TRANSITION")
            session_start, session_end = utc(session.get("date_start")), utc(session.get("date_end"))
            if (timestamp is not None and session_start is not None and session_end is not None
                    and not session_start <= timestamp <= session_end):
                flags.append("PIT_OUTSIDE_SESSION")
            state, overlaps = status_overlap(intervals, begin, finish)
            lane_end = timestamp
            lane_start = timestamp-timedelta(seconds=lane) if timestamp is not None and lane and lane > 0 else None
            lane_state, lane_overlaps = status_overlap(intervals, lane_start, lane_end)
            event_type, evidence = ("ROUTINE_TIRE_CHANGE", "VERIFIED_STINT_TRANSITION") if new else (
                "UNCLASSIFIED", "NO_VERIFIED_TIRE_TRANSITION")
            if not annotations.empty:
                ann = annotations[annotations.pit_event_id == event_id]
                if len(ann) == 1 and str(ann.iloc[0].get("evidence_source", "")).strip():
                    event_type = str(ann.iloc[0]["event_type"])
                    evidence = str(ann.iloc[0]["evidence_source"])
            if state == "RED":
                event_type, evidence = "RED_FLAG_EVENT", "RACE_CONTROL_TIMELINE"
            if flags:
                quality = "AMBIGUOUS_TIMING"
            elif state == "MIXED":
                quality = "MIXED_TRACK_STATE"
            elif state in {"GREEN", "VSC", "SC"} and event_type == "ROUTINE_TIRE_CHANGE":
                quality = f"VALID_ROUTINE_{state}"
            elif event_type != "ROUTINE_TIRE_CHANGE":
                quality = "VALID_EXCEPTIONAL"
            else:
                quality = "UNSUPPORTED_EVENT"
            old_age = integer(old.get("tyre_age_at_start"))
            if old_age is not None and old:
                old_age += int(old["lap_end"])-int(old["lap_start"])+1
            duplicate_count = int(duplicates[pit["_sort"]])
            record = {
                "pit_event_id": event_id, "session_key": sid, "driver_number": int(driver),
                "pit_sequence": sequence, "pit_record_lap": rawlap,
                "resolved_in_lap": integer(ilap.get("lap_number")),
                "resolved_out_lap": integer(olap.get("lap_number")),
                "old_stint_id": f"{sid}:{int(driver)}:{old['stint_number']}" if old else None,
                "new_stint_id": f"{sid}:{int(driver)}:{new['stint_number']}" if new else None,
                "old_compound": old.get("compound"), "new_compound": new.get("compound"),
                "old_tyre_age": old_age,
                "new_tyre_initial_age": integer(new.get("tyre_age_at_start")),
                "lane_duration_s": lane, "stop_duration_s": stop,
                "moving_lane_duration_s": lane-stop if lane is not None and stop is not None else None,
                "pit_timestamp_utc": timestamp, "estimated_lane_entry_utc": lane_start,
                "estimated_lane_exit_utc": lane_end,
                "lane_boundary_method": "BACKWARDS_FROM_PROVIDER_REPORT_TIME_APPROXIMATE",
                "transition_start_utc": begin, "transition_end_utc": finish,
                "observed_in_lap_s": number(ilap.get("lap_duration")),
                "observed_out_lap_s": number(olap.get("lap_duration")),
                "season": integer(session.get("year")),
                "circuit_key": integer(session.get("circuit_key")),
                "circuit_short_name": session.get("circuit_short_name"),
                "team_id": str(drv.iloc[0].team_name) if not drv.empty else "UNKNOWN_TEAM",
                "track_state_at_pit": status_at(intervals, lane_start),
                "track_state_at_exit": status_at(intervals, lane_end),
                "transition_track_status": state, "lane_track_status": lane_state,
                "state_overlap_seconds_json": overlaps,
                "lane_state_overlap_seconds_json": lane_overlaps,
                "pit_event_type": event_type, "classification_evidence": evidence,
                "timing_quality": "B" if not flags else "C",
                "timestamp_precision": "APPROXIMATE_PROVIDER_EVENT_AND_LAP_STARTS",
                "quality_category": quality, "quality_flags_json": json.dumps(sorted(flags)),
                "raw_duplicate_count": duplicate_count,
                "eligible_direct_lane": lane is not None and lane > 0 and not flags,
                "eligible_primary_label": quality == "VALID_ROUTINE_GREEN",
                "possible_double_stack": False,
            }
            result.append(record)
    frame = pd.DataFrame(result)
    # Arrival proximity is evidence of a possible shared box delay, not a diagnosis.
    if not frame.empty:
        for _, group in frame.groupby(["session_key", "team_id"]):
            for a in group.to_dict("records"):
                for b in group.to_dict("records"):
                    if a["driver_number"] == b["driver_number"]:
                        continue
                    ta, tb = utc(a["pit_timestamp_utc"]), utc(b["pit_timestamp_utc"])
                    if ta is not None and tb is not None and abs((ta-tb).total_seconds()) < 5:
                        frame.loc[frame.pit_event_id == a["pit_event_id"], "possible_double_stack"] = True
        mask = frame.possible_double_stack
        frame.loc[mask, "eligible_primary_label"] = False
    return apply_pit_eligibility(add_transition_evidence(frame, laps, race_control))


def add_transition_evidence(
    events: pd.DataFrame, laps: pd.DataFrame, race_control: pd.DataFrame | None,
) -> pd.DataFrame:
    """Recorded traffic/other-visit evidence, not the size of a forecast residual."""
    events = events.copy()
    events["blue_flag_overlap"] = False
    events["multiple_pit_transitions"] = False
    if events.empty:
        return events
    rc = race_control if race_control is not None else pd.DataFrame()
    for index, event in events.iterrows():
        start, end = utc(event.get("transition_start_utc")), utc(event.get("transition_end_utc"))
        if start is None or end is None:
            continue
        inlap = laps[(laps.driver_number == event.driver_number)
                     & (laps.lap_number == event.resolved_in_lap)]
        if not inlap.empty and inlap.iloc[0].get("is_pit_out_lap") in (True, 1):
            events.at[index, "multiple_pit_transitions"] = True
        same = events[(events.driver_number == event.driver_number)
                      & (events.session_key == event.session_key)]
        report_times = pd.to_datetime(same.pit_timestamp_utc, utc=True, errors="coerce")
        if int(((report_times >= start) & (report_times < end)).sum()) > 1:
            events.at[index, "multiple_pit_transitions"] = True
        if not rc.empty:
            dates = pd.to_datetime(rc.date, utc=True, format="ISO8601")
            match = (dates >= start) & (dates < end) & (rc.driver_number == event.driver_number)
            match &= rc.flag.fillna("").str.upper().eq("BLUE")
            events.at[index, "blue_flag_overlap"] = bool(match.any())
    return events


def load_pit_annotations(root: Path) -> pd.DataFrame:
    path = root / "data/raw/reference/pit_event_annotations.csv"
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_csv(path)
    if frame.pit_event_id.duplicated().any() or frame.evidence_source.isna().any():
        raise ValueError("Pit annotations require unique event IDs and recorded evidence")
    return frame


def populate_session_pit_events(
    con: duckdb.DuckDBPyConnection, session_key: int, annotations: pd.DataFrame | None = None,
) -> pd.DataFrame:
    from f1ri.warehouse.schema import PIT_EVENT_SCHEMA
    frames = {name: con.execute(f"SELECT * FROM silver.{name} WHERE session_key=?", [session_key]).df()
              for name in ("pit", "laps", "stints", "sessions", "drivers", "race_control")}
    intervals = con.execute("SELECT * FROM gold.track_status_intervals WHERE session_key=?", [session_key]).df()
    frame = reconstruct_pit_events(
        frames["pit"], frames["laps"], frames["stints"],
        frames["sessions"].iloc[0].to_dict(), intervals, frames["drivers"], annotations, frames["race_control"],
    ) if not frames["sessions"].empty else pd.DataFrame()
    replace_session_frame(con, "gold.pit_events", frame, PIT_EVENT_SCHEMA, session_key)
    return frame
