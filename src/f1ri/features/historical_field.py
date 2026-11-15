"""Historical track-local estimates with a strict event-time prefix."""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from math import hypot

from f1ri.features.physical_positions import LocalizationMode, estimate_from_trace
from f1ri.features.track_geometry import LocationFix, TrackGeometry, map_fixes
from f1ri.simulator.proximity import (
    PhysicalRoute,
    PhysicalSnapshot,
    TrackPosition,
    physical_neighborhoods,
)


class FieldMode(StrEnum):
    CAUSAL = "EVENT_TIME_CAUSAL"
    RETROSPECTIVE = "RETROSPECTIVE_TRUTH_ESTIMATE"
    RECEIPT = "RECEIPT_QUALIFIED"


@dataclass(frozen=True)
class TimedValue:
    at: datetime
    value: object
    source: str


@dataclass(frozen=True)
class Fix:
    point: LocationFix
    raw_stamp: str
    sources: tuple[str, ...]
    conflict: bool = False


class Series:
    """Binary-search access to bounded per-driver time windows."""

    def __init__(self, rows, time):
        self.rows = tuple(sorted(rows, key=time))
        self.times = tuple(time(row) for row in self.rows)

    def last(self, end):
        i = bisect_right(self.times, end)
        return self.rows[i-1] if i else None

    def window(self, start, end):
        return self.rows[bisect_left(self.times, start):bisect_right(self.times, end)]


def reconcile_fixes(rows):
    """Collapse exact overlaps and quarantine conflicting coordinates."""
    by_clock = defaultdict(list)
    for point, raw_stamp, source in rows:
        by_clock[point.at_utc].append((point, raw_stamp, source))
    conflicts = duplicates = 0
    selected = []
    for values in by_clock.values():
        point, raw_stamp, _ = min(values, key=lambda row: (row[1], row[2], row[0].x, row[0].y))
        disagree = len({(row[0].x, row[0].y) for row in values}) > 1
        conflicts += disagree
        duplicates += len(values)-1
        selected.append(Fix(point, raw_stamp, tuple(sorted({row[2] for row in values})), disagree))
    return Series(selected, lambda row: row.point.at_utc), {"duplicates": duplicates, "conflicts": conflicts}


def indexed(rows):
    return {key: Series(values, lambda row: row.at) for key, values in rows.items()}


@dataclass(frozen=True)
class Session:
    key: int
    race: str
    roster: tuple[int, ...]
    start: datetime
    end: datetime
    geometry: TrackGeometry | None
    layout_source: str
    fixes: dict[int, Series]
    laps: dict[int, Series]
    classification: dict[int, Series]
    pit: dict[int, Series]
    control: Series
    source_sha256: str
    release_events: tuple[dict, ...] = ()


def _latest(index, driver, at):
    return index[driver].last(at) if driver in index else None


def _projection(session, driver, at, mode):
    series = session.fixes.get(driver)
    last = series.last(at) if series else None
    age = (at-last.point.at_utc).total_seconds() if last else None
    diagnostics = {"location_utc": last.point.at_utc if last else None, "location_original": last.raw_stamp
                   if last else None, "location_sources": last.sources if last else (), "age_s": age,
                   "maximum_fix_used_utc": None, "bracket_s": None, "projection_error_m": None,
                   "mapped_candidate_m": None, "mapped_distance_from_last_fix_m": None}
    geometry = session.geometry
    if geometry is None or (mode is FieldMode.CAUSAL and geometry.available_at_utc > at):
        return None, diagnostics, "CUTOFF_GEOMETRY_UNAVAILABLE"
    if last is None or age > .9:
        return None, diagnostics, "STALE_LOCATION" if last else "NO_POSITION_EVIDENCE"
    if last.conflict:
        return None, diagnostics, "CONFLICTING_SOURCE_REVISION"
    end = at if mode is FieldMode.CAUSAL else at+timedelta(seconds=1)
    records = series.window(at-timedelta(seconds=4), end)
    prior_conflict = max((r.point.at_utc for r in records if r.conflict and r.point.at_utc < at), default=None)
    next_conflict = min((r.point.at_utc for r in records if r.conflict and r.point.at_utc > at), default=None)
    trace = map_fixes(geometry, (r.point for r in records if not r.conflict and
                                (prior_conflict is None or r.point.at_utc > prior_conflict) and
                                (next_conflict is None or r.point.at_utc < next_conflict)))
    localization = (LocalizationMode.POINT_IN_TIME if mode is FieldMode.CAUSAL else LocalizationMode.RETROSPECTIVE)
    estimate = estimate_from_trace(geometry, trace, at, localization)
    mapped = next(r for r in trace if r.fix.at_utc == last.point.at_utc)
    diagnostics["projection_error_m"] = mapped.projection_error_m
    nxt = next((r for r in trace if r.fix.at_utc > at), None)
    diagnostics["maximum_fix_used_utc"] = (nxt.fix.at_utc if nxt and estimate.progress_m is not None and
                                          estimate.quality.value == "INTERPOLATED" else last.point.at_utc)
    if diagnostics["maximum_fix_used_utc"] > at:
        diagnostics["bracket_s"] = (nxt.fix.at_utc-last.point.at_utc).total_seconds()
    if estimate.progress_m is not None:
        diagnostics["mapped_candidate_m"] = estimate.progress_m
        diagnostics["mapped_distance_from_last_fix_m"] = (
            (estimate.progress_m-mapped.progress_m+geometry.length_m/2) % geometry.length_m-geometry.length_m/2
        )
        recent = [r.fix for r in trace if at-timedelta(seconds=3) <= r.fix.at_utc <= at]
        diagnostics["stationary_trace"] = (len(recent) >= 5 and
            (recent[-1].at_utc-recent[0].at_utc).total_seconds() >= 2 and
            max(hypot(f.x-last.point.x, f.y-last.point.y) for f in recent) <= 10)
    return estimate, diagnostics, estimate.reason


def _position(session, driver, at, mode):
    lap = _latest(session.laps, driver, at)
    rank = _latest(session.classification, driver, at)
    pit = _latest(session.pit, driver, at)
    estimate, details, reason = _projection(session, driver, at, mode)
    completed = lap.value if lap else None
    route = PhysicalRoute.UNKNOWN
    align = False
    pit_recent = pit and (at-pit.at).total_seconds() <= 45
    if pit_recent and pit.value == "LANE_ENTRY_TIMING":
        reason = "PIT_ROUTE_PARTIAL_LANE_TIMING"
    elif details.get("stationary_trace"):
        reason = "STATIONARY_ROUTE_UNVERIFIED"
    elif estimate and estimate.progress_m is not None:
        length = session.geometry.length_m
        local = estimate.progress_m
        elapsed = (at-lap.at).total_seconds() if lap else None
        bounds = 1.5*estimate.uncertainty_m
        # The start/finish corridor may also contain the separate pit branch.
        # Retain the matched coordinate for audit, while excluding it from racing order.
        if min(local, length-local) <= 650+bounds:
            reason = "PIT_BRANCH_UNRESOLVED_NEAR_CONTROL_LINE"
        else:
            route = PhysicalRoute.RACING
            align = elapsed is not None and elapsed <= 180 and local <= 105*elapsed+bounds+35
    classification = rank.value if rank else None
    if route is PhysicalRoute.RACING:
        pos = TrackPosition(driver, route, completed, classification, estimate.progress_m, bounds,
                            estimate.speed_mps, estimate.quality, estimate.latest_observation_utc, align)
    else:
        pos = TrackPosition(driver, route, completed, classification, observed_at_utc=details["location_utc"],
                            lap_aligned=False, reason=reason)
    return pos, details | {
        "lap_anchor_utc": lap.at if lap else None, "lap_source": lap.source if lap else None,
        "classification_utc": rank.at if rank else None, "classification_source": rank.source if rank else None,
        "pit_timing_utc": pit.at if pit else None, "pit_timing_source": pit.source if pit else None,
        "pit_timing_kind": pit.value if pit else None,
        "route_evidence": ("MAIN_LOOP_COMPATIBLE_OUTSIDE_PIT_CORRIDOR" if route is PhysicalRoute.RACING else reason),
    }


def reconstruct_field(session, cutoff, mode=FieldMode.CAUSAL, drivers=None):
    """Report a bounded field without claiming receipt time or complete race coverage."""
    mode = FieldMode(mode)
    if cutoff.utcoffset() is None or not session.start <= cutoff <= session.end:
        raise ValueError("Cutoff must fall within this pilot session and include a UTC offset.")
    roster = tuple(sorted(session.roster))
    requested = set(roster if drivers is None else drivers)
    if not requested or not requested <= set(roster):
        raise ValueError("Request at least one driver from the pilot roster.")
    header = {"session": session.key, "race": session.race, "cutoff": cutoff, "mode": mode,
              "source_sha256": session.source_sha256, "source_availability": "EVENT_TIME_ONLY",
              "layout": session.geometry.version if session.geometry else None,
              "length_m": session.geometry.length_m if session.geometry else None,
              "strategy_support": "HISTORICAL_STRATEGY_UNSUPPORTED", "inventory": None, "receipt_utc": None,
              "field_roster": roster}
    if mode is FieldMode.RECEIPT:
        return header | {"support": "RECEIPT_ORDER_UNAVAILABLE", "drivers": [], "track_status": None,
                         "supported_positions": 0, "supported_predecessors": 0}
    cars, diagnostics = zip(*(_position(session, driver, cutoff, mode) for driver in roster), strict=True)
    geometry = session.geometry
    snapshot = PhysicalSnapshot(session.key, cutoff, geometry.version, geometry.length_m, cars) if geometry else None
    neighborhoods = physical_neighborhoods(snapshot) if snapshot else {}
    visible = []
    for car, diag in zip(cars, diagnostics, strict=True):
        if car.driver_number not in requested:
            continue
        near = neighborhoods.get(car.driver_number)
        ahead = near.ahead if near else None
        behind = near.behind if near else None
        full = near and near.coverage == "COMPLETE"
        row = asdict(car) | diag | {
            "unwrapped_m": car.unwrapped_m(geometry.length_m) if geometry else None,
            "provenance": "DERIVED_FROM_OBSERVED" if car.progress_m is not None else "UNAVAILABLE",
            "alignment": ("RETROSPECTIVE_INTERPOLATED" if car.quality.value == "INTERPOLATED" else
                          "CAUSAL_EXTRAPOLATED" if car.quality.value == "EXTRAPOLATED" else car.quality.value),
            "predecessor": ahead.driver_number if full and ahead else None,
            "successor": behind.driver_number if full and behind else None,
            "subset_ahead": asdict(ahead) if ahead else None, "subset_behind": asdict(behind) if behind else None,
            "ahead_status": ("PREDECESSOR_SUPPORTED" if full and ahead else
                             "NEAREST_WITHIN_SUPPORTED_SUBSET" if ahead else near.ahead_reason if near else "UNAVAILABLE"),
            "behind_status": ("SUCCESSOR_SUPPORTED" if full and behind else
                              "NEAREST_WITHIN_SUPPORTED_SUBSET" if behind else near.behind_reason if near else "UNAVAILABLE"),
            "gap_semantics": "GAP_DISTANCE",
        }
        visible.append(row)
    sources = session.control.last(cutoff)
    count = sum(c.progress_m is not None for c in cars)
    support = ("NO_POSITION_EVIDENCE" if not count else "FIELD_ORDER_SUPPORTED" if snapshot.complete and
               all(n.ahead and n.behind for n in neighborhoods.values()) else "PARTIAL_FIELD_SUPPORTED")
    return header | {"support": support, "drivers": visible, "supported_positions": count,
                     "supported_predecessors": sum(n.ahead is not None and n.coverage == "COMPLETE"
                                                   for n in neighborhoods.values()),
                     "unsupported_relationships": sum(row["predecessor"] is None for row in visible),
                     "track_status": asdict(sources) if sources else None}


def with_sparsity(session, every):
    """Retain a deterministic fraction of observations without modifying source evidence."""
    return replace(session, fixes={driver: Series(series.rows[::every], lambda row: row.point.at_utc)
                                   for driver, series in session.fixes.items()})
