"""Deterministic physical pit timelines and independent synthetic populations."""
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def pit_frames():
    start = datetime(2025, 7, 1, 12, tzinfo=UTC)
    sid = 701
    session = {"session_key": sid, "meeting_key": 88, "year": 2025,
               "session_name": "Race", "session_type": "Race", "circuit_key": 9,
               "circuit_short_name": "Synthetic", "date_start": start,
               "date_end": start+timedelta(hours=3)}
    laps, stints = [], []
    for driver in (1, 2, 3):
        date = start
        for lap in range(1, 15):
            duration = 100.0 if (driver == 1 and lap == 10) else (
                104.0 if driver == 1 and lap == 11 else 90.0)
            laps.append({"session_key": sid, "meeting_key": 88, "driver_number": driver,
                         "lap_number": lap, "date_start": date, "lap_duration": duration,
                         "duration_sector_1": duration/3, "duration_sector_2": duration/3,
                         "duration_sector_3": duration/3, "is_pit_out_lap": driver == 1 and lap == 11,
                         "lap_start_utc": date, "lap_end_estimate_utc": date+timedelta(seconds=duration),
                         "observed_lap_time_s": duration, "compound": "MEDIUM" if lap <= 10 or driver != 1 else "HARD",
                         "tyre_age_at_lap_start": lap-1 if lap <= 10 or driver != 1 else lap-11,
                         "is_clean_green_pace_lap": not (driver == 1 and lap in (10, 11)),
                         "circuit_short_name": "Synthetic", "stint_number": 1 if lap <= 10 or driver != 1 else 2})
            date += timedelta(seconds=duration)
        if driver == 1:
            stints.extend([
                {"session_key": sid, "driver_number": driver, "stint_number": 1,
                 "lap_start": 1, "lap_end": 10, "compound": "MEDIUM", "tyre_age_at_start": 0},
                {"session_key": sid, "driver_number": driver, "stint_number": 2,
                 "lap_start": 11, "lap_end": 14, "compound": "HARD", "tyre_age_at_start": 0},
            ])
        else:
            stints.append({"session_key": sid, "driver_number": driver, "stint_number": 1,
                           "lap_start": 1, "lap_end": 14, "compound": "MEDIUM", "tyre_age_at_start": 0})
    return {
        "sessions": pd.DataFrame([session]), "laps": pd.DataFrame(laps), "stints": pd.DataFrame(stints),
        "pit": pd.DataFrame([{"session_key": sid, "driver_number": 1, "lap_number": 10,
                              "date": start+timedelta(seconds=890), "lane_duration": 26., "stop_duration": 2.5}]),
        "drivers": pd.DataFrame([{"session_key": sid, "driver_number": d, "team_name": f"Team{d}"} for d in (1, 2, 3)]),
        "weather": pd.DataFrame([{"session_key": sid, "date": start, "track_temperature": 30., "rainfall": False}]),
        "race_control": pd.DataFrame([{"session_key": sid, "date": start, "category": "SessionStatus",
                                        "flag": None, "scope": None, "message": "SESSION STARTED"}]),
        "intervals": pd.DataFrame(), "position": pd.DataFrame(),
        "track_intervals": pd.DataFrame([{"start_utc": start, "end_utc": start+timedelta(hours=3), "status": "GREEN"}]),
    }


@pytest.fixture
def synthetic_pit_population():
    rng = np.random.default_rng(127)
    rows = []
    for race in range(24):
        circuit = ("CircuitA", "CircuitB")[race % 2]
        route = 17.0 if circuit == "CircuitA" else 25.0
        for stop in range(30):
            team = "FastTeam" if stop % 2 == 0 else "OtherTeam"
            service = (2.2 if team == "FastTeam" else 2.8) + abs(rng.normal(0, .15))
            if rng.random() < .08:
                service += 3+rng.exponential(2)
            compound = ("HARD", "MEDIUM", "SOFT")[stop % 3]
            transition = {"HARD": .4, "MEDIUM": 0, "SOFT": -.4}[compound]
            rows.append({"pit_event_id": f"{race}:{stop}", "session_key": race,
                         "circuit_short_name": circuit, "season": 2023, "team_id": team,
                         "new_compound": compound, "pit_event_type": "ROUTINE_TIRE_CHANGE",
                         "stop_duration_s": service if stop % 5 else None,
                         "lane_duration_s": route+5+service+rng.normal(0, .15),
                         "transition_loss_s": route+service+transition+rng.normal(0, .3),
                         "label_std_s": .2, "eligible_direct_lane": True, "eligible_primary_label": True,
                         "observation_available_utc": datetime(2023, 1, 1, tzinfo=UTC)+timedelta(days=race, minutes=stop)})
    return pd.DataFrame(rows)
