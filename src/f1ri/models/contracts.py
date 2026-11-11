"""Stable interfaces so model upgrades don't change simulation code."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class PaceRequest:
    session_key: int
    driver_number: int
    lap_number: int
    compound: str
    tyre_age_at_lap_start: int
    track_temperature_c: float | None
    # Caller must provide only information known at the forecast cutoff.
    as_of_utc: datetime


@dataclass(frozen=True)
class PaceDistribution:
    expected_seconds: float
    std_seconds: float
    model_version: str
    trained_through_utc: datetime


class PaceModel(Protocol):
    def predict(self, request: PaceRequest) -> PaceDistribution: ...
