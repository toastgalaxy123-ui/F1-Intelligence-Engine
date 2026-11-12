"""Pit-only contracts, re-exported from contracts.py without changing Pace V2."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class PitStatusSegment:
    """Explicit assumed future state, in seconds relative to projected pit entry."""

    start_s: float
    end_s: float
    status: str

    def __post_init__(self) -> None:
        if not all(math.isfinite(v) for v in (self.start_s, self.end_s)):
            raise ValueError("Scenario boundaries must be finite")
        if self.end_s <= self.start_s or self.status not in {"GREEN", "VSC", "SC"}:
            raise ValueError("Invalid pit scenario segment")


@dataclass(frozen=True)
class PitLossRequest:
    session_key: int
    driver_number: int
    circuit_short_name: str
    season: int
    team_id: str
    as_of_utc: datetime
    latest_completed_lap: int
    intended_pit_lap: int
    old_compound: str
    old_tyre_age: int
    new_compound: str
    new_tyre_initial_age: int = 0
    circuit_key: int | None = None
    layout_version: str | None = None
    pit_speed_limit_kph: float | None = None
    track_status: str = "GREEN"
    forecast_mode: str = "PRE_STOP"
    scenario_mode: str = "INFORMATION_LIMITED"
    future_status_scenario: tuple[PitStatusSegment, ...] = ()
    stop_type: str = "ROUTINE_TIRE_CHANGE"
    hypothetical_service_duration_s: float | None = None
    observed_service_duration_s: float | None = None
    service_observed_at_utc: datetime | None = None
    known_penalty_s: float = 0.0
    track_temperature_c: float | None = None
    rainfall: bool | None = None
    pit_lane_open: bool | None = None
    race_control_constraints: tuple[str, ...] = ()
    pace_model_version: str = "stage3-frozen-7b12014"
    n_samples: int = 2000
    random_seed: int = 42
    retain_samples: bool = False

    def __post_init__(self) -> None:
        if self.as_of_utc.tzinfo is None:
            raise ValueError("Pit forecast cutoff must be timezone-aware")
        if self.intended_pit_lap <= self.latest_completed_lap:
            raise ValueError("Intended pit lap must follow the latest completed lap")
        if min(self.old_tyre_age, self.new_tyre_initial_age) < 0 or self.n_samples < 20:
            raise ValueError("Invalid tyre age or sample count (minimum 20)")
        if self.forecast_mode not in {"PRE_STOP", "IN_PROGRESS"}:
            raise ValueError("Use the measurement API for post-stop reconstruction")
        if self.scenario_mode not in {"INFORMATION_LIMITED", "CONDITIONAL_SCENARIO"}:
            raise ValueError("Unknown scenario mode")
        if self.future_status_scenario and self.scenario_mode != "CONDITIONAL_SCENARIO":
            raise ValueError("Future status requires an explicit conditional scenario")
        for prev, nxt in zip(self.future_status_scenario, self.future_status_scenario[1:]):
            if not math.isclose(prev.end_s, nxt.start_s, abs_tol=1e-9):
                raise ValueError("Scenario timeline must be ordered, contiguous and non-overlapping")
        if self.track_status not in {"GREEN", "VSC", "SC", "MIXED", "UNKNOWN", "RED"}:
            raise ValueError("Unknown track status")
        if self.observed_service_duration_s is not None:
            if self.forecast_mode != "IN_PROGRESS":
                raise ValueError("Realized stationary duration is forbidden in pre-stop forecasts")
            if (self.service_observed_at_utc is None
                    or self.service_observed_at_utc.tzinfo is None
                    or self.service_observed_at_utc > self.as_of_utc):
                raise ValueError("Service duration is not yet observable at this cutoff")
        if (self.observed_service_duration_s is not None
                and self.hypothetical_service_duration_s is not None):
            raise ValueError("Choose observed service or a hypothetical service scenario")
        for value in (self.hypothetical_service_duration_s, self.observed_service_duration_s,
                      self.known_penalty_s, self.pit_speed_limit_kph):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("Service, penalty and speed values must be finite and nonnegative")


@dataclass(frozen=True)
class PitSharedScenario:
    """One parameter draw shared by stops at the same circuit in one simulated race."""

    circuit_short_name: str
    route_standard_normal: tuple[float, ...]
    pace_reference_standard_normal: tuple[float, ...]


@dataclass(frozen=True)
class PitLossDistribution:
    expected_loss_s: float
    median_loss_s: float
    std_loss_s: float
    p25_loss_s: float
    p75_loss_s: float
    p05_loss_s: float
    p95_loss_s: float
    expected_lane_duration_s: float
    expected_stationary_duration_s: float | None
    expected_route_loss_s: float
    expected_transition_loss_s: float | None
    delay_probability: float | None
    track_status_scenario: str
    support_status: str
    confidence_level: str
    n_circuit_events: int
    model_version: str
    pace_reference_version: str
    trained_through_utc: datetime
    forecast_as_of_utc: datetime
    assumptions: tuple[str, ...]
    exclusions: tuple[str, ...]
    includes_stationary_service: bool = True
    includes_pit_out_transition: bool = True
    sampled_losses_s: tuple[float, ...] | None = None
    sampled_lane_durations_s: tuple[float, ...] | None = None
    sampled_stationary_durations_s: tuple[float, ...] | None = None
    sampled_route_losses_s: tuple[float, ...] | None = None
    sampled_transition_losses_s: tuple[float, ...] | None = None
    sampled_reference_errors_s: tuple[float, ...] | None = None
    remaining_loss_s: float | None = None

    def __post_init__(self) -> None:
        values = (self.expected_loss_s, self.median_loss_s, self.std_loss_s,
                  self.p25_loss_s, self.p75_loss_s, self.p05_loss_s, self.p95_loss_s,
                  self.expected_lane_duration_s, self.expected_route_loss_s)
        if not all(math.isfinite(v) for v in values) or self.std_loss_s < 0:
            raise ValueError("Pit distribution contains invalid values")
        if not self.p05_loss_s <= self.p25_loss_s <= self.median_loss_s <= self.p75_loss_s <= self.p95_loss_s:
            raise ValueError("Pit distribution quantiles are not ordered")
        if self.trained_through_utc > self.forecast_as_of_utc:
            raise ValueError("Pit model was trained after the prediction cutoff")


class PitLossModel(Protocol):
    def predict_pit_loss(
        self, request: PitLossRequest, shared_scenario: PitSharedScenario | None = None,
    ) -> PitLossDistribution: ...
