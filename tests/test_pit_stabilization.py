"""A test cohort must never be opened by model fitting or a stale lock."""
import hashlib
import json

import pytest

from f1ri.validation.pit_stabilization import (
    evaluate_locked_holdouts,
    validate_and_lock,
    validate_study_design,
    verify_selection_lock,
)


def config():
    return {'experiment': {'name': 'study'}, 'cohort': {
        'train': [{'session_key': 1}], 'val': [{'session_key': 2}],
        'test': [{'session_key': 3, 'population': 'familiar'}, {'session_key': 4, 'population': 'unseen'}],
    }}


def test_study_design_rejects_shared_sessions_and_missing_population():
    cfg = config()
    validate_study_design(cfg)
    cfg['cohort']['test'][0]['session_key'] = 1
    with pytest.raises(ValueError, match='disjoint'):
        validate_study_design(cfg)
    cfg = config()
    cfg['cohort']['test'][0]['population'] = 'unseen'
    with pytest.raises(ValueError, match='familiar and unseen'):
        validate_study_design(cfg)


def test_outcome_access_permanently_blocks_refitting(tmp_path):
    run = tmp_path/'artifacts/runs/study'
    run.mkdir(parents=True)
    (run/'test_access_log.json').write_text('{}')
    with pytest.raises(ValueError, match='do not retune'):
        validate_and_lock(tmp_path, config(), tmp_path/'config.yaml')


def test_missing_lock_prevents_ingestion_and_any_test_access(tmp_path, monkeypatch):
    def forbidden(*args):
        pytest.fail('Holdout ingestion occurred before verifying a selection lock')
    monkeypatch.setattr('f1ri.validation.pit_stabilization._ingest_entries', forbidden)
    with pytest.raises(FileNotFoundError):
        evaluate_locked_holdouts(tmp_path, config(), tmp_path/'config.yaml', ingest=True)
    assert not (tmp_path/'artifacts/runs/study/test_access_log.json').exists()


def test_configuration_drift_invalidates_saved_selection(tmp_path):
    path = tmp_path/'config.yaml'
    path.write_text('version: 1\n')
    run = tmp_path/'artifacts/runs/study'
    run.mkdir(parents=True)
    (run/'validation_selection.lock.json').write_text(json.dumps({
        'config_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
    }))
    path.write_text('version: 2\n')
    with pytest.raises(ValueError, match='configuration changed'):
        verify_selection_lock(tmp_path, config(), path)
