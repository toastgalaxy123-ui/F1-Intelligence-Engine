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
                    GROUP BY l.driver_number, l.lap_number
                    HAVING COUNT(s.stint_number) <> 1
                )
            """),
        }
        return results
    finally:
        db.close()


def assert_valid(results: dict[str, int]) -> None:
    critical = ("duplicate_lap_keys", "missing_or_overlapping_stints")
    if (results["laps"] == 0 or results["session_metadata_rows"] != 1
            or any(results[key] > 0 for key in critical)):
        raise RuntimeError(f"Data quality failed: {results}")
