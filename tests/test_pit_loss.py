from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest

from f1ri.models.contracts import PitLossRequest
from f1ri.models.pit_loss import PitLossModelV1


def request(**kwargs):
    fields = {'session_key': 9999, 'driver_number': 1, 'circuit_short_name': 'CircuitA', 'season': 2025,
                  'team_id': 'FastTeam', 'as_of_utc': datetime(2025, 1, 1, tzinfo=UTC),
                  'latest_completed_lap': 19, 'intended_pit_lap': 20, 'old_compound': 'MEDIUM',
                  'old_tyre_age': 20, 'new_compound': 'HARD', 'n_samples': 4000, 'retain_samples': True}
    fields.update(kwargs)
    return PitLossRequest(**fields)


def test_physical_recovery_and_distribution(synthetic_pit_population):
    model = PitLossModelV1(circuit_shrinkage=3).fit(synthetic_pit_population,
        training_cutoff_utc=datetime(2024, 1, 1, tzinfo=UTC))
    a = model.predict_pit_loss(request())
    b = model.predict_pit_loss(request(circuit_short_name='CircuitB'))
    assert a.support_status == 'SUPPORTED'
    assert b.expected_loss_s-a.expected_loss_s == pytest.approx(8, abs=.7)
    assert a.expected_loss_s == pytest.approx(17+model.service_.expected('FastTeam')+.4, abs=.8)
    assert a.expected_lane_duration_s == pytest.approx(22+model.service_.expected('FastTeam'), abs=.6)
    assert a.p05_loss_s <= a.p25_loss_s <= a.median_loss_s <= a.p75_loss_s <= a.p95_loss_s
    samples = np.asarray(a.sampled_losses_s)
    assert np.isfinite(samples).all()
    components = np.asarray(a.sampled_route_losses_s)+a.sampled_stationary_durations_s+a.sampled_transition_losses_s-a.sampled_reference_errors_s
    assert np.allclose(samples, components)


def test_service_contribution_counted_once(synthetic_pit_population):
    model = PitLossModelV1().fit(synthetic_pit_population, training_cutoff_utc=datetime(2024, 1, 1, tzinfo=UTC))
    a = model.predict_pit_loss(request(hypothetical_service_duration_s=2.5))
    b = model.predict_pit_loss(request(hypothetical_service_duration_s=5))
    assert b.expected_loss_s-a.expected_loss_s == pytest.approx(2.5)
    assert b.expected_lane_duration_s-a.expected_lane_duration_s == pytest.approx(2.5)
    assert b.expected_stationary_duration_s == 5
    assert np.array_equal(a.sampled_route_losses_s, b.sampled_route_losses_s)


def test_unknown_circuit_and_unsupported_stop(synthetic_pit_population):
    model = PitLossModelV1().fit(synthetic_pit_population, training_cutoff_utc=datetime(2024, 1, 1, tzinfo=UTC))
    known = model.predict_pit_loss(request())
    unseen = model.predict_pit_loss(request(circuit_short_name='NeverSeen'))
    assert unseen.support_status == 'EXTRAPOLATED'
    assert unseen.std_loss_s > known.std_loss_s
    repair = model.predict_pit_loss(request(stop_type='FRONT_WING_REPLACEMENT'))
    assert repair.support_status == 'UNSUPPORTED'


def test_forecast_modes_enforce_observability(synthetic_pit_population):
    with pytest.raises(ValueError, match='forbidden'):
        request(observed_service_duration_s=3)
    with pytest.raises(ValueError, match='not yet observable'):
        request(forecast_mode='IN_PROGRESS', observed_service_duration_s=3,
                service_observed_at_utc=datetime(2026, 1, 1, tzinfo=UTC))
    model = PitLossModelV1().fit(synthetic_pit_population, training_cutoff_utc=datetime(2024, 1, 1, tzinfo=UTC))
    req = request(forecast_mode='IN_PROGRESS', observed_service_duration_s=3,
                  service_observed_at_utc=datetime(2024, 12, 31, tzinfo=UTC))
    result = model.predict_pit_loss(req)
    assert result.remaining_loss_s == pytest.approx(result.expected_loss_s-3)
    with pytest.raises(ValueError, match='beyond'):
        model.predict_pit_loss(replace(req, as_of_utc=datetime(2022, 1, 1, tzinfo=UTC),
                                       observed_service_duration_s=None))
