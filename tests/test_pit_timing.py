from datetime import timedelta

from f1ri.features.pit_events import reconstruct_pit_events, status_overlap


def event(frames):
    return reconstruct_pit_events(frames['pit'], frames['laps'], frames['stints'],
                                  frames['sessions'].iloc[0].to_dict(), frames['track_intervals']).iloc[0]


def test_raw_out_marker_is_required(pit_frames):
    pit_frames['laps']['is_pit_out_lap'] = False
    assert not event(pit_frames).eligible_primary_label


def test_alternate_raw_lap_convention_reconciles(pit_frames):
    pit_frames['pit']['lap_number'] = 11
    assert event(pit_frames).resolved_in_lap == 10


def test_report_timestamp_is_not_assumed_to_be_entry(pit_frames):
    visit = event(pit_frames)
    assert visit.estimated_lane_exit_utc == visit.pit_timestamp_utc
    assert (visit.pit_timestamp_utc-visit.estimated_lane_entry_utc).total_seconds() == 26
    assert 'BACKWARDS' in visit.lane_boundary_method


def test_inconsistent_lap_clock_is_flagged(pit_frames):
    pit_frames['laps'].loc[(pit_frames['laps'].driver_number == 1) & (pit_frames['laps'].lap_number == 11), 'date_start'] += timedelta(seconds=9)
    assert 'LAP_BOUNDARY_DISAGREEMENT' in event(pit_frames).quality_flags_json


def test_mixed_and_gapped_track_state(pit_frames):
    intervals = pit_frames['track_intervals']
    start = pit_frames['sessions'].iloc[0].date_start
    split = start+timedelta(seconds=900)
    intervals.loc[0, 'end_utc'] = split
    intervals.loc[1] = [split, start+timedelta(hours=3), 'VSC']
    assert event(pit_frames).transition_track_status == 'MIXED'
    assert not event(pit_frames).eligible_primary_label
    intervals.loc[1, 'start_utc'] += timedelta(seconds=1)
    state, _ = status_overlap(intervals, split-timedelta(seconds=5), split+timedelta(seconds=5))
    assert state == 'UNKNOWN'
