import numpy as np

from f1ri.models.pit_service import StationaryServiceModel


def test_empirical_mixture_tail_and_partial_pooling(synthetic_pit_population):
    data = synthetic_pit_population
    model = StationaryServiceModel().fit(data)
    assert len(model.global_values_) == int(data.stop_duration_s.notna().sum())
    assert model.expected('FastTeam') < model.expected('OtherTeam')
    samples = model.sample('FastTeam', 20000, np.random.default_rng(13))
    assert samples.min() > 0
    assert .02 < np.mean(samples > 4.5) < .16
    assert np.mean(samples > 4.5) == np.round(model.delay_probability('FastTeam'), 2) or abs(
        np.mean(samples > 4.5)-model.delay_probability('FastTeam')) < .01


def test_no_stationary_measurements_means_unavailable(synthetic_pit_population):
    data = synthetic_pit_population.copy()
    data['stop_duration_s'] = None
    model = StationaryServiceModel().fit(data)
    assert not model.available
    assert model.expected('Unknown') is None
    assert model.sample('Unknown', 100, np.random.default_rng(1)) is None
