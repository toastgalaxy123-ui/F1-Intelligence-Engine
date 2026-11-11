"""Build gold.pace_training_examples with strict point-in-time forecast cutoffs."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import duckdb
import pandas as pd

from f1ri.features.pace_features import (
    canonical_driver_id,
    canonical_team_id,
    compute_recent_clean_stats,
    scheduled_laps_for_circuit,
)

DRY_COMPOUNDS = {"SOFT", "MEDIUM", "HARD"}


def build_pace_examples_dataframe(
    lap_ctx_df: pd.DataFrame,
    drivers_df: pd.DataFrame,
    weather_df: pd.DataFrame,
    horizons: Sequence[int] = (1, 3, 5),
) -> pd.DataFrame:
    """Construct forecast-opportunity rows for a single session from point-in-time records.

    For each driver and completed cutoff lap `l_c` (where the driver did not enter the pits
    on `l_c`), creates a forecast opportunity for `target_lap_number = l_c + h` for each
    horizon `h`. All predictor fields use strictly observations with timestamp <=
    `forecast_as_of_utc` (`lap_end_estimate_utc` of `l_c`).
    """
    if lap_ctx_df.empty:
        return pd.DataFrame()

    drv_lookup: dict[int, tuple[str, str]] = {}
    for _, drow in drivers_df.iterrows():
        dnum = int(drow["driver_number"])
        drv_lookup[dnum] = (
            canonical_driver_id(drow.get("name_acronym"), dnum),
            canonical_team_id(drow.get("team_name")),
        )

    weather_sorted = (
        weather_df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
        if not weather_df.empty and "date" in weather_df.columns
        else pd.DataFrame()
    )
    weather_dates = (
        pd.to_datetime(weather_sorted["date"], utc=True)
        if not weather_sorted.empty
        else pd.Series(dtype="datetime64[ns, UTC]")
    )

    records: list[dict[str, Any]] = []

    for driver_number, drv_laps in lap_ctx_df.groupby("driver_number"):
        drv_num = int(driver_number)
        driver_id, team_id = drv_lookup.get(
            drv_num, (canonical_driver_id(None, drv_num), "UNKNOWN_TEAM")
        )
        ordered = drv_laps.sort_values("lap_number").reset_index(drop=True)
        by_lap = {int(r["lap_number"]): r for _, r in ordered.iterrows()}

        circuit_short_name = (
            str(ordered["circuit_short_name"].iloc[0])
            if pd.notna(ordered["circuit_short_name"].iloc[0])
            else None
        )
        raw_total_laps = int(ordered["race_total_laps"].iloc[0] or 55)
        sched_laps = scheduled_laps_for_circuit(circuit_short_name, raw_total_laps)

        for idx, cutoff_row in ordered.iterrows():
            cutoff_lap = int(cutoff_row["lap_number"])
            cutoff_end_utc = cutoff_row.get("lap_end_estimate_utc")
            cutoff_obs_s = cutoff_row.get("observed_lap_time_s")
            cutoff_stint = (
                int(cutoff_row["stint_number"])
                if pd.notna(cutoff_row.get("stint_number"))
                else None
            )
            cutoff_compound = (
                str(cutoff_row["compound"]).upper()
                if pd.notna(cutoff_row.get("compound"))
                else "UNKNOWN"
            )
            cutoff_tyre_age = (
                int(cutoff_row["tyre_age_at_lap_start"])
                if pd.notna(cutoff_row.get("tyre_age_at_lap_start"))
                else None
            )

            # Cannot issue an after-lap continued-stint forecast if the cutoff lap was incomplete,
            # lacked a valid timestamp/stint, or the driver entered the pits at the end of cutoff_lap.
            if (
                pd.isna(cutoff_end_utc)
                or pd.isna(cutoff_obs_s)
                or float(cutoff_obs_s) <= 0.0
                or cutoff_stint is None
                or cutoff_tyre_age is None
                or bool(cutoff_row.get("is_pit_in_lap", False))
            ):
                continue

            cutoff_ts = pd.Timestamp(cutoff_end_utc)
            if cutoff_ts.tzinfo is None:
                cutoff_ts = cutoff_ts.tz_localize("UTC")
            else:
                cutoff_ts = cutoff_ts.tz_convert("UTC")

            # Strict point-in-time history: only laps completed at or before cutoff_ts
            past_laps = ordered.iloc[: idx + 1]
            past_clean = past_laps[
                past_laps["is_clean_green_pace_lap"].fillna(False).astype(bool)
                & (past_laps["compound"].isin(DRY_COMPOUNDS))
            ]

            stint_laps_so_far = past_laps[past_laps["stint_number"] == cutoff_stint]
            current_stint_laps_completed = len(stint_laps_so_far)
            tyre_age_at_stint_start = int(
                max(0, cutoff_tyre_age - (current_stint_laps_completed - 1))
            )

            # Point-in-time weather lookup as of cutoff_ts
            last_track_temp_c: float | None = None
            last_air_temp_c: float | None = None
            last_rainfall: bool | None = None
            temp_age_s: float | None = None
            if not weather_sorted.empty:
                pos = int(weather_dates.searchsorted(cutoff_ts, side="right")) - 1
                if pos >= 0:
                    wrow = weather_sorted.iloc[pos]
                    w_ts = weather_dates.iloc[pos]
                    if pd.notna(wrow.get("track_temperature")):
                        last_track_temp_c = float(wrow["track_temperature"])
                    if pd.notna(wrow.get("air_temperature")):
                        last_air_temp_c = float(wrow["air_temperature"])
                    if pd.notna(wrow.get("rainfall")):
                        last_rainfall = bool(wrow["rainfall"])
                    temp_age_s = float(max(0.0, (cutoff_ts - w_ts).total_seconds()))

            for h in horizons:
                target_lap = cutoff_lap + int(h)
                if target_lap > sched_laps and target_lap not in by_lap:
                    continue

                recent_stats = compute_recent_clean_stats(
                    past_clean, current_stint=cutoff_stint, target_lap_number=target_lap
                )

                expected_tyre_age = int(cutoff_tyre_age + int(h))
                race_progress = float(target_lap) / float(sched_laps)

                target_row = by_lap.get(target_lap)
                # Check if any intervening lap between cutoff_lap+1 and target_lap changed stint or pitted
                stint_changed = False
                for step_lap in range(cutoff_lap + 1, target_lap + 1):
                    s_row = by_lap.get(step_lap)
                    if s_row is None:
                        stint_changed = True
                        break
                    s_stint = (
                        int(s_row["stint_number"])
                        if pd.notna(s_row.get("stint_number"))
                        else None
                    )
                    if s_stint != cutoff_stint or bool(s_row.get("is_pit_lap", False)):
                        stint_changed = True
                        break

                is_dry_compound = cutoff_compound in DRY_COMPOUNDS and not bool(last_rainfall)

                if target_row is None:
                    obs_target_s = None
                    target_start_utc = None
                    target_end_utc = None
                    target_clean_green = False
                    target_excl = "MISSING_TARGET_LAP"
                else:
                    obs_target_s = (
                        float(target_row["observed_lap_time_s"])
                        if pd.notna(target_row.get("observed_lap_time_s"))
                        else None
                    )
                    target_start_utc = target_row.get("lap_start_utc")
                    target_end_utc = target_row.get("lap_end_estimate_utc")
                    raw_target_clean = bool(target_row.get("is_clean_green_pace_lap", False))
                    raw_target_excl = (
                        str(target_row["exclusion_reason"])
                        if pd.notna(target_row.get("exclusion_reason"))
                        else None
                    )
                    target_ts_anomaly = False
                    if pd.notna(target_start_utc):
                        t_start_ts = pd.Timestamp(target_start_utc)
                        t_start_ts = (
                            t_start_ts.tz_localize("UTC")
                            if t_start_ts.tzinfo is None
                            else t_start_ts.tz_convert("UTC")
                        )
                        if (cutoff_ts - t_start_ts).total_seconds() > 1.5:
                            target_ts_anomaly = True
                    if target_ts_anomaly:
                        target_clean_green = False
                        target_excl = "UPSTREAM_TIMESTAMP_ANOMALY"
                    elif not is_dry_compound:
                        target_clean_green = False
                        target_excl = "WET_OR_NON_DRY_COMPOUND"
                    elif stint_changed:
                        target_clean_green = False
                        target_excl = raw_target_excl or "STINT_CHANGED_BEFORE_TARGET"
                    else:
                        target_clean_green = raw_target_clean
                        target_excl = raw_target_excl

                records.append(
                    {
                        "session_key": int(cutoff_row["session_key"]),
                        "meeting_key": int(cutoff_row["meeting_key"])
                        if pd.notna(cutoff_row.get("meeting_key"))
                        else None,
                        "driver_number": drv_num,
                        "cutoff_lap_number": cutoff_lap,
                        "target_lap_number": target_lap,
                        "horizon": int(h),
                        "forecast_as_of_utc": cutoff_ts.to_pydatetime(),
                        "target_lap_start_utc": target_start_utc,
                        "target_lap_end_utc": target_end_utc,
                        "modeling_eligibility": (
                            "ELIGIBLE_CLEAN_GREEN" if target_clean_green else "INELIGIBLE_TARGET"
                        ),
                        "season": int(cutoff_row["season"])
                        if pd.notna(cutoff_row.get("season"))
                        else None,
                        "regulation_era": str(cutoff_row.get("regulation_era") or "OTHER"),
                        "circuit_key": int(cutoff_row["circuit_key"])
                        if pd.notna(cutoff_row.get("circuit_key"))
                        else None,
                        "circuit_short_name": circuit_short_name,
                        "team_id": team_id,
                        "driver_id": driver_id,
                        "scheduled_race_laps": sched_laps,
                        "race_progress": race_progress,
                        "compound": cutoff_compound,
                        "tyre_age_at_lap_start": expected_tyre_age,
                        "stint_number": cutoff_stint,
                        "current_stint_laps_completed": current_stint_laps_completed,
                        "tyre_age_at_stint_start": tyre_age_at_stint_start,
                        "actual_compound_code": None,
                        "compound_mapping_source": "UNMAPPED",
                        "compound_mapping_confidence": "UNKNOWN",
                        "last_clean_lap_time_s": recent_stats["last_clean_lap_time_s"],
                        "median_last_3_clean_laps_s": recent_stats["median_last_3_clean_laps_s"],
                        "median_last_5_clean_laps_s": recent_stats["median_last_5_clean_laps_s"],
                        "median_stint_clean_laps_s": recent_stats["median_stint_clean_laps_s"],
                        "clean_laps_observed_current_stint": recent_stats[
                            "clean_laps_observed_current_stint"
                        ],
                        "clean_laps_observed_race": recent_stats["clean_laps_observed_race"],
                        "last_clean_lap_age": recent_stats["last_clean_lap_age"],
                        "last_known_track_temp_c": last_track_temp_c,
                        "last_known_air_temp_c": last_air_temp_c,
                        "last_known_rainfall": last_rainfall,
                        "temperature_observation_age_s": temp_age_s,
                        "observed_target_lap_time_s": obs_target_s,
                        "target_is_clean_green": bool(target_clean_green),
                        "target_exclusion_reason": target_excl,
                    }
                )

    return pd.DataFrame(records)


def populate_pace_training_examples(
    con: duckdb.DuckDBPyConnection,
    session_key: int | None = None,
    horizons: Sequence[int] = (1, 3, 5),
) -> int:
    """Populate gold.pace_training_examples for one session or all sessions in gold.lap_context."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS gold.pace_training_examples (
            session_key BIGINT,
            meeting_key BIGINT,
            driver_number INTEGER,
            cutoff_lap_number INTEGER,
            target_lap_number INTEGER,
            horizon INTEGER,
            forecast_as_of_utc TIMESTAMPTZ,
            target_lap_start_utc TIMESTAMPTZ,
            target_lap_end_utc TIMESTAMPTZ,
            modeling_eligibility VARCHAR,
            season INTEGER,
            regulation_era VARCHAR,
            circuit_key BIGINT,
            circuit_short_name VARCHAR,
            team_id VARCHAR,
            driver_id VARCHAR,
            scheduled_race_laps INTEGER,
            race_progress DOUBLE,
            compound VARCHAR,
            tyre_age_at_lap_start INTEGER,
            stint_number INTEGER,
            current_stint_laps_completed INTEGER,
            tyre_age_at_stint_start INTEGER,
            actual_compound_code VARCHAR,
            compound_mapping_source VARCHAR,
            compound_mapping_confidence VARCHAR,
            last_clean_lap_time_s DOUBLE,
            median_last_3_clean_laps_s DOUBLE,
            median_last_5_clean_laps_s DOUBLE,
            median_stint_clean_laps_s DOUBLE,
            clean_laps_observed_current_stint INTEGER,
            clean_laps_observed_race INTEGER,
            last_clean_lap_age INTEGER,
            last_known_track_temp_c DOUBLE,
            last_known_air_temp_c DOUBLE,
            last_known_rainfall BOOLEAN,
            temperature_observation_age_s DOUBLE,
            observed_target_lap_time_s DOUBLE,
            target_is_clean_green BOOLEAN,
            target_exclusion_reason VARCHAR
        )
    """)

    if session_key is not None:
        session_keys = [int(session_key)]
    else:
        rows = con.execute(
            "SELECT DISTINCT session_key FROM gold.lap_context ORDER BY session_key"
        ).fetchall()
        session_keys = [int(r[0]) for r in rows]

    total_inserted = 0
    for sk in session_keys:
        con.execute("DELETE FROM gold.pace_training_examples WHERE session_key = ?", [sk])
        lap_ctx_df = con.execute(
            "SELECT * FROM gold.lap_context WHERE session_key = ? ORDER BY driver_number, lap_number",
            [sk],
        ).df()
        drivers_df = con.execute(
            "SELECT * FROM silver.drivers WHERE session_key = ?",
            [sk],
        ).df()
        weather_df = con.execute(
            "SELECT * FROM silver.weather WHERE session_key = ? ORDER BY date",
            [sk],
        ).df()
        ex_df = build_pace_examples_dataframe(
            lap_ctx_df=lap_ctx_df,
            drivers_df=drivers_df,
            weather_df=weather_df,
            horizons=horizons,
        )
        if not ex_df.empty:
            con.register("_incoming_pace_examples", ex_df)
            try:
                con.execute(
                    "INSERT INTO gold.pace_training_examples SELECT * FROM _incoming_pace_examples"
                )
            finally:
                con.unregister("_incoming_pace_examples")
            total_inserted += len(ex_df)

    return total_inserted
