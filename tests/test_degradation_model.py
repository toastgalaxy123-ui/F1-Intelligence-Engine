from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from f1ri.models.degradation_hierarchical import HierarchicalTireDegradationModel
from f1ri.models.degradation_linear import LinearTireDegradationModel
from f1ri.models.degradation_nonlinear import NonlinearTireDegradationModel


def _build_collinear_fuel_and_degradation_fixture() -> pd.DataFrame:
    """Synthetic dataset where lap number and tire age increase simultaneously during long stints."""
    rng = np.random.default_rng(42)
    t0 = datetime(2024, 9, 1, 13, 0, 0, tzinfo=UTC)
    rows = []
    # True parameters:
    # - Fuel burn slope: -1.65 s over full race progress (0 -> 1)
    # - Tire degradation slopes:
    #   - Sakhir (high deg): SOFT=0.075 s/lap, MEDIUM=0.048 s/lap, HARD=0.030 s/lap
    #   - Monza (low deg): SOFT=0.045 s/lap, MEDIUM=0.026 s/lap, HARD=0.015 s/lap
    circuits = [
        ("Sakhir", 94.0, {"SOFT": (-0.45, 0.075, "C3"), "MEDIUM": (0.0, 0.048, "C2"), "HARD": (0.35, 0.030, "C1")}),
        ("Monza", 81.5, {"SOFT": (-0.40, 0.045, "C5"), "MEDIUM": (0.0, 0.026, "C4"), "HARD": (0.32, 0.015, "C3")}),
    ]
    total_laps = 56
    for s_idx, (circ, base_pace, cmpd_specs) in enumerate(circuits):
        for race_rep in range(3):
            sk = 9000 + s_idx * 10 + race_rep
            for drv_num in range(1, 11):
                drv_off = (drv_num - 5.5) * 0.12
                # Alternate strategy across drivers so pit stops occur at different laps (breaking collinearity)
                pit_lap = 16 + (drv_num % 5) * 4
                stint1_cmpd = "SOFT" if drv_num % 2 == 0 else "MEDIUM"
                stint2_cmpd = "HARD" if drv_num % 3 != 0 else "MEDIUM"
                for lap in range(2, total_laps + 1):
                    if lap == pit_lap or lap == pit_lap + 1:
                        continue
                    stint_num = 1 if lap < pit_lap else 2
                    cmpd = stint1_cmpd if stint_num == 1 else stint2_cmpd
                    stint_lap = lap if stint_num == 1 else (lap - pit_lap)
                    tyre_age = stint_lap - 1
                    off_c, slope_c, pirelli = cmpd_specs[cmpd]
                    prog = lap / float(total_laps)
                    cliff = 0.08 * (max(0.0, float(tyre_age) - 20.0) / 10.0) ** 2
                    true_wear = off_c + slope_c * tyre_age + cliff
                    noise = float(rng.normal(0.0, 0.03))
                    lap_dur = base_pace + drv_off - 1.65 * prog + true_wear + noise
                    rows.append(
                        {
                            "session_key": sk,
                            "driver_number": drv_num,
                            "driver_id": f"D{drv_num}",
                            "team_id": f"T{drv_num % 5}",
                            "stint_id": f"{sk}_{drv_num}_{stint_num}",
                            "stint_number": stint_num,
                            "lap_number": lap,
                            "requested_horizon": 1,
                            "forecast_as_of_utc": t0 + timedelta(seconds=lap * 90),
                            "regulation_era": "2023_2025",
                            "circuit_short_name": circ,
                            "compound_relative": cmpd,
                            "compound_absolute": pirelli,
                            "tyre_age_at_lap_start": tyre_age,
                            "race_progress": prog,
                            "last_known_track_temp_c": 34.0,
                            "is_clean_green_pace_lap": True,
                            "target_is_clean_green": True,
                            "observed_lap_time_s": lap_dur,
                            "observed_target_lap_time_s": lap_dur,
                        }
                    )
    return pd.DataFrame(rows)


def test_linear_and_nonlinear_degradation_parameter_recovery():
    df = _build_collinear_fuel_and_degradation_fixture()

    # 1. D2: Joint linear race-progression + compound degradation model
    m_d2 = LinearTireDegradationModel(joint_race_progress=True, ridge_alpha=5.0).fit(df)
    # Fuel burn slope must be negative, while tire wear slopes must be strictly positive and ordered SOFT > MEDIUM > HARD
    assert m_d2.progress_slope_s_per_race_ < -1.0
    assert (
        m_d2.compound_age_slopes_s_["SOFT"]
        > m_d2.compound_age_slopes_s_["MEDIUM"]
        > m_d2.compound_age_slopes_s_["HARD"]
        > 0.015
    )
    assert m_d2.is_confounded_warning_ is False

    # 2. D3: Nonlinear warm-up + stable + late-stint cliff model
    m_d3 = NonlinearTireDegradationModel(ridge_alpha=5.0).fit(df)
    assert m_d3.progress_slope_s_per_race_ < -1.0
    assert (
        m_d3.compound_linear_slopes_s_["SOFT"]
        > m_d3.compound_linear_slopes_s_["MEDIUM"]
        > m_d3.compound_linear_slopes_s_["HARD"]
        > 0.015
    )
    e5 = m_d3.evaluate_tire("HARD", 5.0, circuit_short_name="Sakhir")
    e25 = m_d3.evaluate_tire("HARD", 25.0, circuit_short_name="Sakhir")
    assert e25.age_degradation_s > e5.age_degradation_s


def test_hierarchical_circuit_pooling_and_collinear_fuel_safeguard():
    df = _build_collinear_fuel_and_degradation_fixture()

    m_d5 = HierarchicalTireDegradationModel(
        ridge_alpha=5.0,
        use_absolute_compounds=True,
        use_circuit_pooling=True,
        use_nonlinear_curve=True,
    ).fit(df)

    # Safeguard check: tire degradation slopes must all be strictly positive (not inverted by negative fuel burn!)
    for cmpd, slope in m_d5.base_model_.compound_linear_slopes_s_.items():
        assert slope > 0.01, f"Expected positive degradation slope for {cmpd}, got {slope}"

    # Circuit hierarchy check: Sakhir degradation on MEDIUM at age 15 must exceed Monza degradation on MEDIUM at age 15
    sakhir_eval = m_d5.evaluate_tire(
        "MEDIUM",
        15.0,
        compound_absolute="C2",
        circuit_short_name="Sakhir",
        regulation_era="2023_2025",
    )
    monza_eval = m_d5.evaluate_tire(
        "MEDIUM",
        15.0,
        compound_absolute="C4",
        circuit_short_name="Monza",
        regulation_era="2023_2025",
    )
    assert sakhir_eval.age_degradation_s > monza_eval.age_degradation_s > 0.0
    assert sakhir_eval.local_slope_s_per_lap > monza_eval.local_slope_s_per_lap > 0.0
