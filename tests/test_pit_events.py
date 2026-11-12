import pandas as pd
import pytest

from f1ri.features.pit_events import reconstruct_pit_events


def reconstruct(frames, annotations=None):
    return reconstruct_pit_events(frames['pit'], frames['laps'], frames['stints'],
                                  frames['sessions'].iloc[0].to_dict(), frames['track_intervals'],
                                  frames['drivers'], annotations)


def test_reconciled_identity_and_tires(pit_frames):
    a = reconstruct(pit_frames)
    pit_frames['laps'] = pit_frames['laps'].sample(frac=1, random_state=9)
    b = reconstruct(pit_frames)
    pd.testing.assert_frame_equal(a, b)
    event = a.iloc[0]
    assert event.pit_event_id == '701:1:1'
    assert (event.resolved_in_lap, event.resolved_out_lap) == (10, 11)
    assert (event.old_compound, event.new_compound) == ('MEDIUM', 'HARD')
    assert (event.old_tyre_age, event.new_tyre_initial_age) == (10, 0)
    assert event.eligible_primary_label
    assert event.moving_lane_duration_s == 23.5


def test_duplicates_retained_and_conflicts_excluded(pit_frames):
    pit_frames['pit'] = pd.concat([pit_frames['pit']]*2, ignore_index=True)
    event = reconstruct(pit_frames).iloc[0]
    assert event.raw_duplicate_count == 2
    assert len(reconstruct(pit_frames)) == 1
    pit_frames['pit'].loc[1, 'lane_duration'] = 45
    events = reconstruct(pit_frames)
    assert not events.eligible_primary_label.any()
    assert events.quality_flags_json.str.contains('CONFLICTING').all()


def test_missing_stationary_is_not_zero(pit_frames):
    pit_frames['pit']['stop_duration'] = None
    event = reconstruct(pit_frames).iloc[0]
    assert pd.isna(event.stop_duration_s)
    assert pd.isna(event.moving_lane_duration_s)
    assert event.eligible_primary_label


@pytest.mark.parametrize('stop,lane', [(30, 26), (-1, 26), (2, -1)])
def test_invalid_durations_are_auditable(pit_frames, stop, lane):
    pit_frames['pit']['stop_duration'] = stop
    pit_frames['pit']['lane_duration'] = lane
    event = reconstruct(pit_frames).iloc[0]
    assert not event.eligible_primary_label
    assert event.timing_quality == 'C'


def test_long_duration_is_not_repair_evidence(pit_frames):
    pit_frames['pit']['stop_duration'] = 12
    event = reconstruct(pit_frames).iloc[0]
    assert event.pit_event_type == 'ROUTINE_TIRE_CHANGE'
    annotations = pd.DataFrame([{'pit_event_id': event.pit_event_id, 'event_type': 'FRONT_WING_REPLACEMENT',
                                  'evidence_source': 'independently reviewed event log'}])
    repaired = reconstruct(pit_frames, annotations).iloc[0]
    assert repaired.pit_event_type == 'FRONT_WING_REPLACEMENT'
    assert not repaired.eligible_primary_label
