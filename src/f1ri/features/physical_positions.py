"""Retrospective interpolation and cutoff-safe physical position estimates."""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from f1ri.features.track_geometry import (
    LocationFix,
    MappedFix,
    TrackGeometry,
    map_fixes,
    signed_loop_delta,
)
from f1ri.simulator.proximity import PositionQuality


class LocalizationMode(StrEnum):
    RETROSPECTIVE = "RETROSPECTIVE"
    POINT_IN_TIME = "POINT_IN_TIME"


@dataclass(frozen=True, slots=True)
class PositionEstimate:
    progress_m: float | None
    uncertainty_m: float | None
    speed_mps: float | None
    latest_observation_utc: datetime | None
    quality: PositionQuality
    reason: str | None = None
    velocity_source: str | None = None


def _missing(reason: str, latest: datetime | None = None) -> PositionEstimate:
    return PositionEstimate(None, None, None, latest, PositionQuality.UNSUPPORTED, reason)


def _speed(left: MappedFix, right: MappedFix, length: float) -> float | None:
    if left.progress_m is None or right.progress_m is None:
        return None
    dt = (right.fix.at_utc - left.fix.at_utc).total_seconds()
    delta = signed_loop_delta(left.progress_m, right.progress_m, length)
    return max(delta / dt, 0.) if 0 < dt <= 1.0 and -5 <= delta <= 105 * dt else None


def estimate_from_trace(
    geometry: TrackGeometry, trace: tuple[MappedFix, ...], as_of: datetime, mode: LocalizationMode,
    *, fallback_speed_mps: float | None = None,
) -> PositionEstimate:
    """Read a prepared trace; point-in-time traces must be mapped from the visible prefix."""
    if as_of.utcoffset() is None:
        raise ValueError("Physical estimates require a timezone-aware cutoff")
    if mode is LocalizationMode.POINT_IN_TIME:
        if geometry.available_at_utc > as_of:
            return _missing("GEOMETRY_NOT_AVAILABLE_AT_CUTOFF")
        if any(fix.fix.at_utc > as_of or fix.fix.availability > as_of for fix in trace):
            raise ValueError("Point-in-time estimates require a visible-prefix trace")
    before = [point for point in trace if point.fix.at_utc <= as_of]
    if not before:
        return _missing("MISSING_LOCATION_PREFIX")
    last = before[-1]
    observed = last.fix.at_utc
    if last.progress_m is None:
        return _missing(last.reason or "UNMAPPED_LOCATION", observed)
    age = (as_of - observed).total_seconds()
    if age > 0.9:
        return _missing("STALE_LOCATION", observed)
    length = geometry.length_m
    velocity = _speed(before[-2], last, length) if len(before) >= 2 else None
    velocity_source = None
    if (mode is LocalizationMode.POINT_IN_TIME and velocity is None and fallback_speed_mps is not None
            and math.isfinite(fallback_speed_mps) and 0 <= fallback_speed_mps <= 105):
        velocity = fallback_speed_mps
        velocity_source = "CAR_SPEED_FALLBACK"
    if age == 0:
        return PositionEstimate(last.progress_m, last.uncertainty_m, velocity, observed,
                                PositionQuality.OBSERVED_ESTIMATE, velocity_source=velocity_source)
    if mode is LocalizationMode.RETROSPECTIVE:
        next_fix = next((point for point in trace if point.fix.at_utc > as_of), None)
        if next_fix is not None and (velocity := _speed(last, next_fix, length)) is not None:
            bound = max(last.uncertainty_m, next_fix.uncertainty_m) + 2
            return PositionEstimate((last.progress_m + velocity * age) % length,
                                    bound, velocity, observed, PositionQuality.INTERPOLATED)
        return _missing("MISSING_VALID_BRACKET", observed)
    if velocity is None:
        return _missing("NO_RECENT_VELOCITY", observed)
    velocity_error = 25 if velocity_source else 15
    bound = last.uncertainty_m + velocity_error * age + 8 * age**2
    if bound >= length / 2:
        return _missing("POSITION_UNCERTAINTY_EXCEEDS_LOOP", observed)
    return PositionEstimate((last.progress_m + velocity * age) % length, bound,
                            velocity, observed, PositionQuality.EXTRAPOLATED,
                            velocity_source=velocity_source)


def estimate_location(
    geometry: TrackGeometry, fixes: Iterable[LocationFix], as_of: datetime, mode: LocalizationMode,
) -> PositionEstimate:
    if mode is LocalizationMode.POINT_IN_TIME:
        fixes = (fix for fix in fixes if fix.at_utc <= as_of and fix.availability <= as_of)
    return estimate_from_trace(geometry, map_fixes(geometry, fixes), as_of, mode)
