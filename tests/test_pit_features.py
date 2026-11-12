from f1ri.features.pit_context import build_pit_opportunity, request_from_json, request_to_json


def test_no_future_stop_required(pit_frames):
    f = pit_frames
    cutoff = f['laps'].query('driver_number == 1 and lap_number == 9').iloc[0].lap_end_estimate_utc
    request, context = build_pit_opportunity(
        session=f['sessions'].iloc[0].to_dict(), driver=f['drivers'].iloc[0].to_dict(),
        laps=f['laps'], stints=f['stints'], race_control=f['race_control'], weather=f['weather'],
        as_of_utc=cutoff, intended_pit_lap=10, new_compound='SOFT')
    assert request.latest_completed_lap == 9
    assert request.old_tyre_age == 10
    assert request.new_compound == 'SOFT'
    assert request.track_status == 'GREEN'
    assert context['pit_event_id'] is None
    assert request == request_from_json(request_to_json(request))
