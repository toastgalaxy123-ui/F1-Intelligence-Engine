"""Construct historical stint records (gold.stint_observations and gold.stint_lap_observations)."""
from __future__ import annotations

from typing import Any

import duckdb
import pandas as pd

from f1ri.features.pace_features import (
    canonical_driver_id,
    canonical_team_id,
    scheduled_laps_for_circuit,
)
from f1ri.ingestion.compound_allocations import resolve_compound_allocation

DRY_COMPOUNDS = {"SOFT", "MEDIUM", "HARD"}


def _safe_bool(val: Any) -> bool:
    if val is None or pd.isna(val):
        return False
    return bool(val)


def canonical_stint_id(session_key: int, driver_number: int, stint_number: int) -> str:
    """Stable unique stint identifier based strictly on (session_key, driver_number, stint_number)."""
    return f"{int(session_key)}_{int(driver_number)}_{int(stint_number)}"


def build_session_stint_tables(
    lap_ctx_df: pd.DataFrame,
    stints_df: pd.DataFrame,
    drivers_df: pd.DataFrame,
    results_df: pd.DataFrame,
    allocations_df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Construct (stint_observations_df, stint_lap_observations_df) for one race session.

    Preserves the tire-age identity:
        a_k = a_start + (k - 1)
    where a_k is tire age at the beginning of the k-th lap of the stint (k = 1, 2, ...).
    """
    if lap_ctx_df.empty or stints_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    drv_lookup: dict[int, tuple[str, str]] = {}
    for _, drow in drivers_df.iterrows():
        dnum = int(drow["driver_number"])
        drv_lookup[dnum] = (
            canonical_driver_id(drow.get("name_acronym"), dnum),
            canonical_team_id(drow.get("team_name")),
        )

    dnf_lookup: dict[int, bool] = {}
    finish_laps_lookup: dict[int, int] = {}
    if not results_df.empty:
        for _, rrow in results_df.iterrows():
            dnum = int(rrow["driver_number"])
            is_dnf = _safe_bool(rrow.get("dnf")) or _safe_bool(rrow.get("dns"))
            dnf_lookup[dnum] = is_dnf
            if pd.notna(rrow.get("number_of_laps")):
                finish_laps_lookup[dnum] = int(rrow["number_of_laps"])

    first_row = lap_ctx_df.iloc[0]
    session_key = int(first_row["session_key"])
    meeting_key = int(first_row["meeting_key"]) if pd.notna(first_row.get("meeting_key")) else None
    season = int(first_row["season"]) if pd.notna(first_row.get("season")) else None
    regulation_era = str(first_row.get("regulation_era") or "2023_2025")
    circuit_key = int(first_row["circuit_key"]) if pd.notna(first_row.get("circuit_key")) else None
    circuit_short_name = (
        str(first_row["circuit_short_name"])
        if pd.notna(first_row.get("circuit_short_name"))
        else None
    )
    raw_total_laps = int(first_row.get("race_total_laps") or 55)
    sched_laps = scheduled_laps_for_circuit(circuit_short_name, raw_total_laps)
    max_race_lap = int(lap_ctx_df["lap_number"].max())

    stint_rows: list[dict[str, Any]] = []
    stint_lap_rows: list[dict[str, Any]] = []

    for driver_number, drv_stints in stints_df.groupby("driver_number"):
        drv_num = int(driver_number)
        driver_id, team_id = drv_lookup.get(
            drv_num, (canonical_driver_id(None, drv_num), "UNKNOWN_TEAM")
        )
        drv_laps = (
            lap_ctx_df[lap_ctx_df["driver_number"] == drv_num]
            .sort_values("lap_number")
            .reset_index(drop=True)
        )
        if drv_laps.empty:
            continue

        drv_max_completed_lap = int(
            drv_laps.loc[drv_laps["observed_lap_time_s"].notna(), "lap_number"].max()
            if drv_laps["observed_lap_time_s"].notna().any()
            else drv_laps["lap_number"].max()
        )
        drv_is_dnf = dnf_lookup.get(drv_num, drv_max_completed_lap < max_race_lap - 2)

        ordered_stints = drv_stints.sort_values(["stint_number", "lap_start"]).reset_index(drop=True)
        n_driver_stints = len(ordered_stints)

        for s_idx, srow in ordered_stints.iterrows():
            if pd.isna(srow.get("stint_number")) or pd.isna(srow.get("lap_start")):
                continue
            stint_num = int(srow["stint_number"])
            lap_start = int(srow["lap_start"])
            raw_lap_end = (
                int(srow["lap_end"])
                if pd.notna(srow.get("lap_end"))
                else int(drv_laps["lap_number"].max())
            )
            stint_id = canonical_stint_id(session_key, drv_num, stint_num)

            compound_rel = (
                str(srow["compound"]).strip().upper()
                if pd.notna(srow.get("compound"))
                else "UNKNOWN"
            )
            tyre_age_start = (
                max(0, int(srow["tyre_age_at_start"]))
                if pd.notna(srow.get("tyre_age_at_start"))
                else 0
            )

            sub_laps = drv_laps[
                (drv_laps["lap_number"] >= lap_start) & (drv_laps["lap_number"] <= raw_lap_end)
            ].copy()

            # Use the earliest lap timestamp in the stint (or None) for point-in-time allocation check
            stint_start_utc = None
            if not sub_laps.empty and sub_laps["lap_start_utc"].notna().any():
                stint_start_utc = sub_laps["lap_start_utc"].dropna().iloc[0]

            alloc_info = resolve_compound_allocation(
                allocations_df=allocations_df,
                meeting_key=meeting_key,
                relative_compound=compound_rel,
                as_of_utc=stint_start_utc,
            )
            compound_abs = alloc_info["compound_absolute"]
            mapping_conf = alloc_info["compound_mapping_confidence"]
            mapping_source = alloc_info["compound_mapping_source"]

            completed_laps = sub_laps[
                sub_laps["observed_lap_time_s"].notna() & (sub_laps["observed_lap_time_s"] > 0)
            ]
            observed_stint_laps = len(completed_laps)
            actual_lap_end = (
                int(completed_laps["lap_number"].max())
                if not completed_laps.empty
                else raw_lap_end
            )

            is_last_stint = s_idx == (n_driver_stints - 1)
            has_pit_at_end = bool(
                sub_laps["is_pit_in_lap"].fillna(False).any()
                if not sub_laps.empty and "is_pit_in_lap" in sub_laps.columns
                else False
            )
            was_pit_terminated = bool((not is_last_stint) or has_pit_at_end)
            ended_by_retirement = bool(is_last_stint and drv_is_dnf and not has_pit_at_end)
            ended_by_race_finish = bool(
                is_last_stint and (not ended_by_retirement) and actual_lap_end >= max_race_lap - 2
            )

            had_neutralization = False
            had_red_flag = False
            weather_cov = 0.0
            mean_temp_c: float | None = None
            clean_count = 0

            if not sub_laps.empty:
                statuses = set(sub_laps["track_status"].dropna().astype(str))
                had_neutralization = bool(statuses & {"SC", "VSC", "RED"})
                had_red_flag = "RED" in statuses
                weather_cov = float(sub_laps["track_temperature_c"].notna().mean())
                if sub_laps["track_temperature_c"].notna().any():
                    mean_temp_c = float(sub_laps["track_temperature_c"].dropna().mean())

            # Emit longitudinal lap observations preserving a_k = a_start + (k - 1)
            for _, lrow in sub_laps.iterrows():
                lap_num = int(lrow["lap_number"])
                k_idx = int(lap_num - lap_start + 1)
                expected_age_k = int(tyre_age_start + (k_idx - 1))
                ctx_age = (
                    int(lrow["tyre_age_at_lap_start"])
                    if pd.notna(lrow.get("tyre_age_at_lap_start"))
                    else expected_age_k
                )
                # Stage 1 formula s.tyre_age_at_start + l.lap_number - s.lap_start equals a_start + (k - 1)
                tyre_age_k = ctx_age

                raw_clean = _safe_bool(lrow.get("is_clean_green_pace_lap"))
                if raw_clean:
                    clean_count += 1

                stint_lap_rows.append(
                    {
                        "stint_id": stint_id,
                        "session_key": session_key,
                        "meeting_key": meeting_key,
                        "season": season,
                        "regulation_era": regulation_era,
                        "circuit_key": circuit_key,
                        "circuit_short_name": circuit_short_name,
                        "driver_number": drv_num,
                        "driver_id": driver_id,
                        "team_id": team_id,
                        "stint_number": stint_num,
                        "stint_lap_index": k_idx,
                        "lap_number": lap_num,
                        "scheduled_race_laps": sched_laps,
                        "race_progress": float(lap_num) / float(sched_laps),
                        "compound_relative": compound_rel,
                        "compound_absolute": compound_abs,
                        "compound_mapping_source": mapping_source,
                        "compound_mapping_confidence": mapping_conf,
                        "tyre_age_at_start": tyre_age_start,
                        "tyre_age_at_lap_start": tyre_age_k,
                        "lap_start_utc": lrow.get("lap_start_utc"),
                        "lap_end_estimate_utc": lrow.get("lap_end_estimate_utc"),
                        "observed_lap_time_s": (
                            float(lrow["observed_lap_time_s"])
                            if pd.notna(lrow.get("observed_lap_time_s"))
                            else None
                        ),
                        "track_status": str(lrow.get("track_status") or "UNKNOWN"),
                        "is_pit_out_lap": _safe_bool(lrow.get("is_pit_out_lap")),
                        "is_pit_in_lap": _safe_bool(lrow.get("is_pit_in_lap")),
                        "is_clean_air": _safe_bool(lrow.get("is_clean_air")),
                        "is_clean_green_pace_lap": raw_clean,
                        "exclusion_reason": (
                            str(lrow["exclusion_reason"])
                            if pd.notna(lrow.get("exclusion_reason"))
                            else None
                        ),
                        "track_temperature_c": (
                            float(lrow["track_temperature_c"])
                            if pd.notna(lrow.get("track_temperature_c"))
                            else None
                        ),
                        "air_temperature_c": (
                            float(lrow["air_temperature_c"])
                            if pd.notna(lrow.get("air_temperature_c"))
                            else None
                        ),
                        "rainfall": _safe_bool(lrow.get("rainfall")),
                    }
                )

            clean_fraction = (
                float(clean_count) / float(observed_stint_laps)
                if observed_stint_laps > 0
                else 0.0
            )
            if observed_stint_laps == 0 or compound_rel not in DRY_COMPOUNDS:
                quality_status = "EXCLUDED"
            elif clean_count == 0 or weather_cov < 0.5:
                quality_status = "REVIEW"
            else:
                quality_status = "VALIDATED"

            stint_rows.append(
                {
                    "stint_id": stint_id,
                    "session_key": session_key,
                    "meeting_key": meeting_key,
                    "season": season,
                    "regulation_era": regulation_era,
                    "circuit_key": circuit_key,
                    "circuit_short_name": circuit_short_name,
                    "driver_number": drv_num,
                    "driver_id": driver_id,
                    "team_id": team_id,
                    "stint_number": stint_num,
                    "compound_relative": compound_rel,
                    "compound_absolute": compound_abs,
                    "compound_mapping_source": mapping_source,
                    "compound_mapping_confidence": mapping_conf,
                    "tyre_age_at_start": tyre_age_start,
                    "used_tyre_set": bool(tyre_age_start > 0),
                    "lap_start": lap_start,
                    "lap_end": actual_lap_end,
                    "observed_stint_laps": observed_stint_laps,
                    "clean_laps_count": clean_count,
                    "clean_lap_fraction": round(clean_fraction, 4),
                    "was_pit_terminated": was_pit_terminated,
                    "ended_by_retirement": ended_by_retirement,
                    "ended_by_race_finish": ended_by_race_finish,
                    "had_neutralization": had_neutralization,
                    "had_red_flag": had_red_flag,
                    "weather_coverage": round(weather_cov, 4),
                    "mean_track_temp_c": round(mean_temp_c, 3) if mean_temp_c is not None else None,
                    "data_quality_status": quality_status,
                }
            )

    return pd.DataFrame(stint_rows), pd.DataFrame(stint_lap_rows)


def ensure_stint_tables_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE SCHEMA IF NOT EXISTS gold")
    con.execute("""
        CREATE TABLE IF NOT EXISTS gold.stint_observations (
            stint_id VARCHAR,
            session_key BIGINT,
            meeting_key BIGINT,
            season INTEGER,
            regulation_era VARCHAR,
            circuit_key BIGINT,
            circuit_short_name VARCHAR,
            driver_number INTEGER,
            driver_id VARCHAR,
            team_id VARCHAR,
            stint_number INTEGER,
            compound_relative VARCHAR,
            compound_absolute VARCHAR,
            compound_mapping_source VARCHAR,
            compound_mapping_confidence VARCHAR,
            tyre_age_at_start INTEGER,
            used_tyre_set BOOLEAN,
            lap_start INTEGER,
            lap_end INTEGER,
            observed_stint_laps INTEGER,
            clean_laps_count INTEGER,
            clean_lap_fraction DOUBLE,
            was_pit_terminated BOOLEAN,
            ended_by_retirement BOOLEAN,
            ended_by_race_finish BOOLEAN,
            had_neutralization BOOLEAN,
            had_red_flag BOOLEAN,
            weather_coverage DOUBLE,
            mean_track_temp_c DOUBLE,
            data_quality_status VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS gold.stint_lap_observations (
            stint_id VARCHAR,
            session_key BIGINT,
            meeting_key BIGINT,
            season INTEGER,
            regulation_era VARCHAR,
            circuit_key BIGINT,
            circuit_short_name VARCHAR,
            driver_number INTEGER,
            driver_id VARCHAR,
            team_id VARCHAR,
            stint_number INTEGER,
            stint_lap_index INTEGER,
            lap_number INTEGER,
            scheduled_race_laps INTEGER,
            race_progress DOUBLE,
            compound_relative VARCHAR,
            compound_absolute VARCHAR,
            compound_mapping_source VARCHAR,
            compound_mapping_confidence VARCHAR,
            tyre_age_at_start INTEGER,
            tyre_age_at_lap_start INTEGER,
            lap_start_utc TIMESTAMPTZ,
            lap_end_estimate_utc TIMESTAMPTZ,
            observed_lap_time_s DOUBLE,
            track_status VARCHAR,
            is_pit_out_lap BOOLEAN,
            is_pit_in_lap BOOLEAN,
            is_clean_air BOOLEAN,
            is_clean_green_pace_lap BOOLEAN,
            exclusion_reason VARCHAR,
            track_temperature_c DOUBLE,
            air_temperature_c DOUBLE,
            rainfall BOOLEAN
        )
    """)


def populate_session_stint_observations(
    con: duckdb.DuckDBPyConnection,
    session_key: int | None = None,
) -> tuple[int, int]:
    """Populate gold.stint_observations and gold.stint_lap_observations for one or all sessions."""
    ensure_stint_tables_schema(con)

    try:
        alloc_df = con.execute("SELECT * FROM silver.compound_allocations").df()
    except duckdb.Error:
        alloc_df = pd.DataFrame()

    if session_key is not None:
        session_keys = [int(session_key)]
    else:
        rows = con.execute(
            "SELECT DISTINCT session_key FROM gold.lap_context ORDER BY session_key"
        ).fetchall()
        session_keys = [int(r[0]) for r in rows]

    total_stints = 0
    total_stint_laps = 0

    for sk in session_keys:
        con.execute("DELETE FROM gold.stint_observations WHERE session_key = ?", [sk])
        con.execute("DELETE FROM gold.stint_lap_observations WHERE session_key = ?", [sk])

        lap_ctx_df = con.execute(
            "SELECT * FROM gold.lap_context WHERE session_key = ? ORDER BY driver_number, lap_number",
            [sk],
        ).df()
        stints_df = con.execute(
            "SELECT * FROM silver.stints WHERE session_key = ? ORDER BY driver_number, stint_number",
            [sk],
        ).df()
        drivers_df = con.execute(
            "SELECT * FROM silver.drivers WHERE session_key = ?",
            [sk],
        ).df()
        try:
            results_df = con.execute(
                "SELECT * FROM silver.session_result WHERE session_key = ?",
                [sk],
            ).df()
        except duckdb.Error:
            results_df = pd.DataFrame()

        s_df, sl_df = build_session_stint_tables(
            lap_ctx_df=lap_ctx_df,
            stints_df=stints_df,
            drivers_df=drivers_df,
            results_df=results_df,
            allocations_df=alloc_df,
        )
        if not s_df.empty:
            con.register("_incoming_stint_obs", s_df)
            try:
                con.execute(
                    "INSERT INTO gold.stint_observations SELECT * FROM _incoming_stint_obs"
                )
            finally:
                con.unregister("_incoming_stint_obs")
            total_stints += len(s_df)

        if not sl_df.empty:
            con.register("_incoming_stint_lap_obs", sl_df)
            try:
                con.execute(
                    "INSERT INTO gold.stint_lap_observations SELECT * FROM _incoming_stint_lap_obs"
                )
            finally:
                con.unregister("_incoming_stint_lap_obs")
            total_stint_laps += len(sl_df)

    return total_stints, total_stint_laps
