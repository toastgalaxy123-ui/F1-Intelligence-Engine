"""Pace/pit time accounting, deliberately without positions or strategy selection."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from f1ri.models.contracts import PitLossDistribution, StintTrajectoryForecast


@dataclass(frozen=True)
class PitTransitionEvent:
    projected_entry_utc: datetime
    projected_rejoin_utc: datetime
    service_duration_s: float | None
    lane_duration_s: float
    net_loss_s: float
    new_compound: str
    new_tyre_initial_age: int


def sample_pit_transition(
    forecast: PitLossDistribution, *, entry_utc: datetime, new_compound: str,
    new_tyre_initial_age: int, sample_index: int = 0,
) -> PitTransitionEvent:
    if forecast.support_status == "UNSUPPORTED":
        raise ValueError("Unsupported pit scenario cannot be inserted into a simulation")
    if entry_utc.tzinfo is None or entry_utc < forecast.forecast_as_of_utc:
        raise ValueError("Projected pit entry must follow the forecast cutoff")
    if forecast.sampled_losses_s is None or forecast.sampled_lane_durations_s is None:
        raise ValueError("Request retained samples for pit timeline integration")
    lane = forecast.sampled_lane_durations_s[sample_index]
    service = forecast.sampled_stationary_durations_s[sample_index] if forecast.sampled_stationary_durations_s else None
    return PitTransitionEvent(entry_utc, entry_utc+timedelta(seconds=lane), service, lane,
                              forecast.sampled_losses_s[sample_index], new_compound,
                              new_tyre_initial_age)


def compare_pit_strategy_times(
    continue_laps: StintTrajectoryForecast, stop_laps: StintTrajectoryForecast,
    pit: PitLossDistribution | None, *, shared_reference_error: bool = False,
) -> dict:
    """Both clean trajectories include identical lap numbers and distance.

    The caller supplies the old-tire in-lap plus the planned new-tire out-lap
    and remaining laps as a combined normal-lap trajectory. Add the NET pit
    penalty once. Lane time and separate tire warm-up are never added.
    """
    if (continue_laps.planned_start_lap != stop_laps.planned_start_lap
            or continue_laps.horizon_laps != stop_laps.horizon_laps
            or continue_laps.as_of_utc != stop_laps.as_of_utc
            or continue_laps.session_key != stop_laps.session_key
            or continue_laps.driver_number != stop_laps.driver_number):
        raise ValueError("Strategy trajectories must cover the same driver, cutoff and lap window")
    penalty = pit.expected_loss_s if pit else 0.0
    if pit:
        if pit.forecast_as_of_utc != stop_laps.as_of_utc:
            raise ValueError("Pace and pit forecasts must use the same cutoff")
        if pit.support_status == "UNSUPPORTED" or pit.track_status_scenario != "GREEN":
            raise ValueError("This clean-air comparison supports green stops only")
        if not pit.includes_pit_out_transition or not pit.includes_stationary_service:
            raise ValueError("Incomplete pit component ownership")
    result = {"continue_expected_s": continue_laps.expected_total_time_s,
              "stop_expected_s": stop_laps.expected_total_time_s+penalty,
              "pit_penalty_s": penalty,
              "stop_minus_continue_s": stop_laps.expected_total_time_s+penalty-continue_laps.expected_total_time_s,
              "assumptions": "Equal-distance clean-air comparison; no traffic, positions or future neutralizations"}
    if shared_reference_error:
        if not pit or pit.sampled_reference_errors_s is None or pit.sampled_losses_s is None:
            raise ValueError("Shared reference accounting needs retained pit samples")
        if stop_laps.sampled_lap_trajectories_s is None:
            raise ValueError("Shared reference accounting needs retained pace samples")
        totals = np.sum(stop_laps.sampled_lap_trajectories_s, axis=1)
        if len(totals) != len(pit.sampled_losses_s):
            raise ValueError("Pace and pit samples must share scenario indices")
        # Caller certifies that the same +reference draw is in normal pace.
        # Pit losses already contain -reference; adding them cancels this
        # nuisance reference uncertainty, as observed-minus-reference requires.
        result["sampled_stop_total_s"] = tuple(totals+np.asarray(pit.sampled_losses_s))
    return result
