"""Tier historical H1 evidence without changing the frozen C2 labels."""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict

import duckdb
import pandas as pd

from f1ri.validation.traffic_short_horizon import lap_evidence_clocks, sector_boundaries


def cached_h1(run):
    with duckdb.connect() as con:
        con.execute("SET TimeZone = 'UTC'")
        result = con.execute("""
            SELECT c.*, p.* EXCLUDE (opportunity_id, session_key, driver_number, horizon_type, as_of_utc)
            FROM read_parquet(?) c JOIN read_parquet(?) p USING (opportunity_id)
            WHERE c.horizon_type = 'H1' ORDER BY c.opportunity_id
        """, [str(run / "candidate_opportunities.parquet"), str(run / "physical_support.parquet")])
        names = [col[0] for col in result.description]
        return {row[0]: dict(zip(names, row, strict=True)) for row in result.fetchall()}


def classify_sector(immutable_failure, stage1_clean, physical):
    if immutable_failure:
        return "EXCLUDED", immutable_failure
    if (physical.get("cutoff_group") in ("CLOSE_SUPPORTED", "CLOSE_UNCERTAIN")
            or physical.get("following_label") or physical.get("mid_interval_close")
            or (physical.get("support_pass") and physical.get("clear_fraction", 1.) < .90)
            or physical.get("first_route_interruption_utc") is not None):
        return "C", "CACHED_PHYSICAL_CONTRADICTION"
    if physical.get("clean_control") and physical.get("timing_pass"):
        return "A", "C2_PHYSICAL_CLEAN"
    if stage1_clean:
        return "B", "STAGE1_CLEAN_TIMING_ONLY"
    return "C", "NO_TRUSTED_CLEAN_LABEL"


def _source_context(warehouse, key, inputs):
    with duckdb.connect(str(warehouse), read_only=True) as con:
        con.execute("SET TimeZone = 'UTC'")
        timing = con.execute("""
            SELECT driver_number, lap_number, min(date_start) AS start
            FROM silver.laps WHERE session_key = ?
            GROUP BY ALL HAVING count(DISTINCT (
                date_start, lap_duration, duration_sector_1, duration_sector_2, duration_sector_3
            )) = 1
        """, [key]).fetchall()
        stints = con.execute("""
            SELECT DISTINCT driver_number, stint_number, lap_start, compound, tyre_age_at_start
            FROM silver.stints WHERE session_key = ? ORDER BY driver_number, lap_start
        """, [key]).fetchall()
        weather = con.execute("""
            SELECT date, min(track_temperature), bool_or(rainfall) FROM silver.weather
            WHERE session_key = ? AND date IS NOT NULL GROUP BY date ORDER BY date
        """, [key]).fetchall()
        pits = con.execute("""
            SELECT driver_number, lap_number, date FROM silver.pit
            WHERE session_key = ? AND date IS NOT NULL
        """, [key]).fetchall()
    starts = {(driver, lap): start for driver, lap, start in timing if start is not None}
    pit_by_driver, stint_by_driver = defaultdict(list), defaultdict(list)
    for driver, lap, at in pits:
        pit_by_driver[driver].append((lap, at))
    for driver, stint, lap, compound, age in stints:
        start = starts.get((driver, lap))
        if start is None:
            continue
        stops = [at for p, at in pit_by_driver[driver] if p is not None and lap - 1 <= p <= lap]
        if stint == 1 or stops:
            visible_at = max(start, min(stops)) if stint != 1 else start
            stint_by_driver[driver].append((lap, visible_at, stint, compound, age))
    green = [(r.start_utc, r.end_utc) for r in inputs["track"].itertuples()
             if r.status == "GREEN" and r.confidence == "HIGH"]
    return starts, stint_by_driver, weather, green


def evidence_rows(warehouse, key, scheduled_laps, inputs, cached, timing_tolerance):
    """Use only lap starts and pit confirmations for tire context at the cutoff."""
    starts, stints, weather, green = _source_context(warehouse, key, inputs)
    weather_clocks = [r[0] for r in weather]
    clocks = lap_evidence_clocks(inputs)
    for lap in inputs["laps"].itertuples():
        driver, number = int(lap.driver_number), int(lap.lap_number)
        lap_start = lap.lap_start_utc
        next_line = starts.get((driver, number + 1))
        durations = [lap.sector_1_s, lap.sector_2_s, lap.sector_3_s]
        bounds = sector_boundaries(lap_start, durations)
        elapsed = float(lap.observed_lap_time_s) if pd.notna(lap.observed_lap_time_s) else None
        valid_timing = (len(bounds) == 4 and elapsed and elapsed > 0 and next_line is not None
                        and starts.get((driver, number)) == lap_start
                        and abs((bounds[-1] - next_line).total_seconds()) <= timing_tolerance
                        and abs(sum(durations) - elapsed) <= timing_tolerance)
        for sector in range(3):
            identity = f"{key}/{driver}/{number}/H1/{sector}"
            physical = cached.get(identity, {})
            cutoff = bounds[sector] if len(bounds) > sector else None
            finish = bounds[sector + 1] if len(bounds) > sector + 1 else None
            weather_i = bisect_right(weather_clocks, cutoff) - 1 if cutoff is not None else -1
            weather_at, temperature, rain = weather[weather_i] if weather_i >= 0 else (None, None, None)
            visible = [r for r in stints[driver] if cutoff is not None and r[1] <= cutoff and r[0] <= number]
            latest_lap = max((r[0] for r in visible), default=None)
            current = [r for r in visible if r[0] == latest_lap]
            tyre_start, tyre_clock, stint, compound, age = current[0] if len(current) == 1 else (None,) * 5
            tyre_age = number - tyre_start + age if age is not None else None
            available = clocks.get((driver, number))
            if available and physical.get("available_by_utc"):
                available = max(available, physical["available_by_utc"])
            if finish and available:
                available = max(finish, available)
            failure = (
                "PIT_OR_STINT_TRANSITION" if lap.is_pit_lap or lap.stint_pit_mismatch else
                "ARCHIVE_PASS" if lap.had_in_lap_on_track_pass else
                "NON_GREEN_OR_WET" if lap.track_status != "GREEN" or not lap.confirmed_green_lap or rain
                or (cutoff and finish and not any(a <= cutoff < finish <= b for a, b in green)) else
                "OFFICIAL_TIMING_UNRECONCILED" if not valid_timing else
                "AVAILABILITY_UNRESOLVED" if not available or available <= cutoff else None
            )
            tier, reason = classify_sector(failure, bool(lap.is_clean_green_pace_lap), physical)
            yield {
                "opportunity_id": identity, "session_key": key, "driver_number": driver, "lap_number": number,
                "checkpoint": sector, "as_of_utc": cutoff, "target_end_utc": finish, "available_by_utc": available,
                "stint_number": stint, "archived_stint_number": int(lap.stint_number) if pd.notna(lap.stint_number) else None,
                "compound": compound, "tyre_age": tyre_age, "tyre_available_by_utc": tyre_clock,
                "track_temperature_c": temperature, "weather_available_by_utc": weather_at,
                "race_progress": number / scheduled_laps, "timing_pass": bool(valid_timing),
                "track_state": lap.track_status, "tier": tier, "label_reason": reason,
                "stage1_exclusion": lap.exclusion_reason, "physical_group": physical.get("cutoff_group"),
                "physical_support_fraction": physical.get("support_fraction"), "physical_timing_pass": physical.get("timing_pass"),
                "sector_elapsed_s": float(durations[sector]) if finish else None, "lap_elapsed_s": elapsed,
                "stage1_clean": bool(lap.is_clean_green_pace_lap), "d8_lap_s": physical.get("d8_lap_s"),
                "d8_lap_std_s": physical.get("d8_lap_std_s"),
                "pace_reference_status": physical.get("pace_reference_status") or "UNATTEMPTED",
                "following_label": bool(physical.get("following_label")), "physical_leader": physical.get("physical_leader"),
                "episode_id": physical.get("episode_id"), "cached": bool(physical),
            }
