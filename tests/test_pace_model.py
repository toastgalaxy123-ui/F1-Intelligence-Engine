from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from f1ri.models.contracts import PaceRequest
from f1ri.models.pace_linear import AdditivePaceModel

T0 = datetime(2024, 9, 1, 13, 0, 0, tzinfo=UTC)


def _build_training_fixture() -> pd.DataFrame:
    rows = []
    for sk, circ, base in [(9472, "Sakhir", 95.0), (9590, "Monza", 82.0)]:
        for drv, team, drv_off in [(1, "RED_BULL", -0.3), (63, "MERCEDES", 0.1)]:
            for lap in range(2, 35):
                cmpd = "SOFT" if lap < 16 else "HARD"
                age = lap if lap < 16 else (lap - 15)
                prog = lap / 55.0
                # True generating process: fuel burn -1.5*prog + tire wear
                wear = (0.06 if cmpd == "SOFT" else 0.025) * age
                prev_wear = (0.06 if cmpd == "SOFT" else 0.025) * max(age - 1, 0)
                t_now = base + drv_off - 1.5 * prog + wear
                t_prev = base + drv_off - 1.5 * ((lap - 1) / 55.0) + prev_wear
                rows.append(
                    {
                        "session_key": sk,
                        "driver_number": drv,
                        "cutoff_lap_number": lap - 1,
                        "target_lap_number": lap,
                        "horizon": 1,
                        "forecast_as_of_utc": T0 + timedelta(seconds=lap * 90),
                        "season": 2024,
                        "regulation_era": "2023_2025",
                        "circuit_short_name": circ,
                        "team_id": team,
                        "driver_id": f"D{drv}",
                        "race_progress": prog,
                        "compound": cmpd,
                        "tyre_age_at_lap_start": age,
                        "stint_number": 1 if lap < 16 else 2,
                        "current_stint_laps_completed": age,
                        "tyre_age_at_stint_start": 0,
                        "last_clean_lap_time_s": t_prev if age > 1 else None,
                        "median_last_3_clean_laps_s": t_prev if age > 1 else None,
                        "median_stint_clean_laps_s": t_prev if age > 1 else None,
                        "clean_laps_observed_current_stint": max(age - 1, 0),
                        "clean_laps_observed_race": max(lap - 2, 0),
                        "last_clean_lap_age": 1,
                        "last_known_track_temp_c": 34.0,
                        "observed_target_lap_time_s": t_now,
                        "target_is_clean_green": True,
                    }
                )
    return pd.DataFrame(rows)


def test_pace_model_reproducibility_and_determinism():
    df = _build_training_fixture()
    m1 = AdditivePaceModel().fit(df)
    m2 = AdditivePaceModel().fit(df)
    assert m1.diagnostics_dict() == m2.diagnostics_dict()

    req = PaceRequest(
        session_key=10014,
        driver_number=1,
        lap_number=20,
        compound="HARD",
        tyre_age_at_lap_start=8,
        track_temperature_c=34.5,
        as_of_utc=T0 + timedelta(days=200),
        circuit_short_name="Sakhir",
        driver_id="D1",
        team_id="RED_BULL",
        race_progress=20 / 57.0,
        last_clean_lap_time_s=94.45,
        median_last_3_clean_laps_s=94.48,
        median_stint_clean_laps_s=94.46,
        clean_laps_observed_current_stint=5,
        clean_laps_observed_race=17,
        last_clean_lap_age=1,
    )
    p1 = m1.predict(req)
    p2 = m1.predict(req)
    assert p1 == p2
    assert p1.p05_seconds < p1.p25_seconds < p1.median_seconds < p1.p75_seconds < p1.p95_seconds

    # Unseen driver/circuit returns wider uncertainty
    req_unseen = PaceRequest(
        session_key=10099,
        driver_number=99,
        lap_number=2,
        compound="MEDIUM",
        tyre_age_at_lap_start=1,
        track_temperature_c=25.0,
        as_of_utc=T0 + timedelta(days=200),
        circuit_short_name="UnseenCircuit",
        driver_id="UNSEEN_DRV",
        team_id="UNSEEN_TEAM",
        race_progress=2 / 55.0,
    )
    p_unseen = m1.predict(req_unseen)
    assert p_unseen.std_seconds > p1.std_seconds
    assert p_unseen.confidence_level == "COLD_START"
