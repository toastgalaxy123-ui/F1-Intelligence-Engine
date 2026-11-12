from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest

from f1ri.models.contracts import PitLossRequest, StintTrajectoryForecast
from f1ri.models.pit_loss import PitLossModelV1
from f1ri.simulator.pit import compare_pit_strategy_times, sample_pit_transition


def trajectory(total, as_of, samples=None):
    return StintTrajectoryForecast(
        session_key=999, driver_number=1, cutoff_lap_number=9, planned_start_lap=10,
        horizon_laps=2, scenario_type='CONTINUE_CURRENT_STINT', compound_relative='MEDIUM',
        compound_absolute=None, tyre_age_at_start=10, as_of_utc=as_of, lap_forecasts=(),
        expected_total_time_s=total, median_total_time_s=total, std_total_time_s=1.,
        p25_total_time_s=total-.67, p75_total_time_s=total+.67,
        p05_total_time_s=total-1.64, p95_total_time_s=total+1.64,
        support_status='SUPPORTED', extrapolation_warning=None, model_version='frozen-stage3',
        degradation_model_version='frozen-stage3', trained_through_utc=datetime(2024, 1, 1, tzinfo=UTC),
        sampled_lap_trajectories_s=samples,
    )


def test_equal_distance_accounting_and_no_pit(synthetic_pit_population):
    cutoff = datetime(2025, 1, 1, tzinfo=UTC)
    request = PitLossRequest(999, 1, 'CircuitA', 2025, 'FastTeam', cutoff, 9, 10,
                             'MEDIUM', 10, 'HARD', retain_samples=True)
    model = PitLossModelV1().fit(synthetic_pit_population,
                               training_cutoff_utc=datetime(2024, 1, 1, tzinfo=UTC))
    pit = model.predict_pit_loss(request)
    stay, stop = trajectory(184., cutoff), trajectory(180., cutoff)
    result = compare_pit_strategy_times(stay, stop, pit)
    assert result['stop_expected_s'] == 180+pit.expected_loss_s
    assert result['stop_minus_continue_s'] == pytest.approx(pit.expected_loss_s-4)
    assert compare_pit_strategy_times(stay, stay, None)['stop_minus_continue_s'] == 0
    with pytest.raises(ValueError, match='same driver'):
        compare_pit_strategy_times(stay, replace(stop, horizon_laps=3), pit)
    entry = cutoff.replace(hour=1)
    event = sample_pit_transition(pit, entry_utc=entry, new_compound='HARD', new_tyre_initial_age=0)
    assert (event.projected_rejoin_utc-entry).total_seconds() == pytest.approx(pit.sampled_lane_durations_s[0], abs=1e-6)
    assert event.lane_duration_s >= event.service_duration_s


def test_shared_reference_error_cancels_in_pace_plus_pit(synthetic_pit_population):
    cutoff = datetime(2025, 1, 1, tzinfo=UTC)
    request = PitLossRequest(999, 1, 'CircuitA', 2025, 'FastTeam', cutoff, 9, 10,
                             'MEDIUM', 10, 'HARD', retain_samples=True)
    model = PitLossModelV1().fit(synthetic_pit_population,
                               training_cutoff_utc=datetime(2024, 1, 1, tzinfo=UTC))
    pit = model.predict_pit_loss(request)
    # Same reference nuisance draw has positive sign in normal pace and
    # negative sign in observed-minus-reference pit loss.
    samples = tuple((90+error, 90.) for error in pit.sampled_reference_errors_s)
    result = compare_pit_strategy_times(trajectory(184, cutoff), trajectory(180, cutoff, samples),
                                        pit, shared_reference_error=True)
    physical = 180+np.asarray(pit.sampled_route_losses_s)+pit.sampled_stationary_durations_s+pit.sampled_transition_losses_s
    assert np.allclose(result['sampled_stop_total_s'], physical)
