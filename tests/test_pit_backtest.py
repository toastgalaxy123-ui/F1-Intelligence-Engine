from datetime import UTC, datetime

import pandas as pd
import pytest

from f1ri.validation.pit_backtest import run_stage4_pipeline
from f1ri.validation.pit_metrics import paired_pit_comparison, pit_metrics
from f1ri.validation.splits import build_split_manifest, validate_stage4_holdout


def test_reserved_stage3_races_cannot_be_called_untouched():
    entries = [{'session_key': 101, 'year': 2023, 'split': 'train'},
               {'session_key': 202, 'year': 2025, 'split': 'test'}]
    manifest = build_split_manifest(entries)
    with pytest.raises(ValueError, match='newly reserved'):
        validate_stage4_holdout(manifest, {202})


def test_race_bootstrap_and_identical_event_population():
    rows = []
    for race in range(6):
        for stop in range(4):
            for model, predicted in [('P1', 22.), ('P8', 20.5)]:
                rows.append({'pit_event_id': f'{race}:{stop}', 'session_key': race, 'split': 'test',
                             'model_id': model, 'eligible_primary_label': True,
                             'target_transition_loss_s': 20., 'expected_loss_s': predicted,
                             'p05_loss_s': 18., 'p25_loss_s': 19., 'p75_loss_s': 21., 'p95_loss_s': 23.})
    data = pd.DataFrame(rows)
    result = paired_pit_comparison(data, 'P8', 'P1')
    assert result['n_events'] == 24
    assert result['n_races'] == 6
    assert result['race_mean_mae_delta_s'] == -1.5
    assert result['p95_delta_s'] < 0
    metric = pit_metrics(data[data.model_id == 'P8'])
    assert metric['coverage_90'] == 1.
    data.loc[(data.model_id == 'P8') & (data.session_key == 1), 'target_transition_loss_s'] = 25
    with pytest.raises(ValueError, match='identical targets'):
        paired_pit_comparison(data, 'P8', 'P1')


def test_missing_snapshots_fail_before_zero_event_success(tmp_path):
    config = tmp_path/'experiment.yaml'
    config.write_text('experiment:\n  name: stage4_test\ncohort:\n  train:\n    - {year: 2023, session_key: 701}\n')
    (tmp_path/'config').mkdir()
    (tmp_path/'config/default.yaml').write_text('data: {}\n')
    with pytest.raises(FileNotFoundError, match='snapshots'):
        run_stage4_pipeline(tmp_path, config)


def test_training_and_calibration_timestamp_order(synthetic_pit_population):
    from f1ri.models.pit_loss import PitLossModelV1
    model = PitLossModelV1().fit(synthetic_pit_population,
                                training_cutoff_utc=datetime(2024, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match='follow the training window'):
        model.calibrate(pd.DataFrame([{'forecast_as_of_utc': datetime(2022, 1, 1, tzinfo=UTC)}]))
