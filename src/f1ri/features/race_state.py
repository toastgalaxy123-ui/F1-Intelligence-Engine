"""Point-in-time field snapshots for the S1 green-flag continuation scenario."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from f1ri.features.lap_context import build_track_status_intervals, populate_session_lap_context
from f1ri.features.pace_features import (
    canonical_driver_id,
    canonical_team_id,
    compute_recent_clean_stats,
)
from f1ri.features.stint_examples import DRY_COMPOUNDS
from f1ri.ingestion.compound_allocations import resolve_compound_allocation
from f1ri.models.contracts import StintForecastRequest
from f1ri.simulator.state import TrackState


@dataclass(frozen=True, slots=True)
class DriverSnapshot:
    driver_number: int
    completed_laps: int | None
    last_crossing_utc: datetime | None
    elapsed_current_lap_s: float | None
    reported_position: int | None
    reported_gap: str | None
    request: StintForecastRequest | None
    support_reason: str | None


@dataclass(frozen=True, slots=True)
class FieldSnapshot:
    session_key: int
    race_start_utc: datetime
    as_of_utc: datetime
    scheduled_laps: int
    track_state: TrackState
    weather_observed_at_utc: datetime | None
    drivers: tuple[DriverSnapshot, ...]
    sector_fractions: tuple[float, float, float]


_VISIBLE_LAPS = """
    WITH started AS (
        SELECT *, LEAD(lap_number) OVER w AS next_lap
        FROM silver.laps WHERE session_key = ? AND date_start <= ?
        WINDOW w AS (PARTITION BY driver_number ORDER BY lap_number, date_start)
    ), available AS (
        SELECT *, next_lap = lap_number + 1 AND isfinite(lap_duration) AND lap_duration > 0
            AND date_start + to_milliseconds(TRY_CAST(round(lap_duration * 1000) AS BIGINT))
                <= ? AS result_available
        FROM started
    )
    SELECT * EXCLUDE (next_lap, result_available) REPLACE (
        CASE WHEN result_available THEN lap_duration END AS lap_duration,
        CASE WHEN result_available THEN duration_sector_1 END AS duration_sector_1,
        CASE WHEN result_available THEN duration_sector_2 END AS duration_sector_2,
        CASE WHEN result_available THEN duration_sector_3 END AS duration_sector_3,
        CASE WHEN result_available THEN is_pit_out_lap END AS is_pit_out_lap
    )
    FROM available ORDER BY driver_number, lap_number, date_start
"""

_VISIBLE_STINTS = """
    WITH visible AS (
        SELECT s.* EXCLUDE (lap_end)
        FROM silver.stints s
        JOIN prefix_laps l ON s.driver_number = l.driver_number AND s.lap_start = l.lap_number
        WHERE s.session_key = ? AND (s.stint_number = 1 OR EXISTS (
            SELECT 1 FROM prefix_pits p WHERE p.driver_number = s.driver_number
            AND p.lap_number BETWEEN s.lap_start - 1 AND s.lap_start
        ))
    ), bounds AS (
        SELECT *, LEAD(lap_start) OVER (PARTITION BY driver_number ORDER BY lap_start)
            AS next_start FROM visible
    )
    SELECT * EXCLUDE (next_start), COALESCE(next_start - 1, (
        SELECT MAX(lap_number) FROM prefix_laps l WHERE l.driver_number = bounds.driver_number
    ))::INTEGER AS lap_end FROM bounds
"""


def _prefix(con: duckdb.DuckDBPyConnection, session_key: int, cutoff: datetime) -> dict:
    query = "SELECT * FROM silver.{} WHERE session_key = ?"
    frames = {name: con.execute(query.format(name), [session_key]).df()
              for name in ("sessions", "drivers", "starting_grid")}
    frames["sessions"]["date_end"] = cutoff
    for name in ("pit", "position", "intervals", "weather", "race_control"):
        frames[name] = con.execute(query.format(name) + " AND date <= ?", [session_key, cutoff]).df()
    frames["laps"] = con.execute(_VISIBLE_LAPS, [session_key, cutoff, cutoff]).df()
    con.register("prefix_laps", frames["laps"])
    con.register("prefix_pits", frames["pit"])
    try:
        frames["stints"] = con.execute(_VISIBLE_STINTS, [session_key]).df()
    finally:
        con.unregister("prefix_laps")
        con.unregister("prefix_pits")
    has_allocations = con.execute("""
        SELECT count(*) FROM information_schema.tables
        WHERE table_schema = 'silver' AND table_name = 'compound_allocations'
    """).fetchone()[0]
    frames["allocations"] = con.execute("""
        SELECT a.* FROM silver.compound_allocations a
        JOIN silver.sessions s ON a.meeting_key = s.meeting_key
        WHERE s.session_key = ? AND a.available_as_of_utc <= ?
    """, [session_key, cutoff]).df() if has_allocations else pd.DataFrame()
    return frames


def _context(frames: dict, session_key: int) -> pd.DataFrame:
    with duckdb.connect() as scratch:
        scratch.execute("SET TimeZone = 'UTC'")
        scratch.execute("CREATE SCHEMA silver")
        scratch.execute("CREATE SCHEMA gold")
        for name, frame in frames.items():
            if name == "allocations":
                continue
            scratch.register("prefix_input", frame)
            scratch.execute(f"CREATE TABLE silver.{name} AS SELECT * FROM prefix_input")
            scratch.unregister("prefix_input")
        populate_session_lap_context(scratch, session_key)
        return scratch.execute("""
            SELECT * FROM gold.lap_context ORDER BY driver_number, lap_number
        """).df()


def _last_by_driver(frame: pd.DataFrame, field: str) -> dict:
    valid = frame.dropna(subset=[field]).sort_values(["date", "driver_number", field])
    return valid.drop_duplicates("driver_number", keep="last").set_index("driver_number")[field].to_dict()


def _known(value):
    return None if pd.isna(value) else value


def _prior_clean_laps(laps: pd.DataFrame, next_lap: int, cutoff: datetime) -> pd.DataFrame:
    past = laps[(laps.lap_number < next_lap) & (laps.lap_end_estimate_utc <= cutoff)]
    return past[past.is_clean_green_pace_lap.fillna(False) & past.compound.isin(DRY_COMPOUNDS)]


def _same_stint_history(laps: pd.DataFrame, current: pd.Series, cutoff: datetime) -> list[dict]:
    """Inspect the prefix context used by the pace request, including excluded laps."""
    starts = laps.set_index("lap_number").lap_start_utc.to_dict()
    previous = laps[laps.lap_number.lt(current.lap_number)
                    & laps.stint_number.eq(current.stint_number)
                    & laps.observed_lap_time_s.notna() & laps.lap_end_estimate_utc.le(cutoff)]
    result = []
    for row in previous.itertuples():
        next_line = starts.get(row.lap_number + 1)
        if pd.isna(next_line) or next_line > cutoff:
            continue
        result.append({
            "lap_number": int(row.lap_number), "stint_number": int(row.stint_number),
            "compound": row.compound, "started_utc": row.lap_start_utc,
            "completed_utc": row.lap_end_estimate_utc, "next_line_utc": next_line,
            "available_by_utc": max(row.lap_end_estimate_utc, next_line),
            "stage1_exclusions": row.exclusion_reason,
            "clean_green": bool(row.is_clean_green_pace_lap),
            "classification_position": _known(row.position_at_lap_start),
            "classification_gap_raw": _known(row.gap_ahead_at_start_raw),
            "classification_gap_s": _known(row.gap_ahead_at_start_s),
            "track_gap_start_s": _known(row.track_gap_ahead_at_start_s),
            "track_gap_end_s": _known(row.track_gap_ahead_at_end_s),
            "clean_air_at_start": _known(row.is_clean_air_at_start),
            "archive_pass_indication": bool(row.had_in_lap_on_track_pass),
            "blue_flag": bool(row.is_blue_flagged_lap), "track_status": row.track_status,
            "pit_in": bool(row.is_pit_in_lap), "pit_out": bool(row.is_pit_out_lap),
        })
    return result


def _clean_lap_evidence(laps: pd.DataFrame, request: StintForecastRequest) -> list[dict]:
    clean = _prior_clean_laps(laps, request.planned_start_lap, request.as_of_utc)
    clean = clean[clean.stint_number.eq(request.stint_number) & clean.observed_lap_time_s.notna()]
    starts = laps.set_index("lap_number").lap_start_utc.to_dict()
    evidence = []
    for row in clean.itertuples():
        next_line = starts.get(row.lap_number + 1)
        if pd.isna(next_line):
            raise ValueError("Visible clean history requires its next timing line")
        available = max(row.lap_end_estimate_utc, next_line)
        if available > request.as_of_utc:
            raise ValueError("Visible clean history cannot depend on future timing")
        evidence.append({"lap_number": int(row.lap_number), "available_by_utc": available})
    if len(evidence) != request.clean_laps_observed_current_stint:
        raise ValueError("Clean history audit differs from the Pace V2 request")
    return evidence


def _driver_request(
    session: dict, driver: dict, grid: dict, laps: pd.DataFrame, current: pd.Series,
    weather: dict, allocations: pd.DataFrame, cutoff: datetime, distance: int, horizon: int,
) -> StintForecastRequest:
    stint = int(current.stint_number)
    next_lap = int(current.lap_number)
    compound = str(current.compound).upper()
    meeting = int(session["meeting_key"])
    allocation = resolve_compound_allocation(allocations, meeting, compound, cutoff)
    past = laps[(laps.lap_number < next_lap) & (laps.lap_end_estimate_utc <= cutoff)]
    clean = _prior_clean_laps(laps, next_lap, cutoff)
    stats = compute_recent_clean_stats(clean, current_stint=stint, target_lap_number=next_lap)
    stint_clean = clean[clean.stint_number == stint]
    previous = past[past.stint_number < stint]
    prev = previous.iloc[-1] if not previous.empty else {}
    prev_cmpd = _known(prev.get("compound"))
    prev_alloc = resolve_compound_allocation(allocations, meeting, prev_cmpd, cutoff) if prev_cmpd else {}
    qual = _known(grid.get("lap_duration"))
    season = int(session["year"])
    return StintForecastRequest(
        session_key=int(session["session_key"]), driver_number=int(driver["driver_number"]),
        cutoff_lap_number=next_lap - 1, planned_start_lap=next_lap,
        horizon_laps=min(horizon, distance - next_lap + 1),
        compound_relative=compound, tyre_age_at_start=int(current.tyre_age_at_lap_start),
        as_of_utc=cutoff, compound_absolute=allocation["compound_absolute"],
        compound_mapping_confidence=allocation["compound_mapping_confidence"],
        meeting_key=meeting, season=season, regulation_era=str(current.regulation_era),
        circuit_key=_known(session["circuit_key"]), circuit_short_name=session["circuit_short_name"],
        driver_id=canonical_driver_id(_known(driver["name_acronym"]), driver["driver_number"]),
        team_id=canonical_team_id(_known(driver["team_name"])),
        stint_number=stint, scheduled_race_laps=distance,
        qualifying_lap_time_s=qual if qual is not None and qual > 0 else None,
        grid_position=_known(grid.get("position")),
        observed_stint_clean_ages=tuple(int(x) for x in stint_clean.tyre_age_at_lap_start),
        observed_stint_clean_times_s=tuple(round(float(x), 4) for x in stint_clean.observed_lap_time_s),
        observed_stint_clean_progress=tuple(round(int(x) / distance, 5) for x in stint_clean.lap_number),
        prev_stint_compound_relative=prev_cmpd,
        prev_stint_compound_absolute=prev_alloc.get("compound_absolute"),
        prev_stint_end_tyre_age=_known(prev.get("tyre_age_at_lap_start")),
        track_temperature_c=_known(weather.get("track_temperature")),
        air_temperature_c=_known(weather.get("air_temperature")),
        **stats,
    )


def build_field_snapshot(
    warehouse: Path, session_key: int, cutoff: datetime, scheduled_laps: int, horizon: int,
    *, clean_history_audit: dict[int, list[dict]] | None = None,
    same_stint_history_audit: dict[int, list[dict]] | None = None,
) -> FieldSnapshot:
    """Build the whole field from one observation cutoff without joining race results."""
    if cutoff.utcoffset() is None or scheduled_laps <= 0 or not 1 <= horizon <= 20:
        raise ValueError("A timezone-aware cutoff, race distance and horizon 1–20 are required")
    cutoff = cutoff.astimezone(UTC)
    with duckdb.connect(str(warehouse), read_only=True) as con:
        con.execute("SET TimeZone = 'UTC'")
        frames = _prefix(con, session_key, cutoff)
    if len(frames["sessions"]) != 1 or frames["sessions"].iloc[0].session_name != "Race":
        raise ValueError("A unique race session is required")
    session = frames["sessions"].iloc[0].to_dict()
    controls = frames["race_control"].to_dict("records")
    starts = [row["date"] for row in controls if row["message"] == "SESSION STARTED"]
    if not starts or frames["laps"].empty:
        raise ValueError("No observed race start or lap evidence at cutoff")
    start = min(starts).to_pydatetime()
    if any(row["flag"] == "CHEQUERED" for row in controls):
        raise ValueError("Cutoff must precede the chequered flag")
    intervals = build_track_status_intervals(
        session_key, controls, start, cutoff + timedelta(microseconds=1),
    )
    track = TrackState(intervals[-1].status) if intervals else TrackState.UNKNOWN
    weather_rows = frames["weather"].sort_values("date")
    weather = weather_rows.iloc[-1].to_dict() if not weather_rows.empty else {}
    weather_at = weather["date"].to_pydatetime() if weather else None
    context = _context(frames, session_key)
    positions = _last_by_driver(frames["position"], "position")
    gaps = _last_by_driver(frames["intervals"], "gap_to_leader_raw")
    grid = frames["starting_grid"].set_index("driver_number").to_dict("index")
    pit_laps = _last_by_driver(frames["pit"], "lap_number")
    stint_starts = set(zip(frames["stints"].driver_number, frames["stints"].lap_start, strict=True))
    grouped = dict(tuple(context.groupby("driver_number")))
    drivers = []
    for driver in frames["drivers"].sort_values("driver_number").to_dict("records"):
        number = int(driver["driver_number"])
        laps = grouped.get(number)
        request = None
        completed = crossing = elapsed = None
        reason = None
        if laps is None or laps.lap_start_utc.isna().all():
            reason = "NO_OBSERVED_CROSSING"
        else:
            current = laps.dropna(subset=["lap_start_utc"]).iloc[-1]
            if same_stint_history_audit is not None and number in same_stint_history_audit:
                same_stint_history_audit[number] = _same_stint_history(laps, current, cutoff)
            lap = int(current.lap_number)
            completed = lap - 1
            crossing = current.lap_start_utc.to_pydatetime()
            elapsed = (cutoff - crossing).total_seconds()
            prior = laps[laps.lap_number < lap].observed_lap_time_s.dropna()
            if elapsed < 0 or elapsed > (float(prior.iloc[-1]) * 1.5 if len(prior) else 180):
                reason = "STALE_CROSSING"
            elif lap > scheduled_laps:
                reason = "RACE_DISTANCE_REACHED"
            elif track is not TrackState.GREEN or _known(weather.get("rainfall")):
                reason = "GREEN_DRY_SCENARIO_UNSUPPORTED"
            elif _known(current.tyre_age_at_lap_start) is None or current.compound not in DRY_COMPOUNDS:
                reason = "UNKNOWN_OR_WET_TYRES"
            elif (
                pit_laps.get(number, -1) >= lap - 1
                and (number, lap) not in stint_starts
            ):
                reason = "UNRESOLVED_CURRENT_PIT"
            else:
                request = _driver_request(
                    session, driver, grid.get(number, {}), laps, current, weather,
                    frames["allocations"], cutoff, scheduled_laps, horizon,
                )
                if clean_history_audit is not None:
                    clean_history_audit[number] = _clean_lap_evidence(laps, request)
        drivers.append(DriverSnapshot(number, completed, crossing, elapsed,
                                      positions.get(number), gaps.get(number), request, reason))
    sectors = context.loc[context.is_clean_green_pace_lap.fillna(False),
                          ["sector_1_s", "sector_2_s", "sector_3_s"]].dropna()
    sectors = sectors[(sectors > 0).all(axis=1)]
    fractions = (1 / 3, 1 / 3, 1 / 3)
    if not sectors.empty:
        pooled = sectors.div(sectors.sum(axis=1), axis=0).mean()
        fractions = tuple(float(value) for value in pooled / pooled.sum())
    return FieldSnapshot(session_key, start, cutoff, scheduled_laps, track, weather_at,
                         tuple(drivers), fractions)
