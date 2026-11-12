import pandas as pd
import pytest

from f1ri.ingestion.pit_lane_reference import load_pit_lane_reference, pit_reference_as_of


def test_reference_availability_and_effective_period(tmp_path):
    path = tmp_path/'reference.csv'
    pd.DataFrame([{'reference_id': 'one', 'circuit_short_name': 'A', 'season': 2025,
                   'layout_version': 'v1', 'pit_speed_limit_kph': 60,
                   'effective_from_utc': '2025-01-01Z'.replace('01Z', '01T00:00:00Z'),
                   'effective_to_utc': '2026-01-01T00:00:00Z',
                   'available_as_of_utc': '2025-03-01T00:00:00Z', 'source_url': 'verified'}]).to_csv(path, index=False)
    ref = load_pit_lane_reference(path)
    assert not pit_reference_as_of(ref, 'A', 2025, pd.Timestamp('2025-02-01', tz='UTC'))
    assert pit_reference_as_of(ref, 'A', 2025, pd.Timestamp('2025-04-01', tz='UTC'))['pit_speed_limit_kph'] == 60
    dup = pd.concat([ref, ref], ignore_index=True)
    dup.to_csv(path, index=False)
    with pytest.raises(ValueError, match='Duplicate'):
        load_pit_lane_reference(path)
