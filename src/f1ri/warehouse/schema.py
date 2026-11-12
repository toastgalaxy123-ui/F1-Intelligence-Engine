"""Explicit silver contracts; never infer persisted database types from API payloads."""
from collections import OrderedDict

SILVER_SCHEMAS: dict[str, dict[str, str]] = {
    "sessions": OrderedDict([
        ("session_key", "BIGINT"), ("meeting_key", "BIGINT"),
        ("year", "INTEGER"), ("session_name", "VARCHAR"),
        ("session_type", "VARCHAR"), ("date_start", "TIMESTAMPTZ"),
        ("date_end", "TIMESTAMPTZ"), ("circuit_key", "BIGINT"),
        ("circuit_short_name", "VARCHAR"), ("country_name", "VARCHAR"),
    ]),
    "drivers": OrderedDict([
        ("session_key", "BIGINT"), ("meeting_key", "BIGINT"),
        ("driver_number", "INTEGER"), ("full_name", "VARCHAR"),
        ("name_acronym", "VARCHAR"), ("team_name", "VARCHAR"),
    ]),
    "laps": OrderedDict([
        ("session_key", "BIGINT"), ("meeting_key", "BIGINT"),
        ("driver_number", "INTEGER"), ("lap_number", "INTEGER"),
        ("date_start", "TIMESTAMPTZ"), ("lap_duration", "DOUBLE"),
        ("duration_sector_1", "DOUBLE"), ("duration_sector_2", "DOUBLE"),
        ("duration_sector_3", "DOUBLE"), ("is_pit_out_lap", "BOOLEAN"),
    ]),
    "stints": OrderedDict([
        ("session_key", "BIGINT"), ("meeting_key", "BIGINT"),
        ("driver_number", "INTEGER"), ("stint_number", "INTEGER"),
        ("lap_start", "INTEGER"), ("lap_end", "INTEGER"),
        ("compound", "VARCHAR"), ("tyre_age_at_start", "INTEGER"),
    ]),
    "pit": OrderedDict([
        ("session_key", "BIGINT"), ("meeting_key", "BIGINT"),
        ("driver_number", "INTEGER"), ("lap_number", "INTEGER"),
        ("date", "TIMESTAMPTZ"), ("lane_duration", "DOUBLE"),
        ("stop_duration", "DOUBLE"),
    ]),
    "weather": OrderedDict([
        ("session_key", "BIGINT"), ("meeting_key", "BIGINT"),
        ("date", "TIMESTAMPTZ"), ("air_temperature", "DOUBLE"),
        ("track_temperature", "DOUBLE"), ("humidity", "DOUBLE"),
        ("rainfall", "BOOLEAN"), ("wind_speed", "DOUBLE"),
    ]),
    "race_control": OrderedDict([
        ("session_key", "BIGINT"), ("meeting_key", "BIGINT"),
        ("date", "TIMESTAMPTZ"), ("lap_number", "INTEGER"),
        ("driver_number", "INTEGER"), ("category", "VARCHAR"),
        ("flag", "VARCHAR"), ("scope", "VARCHAR"),
        ("message", "VARCHAR"),
    ]),
    "session_result": OrderedDict([
        ("session_key", "BIGINT"), ("meeting_key", "BIGINT"),
        ("driver_number", "INTEGER"), ("position", "INTEGER"),
        ("number_of_laps", "INTEGER"), ("dnf", "BOOLEAN"),
        ("dns", "BOOLEAN"), ("dsq", "BOOLEAN"),
    ]),
    "starting_grid": OrderedDict([
        ("session_key", "BIGINT"), ("meeting_key", "BIGINT"),
        ("driver_number", "INTEGER"), ("position", "INTEGER"),
        ("lap_duration", "DOUBLE"),
    ]),
    "position": OrderedDict([
        ("session_key", "BIGINT"), ("meeting_key", "BIGINT"),
        ("driver_number", "INTEGER"), ("date", "TIMESTAMPTZ"),
        ("position", "INTEGER"),
    ]),
    "intervals": OrderedDict([
        ("session_key", "BIGINT"), ("meeting_key", "BIGINT"),
        ("driver_number", "INTEGER"), ("date", "TIMESTAMPTZ"),
        # OpenF1 gap_to_leader and interval may contain strings such as '+1 LAP'.
        ("gap_to_leader_raw", "VARCHAR"), ("interval_raw", "VARCHAR"),
    ]),
}

REFERENCE_SCHEMAS: dict[str, dict[str, str]] = {
    "pit_lane_reference": OrderedDict([
        ("reference_id", "VARCHAR"), ("circuit_short_name", "VARCHAR"),
        ("season", "INTEGER"), ("layout_version", "VARCHAR"),
        ("pit_speed_limit_kph", "DOUBLE"), ("effective_from_utc", "TIMESTAMPTZ"),
        ("effective_to_utc", "TIMESTAMPTZ"), ("available_as_of_utc", "TIMESTAMPTZ"),
        ("source_url", "VARCHAR"), ("mapping_confidence", "VARCHAR"), ("notes", "VARCHAR"),
    ]),
    "compound_allocations": OrderedDict([
        ("meeting_key", "BIGINT"), ("season", "INTEGER"),
        ("circuit_short_name", "VARCHAR"), ("relative_compound", "VARCHAR"),
        ("absolute_compound", "VARCHAR"), ("regulation_era", "VARCHAR"),
        ("source_url", "VARCHAR"), ("source_publication_date", "VARCHAR"),
        ("mapping_confidence", "VARCHAR"), ("available_as_of_utc", "TIMESTAMPTZ"),
    ]),
}


PIT_EVENT_SCHEMA = {
    **dict.fromkeys(("pit_event_id", "old_stint_id", "new_stint_id", "old_compound",
                     "new_compound", "circuit_short_name", "team_id", "track_state_at_pit",
                     "track_state_at_exit", "transition_track_status", "lane_track_status",
                     "state_overlap_seconds_json", "lane_state_overlap_seconds_json",
                     "pit_event_type", "classification_evidence", "timing_quality",
                     "timestamp_precision", "lane_boundary_method", "quality_category", "quality_flags_json"), "VARCHAR"),
    "session_key": "BIGINT", "circuit_key": "BIGINT",
    **dict.fromkeys(("driver_number", "pit_sequence", "pit_record_lap", "resolved_in_lap",
                     "resolved_out_lap", "old_tyre_age", "new_tyre_initial_age", "season",
                     "raw_duplicate_count"), "INTEGER"),
    **dict.fromkeys(("lane_duration_s", "stop_duration_s", "moving_lane_duration_s",
                     "observed_in_lap_s", "observed_out_lap_s"), "DOUBLE"),
    **dict.fromkeys(("pit_timestamp_utc", "estimated_lane_entry_utc", "estimated_lane_exit_utc", "transition_start_utc",
                     "transition_end_utc"), "TIMESTAMPTZ"),
    **dict.fromkeys(("eligible_direct_lane", "eligible_primary_label",
                     "possible_double_stack"), "BOOLEAN"),
}

PIT_OBSERVATION_SCHEMA = {
    "pit_event_id": "VARCHAR", "session_key": "BIGINT", "measurement_method": "VARCHAR",
    "reference_model_version": "VARCHAR", "reference_training_cutoff_utc": "TIMESTAMPTZ",
    "reference_forecast_cutoff_utc": "TIMESTAMPTZ", "observation_available_utc": "TIMESTAMPTZ",
    "reference_training_sessions_json": "VARCHAR", "input_sha256": "VARCHAR",
    **dict.fromkeys(("normal_in_reference_s", "normal_out_reference_s", "transition_loss_s",
                     "label_std_s", "reference_error_std_s", "label_weight",
                     "matched_control_loss_s", "method_disagreement_s"), "DOUBLE"),
    "n_matched_controls": "INTEGER", "reference_support": "VARCHAR",
    "eligible_primary_label": "BOOLEAN", "measurement_flags_json": "VARCHAR",
}

PIT_CONTEXT_SCHEMA = {
    "opportunity_id": "VARCHAR", "pit_event_id": "VARCHAR", "session_key": "BIGINT",
    "driver_number": "INTEGER", "forecast_as_of_utc": "TIMESTAMPTZ",
    "latest_completed_lap": "INTEGER", "intended_pit_lap": "INTEGER",
    "request_json": "VARCHAR", "track_status_at_cutoff": "VARCHAR",
    "n_completed_stops_known": "INTEGER", "last_observed_position": "INTEGER",
    "last_observed_gap_raw": "VARCHAR", "last_weather_observed_utc": "TIMESTAMPTZ",
}

PIT_EXAMPLE_SCHEMA = {
    **PIT_CONTEXT_SCHEMA, "split": "VARCHAR", "target_lane_duration_s": "DOUBLE",
    "target_stop_duration_s": "DOUBLE", "target_transition_loss_s": "DOUBLE",
    "label_std_s": "DOUBLE", "observation_available_utc": "TIMESTAMPTZ",
    "eligible_primary_label": "BOOLEAN", "is_realized_tire_scenario": "BOOLEAN",
    "pit_event_type": "VARCHAR", "measurement_method": "VARCHAR",
}
