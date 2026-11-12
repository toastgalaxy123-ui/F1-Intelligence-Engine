"""Versioned circuit-layout metadata. Missing speed limits remain unknown."""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from f1ri.warehouse.schema import REFERENCE_SCHEMAS


def load_pit_lane_reference(path: Path) -> pd.DataFrame:
    schema = REFERENCE_SCHEMAS["pit_lane_reference"]
    frame = pd.read_csv(path).reindex(columns=list(schema))
    if frame.reference_id.duplicated().any():
        raise ValueError("Duplicate pit-lane reference IDs")
    for column in ("effective_from_utc", "effective_to_utc", "available_as_of_utc"):
        frame[column] = pd.to_datetime(frame[column], utc=True, errors="raise")
    speeds = pd.to_numeric(frame.pit_speed_limit_kph, errors="raise")
    if ((speeds <= 0) | (speeds > 120)).any():
        raise ValueError("Invalid pit-lane speed limit")
    if (frame.effective_from_utc >= frame.effective_to_utc).any():
        raise ValueError("Invalid pit-lane reference effective period")
    for _, group in frame.groupby(["circuit_short_name", "layout_version"], dropna=False):
        group = group.sort_values("effective_from_utc")
        if (group.effective_from_utc.iloc[1:].reset_index(drop=True)
                < group.effective_to_utc.iloc[:-1].reset_index(drop=True)).any():
            raise ValueError("Overlapping circuit-layout reference periods")
    return frame


def pit_reference_as_of(
    frame: pd.DataFrame, circuit: str, season: int, as_of_utc,
) -> dict:
    if frame.empty:
        return {}
    cutoff = pd.Timestamp(as_of_utc)
    selected = frame[(frame.circuit_short_name == circuit) & (frame.season == season)
                     & (frame.available_as_of_utc <= cutoff)
                     & (frame.effective_from_utc <= cutoff)
                     & (frame.effective_to_utc > cutoff)]
    if len(selected) > 1:
        raise ValueError("Ambiguous pit-lane layout at forecast cutoff")
    return selected.iloc[0].to_dict() if len(selected) == 1 else {}


def populate_pit_lane_reference(con: duckdb.DuckDBPyConnection, root: Path) -> pd.DataFrame:
    path = root / "data/raw/reference/pit_lane_reference.csv"
    schema = REFERENCE_SCHEMAS["pit_lane_reference"]
    frame = load_pit_lane_reference(path) if path.exists() else pd.DataFrame(columns=list(schema))
    con.execute("CREATE SCHEMA IF NOT EXISTS silver")
    con.execute("CREATE TABLE IF NOT EXISTS silver.pit_lane_reference (" + ", ".join(
        f'"{k}" {v}' for k, v in schema.items()) + ")")
    con.execute("DELETE FROM silver.pit_lane_reference")
    if not frame.empty:
        con.register("_pit_reference", frame)
        try:
            casts = ", ".join(f'CAST("{k}" AS {v})' for k, v in schema.items())
            con.execute("INSERT INTO silver.pit_lane_reference SELECT " + casts + " FROM _pit_reference")
        finally:
            con.unregister("_pit_reference")
    return frame
