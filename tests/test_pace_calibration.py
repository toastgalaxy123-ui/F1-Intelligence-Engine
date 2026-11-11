from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from f1ri.models.contracts import PaceRequest
from f1ri.models.pace_calibration import ResidualIntervalCalibrator
from f1ri.validation.calibration import compute_probabilistic_metrics

T0 = datetime(2025, 5, 18, 14, 0, 0, tzinfo=UTC)


def test_interval_ordering_and_calibration_coverage():
    rng = np.random.default_rng(42)
    n = 400
    preds = np.full(n, 90.0)
    noise = rng.normal(0.0, 0.32, size=n)
    cal_df = pd.DataFrame(
        {
            "observed_target_lap_time_s": preds + noise,
            "target_is_clean_green": np.ones(n, dtype=bool),
            "clean_laps_observed_current_stint": rng.integers(0, 10, size=n),
        }
    )

    calibrator = ResidualIntervalCalibrator().fit(cal_df, preds)

    eval_rows = []
    eval_noise = rng.normal(0.0, 0.32, size=n)
    for i in range(n):
        req = PaceRequest(
            session_key=9987,
            driver_number=1,
            lap_number=10,
            compound="HARD",
            tyre_age_at_lap_start=8,
            track_temperature_c=35.0,
            as_of_utc=T0,
            clean_laps_observed_current_stint=int(cal_df["clean_laps_observed_current_stint"].iloc[i]),
            clean_laps_observed_race=5,
        )
        dist = calibrator.make_distribution(90.0, req, "v1", T0)
        assert dist.p05_seconds <= dist.p25_seconds <= dist.median_seconds <= dist.p75_seconds <= dist.p95_seconds
        eval_rows.append(
            {
                "observed_target_lap_time_s": 90.0 + float(eval_noise[i]),
                "target_is_clean_green": True,
                "expected_seconds": dist.expected_seconds,
                "std_seconds": dist.std_seconds,
                "p25_seconds": dist.p25_seconds,
                "p75_seconds": dist.p75_seconds,
                "p05_seconds": dist.p05_seconds,
                "p95_seconds": dist.p95_seconds,
            }
        )

    metrics = compute_probabilistic_metrics(pd.DataFrame(eval_rows))
    assert 0.42 <= float(metrics["coverage_50"]) <= 0.58
    assert 0.84 <= float(metrics["coverage_90"]) <= 0.96
    assert float(metrics["crps_s"]) < 0.25
