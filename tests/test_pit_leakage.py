import pickle
from datetime import UTC, datetime, timedelta

import pandas as pd

from f1ri.features.pit_context import build_pit_opportunity
from f1ri.features.pit_events import reconstruct_pit_events
from f1ri.models.pit_loss import PitLossModelV1


def test_future_outcomes_flags_tires_positions_and_weather_do_not_change_request(pit_frames):
    f = pit_frames
    cutoff = f['laps'].query('driver_number == 1 and lap_number == 9').iloc[0].lap_end_estimate_utc
    events = reconstruct_pit_events(f['pit'], f['laps'], f['stints'], f['sessions'].iloc[0].to_dict(), f['track_intervals'])
    def build():
        return build_pit_opportunity(session=f['sessions'].iloc[0].to_dict(),
            driver=f['drivers'].iloc[0].to_dict(), laps=f['laps'], stints=f['stints'],
            race_control=f['race_control'], weather=f['weather'], completed_events=events,
            position=f['position'], intervals=f['intervals'], as_of_utc=cutoff,
            intended_pit_lap=10, new_compound='SOFT')
    base, context = build()
    events['lane_duration_s'] = 900
    events['stop_duration_s'] = 800
    f['stints'].loc[f['stints'].lap_start > 9, 'compound'] = 'WET'
    f['laps'].loc[f['laps'].lap_number > 9, 'lap_duration'] = 9999
    f['laps'].loc[f['laps'].lap_number > 9, 'is_pit_out_lap'] = False
    f['race_control'] = pd.concat([f['race_control'], pd.DataFrame([{'date': cutoff+timedelta(seconds=50),
        'category': 'SafetyCar', 'message': 'SAFETY CAR DEPLOYED', 'flag': None, 'scope': None}])])
    f['weather'] = pd.concat([f['weather'], pd.DataFrame([{'date': cutoff+timedelta(seconds=10), 'track_temperature': 90}])])
    f['position'] = pd.DataFrame([{'driver_number': 1, 'date': cutoff+timedelta(seconds=5), 'position': 20}])
    f['intervals'] = pd.DataFrame([{'driver_number': 1, 'date': cutoff+timedelta(seconds=5), 'gap_to_leader_raw': '900'}])
    altered, altered_context = build()
    assert base == altered
    assert context == altered_context


def test_future_training_removal_reproduces_exact_artifact(synthetic_pit_population):
    data = synthetic_pit_population
    cutoff = datetime(2024, 1, 1, tzinfo=UTC)
    future = data.iloc[:5].copy()
    future['pit_event_id'] = ['future-'+str(i) for i in range(5)]
    future['observation_available_utc'] = datetime(2026, 1, 1, tzinfo=UTC)
    future['transition_loss_s'] = 9999
    a = PitLossModelV1().fit(data, training_cutoff_utc=cutoff)
    b = PitLossModelV1().fit(pd.concat([data, future]), training_cutoff_utc=cutoff)
    assert pickle.dumps(a) == pickle.dumps(b)
