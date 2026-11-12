from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from f1ri.models.contracts import (
    SCENARIO_CONTINUE_STINT,
    SCENARIO_NEW_FRESH,
    PaceRequest,
    StintForecastRequest,
)
from f1ri.models.degradation_hierarchical import HierarchicalTireDegradationModel
from f1ri.models.pace_linear import AdditivePaceModel
from f1ri.models.stint_forecast import PaceModelV2, compare_stint_scenarios
from f1ri.simulator.contracts import HypotheticalStintScenario


def _build_stage3_training_df() -> pd.DataFrame:
    t0 = datetime(2024, 9, 1, 13, 0, 0, tzinfo=UTC)
    rows = []
    for sk, circ, base in [(9472, "Sakhir", 95.0), (9590, "Monza", 82.0)]:
        for drv, team, drv_off in [(1, "RED_BULL", -0.3), (63, "MERCEDES", 0.1)]:
            for lap in range(2, 40):
                stint_num = 1 if lap < 18 else 2
                cmpd = "MEDIUM" if stint_num == 1 else "HARD"
                pirelli = "C2" if cmpd == "MEDIUM" else "C1"
                stint_lap = lap if stint_num == 1 else (lap - 17)
                age = stint_lap - 1
                prog = lap / 56.0
                slope = 0.065 if (circ == "Sakhir" and cmpd == "MEDIUM") else (
                    0.035 if (circ == "Sakhir" and cmpd == "HARD") else (
                        0.025 if cmpd == "MEDIUM" else 0.012
                    )
                )
                off_c = 0.0 if cmpd == "MEDIUM" else 0.45
                t_now = base + drv_off - 1.5 * prog + off_c + slope * age
                t_prev = base + drv_off - 1.5 * ((lap - 1) / 56.0) + off_c + slope * max(age - 1, 0)
                rows.append(
                    {
                        "session_key": sk,
                        "driver_number": drv,
                        "stint_id": f"{sk}_{drv}_{stint_num}",
                        "cutoff_lap_number": lap - 1,
                        "planned_start_lap": lap,
                        "target_lap_number": lap,
                        "horizon": 1,
                        "requested_horizon": 1,
                        "forecast_as_of_utc": t0 + timedelta(seconds=lap * 90),
                        "season": 2024,
                        "regulation_era": "2023_2025",
                        "circuit_short_name": circ,
                        "team_id": team,
                        "driver_id": f"D{drv}",
                        "scheduled_race_laps": 56,
                        "race_progress": prog,
                        "compound": cmpd,
                        "compound_relative": cmpd,
                        "compound_absolute": pirelli,
                        "tyre_age_at_forecast_start": age,
                        "tyre_age_at_lap_start": age,
                        "stint_number": stint_num,
                        "current_stint_laps_completed": stint_lap,
                        "tyre_age_at_stint_start": 0,
                        "last_clean_lap_time_s": t_prev if stint_lap > 1 else None,
                        "median_last_3_clean_laps_s": t_prev if stint_lap > 1 else None,
                        "median_stint_clean_laps_s": t_prev if stint_lap > 1 else None,
                        "clean_laps_observed_current_stint": max(stint_lap - 1, 0),
                        "clean_laps_observed_race": max(lap - 2, 0),
                        "last_clean_lap_age": 1,
                        "last_known_track_temp_c": 34.0,
                        "observed_lap_time_s": t_now,
                        "observed_target_lap_time_s": t_now,
                        "is_clean_green_pace_lap": True,
                        "target_is_clean_green": True,
                    }
                )
    return pd.DataFrame(rows)


def test_pace_v2_no_double_counting_and_cumulative_sum_invariant():
    train_df = _build_stage3_training_df()
    v2 = PaceModelV2(
        degradation_model=HierarchicalTireDegradationModel(),
        model_version="PaceV2_test",
    ).fit(train_df)

    req = StintForecastRequest(
        session_key=9472,
        driver_number=1,
        cutoff_lap_number=25,
        planned_start_lap=26,
        horizon_laps=10,
        compound_relative="HARD",
        compound_absolute="C1",
        tyre_age_at_start=8,
        as_of_utc=datetime(2024, 9, 1, 13, 40, 0, tzinfo=UTC),
        scenario_type=SCENARIO_CONTINUE_STINT,
        stint_number=2,
        scheduled_race_laps=56,
        track_temperature_c=34.0,
        circuit_short_name="Sakhir",
        driver_id="D1",
        team_id="RED_BULL",
        season=2024,
        regulation_era="2023_2025",
        clean_laps_observed_current_stint=5,
        clean_laps_observed_race=22,
        observed_stint_clean_ages=(3, 4, 5, 6, 7),
        observed_stint_clean_times_s=(94.52, 94.55, 94.58, 94.60, 94.63),
        observed_stint_clean_progress=(20 / 56.0, 21 / 56.0, 22 / 56.0, 23 / 56.0, 24 / 56.0),
    )

    fc = v2.forecast_stint(req)
    assert len(fc.lap_forecasts) == 10
    sum_lap_means = sum(p.expected_lap_time_s for p in fc.lap_forecasts)
    assert abs(fc.expected_total_time_s - sum_lap_means) < 1e-4

    # Component A + Component B + Component C must equal expected_lap_time_s on every step
    for p in fc.lap_forecasts:
        comp_sum = (
            p.base_pace_contribution_s
            + p.race_progression_contribution_s
            + p.tire_degradation_contribution_s
        )
        assert abs(comp_sum - p.expected_lap_time_s) < 1e-5

    # Verify single-lap PaceModel compatibility via PaceModelV2.predict(PaceRequest)
    pace_req = PaceRequest(
        session_key=9472,
        driver_number=1,
        lap_number=26,
        compound="HARD",
        tyre_age_at_lap_start=8,
        track_temperature_c=34.0,
        as_of_utc=req.as_of_utc,
        circuit_short_name="Sakhir",
        driver_id="D1",
        team_id="RED_BULL",
        race_progress=26 / 56.0,
        last_clean_lap_time_s=94.63,
        median_last_3_clean_laps_s=94.60,
        median_stint_clean_laps_s=94.58,
        clean_laps_observed_current_stint=5,
        clean_laps_observed_race=22,
        last_clean_lap_age=1,
    )
    single_fc = v2.predict(pace_req)
    assert single_fc.p05_seconds < single_fc.median_seconds < single_fc.p95_seconds


def test_counterfactual_stint_scenario_comparison_and_stage2_frozen_regression():
    train_df = _build_stage3_training_df()
    # Stage 2 frozen regression test: AdditivePaceModel remains unchanged
    v1 = AdditivePaceModel().fit(train_df)
    assert v1.model_version == "PaceV1_E8_calibrated"

    v2 = PaceModelV2(degradation_model=HierarchicalTireDegradationModel()).fit(train_df)
    base_req = StintForecastRequest(
        session_key=9472,
        driver_number=1,
        cutoff_lap_number=16,
        planned_start_lap=17,
        horizon_laps=15,
        compound_relative="MEDIUM",
        compound_absolute="C2",
        tyre_age_at_start=15,
        as_of_utc=datetime(2024, 9, 1, 13, 25, 0, tzinfo=UTC),
        scenario_type=SCENARIO_CONTINUE_STINT,
        stint_number=1,
        scheduled_race_laps=56,
        track_temperature_c=34.0,
        circuit_short_name="Sakhir",
        driver_id="D1",
        team_id="RED_BULL",
        season=2024,
        regulation_era="2023_2025",
        clean_laps_observed_current_stint=14,
        clean_laps_observed_race=14,
        median_last_3_clean_laps_s=95.5,
        median_stint_clean_laps_s=95.4,
        observed_stint_clean_ages=(10, 11, 12, 13, 14),
        observed_stint_clean_times_s=(95.2, 95.3, 95.4, 95.5, 95.6),
    )

    scenarios = [
        HypotheticalStintScenario(
            label="stay_out_old_medium",
            scenario_type=SCENARIO_CONTINUE_STINT,
            compound_relative="MEDIUM",
            compound_absolute="C2",
            tyre_age_at_start=15,
            horizon_laps=15,
        ),
        HypotheticalStintScenario(
            label="pit_fresh_medium",
            scenario_type=SCENARIO_NEW_FRESH,
            compound_relative="MEDIUM",
            compound_absolute="C2",
            tyre_age_at_start=0,
            horizon_laps=15,
        ),
        HypotheticalStintScenario(
            label="pit_fresh_hard",
            scenario_type=SCENARIO_NEW_FRESH,
            compound_relative="HARD",
            compound_absolute="C1",
            tyre_age_at_start=0,
            horizon_laps=15,
        ),
    ]
    results = compare_stint_scenarios(v2, base_req, scenarios)
    assert set(results.keys()) == {"stay_out_old_medium", "pit_fresh_medium", "pit_fresh_hard"}
    # Fresh MEDIUM over 15 laps at Sakhir must have faster cumulative clean-air stint time than 15-lap-old MEDIUM
    assert (
        results["pit_fresh_medium"].expected_total_time_s
        < results["stay_out_old_medium"].expected_total_time_s
    )
