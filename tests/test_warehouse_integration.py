import json
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
from f1ri.ingestion.raw_cache import snapshot_path
from f1ri.validation.quality import assert_valid, check_session
from f1ri.warehouse.build import build_warehouse
from f1ri.warehouse.schema import SILVER_SCHEMAS


def test_minimal_race_pipeline(tmp_path: Path):
    sid = 123
    fixtures = {
        "sessions": [{"session_key":sid,"meeting_key":20,"year":2025,
                      "session_name":"Race","session_type":"Race",
                      "country_name":"Belgium","circuit_short_name":"Spa-Francorchamps"}],
        "laps": [
            {"session_key":sid,"meeting_key":20,"driver_number":44,"lap_number":1,"lap_duration":100.0,"is_pit_out_lap":False,"date_start":"2025-08-01T14:00:00+00:00"},
            {"session_key":sid,"meeting_key":20,"driver_number":44,"lap_number":2,"lap_duration":91.0,"is_pit_out_lap":False,"date_start":"2025-08-01T14:01:40+00:00"},
            {"session_key":sid,"meeting_key":20,"driver_number":44,"lap_number":3,"lap_duration":91.5,"is_pit_out_lap":False,"date_start":"2025-08-01T14:03:11+00:00"},
        ],
        "stints": [{"session_key":sid,"meeting_key":20,"driver_number":44,"stint_number":1,"lap_start":1,"lap_end":3,"compound":"MEDIUM","tyre_age_at_start":0}],
        "weather": [{"session_key":sid,"meeting_key":20,"date":"2025-08-01T13:59:00+00:00","track_temperature":31.0}],
        "pit":[], "drivers":[], "race_control":[], "session_result":[],
        # OpenF1 keys starting_grid by the Qualifying session_key (e.g. 120) within the same meeting_key (20).
        "starting_grid":[{"session_key":120,"meeting_key":20,"driver_number":44,"position":1,"lap_duration":89.5}],
    }
    for ep in SILVER_SCHEMAS:
        if ep in ("position", "intervals"):
            continue
        path = snapshot_path(tmp_path,2025,ep,sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fixtures[ep]))
    dbpath = build_warehouse(tmp_path,2025,sid)
    import duckdb
    con=duckdb.connect(str(dbpath),read_only=True)
    try:
        rows=con.execute("SELECT lap_number, tyre_age_at_lap_start, provisional_pace_candidate, confirmed_green_lap FROM gold.lap_observations ORDER BY lap_number").fetchall()
        meta=con.execute("SELECT DISTINCT season, regulation_era FROM gold.lap_observations").fetchall()
        grid=con.execute("SELECT session_key, meeting_key, driver_number, position FROM silver.starting_grid").fetchall()
    finally:
        con.close()
    assert rows == [(1,0,False,False),(2,1,True,False),(3,2,True,False)]
    assert meta == [(2025, "2023_2025")]
    assert grid == [(sid, 20, 44, 1)]
    assert_valid(check_session(tmp_path,sid))
    # Idempotent per-session rebuild.
    build_warehouse(tmp_path,2025,sid)
    assert check_session(tmp_path,sid)["laps"]==3
