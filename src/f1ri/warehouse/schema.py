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
