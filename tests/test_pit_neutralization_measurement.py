import json

import pandas as pd

from f1ri.validation.pit_neutralization_measurement import measure_neutralized_pit_windows


def test_same_state_field_proxy_keeps_control_provenance_and_remains_unsupported():
    start = pd.Timestamp('2024-10-20T19:00:00Z')
    event = {'pit_event_id': '1:1:1', 'session_key': 1, 'circuit_short_name': 'Example',
             'driver_number': 1, 'transition_track_status': 'VSC', 'eligible_direct_lane': True,
             'state_overlap_seconds_json': '{"VSC":220}', 'transition_start_utc': start,
             'transition_end_utc': start+pd.Timedelta(seconds=220),
             'observed_in_lap_s': 112, 'observed_out_lap_s': 108}
    laps = []
    for driver in (2, 3, 4):
        for lap in (10, 11):
            laps.append({'driver_number': driver, 'lap_number': lap, 'is_pit_lap': False,
                         'is_blue_flagged_lap': False, 'observed_lap_time_s': 100,
                         'lap_start_utc': start+pd.Timedelta(seconds=100*(lap-10)),
                         'lap_end_estimate_utc': start+pd.Timedelta(seconds=100*(lap-9))})
    intervals = pd.DataFrame([{'status': 'VSC', 'start_utc': start, 'end_utc': start+pd.Timedelta(seconds=300)}])
    result = measure_neutralized_pit_windows(pd.DataFrame([event]), pd.DataFrame(laps), intervals).iloc[0]
    assert result.conditional_elapsed_loss_s == 20
    assert result.n_controls == 3
    assert len(json.loads(result.controls_json)) == 3
    assert result.support_status == 'UNSUPPORTED'
    assert not result.anchors_verified
    intervals.loc[0, 'status'] = 'SC'
    unmatched = measure_neutralized_pit_windows(pd.DataFrame([event]), pd.DataFrame(laps), intervals).iloc[0]
    assert unmatched.n_controls == 0
    assert pd.isna(unmatched.conditional_elapsed_loss_s)
