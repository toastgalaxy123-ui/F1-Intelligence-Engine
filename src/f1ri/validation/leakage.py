"""Point-in-time validation helpers."""
from __future__ import annotations
from datetime import datetime
from f1ri.models.contracts import PaceDistribution, PaceRequest


def validate_forecast_timestamp(request: PaceRequest, forecast: PaceDistribution) -> None:
    if request.as_of_utc.tzinfo is None or forecast.trained_through_utc.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware")
    if forecast.trained_through_utc > request.as_of_utc:
        raise ValueError("Model uses training data later than the prediction cutoff")
