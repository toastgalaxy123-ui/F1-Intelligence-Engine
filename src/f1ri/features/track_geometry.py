"""Versioned racing-loop reference and direction-aware telemetry projection."""
from __future__ import annotations

import itertools
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np


def signed_loop_delta(start_m: float, end_m: float, length_m: float) -> float:
    return (end_m - start_m + length_m / 2) % length_m - length_m / 2


@dataclass(frozen=True, slots=True)
class LocationFix:
    at_utc: datetime
    x: float
    y: float
    available_at_utc: datetime | None = None

    def __post_init__(self) -> None:
        if self.at_utc.utcoffset() is None or not all(math.isfinite(v) for v in (self.x, self.y)):
            raise ValueError("Location fixes require finite coordinates and timezone-aware timestamps")
        if self.available_at_utc is not None and (
            self.available_at_utc.utcoffset() is None or self.available_at_utc < self.at_utc
        ):
            raise ValueError("Availability cannot precede the physical observation")

    @property
    def availability(self) -> datetime:
        return self.available_at_utc or self.at_utc


@dataclass(frozen=True, slots=True)
class TrackGeometry:
    version: str
    length_m: float
    available_at_utc: datetime
    points: tuple[tuple[float, float], ...]
    reference_lap_end_gap_m: float
    _starts: np.ndarray = field(init=False, repr=False, compare=False)
    _segments: np.ndarray = field(init=False, repr=False, compare=False)
    _norm_sq: np.ndarray = field(init=False, repr=False, compare=False)
    _distance: np.ndarray = field(init=False, repr=False, compare=False)
    _m_per_unit: float = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        points = np.asarray(self.points, dtype=float)
        if (
            not self.version or not math.isfinite(self.length_m) or self.length_m <= 0
            or self.available_at_utc.utcoffset() is None or points.ndim != 2
            or points.shape[1] != 2 or len(points) < 4 or not np.isfinite(points).all()
            or not np.array_equal(points[0], points[-1])
        ):
            raise ValueError("Geometry requires a versioned closed loop and a timezone-aware origin")
        segments = np.diff(points, axis=0)
        lengths = np.linalg.norm(segments, axis=1)
        if (lengths <= 0).any():
            raise ValueError("Geometry has zero-length segments")
        scale = self.length_m / lengths.sum()
        for key, value in (
            ("_starts", points[:-1]), ("_segments", segments), ("_norm_sq", lengths**2),
            ("_distance", np.concatenate(([0.], lengths.cumsum())) * scale),
            ("_m_per_unit", float(scale)),
        ):
            object.__setattr__(self, key, value)

    def candidates(self, x: float, y: float) -> tuple[np.ndarray, np.ndarray]:
        relative = np.array([x, y]) - self._starts
        fraction = np.clip(np.sum(relative * self._segments, axis=1) / self._norm_sq, 0, 1)
        offset = relative - self._segments * fraction[:, None]
        distance = self._distance[:-1] + fraction * np.diff(self._distance)
        return distance % self.length_m, np.linalg.norm(offset, axis=1) * self._m_per_unit

    @property
    def cumulative_m(self) -> np.ndarray:
        return self._distance.copy()


def _at(fixes: tuple[LocationFix, ...], target: datetime) -> tuple[float, float]:
    for left, right in itertools.pairwise(fixes):
        if left.at_utc <= target <= right.at_utc:
            span = (right.at_utc - left.at_utc).total_seconds()
            if not 0 < span <= 1.25:
                break
            w = (target - left.at_utc).total_seconds() / span
            return left.x + w * (right.x - left.x), left.y + w * (right.y - left.y)
    raise ValueError("Reference lap boundary lacks closely bracketing location samples")


def build_racing_loop(
    fixes: Iterable[LocationFix], *, start: datetime, end: datetime, version: str, length_m: float,
    available_at_utc: datetime,
) -> TrackGeometry:
    """Approximate one closed line from a declared clean lap and its control-line times."""
    visible = tuple(sorted((fix for fix in fixes if fix.availability <= available_at_utc),
                           key=lambda fix: fix.at_utc))
    if start >= end or available_at_utc < end or len({f.at_utc for f in visible}) != len(visible):
        raise ValueError("Reference geometry requires a completed, uniquely timestamped lap")
    head, tail = np.asarray(_at(visible, start)), np.asarray(_at(visible, end))
    internal = [(fix.x, fix.y) for fix in visible if start < fix.at_utc < end]
    if len(internal) < 20:
        raise ValueError("Too few location samples to construct a racing loop")
    center = tuple((head + tail) / 2)
    points = [center]
    for position in internal:
        if position != points[-1] and position != center:
            points.append(position)
    points.append(center)
    raw = np.linalg.norm(np.diff(np.asarray(points), axis=0), axis=1)
    if not raw.sum():
        raise ValueError("Degenerate geometry")
    scale = length_m / raw.sum()
    end_gap = float(np.linalg.norm(head - tail) * scale)
    if end_gap > 80 or float(raw.max() * scale) > 100:
        raise ValueError("Reference lap fails closure or continuity check")
    return TrackGeometry(version, length_m, available_at_utc, tuple(points), end_gap)


@dataclass(frozen=True, slots=True)
class MappedFix:
    fix: LocationFix
    progress_m: float | None
    uncertainty_m: float | None
    projection_error_m: float | None
    reason: str | None = None


def map_fixes(geometry: TrackGeometry, fixes: Iterable[LocationFix]) -> tuple[MappedFix, ...]:
    """Match in source order using direction, a speed bound and branch ambiguity checks."""
    result: list[MappedFix] = []
    prior: MappedFix | None = None
    ordered = sorted(fixes, key=lambda fix: fix.at_utc)
    if len({fix.at_utc for fix in ordered}) != len(ordered):
        raise ValueError("Conflicting same-time location fixes must be screened before projection")
    length = geometry.length_m
    for fix in ordered:
        progress, errors = geometry.candidates(fix.x, fix.y)
        viable = errors <= 25
        if prior is not None:
            dt = (fix.at_utc - prior.fix.at_utc).total_seconds()
            if dt <= 1.25:
                step = (progress - prior.progress_m + length / 2) % length - length / 2
                viable &= (step >= -8) & (step <= 105 * dt + 10)
        if not viable.any():
            result.append(MappedFix(fix, None, None, None, "NO_PLAUSIBLE_SEGMENT"))
            continue
        best = int(np.argmin(np.where(viable, errors, np.inf)))
        local = (progress - progress[best] + length / 2) % length - length / 2
        competitors = viable & (errors <= errors[best] + 4)
        spread = float(np.max(np.abs(local[competitors])))
        if spread > 45:
            result.append(MappedFix(fix, None, None, float(errors[best]), "AMBIGUOUS_SEGMENT"))
            continue
        point = MappedFix(fix, float(progress[best]), max(7., spread + float(errors[best]) + 4.),
                          float(errors[best]))
        result.append(point)
        prior = point
    return tuple(result)


def forward_passages(
    trace: Iterable[MappedFix], marker_m: float, length_m: float,
) -> tuple[datetime, ...]:
    """Interpolate forward passages of a common marker between adjacent good samples."""
    return tuple(row[0] for row in forward_passage_brackets(trace, marker_m, length_m))


def forward_passage_brackets(
    trace: Iterable[MappedFix], marker_m: float, length_m: float,
) -> tuple[tuple[datetime, MappedFix, MappedFix], ...]:
    """Retain the source fixes that bound each interpolated crossing."""
    fixes = tuple(trace)
    passages = []
    for left, right in itertools.pairwise(fixes):
        if left.progress_m is None or right.progress_m is None:
            continue
        span = right.fix.at_utc - left.fix.at_utc
        if not 0 < span.total_seconds() <= 1.0:
            continue
        delta = signed_loop_delta(left.progress_m, right.progress_m, length_m)
        distance = (marker_m - left.progress_m) % length_m
        if 0 < distance <= delta:
            passages.append((left.fix.at_utc + span * (distance / delta), left, right))
    return tuple(passages)
