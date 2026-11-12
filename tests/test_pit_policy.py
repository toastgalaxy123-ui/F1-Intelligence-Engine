"""Real known regressions plus cause/eligibility separation, without HTTP calls."""
import json
from pathlib import Path

import pandas as pd

from f1ri.features.pit_events import add_transition_evidence
from f1ri.features.pit_policy import PitEligibilityPolicy, apply_pit_eligibility


def known_visits():
    return pd.DataFrame(json.loads((Path(__file__).parent/'fixtures/stage4_known_visits.json').read_text())['events'])


def test_monza_red_overlap_and_catalunya_long_stay_are_not_ordinary():
    rows = apply_pit_eligibility(known_visits()).set_index('pit_event_id')
    monza, catalunya = rows.loc['11361:63:1'], rows.loc['11307:23:3']
    assert monza.lane_duration_s == 1846.2
    assert catalunya.lane_duration_s == 777.7
    assert not rows.eligible_direct_lane.any()
    assert not rows.eligible_direct_stationary.any()
    assert not rows.eligible_primary_label.any()
    assert {'RED_FLAG_OVERLAP', 'PIT_LANE_LONG_HOLD'} <= set(json.loads(monza.classification_flags_json))
    assert 'RED_FLAG_OVERLAP' not in json.loads(catalunya.classification_flags_json)
    assert 'UNRESOLVED_LONG_STAY' in json.loads(catalunya.classification_flags_json)
    assert 'REPAIR_OR_GARAGE' not in json.loads(catalunya.classification_flags_json)


def test_policy_is_idempotent_and_retains_raw_values():
    raw = known_visits()
    first = apply_pit_eligibility(raw)
    second = apply_pit_eligibility(first)
    pd.testing.assert_frame_equal(first, second)
    pd.testing.assert_series_equal(raw.lane_duration_s, first.lane_duration_s)


def test_slow_service_is_not_automatically_exceptional_or_confirmed():
    row = known_visits().iloc[0].to_dict()
    row.update(lane_duration_s=30, stop_duration_s=8, transition_track_status='GREEN',
               lane_track_status='GREEN', state_overlap_seconds_json='{"GREEN":190}',
               lane_state_overlap_seconds_json='{"GREEN":30}', possible_double_stack=False,
               pit_event_type='ROUTINE_TIRE_CHANGE', classification_evidence='VERIFIED_STINT_TRANSITION')
    result = apply_pit_eligibility(pd.DataFrame([row])).iloc[0]
    assert result.eligible_direct_lane
    assert result.eligible_direct_stationary
    assert result.routine_status == 'ROUTINE_UNVERIFIED'
    assert result.eligibility_grade == 'SCREENED_UNVERIFIED'
    strict = apply_pit_eligibility(pd.DataFrame([row]), PitEligibilityPolicy(allow_screened_unverified=False))
    assert not strict.eligible_direct_lane.any()
    row.update(pit_event_type='ROUTINE_CONFIRMED', classification_evidence='Reviewed service log #42')
    confirmed = apply_pit_eligibility(pd.DataFrame([row]), PitEligibilityPolicy(allow_screened_unverified=False)).iloc[0]
    assert confirmed.eligible_direct_lane
    assert confirmed.routine_status == 'ROUTINE_CONFIRMED'


def test_confirmed_repair_retains_cause_and_red_flag_reasons():
    row = known_visits().query("pit_event_id == '11361:63:1'").iloc[0].to_dict()
    row.update(pit_event_type='MECHANICAL_REPAIR', classification_evidence='Recorded repair log')
    flags = json.loads(apply_pit_eligibility(pd.DataFrame([row])).iloc[0].classification_flags_json)
    assert {'RED_FLAG_OVERLAP', 'REPAIR_OR_GARAGE', 'PIT_LANE_LONG_HOLD'} <= set(flags)


def test_recorded_blue_flag_and_repeated_operations_exclude_net_only():
    row = known_visits().iloc[0].to_dict()
    row.update(lane_duration_s=30, stop_duration_s=3, transition_track_status='GREEN',
               lane_track_status='GREEN', state_overlap_seconds_json='{"GREEN":190}',
               lane_state_overlap_seconds_json='{"GREEN":30}', possible_double_stack=False,
               pit_event_type='ROUTINE_TIRE_CHANGE', classification_evidence='VERIFIED_STINT_TRANSITION',
               resolved_in_lap=12, transition_start_utc='2024-10-20T19:00:00Z',
               transition_end_utc='2024-10-20T19:03:10Z', pit_timestamp_utc='2024-10-20T19:02:00Z')
    laps = pd.DataFrame([{'driver_number': row['driver_number'], 'lap_number': 12, 'is_pit_out_lap': True}])
    rc = pd.DataFrame([{'driver_number': row['driver_number'], 'date': '2024-10-20T19:01:00.100Z', 'flag': 'BLUE'}])
    frame = add_transition_evidence(pd.DataFrame([row]), laps, rc)
    result = apply_pit_eligibility(frame).iloc[0]
    assert result.eligible_direct_lane and result.eligible_direct_stationary
    assert not result.eligible_primary_label
    assert {'MULTIPLE_PIT_TRANSITIONS', 'ON_TRACK_TRAFFIC_CONTAMINATION'} <= set(json.loads(result.net_label_exclusion_reasons_json))
