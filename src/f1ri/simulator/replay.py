"""Retrospective reconstruction from the typed historical warehouse."""
from __future__ import annotations

import csv
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from f1ri.features.pit_events import load_pit_annotations, reconstruct_pit_events
from f1ri.simulator.engine import RaceRun, run_timeline
from f1ri.simulator.events import Event, EventKind
from f1ri.simulator.state import (
    CarState,
    CarStatus,
    RacePhase,
    RaceRuleset,
    RaceState,
    Route,
    SimulationMode,
    TrackState,
    TyreState,
    elapsed_us,
)

_BOUNDARY_TOLERANCE_US = 1_000_000


@dataclass(frozen=True, slots=True)
class ReplayIssue:
    code: str
    driver_number: int | None = None
    lap_number: int | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DriverAudit:
    driver_number: int
    completed_laps: int
    official_laps: int | None
    timing_position: int | None
    classified_position: int | None
    official_position: int | None
    reported_position: int | None
    status: CarStatus


@dataclass(frozen=True, slots=True)
class TimePenalty:
    driver_number: int
    added_us: int
    source_url: str


@dataclass(frozen=True, slots=True)
class ReplayResult:
    run: RaceRun
    timing_tier: str
    drivers: tuple[DriverAudit, ...]
    issues: tuple[ReplayIssue, ...]
    compared_boundaries: int
    max_boundary_disagreement_s: float | None
    max_sector_disagreement_s: float | None
    recorded_pit_visits: int
    replayed_pit_visits: int
    penalties: tuple[TimePenalty, ...]


def _offset_us(start: datetime, at: datetime) -> int:
    delta = at - start
    return delta // timedelta(microseconds=1)


def _duration_us(seconds: float | None) -> int | None:
    if seconds is None or not math.isfinite(seconds) or seconds <= 0:
        return None
    return round(seconds * 1_000_000) or None


def _rows(con: duckdb.DuckDBPyConnection, table: str, session_key: int) -> list[dict]:
    cursor = con.execute(f"SELECT * FROM {table} WHERE session_key = ?", [session_key])
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _crossings(
    driver: int, laps: list[dict], start: datetime, scheduled_laps: int, issues: list[ReplayIssue],
) -> tuple[list[Event], list[int], list[int]]:
    events: list[Event] = []
    disagreements = []
    sector_disagreements = []
    laps = sorted(laps, key=lambda row: (row["lap_number"] is None, row["lap_number"] or 0))
    previous = 0
    for i, lap in enumerate(laps):
        lap_number = lap["lap_number"]
        lap_start = lap["date_start"]
        duration = _duration_us(lap["lap_duration"])
        if lap_number != i + 1 or lap_number > scheduled_laps or lap_start is None:
            issues.append(ReplayIssue("MISSING_OR_UNEXPECTED_LAP_BOUNDARY", driver, lap_number))
            break
        begin = _offset_us(start, lap_start)
        if duration is None and (i != len(laps) - 1 or lap["lap_duration"] is not None):
            issues.append(ReplayIssue("MISSING_OR_INVALID_LAP_DURATION", driver, lap_number))
            break
        end = begin + duration if duration is not None else None
        if begin < 0 or (end is not None and end <= previous):
            issues.append(ReplayIssue("NONMONOTONIC_LAP_CROSSING", driver, lap_number))
            break
        if end is not None and i + 1 < len(laps):
            following = laps[i + 1]
            if following["lap_number"] == lap_number + 1 and following["date_start"] is not None:
                gap = _offset_us(start, following["date_start"]) - end
                disagreements.append(abs(gap))
                if abs(gap) > _BOUNDARY_TOLERANCE_US:
                    issues.append(ReplayIssue("LAP_BOUNDARY_DISAGREEMENT", driver, lap_number,
                                              f"{gap / 1_000_000:+.3f} s"))
        sector_durations = [
            _duration_us(lap[f"duration_sector_{sector}"]) for sector in (1, 2, 3)
        ]
        if duration is not None and all(value is not None for value in sector_durations):
            sector_gap = abs(sum(sector_durations) - duration)
            sector_disagreements.append(sector_gap)
            if sector_gap > _BOUNDARY_TOLERANCE_US:
                issues.append(ReplayIssue("SECTOR_SUM_DISAGREEMENT", driver, lap_number,
                                          f"{sector_gap / 1_000_000:.3f} s"))
                sector_durations = []
        at = begin
        for sector, sector_duration in enumerate(sector_durations[:2], start=1):
            if sector_duration is None:
                break
            at += sector_duration
            if at <= previous or (end is not None and at >= end):
                issues.append(ReplayIssue("UNUSABLE_SECTOR_CROSSING", driver, lap_number))
                break
            events.append(Event(at, EventKind.CHECKPOINT_CROSSING, driver,
                                (lap_number - 1) * 3 + sector, source="LAP_SECTOR_ESTIMATE"))
            previous = at
        if end is not None:
            events.append(Event(end, EventKind.LAP_COMPLETED, driver, lap_number * 3,
                                source="LAP_START_PLUS_DURATION"))
            previous = end
    return events, disagreements, sector_disagreements


def _pit_events(
    con: duckdb.DuckDBPyConnection,
    session_key: int,
    annotations_root: Path | None,
    start: datetime,
    participation_end: dict[int, int],
    issues: list[ReplayIssue],
) -> tuple[list[Event], int]:
    frames = {
        name: con.execute(f"SELECT * FROM silver.{name} WHERE session_key = ?", [session_key]).df()
        for name in ("pit", "laps", "stints", "sessions", "drivers", "race_control")
    }
    intervals = con.execute(
        "SELECT * FROM gold.track_status_intervals WHERE session_key = ?", [session_key],
    ).df()
    annotations = load_pit_annotations(annotations_root) if annotations_root is not None else None
    visits = reconstruct_pit_events(
        frames["pit"], frames["laps"], frames["stints"], frames["sessions"].iloc[0].to_dict(),
        intervals, frames["drivers"], annotations, frames["race_control"],
    )
    events: list[Event] = []
    last_exit: dict[int, int] = {}
    last_stint: dict[int, int] = {}
    for visit in visits.itertuples(index=False):
        driver = int(visit.driver_number)
        entry = visit.estimated_lane_entry_utc
        exit_ = visit.estimated_lane_exit_utc
        if pd.isna(entry) or pd.isna(exit_):
            issues.append(ReplayIssue("UNUSABLE_PIT_WINDOW", driver, detail=visit.pit_event_id))
            continue
        begin, end = _offset_us(start, entry), _offset_us(start, exit_)
        if (
            begin < 0 or begin <= last_exit.get(driver, -1) or begin >= end
            or end >= participation_end.get(driver, 0)
        ):
            issues.append(ReplayIssue("UNUSABLE_PIT_WINDOW", driver, detail=visit.pit_event_id))
            continue
        tyres = None
        if isinstance(visit.new_stint_id, str) and isinstance(visit.new_compound, str):
            age = visit.new_tyre_initial_age
            stint = int(visit.new_stint_id.rsplit(":", 1)[1])
            if stint <= last_stint.get(driver, 1):
                issues.append(ReplayIssue("NONMONOTONIC_PIT_STINT", driver,
                                          detail=visit.pit_event_id))
            else:
                tyres = TyreState(
                    visit.new_compound,
                    int(age) if age is not None and math.isfinite(age) and age >= 0 else None,
                    stint,
                )
                last_stint[driver] = stint
        if visit.timing_quality == "C":
            issues.append(ReplayIssue("PIT_EVIDENCE_CONFLICT", driver, detail=(
                f"{visit.pit_event_id} {visit.quality_flags_json}"
            )))
        events.extend((
            Event(begin, EventKind.PIT_ENTRY, driver, source=visit.lane_boundary_method),
            Event(end, EventKind.PIT_EXIT, driver, tyres=tyres, source=visit.lane_boundary_method),
        ))
        last_exit[driver] = end
    return events, len(visits)


def replay_session(
    warehouse: Path, session_key: int, scheduled_laps: int, *, annotations_root: Path | None = None,
) -> ReplayResult:
    """Replay a completed race and compare the reconstructed state to recorded results."""
    with duckdb.connect(str(warehouse), read_only=True) as con:
        con.execute("SET TimeZone = 'UTC'")
        return _replay(con, session_key, scheduled_laps, annotations_root)


def _replay(
    con: duckdb.DuckDBPyConnection, session_key: int, scheduled_laps: int,
    annotations_root: Path | None,
) -> ReplayResult:
    sessions = _rows(con, "silver.sessions", session_key)
    if len(sessions) != 1 or sessions[0]["session_name"] != "Race":
        raise ValueError(f"Unsupported replay: missing unique Race session {session_key}")
    ruleset = RaceRuleset(sessions[0]["year"], scheduled_laps)
    controls = _rows(con, "silver.race_control", session_key)
    starts = [
        row["date"] for row in controls if row["date"] is not None
        and row["category"] == "SessionStatus" and (row["message"] or "") == "SESSION STARTED"
    ]
    if not starts:
        raise ValueError("Unsupported replay: no observed race start")
    start = min(starts).astimezone(UTC)
    grid = {row["driver_number"]: row["position"]
            for row in _rows(con, "silver.starting_grid", session_key)}
    results = {row["driver_number"]: row
               for row in _rows(con, "silver.session_result", session_key)}
    stints = {
        row["driver_number"]: row for row in _rows(con, "silver.stints", session_key)
        if row["lap_start"] == 1 and row["stint_number"] == 1
    }
    cars = []
    issues: list[ReplayIssue] = []
    for row in _rows(con, "silver.drivers", session_key):
        driver = row["driver_number"]
        stint = stints.get(driver, {})
        dns = bool(results.get(driver, {}).get("dns"))
        if driver not in grid or grid[driver] is None:
            issues.append(ReplayIssue("MISSING_GRID_POSITION", driver))
        if not stint and not dns:
            issues.append(ReplayIssue("MISSING_STARTING_TYRES", driver))
        cars.append(CarState(
            driver, grid.get(driver), TyreState(stint.get("compound") or "UNKNOWN",
                                                stint.get("tyre_age_at_start")),
            team=row["team_name"], status=CarStatus.DNS if dns else CarStatus.ACTIVE,
            route=Route.OFF_TRACK if dns else Route.ON_TRACK,
        ))
    if not cars:
        raise ValueError("Unsupported replay: missing field")
    initial = RaceState(session_key, start, ruleset, SimulationMode.HISTORICAL_RECONSTRUCTION,
                        f"replay-{session_key}", tuple(sorted(cars, key=lambda car: car.grid_key)))
    laps: dict[int, list[dict]] = defaultdict(list)
    for row in _rows(con, "silver.laps", session_key):
        laps[row["driver_number"]].append(row)
    crossing_events = {}
    boundary_errors: list[int] = []
    sector_errors: list[int] = []
    for car in initial.cars:
        if car.status is CarStatus.DNS:
            if laps.get(car.driver_number):
                issues.append(ReplayIssue("DNS_WITH_LAP_RECORDS", car.driver_number))
            continue
        driver = car.driver_number
        crossings, boundaries, sectors = _crossings(
            driver, laps.get(driver, []), start, scheduled_laps, issues,
        )
        crossing_events[driver] = crossings
        boundary_errors.extend(boundaries)
        sector_errors.extend(sectors)
    full_distance = [
        event.at_us for crossings in crossing_events.values() for event in crossings
        if event.checkpoint == scheduled_laps * 3
    ]
    if not any(crossing_events.values()):
        raise ValueError("Unsupported replay: no usable crossing sequence")
    flag_us = min(full_distance, default=None)
    if flag_us is None:
        issues.append(ReplayIssue("NO_SCHEDULED_DISTANCE_FINISH"))
    recorded_flags = [row["date"] for row in controls
                      if row["flag"] == "CHEQUERED" and row["date"] is not None]
    if not recorded_flags:
        issues.append(ReplayIssue("MISSING_CHEQUERED_MESSAGE"))
    elif flag_us is not None:
        delta = flag_us - _offset_us(start, min(recorded_flags))
        if abs(delta) > _BOUNDARY_TOLERANCE_US:
            issues.append(ReplayIssue("CHEQUERED_TIMING_DISAGREEMENT",
                                      detail=f"{delta / 1_000_000:+.3f} s"))

    events = [Event(0, EventKind.RACE_START, source="RACE_CONTROL_START")]
    participation_end = {}
    for driver, crossings in crossing_events.items():
        finish = next((index for index, event in enumerate(crossings)
                       if flag_us is not None and event.kind is EventKind.LAP_COMPLETED
                       and event.at_us >= flag_us), None)
        if finish is not None and finish < len(crossings) - 1:
            issues.append(ReplayIssue("CROSSINGS_AFTER_FINISH", driver))
            crossings = crossings[:finish + 1]
        events.extend(crossings)
        last_us = crossings[-1].at_us if crossings else 0
        if results.get(driver, {}).get("dnf") and finish is None:
            events.append(Event(last_us + 1, EventKind.RETIREMENT, driver,
                                source="DNF_LAST_OBSERVED_PROGRESS_LOWER_BOUND"))
            participation_end[driver] = last_us + 1
        else:
            participation_end[driver] = last_us

    end = max(participation_end.values())
    intervals = _rows(con, "gold.track_status_intervals", session_key)
    for row in sorted(intervals, key=lambda item: item["start_utc"]):
        at = max(0, _offset_us(start, row["start_utc"]))
        if row["end_utc"] > start and at <= end:
            events.append(Event(at, EventKind.TRACK_STATE_CHANGE,
                                track_state=TrackState(row["status"]), source=row["source_rule"]))
    if not intervals:
        issues.append(ReplayIssue("MISSING_TRACK_STATUS"))
    pit_events, visits = _pit_events(con, session_key, annotations_root, start,
                                    participation_end, issues)
    run = run_timeline(initial, events + pit_events)
    if run.final_state.phase is not RacePhase.FINISHED:
        issues.append(ReplayIssue("INCOMPLETE_RACE_STATE"))
    positions = _rows(con, "silver.position", session_key)
    penalties = _load_penalties(annotations_root, session_key)
    audits = _audit_results(run.final_state, start, results, positions, penalties, issues)
    return ReplayResult(
        run, "C" if issues else "B", audits, tuple(issues), len(boundary_errors),
        max(boundary_errors) / 1_000_000 if boundary_errors else None,
        max(sector_errors) / 1_000_000 if sector_errors else None,
        visits, sum(frame.event.kind is EventKind.PIT_EXIT for frame in run.trace), penalties,
    )


def _load_penalties(root: Path | None, session_key: int) -> tuple[TimePenalty, ...]:
    if root is None:
        return ()
    path = root / "data/raw/reference/race_time_penalties.csv"
    if not path.is_file():
        return ()
    penalties = {}
    with path.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            if int(row["session_key"]) != session_key:
                continue
            driver = int(row["driver_number"])
            added = elapsed_us(float(row["added_seconds"]))
            url = row["source_url"].strip()
            if driver in penalties or driver <= 0 or added == 0 or not url.startswith("https://"):
                raise ValueError("Time penalties require a unique driver, positive time and source")
            penalties[driver] = TimePenalty(driver, added, url)
    return tuple(penalties[driver] for driver in sorted(penalties))


def _audit_results(
    state: RaceState, start: datetime, official: dict[int, dict], positions: list[dict],
    penalties: tuple[TimePenalty, ...], issues: list[ReplayIssue],
) -> tuple[DriverAudit, ...]:
    latest: dict[int, int] = {}
    latest_time: dict[int, datetime] = {}
    for row in sorted((row for row in positions if row["date"] is not None),
                      key=lambda row: (row["date"], row["position"] or 0)):
        if row["position"] is not None and 0 <= _offset_us(start, row["date"]) <= state.race_clock_us:
            driver = row["driver_number"]
            if latest_time.get(driver) == row["date"] and latest[driver] != row["position"]:
                issues.append(ReplayIssue("CONFLICTING_REPORTED_POSITIONS", driver,
                                          detail=row["date"].isoformat()))
            latest[driver] = row["position"]
            latest_time[driver] = row["date"]
    cars = {car.driver_number: car for car in state.cars}
    finishers = [driver for driver in state.timing_order
                 if cars[driver].status is CarStatus.FINISHED]
    timing = {driver: position for position, driver in enumerate(finishers, start=1)}
    added = {penalty.driver_number: penalty.added_us for penalty in penalties}
    for driver in sorted(added.keys() - timing.keys()):
        issues.append(ReplayIssue("PENALTY_WITHOUT_FINISH_TIME", driver))
    classified = sorted(finishers, key=lambda driver: (
        -cars[driver].completed_laps, cars[driver].finish_us + added.get(driver, 0),
    ))
    classification = {driver: position for position, driver in enumerate(classified, start=1)}
    audits = []
    for driver in state.timing_order:
        car = cars[driver]
        result = official.get(driver, {})
        observed_laps, observed_position = result.get("number_of_laps"), result.get("position")
        if not result:
            issues.append(ReplayIssue("MISSING_OFFICIAL_RESULT", driver))
        else:
            if observed_laps != car.completed_laps:
                issues.append(ReplayIssue("FINAL_LAP_COUNT_MISMATCH", driver))
            if observed_position != classification.get(driver):
                issues.append(ReplayIssue("FINAL_CLASSIFICATION_MISMATCH", driver))
            if car.status is CarStatus.FINISHED and driver not in latest:
                issues.append(ReplayIssue("MISSING_REPORTED_POSITION", driver))
            elif driver in timing and latest[driver] != timing[driver]:
                issues.append(ReplayIssue("FINAL_REPORTED_TIMING_MISMATCH", driver))
            expected = CarStatus.DNS if result["dns"] else (
                CarStatus.RETIRED if result["dnf"] else CarStatus.FINISHED
            )
            if expected != car.status:
                issues.append(ReplayIssue("FINAL_STATUS_MISMATCH", driver))
            if result["dsq"]:
                issues.append(ReplayIssue("OFFICIAL_DISQUALIFICATION", driver))
        audits.append(DriverAudit(
            driver, car.completed_laps, observed_laps, timing.get(driver),
            classification.get(driver), observed_position, latest.get(driver), car.status,
        ))
    for driver in sorted(official.keys() - cars.keys()):
        issues.append(ReplayIssue("OFFICIAL_DRIVER_MISSING_FROM_FIELD", driver))
    return tuple(audits)


def replay_summary(result: ReplayResult) -> dict:
    """Compact serialization for the CLI, with full classification audit and issue list."""
    state = result.run.final_state
    return {
        "session_key": state.session_key,
        "simulation_id": state.simulation_id,
        "mode": state.mode,
        "capability": result.run.capability,
        "ruleset": state.ruleset.version,
        "race_start_utc": state.race_start_utc.isoformat(),
        "scheduled_laps": state.ruleset.scheduled_laps,
        "timing_tier": result.timing_tier,
        "race_phase": state.phase,
        "race_clock_s": state.race_clock_s,
        "chequered_s": state.chequered_us / 1_000_000 if state.chequered_us is not None else None,
        "event_counts": dict(Counter(frame.event.kind.name for frame in result.run.trace)),
        "track_states": [frame.state.track_state for frame in result.run.trace
                         if frame.event.kind is EventKind.TRACK_STATE_CHANGE],
        "compared_boundaries": result.compared_boundaries,
        "max_boundary_disagreement_s": result.max_boundary_disagreement_s,
        "max_sector_disagreement_s": result.max_sector_disagreement_s,
        "recorded_pit_visits": result.recorded_pit_visits,
        "replayed_pit_visits": result.replayed_pit_visits,
        "drivers": [asdict(driver) for driver in result.drivers],
        "penalties": [asdict(penalty) for penalty in result.penalties],
        "issues": [asdict(issue) for issue in result.issues],
    }
