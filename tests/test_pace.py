import pandas as pd
import pytest

from f1ri.models.pace import MedianPaceBaseline


def test_baseline_uses_only_fit_rows():
    train = pd.DataFrame({
        "driver_number": [1, 1, 1, 2],
        "compound": ["HARD", "HARD", "HARD", "MEDIUM"],
        "observed_lap_time_s": [91.0, 92.0, 101.0, 95.0],
    })
    model = MedianPaceBaseline().fit(train)
    assert model.predict_seconds(1, "HARD") == 92.0
    assert model.predict_seconds(3, "SOFT") == 93.5
    assert model.predict_seconds(1, "HARD") != 90.0


def test_baseline_requires_training():
    with pytest.raises(RuntimeError):
        MedianPaceBaseline().predict_seconds(1, "SOFT")
