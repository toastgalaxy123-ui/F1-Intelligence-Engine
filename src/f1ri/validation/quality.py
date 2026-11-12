"""Fail loudly on critical data-integrity problems."""
from __future__ import annotations

from pathlib import Path

import duckdb


def check_session(root: Path, session_key: int) -> dict[str, int]:
    path = root / "data" / "warehouse" / "f1ri.duckdb"
    if not path.exists():
        raise FileNotFoundError("Build the warehouse before running quality checks")
    db = duckdb.connect(str(path), read_only=True)
    try:
        def count(sql: str) -> int:
            return int(db.execute(sql, [session_key]).fetchone()[0])
        results = {
            "session_metadata_rows": count("SELECT COUNT(*) FROM silver.sessions WHERE session_key=?"),
            "laps": count("SELECT COUNT(*) FROM silver.laps WHERE session_key=?"),
            "duplicate_lap_keys": count("""
                SELECT COUNT(*) FROM (
                    SELECT driver_number, lap_number FROM silver.laps
                    WHERE session_key=? GROUP BY driver_number, lap_number
                    HAVING COUNT(*) > 1
                )
            """),
            "nonpositive_or_missing_lap_times": count("""
                SELECT COUNT(*) FROM silver.laps WHERE session_key=?
                AND (lap_duration IS NULL OR lap_duration <= 0)
            """),
            "missing_or_overlapping_stints": count("""
                SELECT COUNT(*) FROM (
                    SELECT l.driver_number, l.lap_number,
                    COUNT(s.stint_number) AS n
                    FROM silver.laps l LEFT JOIN silver.stints s
                      ON l.session_key=s.session_key AND l.driver_number=s.driver_number
                     AND l.lap_number BETWEEN s.lap_start AND s.lap_end
                    WHERE l.session_key=?
                    GROUP BY l.session_key, l.driver_number, l.lap_number, l.lap_duration
                    HAVING COUNT(s.stint_number) > 1
                        OR (
                            l.lap_duration IS NOT NULL
                            AND l.lap_duration > 0
                            AND COUNT(s.stint_number) <> 1
                            AND NOT EXISTS (
                                SELECT 1 FROM silver.stints s2
                                WHERE s2.session_key = l.session_key
                                  AND s2.driver_number = l.driver_number
                            )
                        )
                )
            """),
            "track_status_interval_gaps_or_overlaps": count("""
                SELECT COUNT(*) FROM (
                    SELECT
                        start_utc,
                        end_utc,
                        LAG(end_utc) OVER (PARTITION BY session_key ORDER BY start_utc) AS prev_end_utc
                    FROM gold.track_status_intervals
                    WHERE session_key=?
                )
                WHERE end_utc <= start_utc
                   OR (prev_end_utc IS NOT NULL AND prev_end_utc <> start_utc)
            """),
            "clean_green_pace_laps": count("""
                SELECT COUNT(*) FROM gold.lap_context
                WHERE session_key=? AND is_clean_green_pace_lap
            """),
        }
        return results
    finally:
        db.close()


def assert_valid(results: dict[str, int]) -> None:
    critical = (
        "duplicate_lap_keys",
        "missing_or_overlapping_stints",
        "track_status_interval_gaps_or_overlaps",
    )
    if (results["laps"] == 0 or results["session_metadata_rows"] != 1
            or any(results[key] > 0 for key in critical)):
        raise RuntimeError(f"Data quality failed: {results}")


def check_pit_events(events) -> dict[str, int]:
    """Audit all retained visits; suspect timing excludes primary labels, not rows."""
    import pandas as pd
    if events.empty:
        return {"events": 0, "primary_eligible": 0, "duplicate_event_ids": 0}
    lane = pd.to_numeric(events.lane_duration_s, errors="coerce")
    stop = pd.to_numeric(events.stop_duration_s, errors="coerce")
    return {
        "events": len(events), "primary_eligible": int(events.eligible_primary_label.sum()),
        "duplicate_event_ids": int(events.pit_event_id.duplicated().sum()),
        "missing_stationary": int(stop.isna().sum()),
        "invalid_lane": int((lane.isna() | (lane <= 0)).sum()),
        "stationary_exceeds_lane": int((stop > lane).sum()),
        "ambiguous_timing": int((events.timing_quality == "C").sum()),
        "mixed_state": int((events.transition_track_status == "MIXED").sum()),
        "possible_double_stack": int(events.possible_double_stack.sum()),
    }
