from datetime import timedelta

import pandas as pd
import pytest

from f1ri.features.pit_events import reconstruct_pit_events
from f1ri.validation.pit_measurement import (
    NormalLapReference,
    matched_control_estimate,
    measure_anchored_route,
    measure_pit_transitions,
)


def test_known_two_lap_loss_and_independent_controls(pit_frames):
    f = pit_frames
    event = reconstruct_pit_events(f['pit'], f['laps'], f['stints'], f['sessions'].iloc[0].to_dict(), f['track_intervals'])
    past = f['laps'].copy()
    past['session_key'] = 100
    for column in ('lap_start_utc', 'lap_end_estimate_utc'):
        past[column] -= timedelta(days=365)
    reference = NormalLapReference().fit(past, f['sessions'].iloc[0].date_start)
    result = measure_pit_transitions(event, f['laps'], reference)
    primary = result.query("measurement_method == 'TWO_LAP_CROSS_FIT'").iloc[0]
    assert primary.transition_loss_s == pytest.approx(24, abs=.001)
    assert primary.label_std_s > 0
    assert primary.eligible_primary_label
    assert pd.Timestamp(primary.reference_training_cutoff_utc) < primary.reference_forecast_cutoff_utc
    control, count = matched_control_estimate(event.iloc[0].to_dict(), f['laps'])
    assert count == 2
    assert control == 24
    reference.training_sessions = (701,)
    with pytest.raises(ValueError, match='target race'):
        reference.predict_pair(event.iloc[0].to_dict(), f['laps'])


def test_route_measurement_requires_verified_anchors():
    with pytest.raises(ValueError, match='GPS'):
        measure_anchored_route(pit_route_elapsed_s=28, on_track_elapsed_s=8,
                               boundary_std_s=.3, anchors_verified=False, source_sha256='abc')
    result = measure_anchored_route(pit_route_elapsed_s=28, on_track_elapsed_s=8,
                                   boundary_std_s=.3, anchors_verified=True, source_sha256='abc')
    assert result['transition_loss_s'] == 20
    assert not result['includes_pit_out_warmup']


def test_telemetry_stationary_phase_can_precede_report_timestamp():
    from datetime import UTC, datetime

    from f1ri.validation.pit_measurement import audit_stationary_trace
    origin = datetime(2025, 1, 1, tzinfo=UTC)
    trace = pd.DataFrame([{'date': origin+timedelta(seconds=offset),
                           'speed': 0 if -6 <= offset <= -4 else 80}
                          for offset in range(-12, 6)])
    result = audit_stationary_trace(trace, event_utc=origin, lane_duration_s=10,
                                    recorded_stationary_s=3)
    assert result['stationary_trace_estimate_s'] == 3
    assert result['difference_s'] == 0


def test_missing_sensor_sample_does_not_split_one_service():
    from datetime import UTC, datetime

    from f1ri.validation.pit_measurement import audit_stationary_trace
    origin = datetime(2025, 1, 1, tzinfo=UTC)
    offsets = [-9, -8.8, -8.6, -8.4, -8.2, -8, -7, -6.8, -6.6, -6.4, -6.2, -6, -5.8]
    trace = pd.DataFrame([{'date': origin+timedelta(seconds=offset),
                           'speed': 0 if -8 <= offset <= -6 else 80} for offset in offsets])
    result = audit_stationary_trace(trace, event_utc=origin, lane_duration_s=12,
                                    recorded_stationary_s=2.2)
    assert result['stationary_trace_estimate_s'] == pytest.approx(2.2)
    assert result['largest_internal_sample_gap_s'] == 1
    assert result['stationary_lower_bound_s'] <= 2.2 <= result['stationary_upper_bound_s']
