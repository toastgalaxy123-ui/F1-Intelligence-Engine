"""Point-in-time context: track-status intervals, pit masking, as-of traffic gaps and weather."""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import duckdb
import pandas as pd

STATUS_SEVERITY: dict[str, int] = {
    "GREEN": 0,
    "YELLOW": 1,
    "DOUBLE_YELLOW": 2,
    "UNKNOWN": 3,
    "VSC": 4,
    "SC": 5,
    "RED": 6,
}

DEFAULT_CLEAN_AIR_GAP_S = 2.0
SECTOR_RE = re.compile(r"SECTOR\s+(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class TrackStatusInterval:
    session_key: int
    start_utc: datetime
    end_utc: datetime
    status: str
    source_rule: str
    confidence: str


def _extract_sector_key(message: str) -> str:
    match = SECTOR_RE.search(message or "")
    return match.group(1) if match else "ALL"


def _effective_state(
    initialized: bool,
    neutralization: str | None,
    active_sector_flags: dict[str, str],
) -> tuple[str, str]:
    if not initialized:
        return "UNKNOWN", "LOW"
    if neutralization == "RED":
        return "RED", "HIGH"
    if neutralization == "SC":
        return "SC", "HIGH"
    if neutralization == "VSC":
        return "VSC", "HIGH"
    if neutralization == "UNKNOWN":
        return "UNKNOWN", "LOW"
    if "DOUBLE_YELLOW" in active_sector_flags.values():
        return "DOUBLE_YELLOW", "HIGH"
    if "YELLOW" in active_sector_flags.values():
        return "YELLOW", "HIGH"
    return "GREEN", "HIGH"


def build_track_status_intervals(
    session_key: int,
    race_control_events: Sequence[Mapping[str, Any]],
    session_start_utc: datetime | None,
    session_end_utc: datetime | None,
) -> list[TrackStatusInterval]:
    """Reconstruct continuous, non-overlapping track-status intervals from race-control events.

    Never assumes GREEN when race-control data is missing or uninitialized.
    Tracks track-wide neutralization (RED, SC, VSC) separately from sector-level
    yellow/double-yellow flags so clearing a sector during an active VSC/SC never
    prematurely restores GREEN.
    """
    if session_start_utc is None or session_end_utc is None or session_end_utc <= session_start_utc:
        return []

    valid_events = [
        e for e in race_control_events
        if e.get("date") is not None and isinstance(e.get("date"), datetime)
    ]
    if not valid_events:
        return [
            TrackStatusInterval(
                session_key=session_key,
                start_utc=session_start_utc,
                end_utc=session_end_utc,
                status="UNKNOWN",
                source_rule="NO_RACE_CONTROL_EVENTS",
                confidence="LOW",
            )
        ]

    sorted_events = sorted(valid_events, key=lambda e: e["date"])

    # Determine whether race start is explicitly signaled near or before session_start_utc.
    initialized = False
    neutralization: str | None = None
    active_sector_flags: dict[str, str] = {}
    current_rule = "UNINITIALIZED_BEFORE_FIRST_EVENT"

    start_tolerance = session_start_utc + timedelta(seconds=5)
    for ev in sorted_events:
        ev_time: datetime = ev["date"]
        if ev_time > start_tolerance:
            break
        cat = (ev.get("category") or "").strip()
        flag = (ev.get("flag") or "").strip().upper()
        scope = (ev.get("scope") or "").strip()
        msg = (ev.get("message") or "").strip().upper()
        if (cat == "SessionStatus" and "SESSION STARTED" in msg) or (
            cat == "Flag" and flag == "GREEN" and scope == "Track"
        ):
            initialized = True
            neutralization = None
            active_sector_flags.clear()
            current_rule = "SESSION_STARTED"

    current_status, current_conf = _effective_state(
        initialized, neutralization, active_sector_flags
    )
    current_start = session_start_utc
    intervals: list[TrackStatusInterval] = []

    for ev in sorted_events:
        ev_time = ev["date"]
        if ev_time < session_start_utc and initialized:
            # Pre-start reconnaissance events are superseded once the race starts;
            # only process if we were not initialized by an explicit SESSION STARTED.
            continue
        if ev_time > session_end_utc:
            break

        cat = (ev.get("category") or "").strip()
        flag = (ev.get("flag") or "").strip().upper()
        scope = (ev.get("scope") or "").strip()
        msg = (ev.get("message") or "").strip().upper()

        rule_candidate: str | None = None

        if cat == "SessionStatus":
            if "SESSION STARTED" in msg or "SESSION RESUMED" in msg:
                initialized = True
                neutralization = None
                active_sector_flags.clear()
                rule_candidate = "SESSION_STARTED"
            elif "ABORTED" in msg or "SUSPENDED" in msg:
                initialized = True
                neutralization = "RED"
                active_sector_flags.clear()
                rule_candidate = "SESSION_SUSPENDED"
            elif "SESSION FINISHED" in msg:
                continue
            else:
                initialized = True
                neutralization = "UNKNOWN"
                rule_candidate = "UNKNOWN_SESSION_STATUS"

        elif cat == "SafetyCar":
            initialized = True
            if "VIRTUAL SAFETY CAR DEPLOYED" in msg or "VSC DEPLOYED" in msg:
                if neutralization not in ("SC", "RED"):
                    neutralization = "VSC"
                    rule_candidate = "VSC_DEPLOYED"
            elif "VIRTUAL SAFETY CAR ENDING" in msg or "VSC ENDING" in msg:
                # VSC remains in effect until TRACK CLEAR / GREEN.
                if neutralization is None:
                    neutralization = "VSC"
                    rule_candidate = "VSC_ENDING"
            elif "SAFETY CAR DEPLOYED" in msg or "SC DEPLOYED" in msg:
                if neutralization != "RED":
                    neutralization = "SC"
                    rule_candidate = "SC_DEPLOYED"
            elif "SAFETY CAR IN THIS LAP" in msg or "SC IN THIS LAP" in msg:
                # SC remains in effect until cars cross the line / TRACK CLEAR.
                if neutralization is None:
                    neutralization = "SC"
                    rule_candidate = "SC_IN_THIS_LAP"
            else:
                neutralization = "UNKNOWN"
                rule_candidate = "UNRECOGNIZED_SAFETY_CAR_EVENT"

        elif cat == "Flag":
            # Driver-scoped flags (BLUE, BLACK AND WHITE, etc.) do not change track status.
            if scope == "Driver" or flag in (
                "BLUE",
                "BLACK AND WHITE",
                "BLACK",
                "BLACK AND ORANGE",
            ):
                continue
            if flag == "CHEQUERED":
                continue
            initialized = True
            if flag == "RED":
                neutralization = "RED"
                active_sector_flags.clear()
                rule_candidate = "RED_FLAG"
            elif flag == "GREEN" and (scope == "Track" or not scope):
                neutralization = None
                active_sector_flags.clear()
                rule_candidate = "TRACK_GREEN"
            elif flag == "CLEAR" and (scope == "Track" or "TRACK CLEAR" in msg):
                neutralization = None
                active_sector_flags.clear()
                rule_candidate = "TRACK_CLEAR"
            elif flag == "DOUBLE YELLOW":
                sec = _extract_sector_key(msg)
                active_sector_flags[sec] = "DOUBLE_YELLOW"
                rule_candidate = "SECTOR_DOUBLE_YELLOW"
            elif flag == "YELLOW":
                sec = _extract_sector_key(msg)
                # Do not downgrade a sector already under DOUBLE_YELLOW unless cleared.
                if active_sector_flags.get(sec) != "DOUBLE_YELLOW":
                    active_sector_flags[sec] = "YELLOW"
                rule_candidate = "SECTOR_YELLOW"
            elif flag == "CLEAR" and scope == "Sector":
                sec = _extract_sector_key(msg)
                if sec == "ALL":
                    active_sector_flags.clear()
                else:
                    active_sector_flags.pop(sec, None)
                rule_candidate = "SECTOR_CLEAR"
            else:
                neutralization = "UNKNOWN"
                rule_candidate = "UNRECOGNIZED_FLAG_EVENT"
        else:
            # Categories like Drs or Other do not directly change track status unless RED FLAG.
            continue

        new_status, new_conf = _effective_state(initialized, neutralization, active_sector_flags)
        clamp_time = max(ev_time, current_start)
        if new_status != current_status or new_conf != current_conf:
            if clamp_time > current_start:
                intervals.append(
                    TrackStatusInterval(
                        session_key=session_key,
                        start_utc=current_start,
                        end_utc=clamp_time,
                        status=current_status,
                        source_rule=current_rule,
                        confidence=current_conf,
                    )
                )
            current_start = clamp_time
            current_status = new_status
            current_conf = new_conf
            if rule_candidate is not None:
                current_rule = rule_candidate

    if session_end_utc > current_start:
        intervals.append(
            TrackStatusInterval(
                session_key=session_key,
                start_utc=current_start,
                end_utc=session_end_utc,
                status=current_status,
                source_rule=current_rule,
                confidence=current_conf,
            )
        )

    return intervals


def populate_session_lap_context(
    con: duckdb.DuckDBPyConnection,
    session_key: int,
    clean_air_gap_s: float = DEFAULT_CLEAN_AIR_GAP_S,
) -> None:
    """Populate gold.track_status_intervals and gold.lap_context for a single session_key."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS gold.track_status_intervals (
            session_key BIGINT,
            start_utc TIMESTAMPTZ,
            end_utc TIMESTAMPTZ,
            status VARCHAR,
            source_rule VARCHAR,
            confidence VARCHAR
        )
    """)
    con.execute("DELETE FROM gold.track_status_intervals WHERE session_key = ?", [session_key])

    bounds = con.execute(
        """
        SELECT
            COALESCE(
                MIN(l.date_start),
                (SELECT MIN(s.date_start) FROM silver.sessions s WHERE s.session_key = ?)
            ) AS min_start_utc,
            COALESCE(
                MAX(
                    CASE
                        WHEN l.date_start IS NOT NULL AND l.lap_duration IS NOT NULL AND l.lap_duration > 0
                        THEN l.date_start + to_milliseconds(CAST(ROUND(l.lap_duration * 1000) AS BIGINT))
                        ELSE l.date_start
                    END
                ),
                (SELECT MAX(s.date_end) FROM silver.sessions s WHERE s.session_key = ?)
            ) AS max_end_utc
        FROM silver.laps l
        WHERE l.session_key = ?
        """,
        [session_key, session_key, session_key],
    ).fetchone()

    session_start_utc = bounds[0] if bounds else None
    session_end_utc = bounds[1] if bounds else None
    if (
        session_start_utc is not None
        and session_end_utc is not None
        and session_end_utc <= session_start_utc
    ):
        session_end_utc = session_start_utc + timedelta(seconds=1)

    rc_rows = con.execute(
        """
        SELECT date, lap_number, driver_number, category, flag, scope, message
        FROM silver.race_control
        WHERE session_key = ?
        ORDER BY date ASC NULLS LAST
        """,
        [session_key],
    ).fetchall()
    rc_dicts = [
        {
            "date": r[0],
            "lap_number": r[1],
            "driver_number": r[2],
            "category": r[3],
            "flag": r[4],
            "scope": r[5],
            "message": r[6],
        }
        for r in rc_rows
    ]

    intervals = build_track_status_intervals(
        session_key=session_key,
        race_control_events=rc_dicts,
        session_start_utc=session_start_utc,
        session_end_utc=session_end_utc,
    )
    if intervals:
        iv_df = pd.DataFrame([iv.__dict__ for iv in intervals])
        con.register("_incoming_intervals", iv_df)
        try:
            con.execute("""
                INSERT INTO gold.track_status_intervals
                SELECT
                    CAST(session_key AS BIGINT),
                    CAST(start_utc AS TIMESTAMPTZ),
                    CAST(end_utc AS TIMESTAMPTZ),
                    CAST(status AS VARCHAR),
                    CAST(source_rule AS VARCHAR),
                    CAST(confidence AS VARCHAR)
                FROM _incoming_intervals
            """)
        finally:
            con.unregister("_incoming_intervals")

    # Ensure optional silver tables exist even if --full was not passed during ingest.
    con.execute("""
        CREATE TABLE IF NOT EXISTS silver.position (
            session_key BIGINT,
            meeting_key BIGINT,
            driver_number INTEGER,
            date TIMESTAMPTZ,
            position INTEGER
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS silver.intervals (
            session_key BIGINT,
            meeting_key BIGINT,
            driver_number INTEGER,
            date TIMESTAMPTZ,
            gap_to_leader_raw VARCHAR,
            interval_raw VARCHAR
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS gold.lap_context (
            session_key BIGINT,
            meeting_key BIGINT,
            driver_number INTEGER,
            season INTEGER,
            circuit_key BIGINT,
            circuit_short_name VARCHAR,
            regulation_era VARCHAR,
            lap_number INTEGER,
            race_total_laps INTEGER,
            lap_start_utc TIMESTAMPTZ,
            lap_end_estimate_utc TIMESTAMPTZ,
            elapsed_race_time_s DOUBLE,
            observed_lap_time_s DOUBLE,
            sector_1_s DOUBLE,
            sector_2_s DOUBLE,
            sector_3_s DOUBLE,
            stint_number INTEGER,
            compound VARCHAR,
            tyre_age_at_lap_start INTEGER,
            fuel_load_proxy DOUBLE,
            track_temperature_c DOUBLE,
            air_temperature_c DOUBLE,
            rainfall BOOLEAN,
            position_at_lap_start INTEGER,
            gap_to_leader_at_start_raw VARCHAR,
            gap_to_leader_at_start_s DOUBLE,
            gap_ahead_at_start_raw VARCHAR,
            gap_ahead_at_start_s DOUBLE,
            track_gap_ahead_at_start_s DOUBLE,
            track_gap_ahead_at_end_s DOUBLE,
            had_in_lap_on_track_pass BOOLEAN,
            is_blue_flagged_lap BOOLEAN,
            is_clean_air_at_start BOOLEAN,
            is_clean_air BOOLEAN,
            is_pit_in_lap BOOLEAN,
            is_pit_out_lap BOOLEAN,
            is_pit_lap BOOLEAN,
            stint_pit_mismatch BOOLEAN,
            track_status VARCHAR,
            confirmed_green_lap BOOLEAN,
            non_green_overlap_s DOUBLE,
            is_clean_green_pace_lap BOOLEAN,
            exclusion_reason VARCHAR
        )
    """)
    con.execute("DELETE FROM gold.lap_context WHERE session_key = ?", [session_key])

    con.execute(
        """
        INSERT INTO gold.lap_context
        WITH session_bounds AS (
            SELECT
                ?::BIGINT AS session_key,
                MIN(date_start) AS race_start_utc,
                GREATEST(MAX(lap_number), 1) AS race_total_laps
            FROM silver.laps
            WHERE session_key = ?
        ),
        stint_bounds AS (
            SELECT
                session_key,
                driver_number,
                MAX(stint_number) AS max_stint_number
            FROM silver.stints
            WHERE session_key = ?
            GROUP BY session_key, driver_number
        ),
        lap_base AS (
            SELECT
                l.session_key,
                l.meeting_key,
                l.driver_number,
                sess.year AS season,
                sess.circuit_key,
                sess.circuit_short_name,
                CASE
                    WHEN sess.year >= 2026 THEN '2026_PLUS'
                    WHEN sess.year BETWEEN 2023 AND 2025 THEN '2023_2025'
                    ELSE 'OTHER'
                END AS regulation_era,
                l.lap_number,
                sb.race_total_laps,
                COALESCE(l.date_start, CASE WHEN l.lap_number = 1 THEN sb.race_start_utc END)
                    AS lap_start_utc,
                LEAD(l.date_start) OVER (
                    PARTITION BY l.session_key, l.driver_number ORDER BY l.lap_number
                ) AS next_lap_start_utc,
                sb.race_start_utc,
                l.lap_duration AS observed_lap_time_s,
                l.duration_sector_1 AS sector_1_s,
                l.duration_sector_2 AS sector_2_s,
                l.duration_sector_3 AS sector_3_s,
                s.stint_number,
                s.compound,
                s.tyre_age_at_start + l.lap_number - s.lap_start AS tyre_age_at_lap_start,
                s.lap_start AS stint_lap_start,
                s.lap_end AS stint_lap_end,
                stb.max_stint_number,
                COALESCE(l.is_pit_out_lap, FALSE) AS raw_pit_out_lap,
                EXISTS (
                    SELECT 1 FROM silver.pit p
                    WHERE p.session_key = l.session_key
                      AND p.driver_number = l.driver_number
                      AND p.lap_number = l.lap_number
                ) AS pit_on_this_lap,
                EXISTS (
                    SELECT 1 FROM silver.pit p
                    WHERE p.session_key = l.session_key
                      AND p.driver_number = l.driver_number
                      AND p.lap_number = l.lap_number - 1
                ) AS pit_on_prev_lap
            FROM silver.laps l
            CROSS JOIN session_bounds sb
            LEFT JOIN silver.sessions sess
              ON l.session_key = sess.session_key
            LEFT JOIN silver.stints s
              ON l.session_key = s.session_key
             AND l.driver_number = s.driver_number
             AND l.lap_number BETWEEN s.lap_start AND s.lap_end
            LEFT JOIN stint_bounds stb
              ON l.session_key = stb.session_key
             AND l.driver_number = stb.driver_number
            WHERE l.session_key = ?
        ),
        lap_windows AS (
            SELECT
                lb.*,
                COALESCE(
                    CASE
                        WHEN lb.lap_start_utc IS NOT NULL
                         AND lb.observed_lap_time_s IS NOT NULL
                         AND lb.observed_lap_time_s > 0
                        THEN lb.lap_start_utc + to_milliseconds(
                            CAST(ROUND(lb.observed_lap_time_s * 1000) AS BIGINT)
                        )
                    END,
                    lb.next_lap_start_utc
                ) AS lap_end_estimate_utc,
                (
                    lb.pit_on_this_lap
                    OR (
                        lb.stint_number IS NOT NULL
                        AND lb.max_stint_number IS NOT NULL
                        AND lb.stint_number < lb.max_stint_number
                        AND lb.lap_number = lb.stint_lap_end
                    )
                ) AS is_pit_in_lap,
                (
                    lb.raw_pit_out_lap
                    OR lb.pit_on_prev_lap
                    OR (
                        lb.stint_number IS NOT NULL
                        AND lb.lap_number > 1
                        AND lb.lap_number = lb.stint_lap_start
                    )
                ) AS is_pit_out_lap,
                (
                    lb.stint_number IS NOT NULL
                    AND lb.max_stint_number IS NOT NULL
                    AND lb.stint_number < lb.max_stint_number
                    AND lb.lap_number = lb.stint_lap_end
                    AND NOT lb.pit_on_this_lap
                ) AS stint_end_without_pit_record
            FROM lap_base lb
        ),
        lap_status AS (
            SELECT
                lw.session_key,
                lw.driver_number,
                lw.lap_number,
                COUNT(iv.status) AS matched_intervals,
                COALESCE(
                    SUM(
                        CASE
                            WHEN iv.status <> 'GREEN' THEN GREATEST(
                                0.0,
                                epoch(
                                    LEAST(lw.lap_end_estimate_utc, iv.end_utc)
                                    - GREATEST(lw.lap_start_utc, iv.start_utc)
                                )
                            )
                            ELSE 0.0
                        END
                    ),
                    0.0
                ) AS non_green_overlap_s,
                MAX(
                    CASE iv.status
                        WHEN 'RED' THEN 6
                        WHEN 'SC' THEN 5
                        WHEN 'VSC' THEN 4
                        WHEN 'UNKNOWN' THEN 3
                        WHEN 'DOUBLE_YELLOW' THEN 2
                        WHEN 'YELLOW' THEN 1
                        WHEN 'GREEN' THEN 0
                        ELSE 3
                    END
                ) AS max_severity
            FROM lap_windows lw
            LEFT JOIN gold.track_status_intervals iv
              ON lw.session_key = iv.session_key
             AND lw.lap_start_utc IS NOT NULL
             AND lw.lap_end_estimate_utc IS NOT NULL
             AND iv.start_utc < lw.lap_end_estimate_utc
             AND iv.end_utc > lw.lap_start_utc
            GROUP BY lw.session_key, lw.driver_number, lw.lap_number
        ),
        lap_traffic AS (
            SELECT
                a.session_key,
                a.driver_number,
                a.lap_number,
                MIN(
                    CASE
                        WHEN b.lap_start_utc <= a.lap_start_utc
                         AND a.lap_start_utc - b.lap_start_utc <= INTERVAL '10 seconds'
                         AND NOT b.is_pit_out_lap
                         AND epoch(a.lap_start_utc - b.lap_start_utc) > 0.0
                        THEN epoch(a.lap_start_utc - b.lap_start_utc)
                    END
                ) AS track_gap_ahead_at_start_s,
                MIN(
                    CASE
                        WHEN b.lap_end_estimate_utc <= a.lap_end_estimate_utc
                         AND a.lap_end_estimate_utc - b.lap_end_estimate_utc <= INTERVAL '10 seconds'
                         AND NOT b.is_pit_in_lap
                         AND epoch(a.lap_end_estimate_utc - b.lap_end_estimate_utc) > 0.0
                        THEN epoch(a.lap_end_estimate_utc - b.lap_end_estimate_utc)
                    END
                ) AS track_gap_ahead_at_end_s,
                COALESCE(
                    BOOL_OR(
                        CASE
                            WHEN b.lap_start_utc IS NOT NULL
                             AND b.lap_end_estimate_utc IS NOT NULL
                             AND NOT b.is_pit_in_lap
                             AND NOT b.is_pit_out_lap
                             AND (
                                 (b.lap_start_utc < a.lap_start_utc AND b.lap_end_estimate_utc >= a.lap_end_estimate_utc)
                                 OR (b.lap_start_utc > a.lap_start_utc AND b.lap_end_estimate_utc <= a.lap_end_estimate_utc)
                             )
                            THEN TRUE
                            ELSE FALSE
                        END
                    ),
                    FALSE
                ) AS had_in_lap_on_track_pass
            FROM lap_windows a
            LEFT JOIN lap_windows b
              ON a.session_key = b.session_key
             AND a.driver_number <> b.driver_number
             AND a.lap_start_utc IS NOT NULL
             AND a.lap_end_estimate_utc IS NOT NULL
             AND b.lap_start_utc IS NOT NULL
             AND b.lap_end_estimate_utc IS NOT NULL
             AND b.lap_start_utc <= a.lap_end_estimate_utc + INTERVAL '10 seconds'
             AND b.lap_end_estimate_utc >= a.lap_start_utc - INTERVAL '10 seconds'
            GROUP BY a.session_key, a.driver_number, a.lap_number
        ),
        enriched AS (
            SELECT
                lw.*,
                CASE
                    WHEN lw.lap_start_utc IS NOT NULL AND lw.race_start_utc IS NOT NULL
                    THEN GREATEST(0.0, epoch(lw.lap_start_utc - lw.race_start_utc))
                END AS elapsed_race_time_s,
                GREATEST(
                    0.0,
                    1.0 - CAST(lw.lap_number - 1 AS DOUBLE) / CAST(lw.race_total_laps AS DOUBLE)
                ) AS fuel_load_proxy,
                (
                    SELECT w.track_temperature
                    FROM silver.weather w
                    WHERE w.session_key = lw.session_key
                      AND lw.lap_start_utc IS NOT NULL
                      AND w.date <= lw.lap_start_utc
                    ORDER BY w.date DESC
                    LIMIT 1
                ) AS track_temperature_c,
                (
                    SELECT w.air_temperature
                    FROM silver.weather w
                    WHERE w.session_key = lw.session_key
                      AND lw.lap_start_utc IS NOT NULL
                      AND w.date <= lw.lap_start_utc
                    ORDER BY w.date DESC
                    LIMIT 1
                ) AS air_temperature_c,
                (
                    SELECT w.rainfall
                    FROM silver.weather w
                    WHERE w.session_key = lw.session_key
                      AND lw.lap_start_utc IS NOT NULL
                      AND w.date <= lw.lap_start_utc
                    ORDER BY w.date DESC
                    LIMIT 1
                ) AS rainfall,
                COALESCE(
                    (
                        SELECT p.position
                        FROM silver.position p
                        WHERE p.session_key = lw.session_key
                          AND p.driver_number = lw.driver_number
                          AND lw.lap_start_utc IS NOT NULL
                          AND p.date <= lw.lap_start_utc
                        ORDER BY p.date DESC
                        LIMIT 1
                    ),
                    (
                        SELECT g.position
                        FROM silver.starting_grid g
                        WHERE g.session_key = lw.session_key
                          AND g.driver_number = lw.driver_number
                        LIMIT 1
                    )
                ) AS position_at_lap_start,
                (
                    SELECT i.gap_to_leader_raw
                    FROM silver.intervals i
                    WHERE i.session_key = lw.session_key
                      AND i.driver_number = lw.driver_number
                      AND lw.lap_start_utc IS NOT NULL
                      AND i.date <= lw.lap_start_utc
                    ORDER BY i.date DESC
                    LIMIT 1
                ) AS gap_to_leader_at_start_raw,
                (
                    SELECT i.interval_raw
                    FROM silver.intervals i
                    WHERE i.session_key = lw.session_key
                      AND i.driver_number = lw.driver_number
                      AND lw.lap_start_utc IS NOT NULL
                      AND i.date <= lw.lap_start_utc
                    ORDER BY i.date DESC
                    LIMIT 1
                ) AS gap_ahead_at_start_raw,
                EXISTS (
                    SELECT 1
                    FROM silver.race_control rc
                    WHERE rc.session_key = lw.session_key
                      AND rc.driver_number = lw.driver_number
                      AND rc.flag = 'BLUE'
                      AND lw.lap_start_utc IS NOT NULL
                      AND lw.lap_end_estimate_utc IS NOT NULL
                      AND rc.date >= lw.lap_start_utc
                      AND rc.date <= lw.lap_end_estimate_utc
                ) AS is_blue_flagged_lap,
                lt.track_gap_ahead_at_start_s,
                lt.track_gap_ahead_at_end_s,
                lt.had_in_lap_on_track_pass,
                CASE
                    WHEN ls.matched_intervals IS NULL OR ls.matched_intervals = 0 THEN 'UNKNOWN'
                    WHEN ls.max_severity = 6 THEN 'RED'
                    WHEN ls.max_severity = 5 THEN 'SC'
                    WHEN ls.max_severity = 4 THEN 'VSC'
                    WHEN ls.max_severity = 3 THEN 'UNKNOWN'
                    WHEN ls.max_severity = 2 THEN 'DOUBLE_YELLOW'
                    WHEN ls.max_severity = 1 THEN 'YELLOW'
                    WHEN ls.max_severity = 0 THEN 'GREEN'
                    ELSE 'UNKNOWN'
                END AS track_status,
                (
                    ls.matched_intervals > 0
                    AND ls.max_severity = 0
                    AND ls.non_green_overlap_s = 0.0
                    AND lw.lap_start_utc IS NOT NULL
                    AND lw.lap_end_estimate_utc IS NOT NULL
                ) AS confirmed_green_lap,
                ls.non_green_overlap_s
            FROM lap_windows lw
            LEFT JOIN lap_status ls
              ON lw.session_key = ls.session_key
             AND lw.driver_number = ls.driver_number
             AND lw.lap_number = ls.lap_number
            LEFT JOIN lap_traffic lt
              ON lw.session_key = lt.session_key
             AND lw.driver_number = lt.driver_number
             AND lw.lap_number = lt.lap_number
        ),
        typed_gaps AS (
            SELECT
                e.*,
                CASE
                    WHEN e.position_at_lap_start = 1 THEN 0.0
                    ELSE TRY_CAST(e.gap_to_leader_at_start_raw AS DOUBLE)
                END AS gap_to_leader_at_start_s,
                CASE
                    WHEN e.position_at_lap_start = 1 THEN NULL
                    WHEN TRY_CAST(e.gap_ahead_at_start_raw AS DOUBLE) > 0.0
                    THEN TRY_CAST(e.gap_ahead_at_start_raw AS DOUBLE)
                    ELSE NULL
                END AS gap_ahead_at_start_s,
                (e.is_pit_in_lap OR e.is_pit_out_lap) AS is_pit_lap,
                (
                    e.stint_end_without_pit_record
                    AND e.track_status <> 'RED'
                ) AS stint_pit_mismatch
            FROM enriched e
        ),
        clean_air_eval AS (
            SELECT
                tg.*,
                CASE
                    WHEN tg.lap_start_utc IS NULL THEN NULL
                    WHEN tg.position_at_lap_start = 1 THEN
                        (tg.track_gap_ahead_at_start_s IS NULL OR tg.track_gap_ahead_at_start_s >= ?)
                    WHEN tg.gap_ahead_at_start_s IS NOT NULL THEN
                        (tg.gap_ahead_at_start_s >= ?
                         AND (tg.track_gap_ahead_at_start_s IS NULL OR tg.track_gap_ahead_at_start_s >= ?))
                    ELSE
                        (tg.track_gap_ahead_at_start_s IS NULL OR tg.track_gap_ahead_at_start_s >= ?)
                END AS is_clean_air_at_start
            FROM typed_gaps tg
        ),
        final_eval AS (
            SELECT
                ca.*,
                CASE
                    WHEN ca.lap_start_utc IS NULL OR ca.lap_end_estimate_utc IS NULL THEN NULL
                    ELSE (
                        COALESCE(ca.is_clean_air_at_start, FALSE)
                        AND (ca.track_gap_ahead_at_end_s IS NULL OR ca.track_gap_ahead_at_end_s >= ?)
                        AND NOT ca.had_in_lap_on_track_pass
                        AND NOT ca.is_blue_flagged_lap
                    )
                END AS is_clean_air,
                NULLIF(
                    CONCAT_WS(
                        '|',
                        CASE WHEN ca.lap_number = 1 THEN 'LAP_1' END,
                        CASE
                            WHEN ca.observed_lap_time_s IS NULL OR ca.observed_lap_time_s <= 0
                            THEN 'INCOMPLETE_OR_INVALID_LAP'
                        END,
                        CASE WHEN ca.stint_number IS NULL THEN 'MISSING_STINT' END,
                        CASE WHEN ca.is_pit_in_lap THEN 'PIT_IN' END,
                        CASE WHEN ca.is_pit_out_lap THEN 'PIT_OUT' END,
                        CASE
                            WHEN NOT ca.confirmed_green_lap
                            THEN 'TRACK_STATUS_' || ca.track_status
                        END,
                        CASE WHEN ca.is_blue_flagged_lap THEN 'BLUE_FLAG' END,
                        CASE
                            WHEN NOT (
                                COALESCE(ca.is_clean_air_at_start, FALSE)
                                AND (ca.track_gap_ahead_at_end_s IS NULL OR ca.track_gap_ahead_at_end_s >= ?)
                                AND NOT ca.had_in_lap_on_track_pass
                            )
                            THEN 'TRAFFIC'
                        END
                    ),
                    ''
                ) AS exclusion_reason
            FROM clean_air_eval ca
        )
        SELECT
            session_key,
            meeting_key,
            driver_number,
            season,
            circuit_key,
            circuit_short_name,
            regulation_era,
            lap_number,
            race_total_laps,
            lap_start_utc,
            lap_end_estimate_utc,
            elapsed_race_time_s,
            observed_lap_time_s,
            sector_1_s,
            sector_2_s,
            sector_3_s,
            stint_number,
            compound,
            tyre_age_at_lap_start,
            fuel_load_proxy,
            track_temperature_c,
            air_temperature_c,
            rainfall,
            position_at_lap_start,
            gap_to_leader_at_start_raw,
            gap_to_leader_at_start_s,
            gap_ahead_at_start_raw,
            gap_ahead_at_start_s,
            track_gap_ahead_at_start_s,
            track_gap_ahead_at_end_s,
            had_in_lap_on_track_pass,
            is_blue_flagged_lap,
            is_clean_air_at_start,
            is_clean_air,
            is_pit_in_lap,
            is_pit_out_lap,
            is_pit_lap,
            stint_pit_mismatch,
            track_status,
            confirmed_green_lap,
            non_green_overlap_s,
            (exclusion_reason IS NULL) AS is_clean_green_pace_lap,
            exclusion_reason
        FROM final_eval
        ORDER BY driver_number, lap_number
        """,
        [
            session_key,
            session_key,
            session_key,
            session_key,
            clean_air_gap_s,
            clean_air_gap_s,
            clean_air_gap_s,
            clean_air_gap_s,
            clean_air_gap_s,
            clean_air_gap_s,
        ],
    )
