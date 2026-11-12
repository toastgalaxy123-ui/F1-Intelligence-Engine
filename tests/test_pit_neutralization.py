from dataclasses import replace
from datetime import UTC, datetime

import pandas as pd
import pytest

from f1ri.models.contracts import PitLossRequest, PitStatusSegment
from f1ri.models.pit_neutralization import PitNeutralizationModel


def req(**kwargs):
    base = {'session_key': 10, 'driver_number': 1, 'circuit_short_name': 'A', 'season': 2025, 'team_id': 'Team',
                'as_of_utc': datetime(2025, 1, 1, tzinfo=UTC), 'latest_completed_lap': 10,
                'intended_pit_lap': 11, 'old_compound': 'MEDIUM', 'old_tyre_age': 10, 'new_compound': 'HARD'}
    return PitLossRequest(**(base | kwargs))


def test_no_invented_vsc_discount():
    model = PitNeutralizationModel()
    assert model.adjustment(req())[:3] == (0., 0., 'SUPPORTED')
    mean, std, support, assumptions = model.adjustment(req(track_status='VSC'))
    assert mean == 0
    assert std >= 10
    assert support == 'UNSUPPORTED'
    assert any('persists' in s for s in assumptions)


def test_known_neutralization_and_explicit_mixed_timeline():
    controls = pd.DataFrame([{'circuit_short_name': 'A', 'track_status': 'VSC',
                              'on_track_extra_s': 8., 'session_key': i % 3,
                              'anchors_verified': True,
                              'observation_available_utc': datetime(2024, 1, 1, tzinfo=UTC)} for i in range(15)])
    model = PitNeutralizationModel().fit(controls)
    a = model.adjustment(req(track_status='VSC'))
    assert a[0] == -8
    assert a[2] == 'SUPPORTED'
    segments = (PitStatusSegment(0, 10, 'GREEN'), PitStatusSegment(10, 20, 'VSC'))
    scenario = req(track_status='MIXED', scenario_mode='CONDITIONAL_SCENARIO', future_status_scenario=segments)
    assert model.adjustment(scenario)[0] == -4
    with pytest.raises(ValueError, match='explicit conditional'):
        replace(scenario, scenario_mode='INFORMATION_LIMITED')
    assert model.adjustment(req(track_status='SC'))[2] == 'UNSUPPORTED'
