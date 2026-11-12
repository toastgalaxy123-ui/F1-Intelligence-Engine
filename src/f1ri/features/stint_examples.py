"""Build gold.stint_training_examples for point-in-time multi-horizon stint forecasting."""
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from f1ri.features.pace_features import (
    canonical_driver_id,
    canonical_team_id,
    compute_recent_clean_stats,
    scheduled_laps_for_circuit,
)
from f1ri.features.stint_observations import canonical_stint_id
from f1ri.ingestion.compound_allocations import resolve_compound_allocation
from f1ri.models.contracts import (
    SCENARIO_CONTINUE_STINT,
    SCENARIO_NEW_FRESH,
    SCENARIO_NEW_USED,
)

DRY_COMPOUNDS = {"SOFT", "MEDIUM", "HARD"}
DEFAULT_STINT_HORIZONS = (1, 3, 5, 10, 15, 20)


def _safe_bool(val: Any) -> bool:
    if val is None or pd.isna(val):
        return False
    return bool(val)


def build_stint_examples_dataframe(
    lap_ctx_df: pd.DataFrame,
    drivers_df: pd.DataFrame,
    weather_df: pd.DataFrame,
    allocations_df: pd.DataFrame | None = None,
    grid_df: pd.DataFrame | None = None,
    horizons: Sequence[int] = DEFAULT_STINT_HORIZONS,
    include_new_stint_scenarios: bool = True,
) -> pd.DataFrame:
    """Construct point-in-time forecast cutoff x scenario x horizon examples for one session.

    All predictor columns use strictly information known at or before `forecast_as_of_utc`
    (`lap_end_estimate_utc` of `cutoff_lap_number`). Hypothetical scenario inputs (compound,
    initial tire age, horizon) never read future pit stops or future stint durations.
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

    grid_lookup: dict[int, tuple[int | None, float | None]] = {}
    if grid_df is not None and not grid_df.empty:
        for _, grow in grid_df.iterrows():
            dnum = int(grow["driver_number"])
            pos = int(grow["position"]) if pd.notna(grow.get("position")) else None
            q_lap = (
                float(grow["lap_duration"])
                if pd.notna(grow.get("lap_duration")) and float(grow["lap_duration"]) > 0
                else None
            )
            grid_lookup[dnum] = (pos, q_lap)

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

    alloc_df = allocations_df if allocations_df is not None else pd.DataFrame()
    records: list[dict[str, Any]] = []

    for driver_number, drv_laps in lap_ctx_df.groupby("driver_number"):
        drv_num = int(driver_number)
        driver_id, team_id = drv_lookup.get(
            drv_num, (canonical_driver_id(None, drv_num), "UNKNOWN_TEAM")
        )
        grid_pos, qual_time_s = grid_lookup.get(drv_num, (None, None))

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
                str(cutoff_row["compound"]).strip().upper()
                if pd.notna(cutoff_row.get("compound"))
                else "UNKNOWN"
            )
            cutoff_tyre_age = (
                int(cutoff_row["tyre_age_at_lap_start"])
                if pd.notna(cutoff_row.get("tyre_age_at_lap_start"))
                else None
            )

            if (
                pd.isna(cutoff_end_utc)
                or pd.isna(cutoff_obs_s)
                or float(cutoff_obs_s) <= 0.0
                or cutoff_stint is None
                or cutoff_tyre_age is None
            ):
                continue

            cutoff_ts = pd.Timestamp(cutoff_end_utc)
            if cutoff_ts.tzinfo is None:
                cutoff_ts = cutoff_ts.tz_localize("UTC")
            else:
                cutoff_ts = cutoff_ts.tz_convert("UTC")

            # Strict point-in-time history up through cutoff_lap
            past_laps = ordered.iloc[: idx + 1]
            past_clean = past_laps[
                past_laps["is_clean_green_pace_lap"].fillna(False).astype(bool)
                & (past_laps["compound"].isin(DRY_COMPOUNDS))
            ]

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

            meeting_key = (
                int(cutoff_row["meeting_key"])
                if pd.notna(cutoff_row.get("meeting_key"))
                else None
            )

            # Determine previous stint metadata strictly from past_laps
            prev_stint_laps = past_laps[past_laps["stint_number"] < cutoff_stint]
            prev_cmpd_rel: str | None = None
            prev_cmpd_abs: str | None = None
            prev_end_age: int | None = None
            if not prev_stint_laps.empty:
                last_prev = prev_stint_laps.iloc[-1]
                prev_cmpd_rel = (
                    str(last_prev["compound"]).strip().upper()
                    if pd.notna(last_prev.get("compound"))
                    else None
                )
                prev_end_age = (
                    int(last_prev["tyre_age_at_lap_start"])
                    if pd.notna(last_prev.get("tyre_age_at_lap_start"))
                    else None
                )
                if prev_cmpd_rel:
                    prev_alloc = resolve_compound_allocation(
                        alloc_df, meeting_key, prev_cmpd_rel, cutoff_ts
                    )
                    prev_cmpd_abs = prev_alloc["compound_absolute"]

            is_pit_in_at_cutoff = _safe_bool(cutoff_row.get("is_pit_in_lap"))

            # Build list of scenarios to evaluate from this cutoff
            scenarios_to_emit: list[dict[str, Any]] = []
            if not is_pit_in_at_cutoff:
                # Scenario A: Continue the current stint on the same tire set
                stint_clean_so_far = past_clean[past_clean["stint_number"] == cutoff_stint]
                stint_laps_so_far = past_laps[past_laps["stint_number"] == cutoff_stint]
                cur_stint_completed = len(stint_laps_so_far)
                tyre_age_stint_start = int(
                    max(0, cutoff_tyre_age - (cur_stint_completed - 1))
                )
                scenarios_to_emit.append(
                    {
                        "scenario_type": SCENARIO_CONTINUE_STINT,
                        "stint_number": cutoff_stint,
                        "planned_start_lap": cutoff_lap + 1,
                        "compound_relative": cutoff_compound,
                        "tyre_age_at_forecast_start": int(cutoff_tyre_age + 1),
                        "tyre_age_at_stint_start": tyre_age_stint_start,
                        "current_stint_laps_completed": cur_stint_completed,
                        "stint_clean_df": stint_clean_so_far,
                        "prev_cmpd_rel": prev_cmpd_rel,
                        "prev_cmpd_abs": prev_cmpd_abs,
                        "prev_end_age": prev_end_age,
                    }
                )
            elif include_new_stint_scenarios:
                # Driver pitted at the end of cutoff_lap; if a new stint started on cutoff_lap + 1,
                # emit a NEW_STINT scenario evaluating the flying pace laps starting after the pit-out lap.
                next_row = by_lap.get(cutoff_lap + 1)
                if (
                    next_row is not None
                    and pd.notna(next_row.get("stint_number"))
                    and int(next_row["stint_number"]) > cutoff_stint
                    and pd.notna(next_row.get("compound"))
                    and pd.notna(next_row.get("tyre_age_at_lap_start"))
                ):
                    new_stint_num = int(next_row["stint_number"])
                    new_cmpd = str(next_row["compound"]).strip().upper()
                    new_start_age = max(0, int(next_row["tyre_age_at_lap_start"]))
                    next_is_pit_out = _safe_bool(next_row.get("is_pit_out_lap"))
                    flying_start_lap = cutoff_lap + 2 if next_is_pit_out else cutoff_lap + 1
                    flying_start_age = new_start_age + 1 if next_is_pit_out else new_start_age
                    cur_alloc = resolve_compound_allocation(
                        alloc_df, meeting_key, cutoff_compound, cutoff_ts
                    )
                    scenarios_to_emit.append(
                        {
                            "scenario_type": (
                                SCENARIO_NEW_USED if new_start_age > 0 else SCENARIO_NEW_FRESH
                            ),
                            "stint_number": new_stint_num,
                            "planned_start_lap": flying_start_lap,
                            "compound_relative": new_cmpd,
                            "tyre_age_at_forecast_start": flying_start_age,
                            "tyre_age_at_stint_start": new_start_age,
                            "current_stint_laps_completed": 0,
                            "stint_clean_df": pd.DataFrame(),
                            "prev_cmpd_rel": cutoff_compound,
                            "prev_cmpd_abs": cur_alloc["compound_absolute"],
                            "prev_end_age": cutoff_tyre_age,
                        }
                    )

            for scen in scenarios_to_emit:
                scen_type = scen["scenario_type"]
                scen_stint = int(scen["stint_number"])
                planned_start_lap = int(scen["planned_start_lap"])
                scen_cmpd_rel = str(scen["compound_relative"])
                scen_start_age = int(scen["tyre_age_at_forecast_start"])
                stint_clean_df = scen["stint_clean_df"]

                alloc_info = resolve_compound_allocation(
                    alloc_df, meeting_key, scen_cmpd_rel, cutoff_ts
                )
                scen_cmpd_abs = alloc_info["compound_absolute"]

                if not stint_clean_df.empty:
                    obs_ages = [int(a) for a in stint_clean_df["tyre_age_at_lap_start"].tolist()]
                    obs_times = [
                        round(float(t), 4) for t in stint_clean_df["observed_lap_time_s"].tolist()
                    ]
                    obs_prog = [
                        round(float(lnum) / float(sched_laps), 5)
                        for lnum in stint_clean_df["lap_number"].tolist()
                    ]
                else:
                    obs_ages = []
                    obs_times = []
                    obs_prog = []

                obs_ages_json = json.dumps(obs_ages)
                obs_times_json = json.dumps(obs_times)
                obs_prog_json = json.dumps(obs_prog)

                recent_stats = compute_recent_clean_stats(
                    past_clean,
                    current_stint=(
                        scen_stint if scen_type == SCENARIO_CONTINUE_STINT else -1
                    ),
                    target_lap_number=planned_start_lap,
                )

                for h in horizons:
                    h_int = int(h)
                    target_end_lap = planned_start_lap + h_int - 1
                    if target_end_lap > sched_laps and target_end_lap not in by_lap:
                        continue

                    # Evaluate the actual future window [planned_start_lap .. planned_start_lap + h - 1]
                    # Strictly for target outcome & censoring columns, never for predictors.
                    is_dry_scenario = scen_cmpd_rel in DRY_COMPOUNDS and not bool(last_rainfall)

                    traj_times: list[float | None] = []
                    traj_clean_flags: list[bool] = []
                    contiguous_same_stint = 0
                    censoring_reason: str | None = None

                    for step_k in range(1, h_int + 1):
                        step_lap = planned_start_lap + step_k - 1
                        s_row = by_lap.get(step_lap)
                        if s_row is None:
                            if censoring_reason is None:
                                censoring_reason = (
                                    "RACE_FINISH_REACHED"
                                    if step_lap > int(ordered["lap_number"].max())
                                    and step_lap >= sched_laps - 1
                                    else "RETIREMENT_OR_INCOMPLETE"
                                )
                            traj_times.append(None)
                            traj_clean_flags.append(False)
                            continue

                        s_stint = (
                            int(s_row["stint_number"])
                            if pd.notna(s_row.get("stint_number"))
                            else None
                        )
                        s_pit_in = _safe_bool(s_row.get("is_pit_in_lap"))
                        s_pit_out = _safe_bool(s_row.get("is_pit_out_lap"))
                        # For a NEW_STINT scenario, step_k == 1 is the pit-out lap itself;
                        # for CONTINUE_CURRENT_STINT, any pit lap or stint change terminates continuation.
                        if s_stint != scen_stint or s_pit_in or (
                            scen_type == SCENARIO_CONTINUE_STINT and s_pit_out
                        ):
                            if censoring_reason is None:
                                censoring_reason = "FUTURE_PIT_TRANSITION"
                            traj_times.append(None)
                            traj_clean_flags.append(False)
                            continue

                        if censoring_reason != "FUTURE_PIT_TRANSITION":
                            contiguous_same_stint += 1

                        s_obs = (
                            float(s_row["observed_lap_time_s"])
                            if pd.notna(s_row.get("observed_lap_time_s"))
                            and float(s_row["observed_lap_time_s"]) > 0
                            else None
                        )
                        s_clean = _safe_bool(s_row.get("is_clean_green_pace_lap")) and is_dry_scenario

                        # Check timestamp ordering integrity
                        s_start_utc = s_row.get("lap_start_utc")
                        if pd.notna(s_start_utc):
                            t_s = pd.Timestamp(s_start_utc)
                            t_s = (
                                t_s.tz_localize("UTC")
                                if t_s.tzinfo is None
                                else t_s.tz_convert("UTC")
                            )
                            if (cutoff_ts - t_s).total_seconds() > 1.5:
                                s_clean = False
                                if censoring_reason is None:
                                    censoring_reason = "UPSTREAM_TIMESTAMP_ANOMALY"

                        if not s_clean and censoring_reason is None:
                            t_stat = str(s_row.get("track_status") or "GREEN")
                            if not is_dry_scenario:
                                censoring_reason = "WET_OR_NON_DRY_COMPOUND"
                            elif s_obs is None:
                                censoring_reason = "RETIREMENT_OR_INCOMPLETE"
                            elif t_stat in ("SC", "VSC", "RED", "YELLOW"):
                                censoring_reason = "NEUTRALIZATION_SC_VSC_RED"
                            elif s_pit_out:
                                censoring_reason = "PIT_OUT_LAP_IN_HORIZON"
                            else:
                                censoring_reason = "TRAFFIC_CONTAMINATION"

                        traj_times.append(s_obs)
                        traj_clean_flags.append(s_clean)

                    all_clean = (
                        len(traj_clean_flags) == h_int
                        and all(traj_clean_flags)
                        and all(t is not None for t in traj_times)
                    )
                    if all_clean:
                        censoring_reason = "COMPLETE_CLEAN"
                    elif censoring_reason is None:
                        censoring_reason = "INCOMPLETE_HORIZON"

                    # Endpoint target lap (at l_c + h) for Dataset A compatibility
                    end_obs_s = traj_times[-1] if traj_times else None
                    end_is_clean = bool(traj_clean_flags[-1]) if traj_clean_flags else False
                    end_row = by_lap.get(target_end_lap)
                    end_start_utc = end_row.get("lap_start_utc") if end_row is not None else None
                    end_end_utc = (
                        end_row.get("lap_end_estimate_utc") if end_row is not None else None
                    )

                    if end_is_clean:
                        end_excl = None
                    elif censoring_reason == "FUTURE_PIT_TRANSITION":
                        end_excl = "FUTURE_PIT_TRANSITION"
                    elif end_row is None:
                        end_excl = "MISSING_TARGET_LAP"
                    else:
                        end_excl = (
                            str(end_row["exclusion_reason"])
                            if pd.notna(end_row.get("exclusion_reason"))
                            else censoring_reason
                        )

                    clean_cumulative_s = (
                        round(float(sum(t for t in traj_times if t is not None)), 4)
                        if all_clean
                        else None
                    )
                    obs_pace_change_s: float | None = None
                    if all_clean and h_int >= 3:
                        k_win = max(1, h_int // 3)
                        early_mean = float(np.mean([t for t in traj_times[:k_win] if t is not None]))
                        late_mean = float(np.mean([t for t in traj_times[-k_win:] if t is not None]))
                        obs_pace_change_s = round(late_mean - early_mean, 4)

                    records.append(
                        {
                            "session_key": int(cutoff_row["session_key"]),
                            "meeting_key": meeting_key,
                            "stint_id": canonical_stint_id(
                                int(cutoff_row["session_key"]), drv_num, scen_stint
                            ),
                            "driver_number": drv_num,
                            "cutoff_lap_number": cutoff_lap,
                            "planned_start_lap": planned_start_lap,
                            "target_lap_number": target_end_lap,
                            "requested_horizon": h_int,
                            "scenario_type": scen_type,
                            "forecast_as_of_utc": cutoff_ts.to_pydatetime(),
                            "target_lap_start_utc": end_start_utc,
                            "target_lap_end_utc": end_end_utc,
                            "season": int(cutoff_row["season"])
                            if pd.notna(cutoff_row.get("season"))
                            else None,
                            "regulation_era": str(cutoff_row.get("regulation_era") or "2023_2025"),
                            "circuit_key": int(cutoff_row["circuit_key"])
                            if pd.notna(cutoff_row.get("circuit_key"))
                            else None,
                            "circuit_short_name": circuit_short_name,
                            "team_id": team_id,
                            "driver_id": driver_id,
                            "scheduled_race_laps": sched_laps,
                            "race_progress": float(target_end_lap) / float(sched_laps),
                            "stint_number": scen_stint,
                            "compound_relative": scen_cmpd_rel,
                            "compound_absolute": scen_cmpd_abs,
                            "compound_mapping_source": alloc_info["compound_mapping_source"],
                            "compound_mapping_confidence": alloc_info["compound_mapping_confidence"],
                            "tyre_age_at_forecast_start": scen_start_age,
                            "tyre_age_at_lap_start": int(scen_start_age + h_int - 1),
                            "tyre_age_at_stint_start": int(scen["tyre_age_at_stint_start"]),
                            "current_stint_laps_completed": int(
                                scen["current_stint_laps_completed"]
                            ),
                            "qualifying_lap_time_s": qual_time_s,
                            "grid_position": grid_pos,
                            "last_clean_lap_time_s": recent_stats["last_clean_lap_time_s"],
                            "median_last_3_clean_laps_s": recent_stats[
                                "median_last_3_clean_laps_s"
                            ],
                            "median_last_5_clean_laps_s": recent_stats[
                                "median_last_5_clean_laps_s"
                            ],
                            "median_stint_clean_laps_s": recent_stats[
                                "median_stint_clean_laps_s"
                            ],
                            "clean_laps_observed_current_stint": recent_stats[
                                "clean_laps_observed_current_stint"
                            ],
                            "clean_laps_observed_race": recent_stats["clean_laps_observed_race"],
                            "last_clean_lap_age": recent_stats["last_clean_lap_age"],
                            "observed_stint_clean_ages_json": obs_ages_json,
                            "observed_stint_clean_times_json": obs_times_json,
                            "observed_stint_clean_progress_json": obs_prog_json,
                            "prev_stint_compound_relative": scen["prev_cmpd_rel"],
                            "prev_stint_compound_absolute": scen["prev_cmpd_abs"],
                            "prev_stint_end_tyre_age": scen["prev_end_age"],
                            "last_known_track_temp_c": last_track_temp_c,
                            "last_known_air_temp_c": last_air_temp_c,
                            "last_known_rainfall": last_rainfall,
                            "temperature_observation_age_s": temp_age_s,
                            "observed_target_lap_time_s": end_obs_s,
                            "target_is_clean_green": bool(end_is_clean),
                            "target_exclusion_reason": end_excl,
                            "trajectory_is_complete_clean": bool(all_clean),
                            "trajectory_clean_laps_count": int(sum(traj_clean_flags)),
                            "trajectory_contiguous_same_stint_laps": int(contiguous_same_stint),
                            "trajectory_censoring_reason": censoring_reason,
                            "observed_cumulative_time_s": clean_cumulative_s,
                            "observed_pace_change_s": obs_pace_change_s,
                            "observed_lap_times_json": json.dumps(traj_times),
                            "observed_lap_clean_flags_json": json.dumps(traj_clean_flags),
                        }
                    )

    return pd.DataFrame(records)


def populate_stint_training_examples(
    con: duckdb.DuckDBPyConnection,
    session_key: int | None = None,
    horizons: Sequence[int] = DEFAULT_STINT_HORIZONS,
) -> int:
    """Create and populate gold.stint_training_examples across one or all sessions."""
    con.execute("CREATE SCHEMA IF NOT EXISTS gold")
    con.execute("""
        CREATE TABLE IF NOT EXISTS gold.stint_training_examples (
            session_key BIGINT,
            meeting_key BIGINT,
            stint_id VARCHAR,
            driver_number INTEGER,
            cutoff_lap_number INTEGER,
            planned_start_lap INTEGER,
            target_lap_number INTEGER,
            requested_horizon INTEGER,
            scenario_type VARCHAR,
            forecast_as_of_utc TIMESTAMPTZ,
            target_lap_start_utc TIMESTAMPTZ,
            target_lap_end_utc TIMESTAMPTZ,
            season INTEGER,
            regulation_era VARCHAR,
            circuit_key BIGINT,
            circuit_short_name VARCHAR,
            team_id VARCHAR,
            driver_id VARCHAR,
            scheduled_race_laps INTEGER,
            race_progress DOUBLE,
            stint_number INTEGER,
            compound_relative VARCHAR,
            compound_absolute VARCHAR,
            compound_mapping_source VARCHAR,
            compound_mapping_confidence VARCHAR,
            tyre_age_at_forecast_start INTEGER,
            tyre_age_at_lap_start INTEGER,
            tyre_age_at_stint_start INTEGER,
            current_stint_laps_completed INTEGER,
            qualifying_lap_time_s DOUBLE,
            grid_position INTEGER,
            last_clean_lap_time_s DOUBLE,
            median_last_3_clean_laps_s DOUBLE,
            median_last_5_clean_laps_s DOUBLE,
            median_stint_clean_laps_s DOUBLE,
            clean_laps_observed_current_stint INTEGER,
            clean_laps_observed_race INTEGER,
            last_clean_lap_age INTEGER,
            observed_stint_clean_ages_json VARCHAR,
            observed_stint_clean_times_json VARCHAR,
            observed_stint_clean_progress_json VARCHAR,
            prev_stint_compound_relative VARCHAR,
            prev_stint_compound_absolute VARCHAR,
            prev_stint_end_tyre_age INTEGER,
            last_known_track_temp_c DOUBLE,
            last_known_air_temp_c DOUBLE,
            last_known_rainfall BOOLEAN,
            temperature_observation_age_s DOUBLE,
            observed_target_lap_time_s DOUBLE,
            target_is_clean_green BOOLEAN,
            target_exclusion_reason VARCHAR,
            trajectory_is_complete_clean BOOLEAN,
            trajectory_clean_laps_count INTEGER,
            trajectory_contiguous_same_stint_laps INTEGER,
            trajectory_censoring_reason VARCHAR,
            observed_cumulative_time_s DOUBLE,
            observed_pace_change_s DOUBLE,
            observed_lap_times_json VARCHAR,
            observed_lap_clean_flags_json VARCHAR
        )
    """)

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

    total_inserted = 0
    for sk in session_keys:
        con.execute("DELETE FROM gold.stint_training_examples WHERE session_key = ?", [sk])
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
        try:
            grid_df = con.execute(
                "SELECT * FROM silver.starting_grid WHERE session_key = ?",
                [sk],
            ).df()
        except duckdb.Error:
            grid_df = pd.DataFrame()

        ex_df = build_stint_examples_dataframe(
            lap_ctx_df=lap_ctx_df,
            drivers_df=drivers_df,
            weather_df=weather_df,
            allocations_df=alloc_df,
            grid_df=grid_df,
            horizons=horizons,
        )
        if not ex_df.empty:
            con.register("_incoming_stint_examples", ex_df)
            try:
                con.execute(
                    "INSERT INTO gold.stint_training_examples SELECT * FROM _incoming_stint_examples"
                )
            finally:
                con.unregister("_incoming_stint_examples")
            total_inserted += len(ex_df)

    return total_inserted
