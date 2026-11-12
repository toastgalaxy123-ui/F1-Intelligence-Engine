"""Build hypothetical opportunities exclusively from observations available at cutoff."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import timedelta

import pandas as pd

from f1ri.features.lap_context import build_track_status_intervals
from f1ri.features.pit_events import integer, number, utc
from f1ri.ingestion.pit_lane_reference import pit_reference_as_of
from f1ri.models.contracts import PitLossRequest, PitStatusSegment


def request_to_json(request: PitLossRequest) -> str:
    return json.dumps(asdict(request), default=lambda x: x.isoformat(), sort_keys=True)


def request_from_json(payload: str) -> PitLossRequest:
    fields = json.loads(payload)
    fields["as_of_utc"] = utc(fields["as_of_utc"]).to_pydatetime()
    if fields.get("service_observed_at_utc"):
        fields["service_observed_at_utc"] = utc(fields["service_observed_at_utc"]).to_pydatetime()
    fields["future_status_scenario"] = tuple(PitStatusSegment(**s) for s in fields["future_status_scenario"])
    fields["race_control_constraints"] = tuple(fields.get("race_control_constraints", ()))
    return PitLossRequest(**fields)


def _latest(frame: pd.DataFrame, as_of, driver: int | None = None) -> dict:
    if frame.empty or "date" not in frame:
        return {}
    dates = pd.to_datetime(frame.date, utc=True, errors="coerce")
    selected = frame[dates <= as_of]
    if driver is not None and "driver_number" in frame:
        selected = selected[selected.driver_number == driver]
    return selected.sort_values("date").iloc[-1].to_dict() if not selected.empty else {}


def build_pit_opportunity(
    *, session: dict, driver: dict, laps: pd.DataFrame, stints: pd.DataFrame,
    race_control: pd.DataFrame, weather: pd.DataFrame, as_of_utc,
    intended_pit_lap: int, new_compound: str, new_tyre_initial_age: int = 0,
    completed_events: pd.DataFrame | None = None, position: pd.DataFrame | None = None,
    intervals: pd.DataFrame | None = None, reference: pd.DataFrame | None = None,
    **scenario,
) -> tuple[PitLossRequest, dict]:
    """No actual future pit, tire choice, gap, or flag timeline is required.

    Use raw laps here, not final-race lap_context flags whose construction may
    depend on the next lap. Event-derived facts belong in targets, never predictors.
    """
    cutoff = utc(as_of_utc)
    if cutoff is None:
        raise ValueError("Missing forecast cutoff")
    dnum = int(driver["driver_number"])
    known = laps[laps.driver_number == dnum].copy()
    starts = pd.to_datetime(known.date_start, utc=True, errors="coerce")
    times = pd.to_numeric(known.lap_duration, errors="coerce")
    ends = starts + pd.to_timedelta(times, unit="s")
    known = known[(ends <= cutoff) & (times > 0)].sort_values("lap_number")
    if known.empty:
        raise ValueError("No completed lap is observable at cutoff")
    last = known.iloc[-1]
    last_lap = int(last.lap_number)
    # Do not use an eventual stint end: only the latest already-started tire set.
    sets = stints[(stints.driver_number == dnum) & (stints.lap_start <= last_lap)]
    if sets.empty:
        raise ValueError("Current tire state is unavailable")
    current = sets.sort_values("lap_start").iloc[-1]
    old_age = integer(current.get("tyre_age_at_start"))
    if old_age is None:
        raise ValueError("Current tire age is unavailable")
    old_age += intended_pit_lap - int(current.lap_start) + 1
    rc = race_control.copy()
    if not rc.empty:
        rc = rc[pd.to_datetime(rc.date, utc=True) <= cutoff].sort_values("date")
    records = rc.to_dict("records")
    for row in records:
        row["date"] = utc(row["date"]).to_pydatetime()
    # Ending immediately after cutoff lets the existing status machine return
    # the state then, without observing any later GREEN/VSC/SC messages.
    start = utc(session.get("date_start"))
    timeline = build_track_status_intervals(
        int(session["session_key"]), records, start.to_pydatetime(),
        cutoff.to_pydatetime() + timedelta(microseconds=1),
    ) if start is not None else []
    status = timeline[-1].status if timeline else "UNKNOWN"
    if status not in {"GREEN", "VSC", "SC", "RED"}:
        status = "UNKNOWN"
    w = _latest(weather, cutoff)
    p = _latest(position if position is not None else pd.DataFrame(), cutoff, dnum)
    g = _latest(intervals if intervals is not None else pd.DataFrame(), cutoff, dnum)
    ref = pit_reference_as_of(
        reference if reference is not None else pd.DataFrame(),
        str(session.get("circuit_short_name", "UNKNOWN")), int(session["year"]), cutoff,
    )
    known_stop_count = 0
    if completed_events is not None and not completed_events.empty:
        available = pd.to_datetime(completed_events.transition_end_utc, utc=True)
        known_stop_count = int((available <= cutoff).sum())
    # Only explicit race-control instructions establish open/closed, never infer.
    pit_open = None
    for row in records:
        msg = str(row.get("message", "")).upper()
        if "PIT LANE CLOSED" in msg or "PIT ENTRY CLOSED" in msg:
            pit_open = False
        if "PIT LANE OPEN" in msg or "PIT ENTRY OPEN" in msg:
            pit_open = True
    request = PitLossRequest(
        session_key=int(session["session_key"]), driver_number=dnum,
        circuit_short_name=str(session.get("circuit_short_name", "UNKNOWN")),
        season=int(session["year"]), team_id=str(driver.get("team_name", "UNKNOWN_TEAM")),
        circuit_key=integer(session.get("circuit_key")), as_of_utc=cutoff.to_pydatetime(),
        latest_completed_lap=last_lap, intended_pit_lap=intended_pit_lap,
        old_compound=str(current.compound), old_tyre_age=old_age,
        new_compound=new_compound, new_tyre_initial_age=new_tyre_initial_age,
        track_status=scenario.pop("track_status", status),
        layout_version=str(ref["layout_version"]) if ref.get("layout_version") else None,
        pit_speed_limit_kph=number(ref.get("pit_speed_limit_kph")),
        track_temperature_c=number(w.get("track_temperature")),
        rainfall=bool(w["rainfall"]) if pd.notna(w.get("rainfall")) else None,
        pit_lane_open=pit_open, **scenario,
    )
    context = {
        "opportunity_id": f"{request.session_key}:{dnum}:{last_lap}:{intended_pit_lap}:{new_compound}:{new_tyre_initial_age}",
        "pit_event_id": None, "session_key": request.session_key, "driver_number": dnum,
        "forecast_as_of_utc": cutoff, "latest_completed_lap": last_lap,
        "intended_pit_lap": intended_pit_lap, "request_json": request_to_json(request),
        "track_status_at_cutoff": status, "n_completed_stops_known": known_stop_count,
        "last_observed_position": integer(p.get("position")),
        "last_observed_gap_raw": g.get("gap_to_leader_raw", g.get("gap_to_leader")),
        "last_weather_observed_utc": utc(w.get("date")),
    }
    return request, context
