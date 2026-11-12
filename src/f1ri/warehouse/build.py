"""Typed warehouse build with idempotent per-session replacements."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from f1ri.features.lap_context import populate_session_lap_context
from f1ri.features.stint_observations import populate_session_stint_observations
from f1ri.ingestion.compound_allocations import populate_silver_compound_allocations
from f1ri.ingestion.raw_cache import read_snapshot, snapshot_path
from f1ri.warehouse.schema import SILVER_SCHEMAS


def _records_for_endpoint(
    root: Path, year: int, session_key: int, endpoint: str, meeting_key: int | None = None
) -> list[dict]:
    path = snapshot_path(root, year, endpoint, session_key)
    if not path.exists():
        raise FileNotFoundError(f"Missing {endpoint} snapshot: {path}. Run 'f1ri ingest' first.")
    data = read_snapshot(path)
    if endpoint == "starting_grid":
        # OpenF1 keys starting_grid by the Qualifying session_key within the same meeting_key.
        if any(
            row.get("session_key") != session_key
            and (meeting_key is None or row.get("meeting_key") != meeting_key)
            for row in data
        ):
            raise ValueError(f"Meeting/session key mismatch inside {path}; refusing to mix sessions")
    elif any(row.get("session_key") != session_key for row in data):
        raise ValueError(f"Session key mismatch inside {path}; refusing to mix sessions")
    return data


def _load_silver_table(con: duckdb.DuckDBPyConnection, endpoint: str, rows: list[dict],
                       session_key: int) -> None:
    schema = SILVER_SCHEMAS[endpoint]
    column_defs = ", ".join(f'"{key}" {dtype}' for key, dtype in schema.items())
    con.execute(f'CREATE TABLE IF NOT EXISTS silver."{endpoint}" ({column_defs})')
    con.execute(f'DELETE FROM silver."{endpoint}" WHERE session_key = ?', [session_key])
    if not rows:
        return
    # A small pandas adapter is appropriate for lap/event data. Telemetry never goes here.
    if endpoint == "stints":
        # Sort per-driver stints and resolve any boundary overlap or open-ended stint_end in raw OpenF1 feeds.
        by_driver: dict[Any, list[dict]] = {}
        for r in rows:
            by_driver.setdefault(r.get("driver_number"), []).append(dict(r))
        normalized_rows: list[dict] = []
        for drv_rows in by_driver.values():
            drv_rows.sort(key=lambda x: (x.get("stint_number") or 0, x.get("lap_start") or 0))
            for idx in range(len(drv_rows) - 1):
                cur_end = drv_rows[idx].get("lap_end")
                nxt_start = drv_rows[idx + 1].get("lap_start")
                cur_start = drv_rows[idx].get("lap_start")
                if (
                    cur_end is not None
                    and nxt_start is not None
                    and cur_start is not None
                    and cur_end >= nxt_start > cur_start
                ):
                    drv_rows[idx]["lap_end"] = nxt_start - 1
            normalized_rows.extend(drv_rows)
        rows = normalized_rows
    fixed_rows = []
    for row in rows:
        fixed = {col: row.get(col) for col in schema}
        if endpoint == "starting_grid":
            fixed["session_key"] = session_key
        if endpoint == "intervals":
            g_raw = row.get("gap_to_leader")
            i_raw = row.get("interval")
            fixed["gap_to_leader_raw"] = None if g_raw is None else str(g_raw)
            fixed["interval_raw"] = None if i_raw is None else str(i_raw)
        for col, dtype in schema.items():
            if dtype == "VARCHAR" and fixed.get(col) is not None:
                fixed[col] = str(fixed[col])
        fixed_rows.append(fixed)
    df = pd.DataFrame(fixed_rows, columns=list(schema))
    con.register("_incoming", df)
    cols = ", ".join(f'"{col}"' for col in schema)
    casts = ", ".join(f'TRY_CAST("{col}" AS {dtype})' for col, dtype in schema.items())
    try:
        con.execute(f'INSERT INTO silver."{endpoint}" ({cols}) SELECT {casts} FROM _incoming')
    finally:
        con.unregister("_incoming")


def build_warehouse(root: Path, year: int, session_key: int) -> Path:
    db_path = root / "data" / "warehouse" / "f1ri.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = duckdb.connect(str(db_path))
    try:
        db.execute("CREATE SCHEMA IF NOT EXISTS silver")
        db.execute("CREATE SCHEMA IF NOT EXISTS gold")
        db.execute("BEGIN TRANSACTION")
        try:
            session_rows = _records_for_endpoint(root, year, session_key, "sessions")
            meeting_key = session_rows[0].get("meeting_key") if session_rows else None
            for endpoint in SILVER_SCHEMAS:
                path = snapshot_path(root, year, endpoint, session_key)
                if not path.exists():
                    # Optional endpoints are absent unless --full is requested.
                    if endpoint in ("position", "intervals"):
                        continue
                    raise FileNotFoundError(f"Required snapshot missing: {path}")
                _load_silver_table(
                    db,
                    endpoint,
                    _records_for_endpoint(root, year, session_key, endpoint, meeting_key=meeting_key),
                    session_key,
                )
            # One observable lap per (session, driver, lap). Stint rows must not overlap.
            # Future state reconstruction must be added in a separately tested module.
            db.execute("""
                CREATE OR REPLACE TABLE gold.lap_observations AS
                SELECT
                    l.session_key, l.meeting_key, l.driver_number,
                    sess.year AS season, sess.circuit_key, sess.circuit_short_name,
                    CASE WHEN sess.year >= 2026 THEN '2026_PLUS'
                         WHEN sess.year BETWEEN 2023 AND 2025 THEN '2023_2025'
                         ELSE 'OTHER' END AS regulation_era,
                    l.lap_number, l.date_start AS lap_start_utc,
                    l.lap_duration AS observed_lap_time_s,
                    l.duration_sector_1 AS sector_1_s,
                    l.duration_sector_2 AS sector_2_s,
                    l.duration_sector_3 AS sector_3_s,
                    s.stint_number, s.compound,
                    s.tyre_age_at_start + l.lap_number - s.lap_start
                        AS tyre_age_at_lap_start,
                    COALESCE(l.is_pit_out_lap, FALSE) AS pit_out_lap,
                    EXISTS (
                        SELECT 1 FROM silver.pit p
                        WHERE p.session_key=l.session_key
                          AND p.driver_number=l.driver_number
                          AND p.lap_number=l.lap_number
                    ) AS pit_event_lap,
                    (
                        SELECT w.track_temperature
                        FROM silver.weather w
                        WHERE w.session_key=l.session_key AND w.date <= l.date_start
                        ORDER BY w.date DESC LIMIT 1
                    ) AS latest_observed_track_temp_c,
                    -- Conservative: no unverified 'green' or 'clean-air' label.
                    'UNCLASSIFIED'::VARCHAR AS track_status,
                    FALSE AS confirmed_green_lap,
                    (l.lap_number > 1 AND l.lap_duration IS NOT NULL
                     AND l.lap_duration > 0 AND s.stint_number IS NOT NULL
                     AND NOT COALESCE(l.is_pit_out_lap, FALSE)
                     AND NOT EXISTS (
                        SELECT 1 FROM silver.pit p
                        WHERE p.session_key=l.session_key
                          AND p.driver_number=l.driver_number
                          AND p.lap_number IN (l.lap_number, l.lap_number - 1)
                     )) AS provisional_pace_candidate
                FROM silver.laps l
                LEFT JOIN silver.sessions sess ON l.session_key=sess.session_key
                LEFT JOIN silver.stints s
                  ON l.session_key=s.session_key
                 AND l.driver_number=s.driver_number
                 AND l.lap_number BETWEEN s.lap_start AND s.lap_end
            """)
            populate_session_lap_context(db, session_key)
            populate_silver_compound_allocations(db, root)
            populate_session_stint_observations(db, session_key)
            db.execute("""
                UPDATE gold.lap_observations AS obs
                SET track_status = ctx.track_status,
                    confirmed_green_lap = ctx.confirmed_green_lap
                FROM gold.lap_context AS ctx
                WHERE obs.session_key = ctx.session_key
                  AND obs.driver_number = ctx.driver_number
                  AND obs.lap_number = ctx.lap_number
            """)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    finally:
        db.close()
    return db_path
