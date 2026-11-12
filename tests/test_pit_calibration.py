from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest

from f1ri.models.contracts import PitLossRequest
from f1ri.models.pit_calibration import CalibratedPitLossModel
from f1ri.models.pit_loss import PitLossModelV1


def setup_model(population):
    model = PitLossModelV1().fit(population, training_cutoff_utc=datetime(2024, 1, 1, tzinfo=UTC))
    request = PitLossRequest(999, 1, 'CircuitA', 2025, 'FastTeam', datetime(2025, 1, 1, tzinfo=UTC),
                             10, 11, 'MEDIUM', 10, 'HARD', n_samples=2000, retain_samples=True)
    return model, request


def test_can_narrow_and_widen_without_changing_mean_or_reference(synthetic_pit_population):
    model, request = setup_model(synthetic_pit_population)
    narrow = CalibratedPitLossModel(model, .5).predict_pit_loss(request)
    wide = CalibratedPitLossModel(model, 1.5).predict_pit_loss(request)
    base = model.predict_pit_loss(request)
    assert narrow.std_loss_s < base.std_loss_s < wide.std_loss_s
    assert narrow.expected_loss_s == pytest.approx(base.expected_loss_s)
    assert narrow.sampled_reference_errors_s == base.sampled_reference_errors_s
    assert narrow.sampled_stationary_durations_s == base.sampled_stationary_durations_s
    total = (np.asarray(narrow.sampled_route_losses_s)+narrow.sampled_stationary_durations_s
             +narrow.sampled_transition_losses_s-narrow.sampled_reference_errors_s)
    assert np.allclose(total, narrow.sampled_losses_s)


def test_conditional_service_delta_counted_once_after_calibration(synthetic_pit_population):
    base, req = setup_model(synthetic_pit_population)
    calibrated = CalibratedPitLossModel(base, .5)
    a = calibrated.predict_pit_loss(replace(req, hypothetical_service_duration_s=2.5))
    b = calibrated.predict_pit_loss(replace(req, hypothetical_service_duration_s=5))
    assert b.expected_loss_s-a.expected_loss_s == pytest.approx(2.5)
    assert np.allclose(np.asarray(b.sampled_losses_s)-a.sampled_losses_s, 2.5)
    assert np.allclose(a.sampled_route_losses_s, b.sampled_route_losses_s)


def test_future_calibration_is_rejected(synthetic_pit_population):
    base, req = setup_model(synthetic_pit_population)
    with pytest.raises(ValueError, match='after this forecast'):
        CalibratedPitLossModel(base, .75, datetime(2026, 1, 1, tzinfo=UTC)).predict_pit_loss(req)
