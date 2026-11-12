from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from f1ri.simulator.replay import replay_session, replay_summary
from f1ri.warehouse.build import _load_silver_table


def test_warehouse_replay_preserves_crossings_and_audits_penalty(tmp_path: Path):
    warehouse = tmp_path / "race.duckdb"
    start = datetime(2025, 10, 5, tzinfo=UTC)
    sid = 100
    drivers = (63, 44, 31)
    with duckdb.connect(str(warehouse)) as con:
        con.execute("CREATE SCHEMA silver")
        con.execute("CREATE SCHEMA gold")
        end = start + timedelta(seconds=200)
        tables = {
            "sessions": [{"session_name": "Race", "year": 2025, "date_start": start,
                          "date_end": end}],
            "drivers": [{"driver_number": d, "team_name": f"Team {d}"} for d in drivers],
            "starting_grid": [{"driver_number": d, "position": p}
                              for p, d in enumerate(drivers, 1)],
            "stints": [{"driver_number": d, "stint_number": 1, "lap_start": 1, "lap_end": 2,
                        "compound": "MEDIUM", "tyre_age_at_start": 0} for d in drivers],
            "session_result": [
                {"driver_number": 63, "number_of_laps": 2, "position": 2},
                {"driver_number": 44, "number_of_laps": 2, "position": 1},
                {"driver_number": 31, "number_of_laps": 1, "dnf": True},
            ],
            "position": [{"driver_number": d, "position": p, "date": start}
                         for p, d in enumerate(drivers, 1)],
            "race_control": [
                {"category": "SessionStatus", "message": "SESSION STARTED", "date": start},
                {"category": "Flag", "flag": "CHEQUERED", "date": start + timedelta(seconds=180)},
            ],
            "laps": [
                {"driver_number": driver, "lap_number": lap, "date_start": (
                    start + timedelta(seconds=offset * (lap - 1))
                ), "lap_duration": duration}
                for driver, offset in ((63, 90), (44, 91), (31, 92))
                for lap, duration in ((1, offset), (2, offset if driver != 31 else None))
            ],
            "pit": [],
        }
        for name, rows in tables.items():
            _load_silver_table(con, name, [dict(row, session_key=sid) for row in rows], sid)
        con.execute("""
            CREATE TABLE gold.track_status_intervals (
                session_key BIGINT, start_utc TIMESTAMPTZ, end_utc TIMESTAMPTZ,
                status VARCHAR, source_rule VARCHAR
            )
        """)
        con.execute("INSERT INTO gold.track_status_intervals VALUES (?, ?, ?, 'GREEN', 'TEST')",
                    [sid, start, end])

    raw = replay_session(warehouse, sid, 2)
    assert raw.timing_tier == "C"
    assert {issue.code for issue in raw.issues} == {"FINAL_CLASSIFICATION_MISMATCH"}
    reference = tmp_path / "data/raw/reference"
    reference.mkdir(parents=True)
    (reference / "race_time_penalties.csv").write_text(
        "session_key,driver_number,added_seconds,source_url\n"
        "100,63,5,https://example.org/decision\n", encoding="utf-8",
    )
    corrected = replay_session(warehouse, sid, 2, annotations_root=tmp_path)
    assert corrected.run == raw.run
    assert corrected.timing_tier == "B"
    assert corrected.issues == ()
    assert corrected.compared_boundaries == 3
    assert [(d.driver_number, d.completed_laps, d.timing_position, d.classified_position)
            for d in corrected.drivers] == [(63, 2, 1, 2), (44, 2, 2, 1), (31, 1, None, None)]
    assert replay_summary(corrected)["event_counts"]["RETIREMENT"] == 1
