"""Evidence-qualified branch matching and circular-order certificates."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from math import hypot, isfinite

import numpy as np

from f1ri.features.historical_field import FieldMode
from f1ri.features.track_geometry import TrackGeometry, signed_loop_delta


class Route(StrEnum):
    MAIN = "RACING_LOOP"
    APPROACH = "PIT_ENTRY_APPROACH"
    LANE = "PIT_LANE"
    SERVICE = "SERVICE_REGION"
    EXIT = "PIT_EXIT_ROUTE"
    MERGE = "RACING_MERGE"
    UNKNOWN = "UNKNOWN_ROUTE"


@dataclass(frozen=True)
class LaneCurve:
    """Open polyline expressed in the same session Cartesian frame as the loop."""

    layout: str
    points: tuple[tuple[float, float], ...]
    metres_per_unit: float
    _starts: np.ndarray = field(init=False, repr=False, compare=False)
    _segments: np.ndarray = field(init=False, repr=False, compare=False)
    _steps: np.ndarray = field(init=False, repr=False, compare=False)
    _lengths: np.ndarray = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        xy = np.asarray(self.points, dtype=float)
        if xy.ndim != 2 or xy.shape[1] != 2 or len(xy) < 4 or not np.isfinite(xy).all():
            raise ValueError("A pit reference requires a finite open curve.")
        segments = np.diff(xy, axis=0)
        lengths = np.linalg.norm(segments, axis=1)
        if min(lengths) <= 0 or not isfinite(self.metres_per_unit) or self.metres_per_unit <= 0:
            raise ValueError("A pit reference requires nonzero segments and a finite positive scale.")
        object.__setattr__(self, "_starts", xy[:-1])
        object.__setattr__(self, "_segments", segments)
        object.__setattr__(self, "_steps", lengths)
        object.__setattr__(self, "_lengths", np.concatenate(([0.], np.cumsum(lengths)))*self.metres_per_unit)

    @property
    def length_m(self):
        return float(self._lengths[-1])

    def project(self, x, y):
        delta = np.array([x, y])-self._starts
        fraction = np.clip(np.sum(delta*self._segments, axis=1)/self._steps**2, 0., 1.)
        residual = np.linalg.norm(delta-self._segments*fraction[:, None], axis=1)*self.metres_per_unit
        idx = int(np.argmin(residual))
        along = self._lengths[idx] + fraction[idx]*self._steps[idx]*self.metres_per_unit
        return float(along), float(residual[idx])


@dataclass(frozen=True)
class RouteGraph:
    session: int
    loop: TrackGeometry
    lane: LaneCurve
    fit_id: str
    fit_through: datetime
    fit_sources: tuple[str, ...]
    core_start_m: float
    core_span_m: float
    policy: dict
    verified_through: datetime | None = None

    def __post_init__(self):
        if self.loop.version != self.lane.layout or self.fit_through.utcoffset() is None:
            raise ValueError("Route graph requires one layout and an aware geometry clock.")
        if not 0 < self.core_span_m < self.loop.length_m/2 or not 0 <= self.core_start_m < self.loop.length_m:
            raise ValueError("Pit core must project to one short oriented track interval.")
        if self.verified_through and self.verified_through < self.fit_through:
            raise ValueError("Independent evaluation must follow the fit evidence.")

    def compatible(self, session):
        return session.key == self.session and session.geometry is not None and session.geometry == self.loop


def fit_graph(session, visit, policy):
    """Use only a declared visit's stabilized lane interior; endpoints are observations."""
    entry, exit_at = visit["entry"], visit["exit"]
    series = session.fixes.get(visit["driver"])
    if not series or not exit_at or not 10 <= (exit_at-entry).total_seconds() <= 75:
        return None, "LANE_TIMING_OR_FIXES_UNAVAILABLE"
    trim = timedelta(seconds=policy["fit_lane_trim_s"])
    records = series.window(entry+trim, exit_at-trim)
    if len(records) < 10 or any(row.conflict for row in records):
        return None, "SPARSE_OR_CONFLICTING_FIT"
    loop = session.geometry
    raw_length = sum(hypot(a[0]-b[0], a[1]-b[1]) for a, b in pairwise(loop.points))
    scale = loop.length_m/raw_length
    points = [(records[0].point.x, records[0].point.y)]
    for left, right in pairwise(records):
        a, b = left.point, right.point
        step = hypot(b.x-a.x, b.y-a.y)*scale
        seconds = (b.at_utc-a.at_utc).total_seconds()
        if (not 0 < seconds <= policy["max_fix_span_s"] or step > policy["max_segment_m"]
                or step > 40*seconds+5):
            return None, "PIT_FIT_GAP_OR_SPEED"
        if hypot(b.x-points[-1][0], b.y-points[-1][1])*scale >= 1:
            points.append((b.x, b.y))
    if len(points) < 4:
        return None, "DEGENERATE_PIT_FIT"
    lane = LaneCurve(loop.version, tuple(points), scale)
    if lane.length_m < policy["minimum_curve_length_m"]:
        return None, "PIT_FIT_TOO_SHORT"
    projected = []
    for x, y in points:
        along, errors = loop.candidates(x, y)
        idx = errors.argmin()
        if errors[idx] > policy["max_reference_offset_m"]:
            return None, "COORDINATE_FRAME_MISMATCH"
        projected.append(float(along[idx]))
    span = signed_loop_delta(projected[0], projected[-1], loop.length_m)
    if (not 0 < span < loop.length_m/2 or any(signed_loop_delta(a, b, loop.length_m) < -8
                                             for a, b in pairwise(projected))):
        return None, "PIT_FIT_DIRECTION_UNRESOLVED"
    return RouteGraph(session.key, loop, lane, visit["id"], max(exit_at, loop.available_at_utc),
                      tuple(sorted({s for r in records for s in r.sources})), projected[0], span, policy), None


def match_fix(graph, fix):
    """Compare route hypotheses with an explicit interior and separation requirement."""
    coordinates, errors = graph.loop.candidates(fix.x, fix.y)
    idx = int(np.argmin(errors))
    track, racing_error = float(coordinates[idx]), float(errors[idx])
    lane, lane_error = graph.lane.project(fix.x, fix.y)
    radius = graph.policy["route_radius_m"]
    margin = max(2*radius, graph.policy["min_cross_route_margin_m"])
    core = 10 <= (track-graph.core_start_m) % graph.loop.length_m <= graph.core_span_m-10
    interior = 10 <= lane <= graph.lane.length_m-10
    label = Route.UNKNOWN
    if interior and lane_error <= radius and racing_error-lane_error >= margin:
        label = Route.LANE
    elif core and racing_error <= radius and lane_error-racing_error >= margin:
        label = Route.MAIN
    return {"route": label, "racing_residual_m": racing_error, "pit_residual_m": lane_error,
            "track_m": track, "pit_progress_m": lane, "core_alternative_covered": core, "source_utc": fix.at_utc}


def matched_at(graph, session, driver, at, mode):
    """Two consistent fixes support an event-time assignment; retrospective adds a bracket."""
    if (mode is FieldMode.RECEIPT or graph is None or not graph.compatible(session)
            or graph.verified_through is None
            or (mode is FieldMode.CAUSAL and graph.verified_through > at)):
        return {"route": Route.UNKNOWN, "reason": "LAYOUT_OR_GEOMETRY_NOT_AVAILABLE_FOR_MODE"}
    series = session.fixes.get(driver)
    last = series.last(at) if series else None
    if not last or last.conflict or (at-last.point.at_utc).total_seconds() > graph.policy["position_age_limit_s"]:
        return {"route": Route.UNKNOWN, "reason": "STALE_MISSING_OR_CONFLICTING_FIX"}
    visible = series.window(at-timedelta(seconds=graph.policy["history_s"]), at)
    match = match_fix(graph, last.point)
    if len(visible) < 2 or visible[-2].conflict or match["route"] is Route.UNKNOWN:
        return match | {"route": Route.UNKNOWN, "reason": "INSUFFICIENT_DISTINCT_ROUTE_EVIDENCE"}
    other = match_fix(graph, visible[-2].point)
    sources = [last, visible[-2]]
    seq = [other, match]
    if mode is FieldMode.RETROSPECTIVE and last.point.at_utc < at:
        following = next((r for r in series.window(at, at+timedelta(seconds=graph.policy["after_s"]))
                          if r.point.at_utc > at), None)
        if following is None or following.conflict:
            return match | {"route": Route.UNKNOWN, "reason": "MISSING_VALID_RETROSPECTIVE_BRACKET"}
        sources.append(following)
        seq.append(match_fix(graph, following.point))
    for left, right in pairwise(seq):
        dt = (right["source_utc"]-left["source_utc"]).total_seconds()
        same = left["route"] == right["route"] == match["route"]
        stepping = (signed_loop_delta(left["track_m"], right["track_m"], graph.loop.length_m)
                    if match["route"] == Route.MAIN else right["pit_progress_m"]-left["pit_progress_m"])
        vmax = graph.policy["max_racing_speed_mps"] if match["route"] == Route.MAIN else 40
        if not same or not 0 < dt <= graph.policy["max_fix_span_s"] or not -5 <= stepping <= vmax*dt+5:
            return match | {"route": Route.UNKNOWN, "reason": "ROUTE_OR_MOTION_NOT_CONTINUOUS"}
    if mode is FieldMode.CAUSAL:
        if match["route"] == Route.LANE:
            along, length, speed_bound = match["pit_progress_m"], graph.lane.length_m, 40
        else:
            along = (match["track_m"]-graph.core_start_m) % graph.loop.length_m
            length, speed_bound = graph.core_span_m, graph.policy["max_racing_speed_mps"]
        remaining = speed_bound*(at-last.point.at_utc).total_seconds()+graph.policy["route_radius_m"]
        if min(along, length-along) <= remaining:
            return match | {"route": Route.UNKNOWN, "reason": "REACHABLE_ROUTE_CORE_EDGE"}
    return match | {"reason": "SEPARABLE_HELD_CHECKED_ROUTE_CORE", "latest_fix_used": seq[-1]["source_utc"],
                    "sources": sorted({s for row in sources for s in row.sources}),
                    "stationary_candidate": match["route"] == Route.LANE and
                    abs(seq[-1]["pit_progress_m"]-seq[0]["pit_progress_m"]) <= 1.}


def oriented_interval(subject, other, length, *, forward=True):
    """A conservative circular distance interval with explicit wrap ambiguity."""
    step = (other[0]-subject[0]) % length if forward else (subject[0]-other[0]) % length
    bound = subject[1]+other[1]
    return (0., length) if step <= bound or step+bound >= length else (step-bound, step+bound)


@dataclass(frozen=True)
class OrderWitness:
    session: int
    target: int
    neighbor: int
    at: datetime
    source: str
    direction: str


def certificate(target, positions, length, *, off_route=None, movement_bounds=None, witness=None,
                session=None, cutoff=None, forward=True):
    """Account for every relevant competitor; optional exclusions carry their source evidence.

    Each position carries local progress, its bound and the lap-unwrapped estimate.
    Independent corroboration must match this session, directed pair and exact event clock.
    """
    off_route = off_route or {}
    movement_bounds = movement_bounds or {}
    subject = positions.get(target)
    proof = {"target": target, "direction": "AHEAD" if forward else "BEHIND", "certified": None,
             "bounded_nearest": None, "candidates": [], "possible_interveners": [], "exclusions": {},
             "separation_interval_m": None, "independent_reference": None}
    if not subject or subject[2] is None:
        return proof | {"status": "TARGET_ROUTE_OR_LAP_UNAVAILABLE"}
    ranges = {d: oriented_interval(subject, point, length, forward=forward)
              for d, point in positions.items() if d != target and point is not None and d not in off_route}
    if not ranges:
        return proof | {"status": "NO_SUPPORTED_COMPETITOR"}
    limit = min(b for _, b in ranges.values())
    candidates = sorted(d for d, (a, _) in ranges.items() if a <= limit)
    proof["candidates"] = candidates
    for d in sorted(set(positions)-{target}):
        if d in off_route:
            proof["exclusions"][d] = {"reason": "OBSERVED_OFF_RACING_ROUTE", "evidence": off_route[d]}
        elif d in ranges:
            if ranges[d][0] > limit:
                proof["exclusions"][d] = {"reason": "OUTSIDE_CANDIDATE_ARC", "interval_m": ranges[d]}
        elif d in movement_bounds:
            local, uncertainty, evidence = movement_bounds[d]
            bound = oriented_interval(subject, (local, uncertainty), length, forward=forward)
            if bound[0] > limit:
                proof["exclusions"][d] = {"reason": "MOVEMENT_ENVELOPE_CANNOT_INTERVENE", "interval_m": bound,
                                          "evidence": evidence}
            else:
                proof["possible_interveners"].append(d)
        else:
            proof["possible_interveners"].append(d)
    if len(candidates) != 1:
        return proof | {"status": "ORDER_AMBIGUOUS"}
    driver = candidates[0]
    interval = ranges[driver]
    if interval[0] == 0 or positions[driver][2] is None:
        return proof | {"status": "ORDER_AMBIGUOUS"}
    proof["separation_interval_m"] = interval
    if proof["possible_interveners"]:
        return proof | {"status": "MISSING_CAR_CAN_INTERVENE"}
    proof["bounded_nearest"] = driver
    if isinstance(witness, OrderWitness) and witness.source and cutoff is not None and (
        witness.session, witness.target, witness.neighbor, witness.at, witness.direction
    ) == (session, target, driver, cutoff, proof["direction"]):
        return proof | {"certified": driver, "status": "ORDER_CERTIFIED", "independent_reference": asdict(witness)}
    return proof | {"status": "ORDER_PARTIALLY_CONSTRAINED"}
