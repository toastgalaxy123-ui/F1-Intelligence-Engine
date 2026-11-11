from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from f1ri.features.lap_context import build_track_status_intervals
from f1ri.ingestion.raw_cache import snapshot_path
from f1ri.validation.quality import assert_valid, check_session
from f1ri.warehouse.build import build_warehouse
from f1ri.warehouse.schema import SILVER_SCHEMAS

T0 = datetime(2025, 10, 5, 12, 0, 0, tzinfo=UTC)


def _ts(offset_s: float) -> datetime:
    return T0 + timedelta(seconds=offset_s)


def test_missing_race_control_defaults_to_unknown():
    intervals = build_track_status_intervals(
        session_key=999,
        race_control_events=[],
        session_start_utc=_ts(0),
        session_end_utc=_ts(600),
    )
    assert len(intervals) == 1
    assert intervals[0].status == "UNKNOWN"
    assert intervals[0].confidence == "LOW"
    assert intervals[0].source_rule == "NO_RACE_CONTROL_EVENTS"


def test_sector_yellow_and_double_yellow_transitions():
    events = [
        {"date": _ts(0), "category": "SessionStatus", "flag": None, "scope": None, "message": "SESSION STARTED"},
        {"date": _ts(100), "category": "Flag", "flag": "YELLOW", "scope": "Sector", "message": "YELLOW IN TRACK SECTOR 4"},
        {"date": _ts(120), "category": "Flag", "flag": "DOUBLE YELLOW", "scope": "Sector", "message": "DOUBLE YELLOW IN TRACK SECTOR 5"},
        # Clearing sector 5 leaves sector 4 still YELLOW.
        {"date": _ts(150), "category": "Flag", "flag": "CLEAR", "scope": "Sector", "message": "CLEAR IN TRACK SECTOR 5"},
        # Clearing sector 4 restores GREEN.
        {"date": _ts(180), "category": "Flag", "flag": "CLEAR", "scope": "Sector", "message": "CLEAR IN TRACK SECTOR 4"},
    ]
    ivs = build_track_status_intervals(999, events, _ts(0), _ts(300))
    assert [(iv.start_utc, iv.end_utc, iv.status) for iv in ivs] == [
        (_ts(0), _ts(100), "GREEN"),
        (_ts(100), _ts(120), "YELLOW"),
        (_ts(120), _ts(150), "DOUBLE_YELLOW"),
        (_ts(150), _ts(180), "YELLOW"),
        (_ts(180), _ts(300), "GREEN"),
    ]


def test_sector_clear_during_vsc_and_sc_does_not_restore_green():
    events = [
        {"date": _ts(0), "category": "SessionStatus", "flag": None, "scope": None, "message": "SESSION STARTED"},
        {"date": _ts(50), "category": "Flag", "flag": "YELLOW", "scope": "Sector", "message": "YELLOW IN TRACK SECTOR 8"},
        {"date": _ts(70), "category": "SafetyCar", "flag": None, "scope": None, "message": "VIRTUAL SAFETY CAR DEPLOYED"},
        {"date": _ts(110), "category": "Flag", "flag": "CLEAR", "scope": "Sector", "message": "CLEAR IN TRACK SECTOR 8"},
        {"date": _ts(120), "category": "SafetyCar", "flag": None, "scope": None, "message": "VIRTUAL SAFETY CAR ENDING"},
        {"date": _ts(135), "category": "Flag", "flag": "CLEAR", "scope": "Track", "message": "TRACK CLEAR"},
        {"date": _ts(200), "category": "SafetyCar", "flag": None, "scope": None, "message": "SAFETY CAR DEPLOYED"},
        {"date": _ts(240), "category": "Flag", "flag": "DOUBLE YELLOW", "scope": "Sector", "message": "DOUBLE YELLOW IN TRACK SECTOR 2"},
        {"date": _ts(260), "category": "Flag", "flag": "CLEAR", "scope": "Sector", "message": "CLEAR IN TRACK SECTOR 2"},
        {"date": _ts(290), "category": "SafetyCar", "flag": None, "scope": None, "message": "SAFETY CAR IN THIS LAP"},
        {"date": _ts(310), "category": "Flag", "flag": "CLEAR", "scope": "Track", "message": "TRACK CLEAR"},
    ]
    ivs = build_track_status_intervals(999, events, _ts(0), _ts(400))
    assert [(iv.start_utc, iv.end_utc, iv.status) for iv in ivs] == [
        (_ts(0), _ts(50), "GREEN"),
        (_ts(50), _ts(70), "YELLOW"),
        (_ts(70), _ts(135), "VSC"),
        (_ts(135), _ts(200), "GREEN"),
        (_ts(200), _ts(310), "SC"),
        (_ts(310), _ts(400), "GREEN"),
    ]


def test_red_flag_and_driver_flags_handling():
    events = [
        {"date": _ts(0), "category": "SessionStatus", "flag": None, "scope": None, "message": "SESSION STARTED"},
        {"date": _ts(30), "category": "Flag", "flag": "BLUE", "scope": "Driver", "message": "WAVED BLUE FLAG FOR CAR 18"},
        {"date": _ts(60), "category": "Flag", "flag": "RED", "scope": "Track", "message": "RED FLAG"},
        {"date": _ts(180), "category": "SessionStatus", "flag": None, "scope": None, "message": "SESSION RESUMED"},
    ]
    ivs = build_track_status_intervals(999, events, _ts(0), _ts(240))
    assert [(iv.start_utc, iv.end_utc, iv.status) for iv in ivs] == [
        (_ts(0), _ts(60), "GREEN"),
        (_ts(60), _ts(180), "RED"),
        (_ts(180), _ts(240), "GREEN"),
    ]


def test_lap_context_exclusions_and_traffic(tmp_path: Path):
    sid = 456
    fixtures = {
        "sessions": [
            {
                "session_key": sid,
                "meeting_key": 30,
                "year": 2025,
                "session_name": "Race",
                "session_type": "Race",
                "country_name": "Singapore",
                "circuit_short_name": "Singapore",
            }
        ],
        "drivers": [
            {"session_key": sid, "meeting_key": 30, "driver_number": 63, "full_name": "George Russell", "name_acronym": "RUS", "team_name": "Mercedes"},
            {"session_key": sid, "meeting_key": 30, "driver_number": 1, "full_name": "Max Verstappen", "name_acronym": "VER", "team_name": "Red Bull"},
            {"session_key": sid, "meeting_key": 30, "driver_number": 18, "full_name": "Lance Stroll", "name_acronym": "STR", "team_name": "Aston Martin"},
        ],
        "starting_grid": [
            {"session_key": sid, "meeting_key": 30, "driver_number": 63, "position": 1, "lap_duration": 89.1},
            {"session_key": sid, "meeting_key": 30, "driver_number": 1, "position": 2, "lap_duration": 89.3},
            {"session_key": sid, "meeting_key": 30, "driver_number": 18, "position": 20, "lap_duration": 91.2},
        ],
        "laps": [
            # Driver 63 (Leader):
            # Lap 1: opening lap (excluded by LAP_1)
            {"session_key": sid, "meeting_key": 30, "driver_number": 63, "lap_number": 1, "date_start": _ts(0).isoformat(), "lap_duration": 100.0, "is_pit_out_lap": False},
            # Lap 2: clean green lap
            {"session_key": sid, "meeting_key": 30, "driver_number": 63, "lap_number": 2, "date_start": _ts(100).isoformat(), "lap_duration": 95.0, "is_pit_out_lap": False},
            # Lap 3: partial VSC overlap (VSC from 250s to 280s, lap is 195s to 295s)
            {"session_key": sid, "meeting_key": 30, "driver_number": 63, "lap_number": 3, "date_start": _ts(195).isoformat(), "lap_duration": 100.0, "is_pit_out_lap": False},
            # Lap 4: pit-in lap
            {"session_key": sid, "meeting_key": 30, "driver_number": 63, "lap_number": 4, "date_start": _ts(295).isoformat(), "lap_duration": 102.0, "is_pit_out_lap": False},
            # Lap 5: pit-out lap
            {"session_key": sid, "meeting_key": 30, "driver_number": 63, "lap_number": 5, "date_start": _ts(397).isoformat(), "lap_duration": 112.0, "is_pit_out_lap": True},
            # Driver 1 (P2, 1.2s behind 63 at start of lap 2 -> TRAFFIC):
            {"session_key": sid, "meeting_key": 30, "driver_number": 1, "lap_number": 1, "date_start": _ts(0).isoformat(), "lap_duration": 101.2, "is_pit_out_lap": False},
            {"session_key": sid, "meeting_key": 30, "driver_number": 1, "lap_number": 2, "date_start": _ts(101.2).isoformat(), "lap_duration": 95.5, "is_pit_out_lap": False},
            # Driver 18 (lapped car with '+1 LAP' gap_to_leader_raw and 3.5s interval_raw):
            {"session_key": sid, "meeting_key": 30, "driver_number": 18, "lap_number": 1, "date_start": _ts(0).isoformat(), "lap_duration": 105.0, "is_pit_out_lap": False},
            {"session_key": sid, "meeting_key": 30, "driver_number": 18, "lap_number": 2, "date_start": _ts(105.0).isoformat(), "lap_duration": 96.0, "is_pit_out_lap": False},
        ],
        "stints": [
            {"session_key": sid, "meeting_key": 30, "driver_number": 63, "stint_number": 1, "lap_start": 1, "lap_end": 4, "compound": "MEDIUM", "tyre_age_at_start": 0},
            {"session_key": sid, "meeting_key": 30, "driver_number": 63, "stint_number": 2, "lap_start": 5, "lap_end": 5, "compound": "HARD", "tyre_age_at_start": 0},
            {"session_key": sid, "meeting_key": 30, "driver_number": 1, "stint_number": 1, "lap_start": 1, "lap_end": 2, "compound": "SOFT", "tyre_age_at_start": 2},
            {"session_key": sid, "meeting_key": 30, "driver_number": 18, "stint_number": 1, "lap_start": 1, "lap_end": 2, "compound": "HARD", "tyre_age_at_start": 0},
        ],
        "pit": [
            {"session_key": sid, "meeting_key": 30, "driver_number": 63, "lap_number": 4, "date": _ts(380).isoformat(), "lane_duration": 24.0, "stop_duration": 2.5},
        ],
        "weather": [
            {"session_key": sid, "meeting_key": 30, "date": _ts(0).isoformat(), "track_temperature": 37.5, "air_temperature": 29.8, "rainfall": False},
        ],
        "race_control": [
            {"session_key": sid, "meeting_key": 30, "date": _ts(0).isoformat(), "lap_number": 1, "driver_number": None, "category": "SessionStatus", "flag": None, "scope": None, "message": "SESSION STARTED"},
            {"session_key": sid, "meeting_key": 30, "date": _ts(250).isoformat(), "lap_number": 3, "driver_number": None, "category": "SafetyCar", "flag": None, "scope": None, "message": "VIRTUAL SAFETY CAR DEPLOYED"},
            {"session_key": sid, "meeting_key": 30, "date": _ts(280).isoformat(), "lap_number": 3, "driver_number": None, "category": "Flag", "flag": "CLEAR", "scope": "Track", "message": "TRACK CLEAR"},
        ],
        "session_result": [],
        "position": [
            {"session_key": sid, "meeting_key": 30, "driver_number": 63, "date": _ts(0).isoformat(), "position": 1},
            {"session_key": sid, "meeting_key": 30, "driver_number": 1, "date": _ts(0).isoformat(), "position": 2},
            {"session_key": sid, "meeting_key": 30, "driver_number": 18, "date": _ts(0).isoformat(), "position": 20},
        ],
        "intervals": [
            {"session_key": sid, "meeting_key": 30, "driver_number": 63, "date": _ts(99).isoformat(), "gap_to_leader": "0.0", "interval": "0.0"},
            {"session_key": sid, "meeting_key": 30, "driver_number": 1, "date": _ts(100).isoformat(), "gap_to_leader": "1.2", "interval": "1.2"},
            {"session_key": sid, "meeting_key": 30, "driver_number": 18, "date": _ts(104).isoformat(), "gap_to_leader": "+1 LAP", "interval": "3.5"},
        ],
    }

    for ep in SILVER_SCHEMAS:
        path = snapshot_path(tmp_path, 2025, ep, sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fixtures[ep]))

    dbpath = build_warehouse(tmp_path, 2025, sid)
    assert_valid(check_session(tmp_path, sid))

    con = duckdb.connect(str(dbpath), read_only=True)
    try:
        rus = con.execute("""
            SELECT lap_number, track_status, confirmed_green_lap, is_pit_in_lap, is_pit_out_lap,
                   is_clean_air, is_clean_green_pace_lap, exclusion_reason
            FROM gold.lap_context
            WHERE session_key = ? AND driver_number = 63
            ORDER BY lap_number
        """, [sid]).fetchall()

        ver_lap2 = con.execute("""
            SELECT gap_ahead_at_start_s, is_clean_air, is_clean_green_pace_lap, exclusion_reason
            FROM gold.lap_context
            WHERE session_key = ? AND driver_number = 1 AND lap_number = 2
        """, [sid]).fetchone()

        str_lap2 = con.execute("""
            SELECT gap_to_leader_at_start_raw, gap_to_leader_at_start_s, gap_ahead_at_start_s,
                   is_clean_air, is_clean_green_pace_lap
            FROM gold.lap_context
            WHERE session_key = ? AND driver_number = 18 AND lap_number = 2
        """, [sid]).fetchone()
    finally:
        con.close()

    assert rus == [
        (1, "GREEN", True, False, False, True, False, "LAP_1"),
        (2, "GREEN", True, False, False, True, True, None),
        (3, "VSC", False, False, False, True, False, "TRACK_STATUS_VSC"),
        (4, "GREEN", True, True, False, True, False, "PIT_IN"),
        (5, "GREEN", True, False, True, True, False, "PIT_OUT"),
    ]
    assert ver_lap2 == (pytest.approx(1.2), False, False, "TRAFFIC")
    assert str_lap2 == ("+1 LAP", None, pytest.approx(3.5), True, True)
