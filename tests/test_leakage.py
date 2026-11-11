from datetime import UTC, datetime, timedelta

import pytest

from f1ri.models.contracts import PaceDistribution, PaceRequest
from f1ri.validation.leakage import validate_forecast_timestamp


def test_rejects_model_fitted_after_prediction_cutoff():
    now = datetime(2025, 9, 21, tzinfo=UTC)
    request = PaceRequest(101, 4, 20, "HARD", 9, 39.1, now)
    dist = PaceDistribution(91.2, 0.3, "v0", now + timedelta(seconds=1))
    with pytest.raises(ValueError):
        validate_forecast_timestamp(request, dist)
    good = PaceDistribution(91.2, 0.3, "v0", now - timedelta(days=1))
    validate_forecast_timestamp(request, good)
