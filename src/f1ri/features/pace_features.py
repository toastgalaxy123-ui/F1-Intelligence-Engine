"""Point-in-time feature definitions, identity normalization, and dataset quality auditing."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from f1ri.models.contracts import PaceRequest

# Known pre-race scheduled lap counts by circuit_short_name (prevents shortened races from leaking).
SCHEDULED_LAPS_BY_CIRCUIT: dict[str, int] = {
    "Sakhir": 57,
    "Bahrain": 57,
    "Jeddah": 50,
    "Melbourne": 58,
    "Suzuka": 53,
    "Shanghai": 56,
    "Miami": 57,
    "Imola": 63,
    "Monte Carlo": 78,
    "Monaco": 78,
    "Catalunya": 66,
    "Barcelona": 66,
    "Montreal": 70,
    "Spielberg": 71,
    "Silverstone": 52,
    "Hungaroring": 70,
    "Spa-Francorchamps": 44,
    "Zandvoort": 72,
    "Monza": 53,
    "Baku": 51,
    "Singapore": 62,
    "Austin": 56,
    "Mexico City": 71,
    "Interlagos": 71,
    "Las Vegas": 50,
    "Lusail": 57,
    "Yas Marina Circuit": 58,
}

TEAM_CANONICAL_MAP: dict[str, str] = {
    "red bull racing": "RED_BULL",
    "red bull": "RED_BULL",
    "mercedes": "MERCEDES",
    "ferrari": "FERRARI",
    "mclaren": "MCLAREN",
    "aston martin": "ASTON_MARTIN",
    "alpine": "ALPINE",
    "williams": "WILLIAMS",
    "haas f1 team": "HAAS",
    "haas": "HAAS",
    "alphatauri": "RACING_BULLS",
    "rb": "RACING_BULLS",
    "racing bulls": "RACING_BULLS",
    "visa cash app rb": "RACING_BULLS",
    "alfa romeo": "SAUBER",
    "kick sauber": "SAUBER",
    "sauber": "SAUBER",
    "audi": "SAUBER",
}


def canonical_team_id(raw_team_name: str | None) -> str:
    if not raw_team_name:
        return "UNKNOWN_TEAM"
    norm = raw_team_name.strip().casefold()
    if norm in TEAM_CANONICAL_MAP:
        return TEAM_CANONICAL_MAP[norm]
    for key, val in TEAM_CANONICAL_MAP.items():
        if key in norm:
            return val
    return raw_team_name.strip().upper().replace(" ", "_")


def canonical_driver_id(name_acronym: str | None, driver_number: int | None) -> str:
    if name_acronym and str(name_acronym).strip():
        return str(name_acronym).strip().upper()
    if driver_number is not None:
        return f"DRV_{int(driver_number)}"
    return "UNKNOWN_DRIVER"


def scheduled_laps_for_circuit(circuit_short_name: str | None, fallback_laps: int) -> int:
    if circuit_short_name and circuit_short_name in SCHEDULED_LAPS_BY_CIRCUIT:
        return SCHEDULED_LAPS_BY_CIRCUIT[circuit_short_name]
    return max(int(fallback_laps or 55), 1)


def compute_recent_clean_stats(
    prior_clean_laps: pd.DataFrame,
    current_stint: int | None,
    target_lap_number: int,
) -> dict[str, Any]:
    """Compute point-in-time recent clean-lap statistics from strictly completed past laps."""
    if prior_clean_laps.empty:
        return {
            "last_clean_lap_time_s": None,
            "median_last_3_clean_laps_s": None,
            "median_last_5_clean_laps_s": None,
            "median_stint_clean_laps_s": None,
            "clean_laps_observed_current_stint": 0,
            "clean_laps_observed_race": 0,
            "last_clean_lap_age": None,
        }

    ordered = prior_clean_laps.sort_values("lap_number")
    times = ordered["observed_lap_time_s"].dropna().to_numpy(dtype=float)
    laps = ordered["lap_number"].to_numpy(dtype=int)

    if len(times) == 0:
        return {
            "last_clean_lap_time_s": None,
            "median_last_3_clean_laps_s": None,
            "median_last_5_clean_laps_s": None,
            "median_stint_clean_laps_s": None,
            "clean_laps_observed_current_stint": 0,
            "clean_laps_observed_race": 0,
            "last_clean_lap_age": None,
        }

    stint_times = np.array([], dtype=float)
    if current_stint is not None and "stint_number" in ordered.columns:
        stint_sub = ordered[ordered["stint_number"] == current_stint]["observed_lap_time_s"].dropna()
        stint_times = stint_sub.to_numpy(dtype=float)

    return {
        "last_clean_lap_time_s": float(times[-1]),
        "median_last_3_clean_laps_s": float(np.median(times[-3:])),
        "median_last_5_clean_laps_s": float(np.median(times[-5:])),
        "median_stint_clean_laps_s": float(np.median(stint_times[-3:])) if len(stint_times) > 0 else None,
        "clean_laps_observed_current_stint": len(stint_times),
        "clean_laps_observed_race": len(times),
        "last_clean_lap_age": int(target_lap_number - int(laps[-1])),
    }


def row_to_pace_request(row: pd.Series | dict[str, Any]) -> PaceRequest:
    """Convert a gold.pace_training_examples row into a typed PaceRequest."""
    get = row.get if isinstance(row, dict) else lambda k, d=None: row[k] if k in row and pd.notna(row[k]) else d
    as_of = get("forecast_as_of_utc")
    if isinstance(as_of, pd.Timestamp):
        as_of = as_of.to_pydatetime()
    elif not isinstance(as_of, datetime):
        raise TypeError(f"forecast_as_of_utc must be a timezone-aware datetime, got {type(as_of)}")

    return PaceRequest(
        session_key=int(get("session_key")),
        driver_number=int(get("driver_number")),
        lap_number=int(get("target_lap_number")),
        compound=str(get("compound") or "UNKNOWN"),
        tyre_age_at_lap_start=int(get("tyre_age_at_lap_start") or 0),
        track_temperature_c=float(get("last_known_track_temp_c")) if get("last_known_track_temp_c") is not None else None,
        as_of_utc=as_of,
        horizon=int(get("horizon") or 1),
        season=int(get("season")) if get("season") is not None else None,
        regulation_era=str(get("regulation_era")) if get("regulation_era") is not None else None,
        circuit_key=int(get("circuit_key")) if get("circuit_key") is not None else None,
        circuit_short_name=str(get("circuit_short_name")) if get("circuit_short_name") is not None else None,
        driver_id=str(get("driver_id")) if get("driver_id") is not None else None,
        team_id=str(get("team_id")) if get("team_id") is not None else None,
        race_progress=float(get("race_progress")) if get("race_progress") is not None else None,
        stint_number=int(get("stint_number")) if get("stint_number") is not None else None,
        current_stint_laps_completed=int(get("current_stint_laps_completed") or 0),
        tyre_age_at_stint_start=int(get("tyre_age_at_stint_start") or 0),
        last_clean_lap_time_s=float(get("last_clean_lap_time_s")) if get("last_clean_lap_time_s") is not None else None,
        median_last_3_clean_laps_s=float(get("median_last_3_clean_laps_s")) if get("median_last_3_clean_laps_s") is not None else None,
        median_last_5_clean_laps_s=float(get("median_last_5_clean_laps_s")) if get("median_last_5_clean_laps_s") is not None else None,
        median_stint_clean_laps_s=float(get("median_stint_clean_laps_s")) if get("median_stint_clean_laps_s") is not None else None,
        clean_laps_observed_current_stint=int(get("clean_laps_observed_current_stint") or 0),
        clean_laps_observed_race=int(get("clean_laps_observed_race") or 0),
        last_clean_lap_age=int(get("last_clean_lap_age")) if get("last_clean_lap_age") is not None else None,
        air_temperature_c=float(get("last_known_air_temp_c")) if get("last_known_air_temp_c") is not None else None,
        temperature_observation_age_s=float(get("temperature_observation_age_s")) if get("temperature_observation_age_s") is not None else None,
        actual_compound_code=str(get("actual_compound_code")) if get("actual_compound_code") is not None else None,
        compound_mapping_source=str(get("compound_mapping_source")) if get("compound_mapping_source") is not None else None,
        compound_mapping_confidence=str(get("compound_mapping_confidence")) if get("compound_mapping_confidence") is not None else None,
    )


def build_dataset_quality_audit(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Generate comprehensive Stage 2.1 dataset quality and selection-bias diagnostics."""
    race_summary = con.execute("""
        SELECT
            c.session_key,
            c.season,
            c.regulation_era,
            c.circuit_short_name,
            COUNT(*) AS total_laps,
            SUM(CASE WHEN c.is_clean_green_pace_lap THEN 1 ELSE 0 END) AS clean_green_laps,
            ROUND(100.0 * AVG(CASE WHEN c.is_clean_green_pace_lap THEN 1.0 ELSE 0.0 END), 2) AS clean_lap_pct,
            COUNT(DISTINCT CASE WHEN c.is_clean_green_pace_lap THEN (c.driver_number::VARCHAR || '_' || c.stint_number::VARCHAR) END) AS clean_stints,
            ROUND(MEDIAN(CASE WHEN c.is_clean_green_pace_lap THEN c.observed_lap_time_s END), 3) AS median_clean_lap_s,
            ROUND(
                QUANTILE_CONT(CASE WHEN c.is_clean_green_pace_lap THEN c.observed_lap_time_s END, 0.75)
                - QUANTILE_CONT(CASE WHEN c.is_clean_green_pace_lap THEN c.observed_lap_time_s END, 0.25),
                3
            ) AS iqr_clean_lap_s,
            ROUND(100.0 * AVG(CASE WHEN c.track_temperature_c IS NULL THEN 1.0 ELSE 0.0 END), 2) AS missing_weather_pct,
            ROUND(100.0 * AVG(CASE WHEN c.tyre_age_at_lap_start IS NULL THEN 1.0 ELSE 0.0 END), 2) AS missing_tyre_age_pct,
            SUM(CASE WHEN c.track_status = 'UNKNOWN' THEN 1 ELSE 0 END) AS unknown_track_status_laps,
            SUM(CASE WHEN c.compound IN ('INTERMEDIATE', 'WET') OR COALESCE(c.rainfall, FALSE) THEN 1 ELSE 0 END) AS wet_or_inter_laps
        FROM gold.lap_context c
        GROUP BY 1, 2, 3, 4
        ORDER BY c.season, c.session_key
    """).df()

    by_compound = con.execute("""
        SELECT
            COALESCE(compound, 'UNKNOWN') AS compound,
            COUNT(*) AS total_laps,
            SUM(CASE WHEN is_clean_green_pace_lap THEN 1 ELSE 0 END) AS clean_laps,
            ROUND(100.0 * AVG(CASE WHEN is_clean_green_pace_lap THEN 1.0 ELSE 0.0 END), 2) AS retention_pct
        FROM gold.lap_context
        GROUP BY 1
        ORDER BY total_laps DESC
    """).df()

    by_tyre_age = con.execute("""
        SELECT
            CASE
                WHEN tyre_age_at_lap_start IS NULL THEN 'MISSING'
                WHEN tyre_age_at_lap_start <= 5 THEN '00_05_FRESH'
                WHEN tyre_age_at_lap_start <= 15 THEN '06_15_MID'
                WHEN tyre_age_at_lap_start <= 25 THEN '16_25_USED'
                ELSE '26_PLUS_OLD'
            END AS tyre_age_bucket,
            COUNT(*) AS total_laps,
            SUM(CASE WHEN is_clean_green_pace_lap THEN 1 ELSE 0 END) AS clean_laps,
            ROUND(100.0 * AVG(CASE WHEN is_clean_green_pace_lap THEN 1.0 ELSE 0.0 END), 2) AS retention_pct
        FROM gold.lap_context
        GROUP BY 1
        ORDER BY 1
    """).df()

    by_race_phase = con.execute("""
        SELECT
            CASE
                WHEN lap_number = 1 THEN '0_LAP_1'
                WHEN CAST(lap_number AS DOUBLE) / race_total_laps <= 0.33 THEN '1_EARLY_THIRD'
                WHEN CAST(lap_number AS DOUBLE) / race_total_laps <= 0.67 THEN '2_MIDDLE_THIRD'
                ELSE '3_LATE_THIRD'
            END AS race_phase,
            COUNT(*) AS total_laps,
            SUM(CASE WHEN is_clean_green_pace_lap THEN 1 ELSE 0 END) AS clean_laps,
            ROUND(100.0 * AVG(CASE WHEN is_clean_green_pace_lap THEN 1.0 ELSE 0.0 END), 2) AS retention_pct
        FROM gold.lap_context
        GROUP BY 1
        ORDER BY 1
    """).df()

    by_position_group = con.execute("""
        SELECT
            CASE
                WHEN position_at_lap_start IS NULL THEN 'UNKNOWN_POS'
                WHEN position_at_lap_start = 1 THEN '1_LEADER_P1'
                WHEN position_at_lap_start BETWEEN 2 AND 5 THEN '2_FRONT_P2_P5'
                WHEN position_at_lap_start BETWEEN 6 AND 12 THEN '3_MIDFIELD_P6_P12'
                ELSE '4_BACK_P13_P20'
            END AS position_group,
            COUNT(*) AS total_laps,
            SUM(CASE WHEN is_clean_green_pace_lap THEN 1 ELSE 0 END) AS clean_laps,
            ROUND(100.0 * AVG(CASE WHEN is_clean_green_pace_lap THEN 1.0 ELSE 0.0 END), 2) AS retention_pct
        FROM gold.lap_context
        GROUP BY 1
        ORDER BY 1
    """).df()

    return {
        "race_summary": race_summary.to_dict(orient="records"),
        "retention_by_compound": by_compound.to_dict(orient="records"),
        "retention_by_tyre_age": by_tyre_age.to_dict(orient="records"),
        "retention_by_race_phase": by_race_phase.to_dict(orient="records"),
        "retention_by_position_group": by_position_group.to_dict(orient="records"),
    }
