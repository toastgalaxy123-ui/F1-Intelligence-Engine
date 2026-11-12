from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest

from f1ri.models.contracts import PitLossRequest
from f1ri.models.pit_loss import PitLossModelV1
from f1ri.models.pit_uncertainty import empirical_crps, sample_shared_pit_scenario


def test_reproducible_and_shared_parameter_samples(synthetic_pit_population):
    model = PitLossModelV1().fit(synthetic_pit_population, training_cutoff_utc=datetime(2024, 1, 1, tzinfo=UTC))
    req = PitLossRequest(999, 1, 'CircuitA', 2025, 'FastTeam', datetime(2025, 1, 1, tzinfo=UTC),
                         10, 11, 'MEDIUM', 10, 'HARD', n_samples=4000, retain_samples=True)
    scenario = sample_shared_pit_scenario('CircuitA', req.n_samples, 123)
    a = model.predict_pit_loss(req, scenario)
    assert a == model.predict_pit_loss(req, scenario)
    b = model.predict_pit_loss(replace(req, random_seed=997), scenario)
    assert a.sampled_reference_errors_s == b.sampled_reference_errors_s
    assert a.sampled_losses_s != b.sampled_losses_s
    with pytest.raises(ValueError, match='match circuit'):
        model.predict_pit_loss(replace(req, circuit_short_name='CircuitB'), scenario)
    assert empirical_crps(np.array([2., 2., 2.]), 3.) == 1.
