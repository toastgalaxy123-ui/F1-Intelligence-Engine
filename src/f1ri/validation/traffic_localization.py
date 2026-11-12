"""Cross-circuit reconstruction diagnostics from frozen, bounded windows."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import timedelta
from itertools import pairwise

import numpy as np
import pandas as pd

from f1ri.features.physical_positions import LocalizationMode, estimate_from_trace
from f1ri.features.track_geometry import (
    TrackGeometry,
    forward_passages,
    map_fixes,
    signed_loop_delta,
)
from f1ri.simulator.proximity import (
    PhysicalRoute,
    PhysicalSnapshot,
    TrackPosition,
    physical_neighborhoods,
)
from f1ri.validation.proximity_pilot import _asof, _driver_rows, _fixes, _physical_row, _pit_route
from f1ri.validation.traffic_acquisition import TrafficWindow
from f1ri.validation.traffic_coverage import (
    MATCHED_RETRO,
    NO_SPEED,
    CoverageReplay,
    observation_context,
)


def _abs_seconds(left, right):
    return abs((left - right).total_seconds())


def _nearest_passage(passages, timing, max_error):
    if not passages:
        return None
    index = bisect_left(passages, timing)
    nearest = min(passages[max(0, index - 1):index + 1], key=lambda at: abs(at - timing))
    return nearest if _abs_seconds(nearest, timing) <= max_error else None


def _passage_bounds(trace, times, crossing, length_m):
    i = bisect_left(times, crossing)
    if not 0 < i < len(trace):
        return None
    left, right = trace[i - 1:i + 1]
    if left.progress_m is None or right.progress_m is None:
        return None
    delta = signed_loop_delta(left.progress_m, right.progress_m, length_m)
    dt = (times[i] - times[i - 1]).total_seconds()
    if delta <= 0:
        return None
    return max(left.uncertainty_m, right.uncertainty_m) * dt / delta + dt / 2


def control_passage_checks(
    geometry: TrackGeometry, traces, timing, pits, window: TrafficWindow, config: dict,
) -> list[dict]:
    """Check previous passage at one marker using independently timestamped lap starts."""
    start, end = window.telemetry.start_utc, window.telemetry.end_utc
    limit = config["timing_passage_gap_limit_s"]
    tolerance = config["maximum_timing_marker_match_s"]
    # Keep pit and unmatchable timing rows in the source sequence so they
    # cannot turn an earlier passage into the expected immediate predecessor.
    observed = [(at, int(row.driver_number), int(row.lap_number))
                for row in timing.itertuples() if start <= (at := row.date_start) <= end]
    projected = sorted((at, driver)
                       for driver, trace in traces.items()
                       for at in forward_passages(trace, 0, geometry.length_m)
                       if _pit_route(at, LocalizationMode.RETROSPECTIVE,
                                     pits.get(driver, ((), ()))[1])[0] is PhysicalRoute.RACING)
    projected_times = [at for at, _ in projected]
    by_driver = {driver: [at for at, number in projected if number == driver] for driver in traces}
    times = {driver: tuple(point.fix.at_utc for point in trace) for driver, trace in traces.items()}
    out = []
    for (before, expected_driver, _), (at, driver, lap) in pairwise(observed):
        expected_gap = (at - before).total_seconds()
        if (expected_driver == driver or not 0 < expected_gap <= limit
                or before <= start + timedelta(seconds=tolerance)
                or at >= end - timedelta(seconds=tolerance)):
            continue
        if any(_pit_route(clock, LocalizationMode.RETROSPECTIVE,
                          pits.get(number, ((), ()))[1])[0] is not PhysicalRoute.RACING
               for clock, number in ((before, expected_driver), (at, driver))):
            continue
        own = _nearest_passage(by_driver.get(driver, []), at, tolerance)
        predecessor = None if own is None else bisect_left(projected_times, own) - 1
        other, number = projected[predecessor] if predecessor is not None and predecessor >= 0 else (None, None)
        actual_gap = (own - other).total_seconds() if other is not None else None
        matched = number == expected_driver and _abs_seconds(other, before) <= tolerance
        covered = halfwidth = None
        if matched:
            own_bound = _passage_bounds(traces[driver], times[driver], own, geometry.length_m)
            other_bound = _passage_bounds(traces[number], times[number], other, geometry.length_m)
            if own_bound is not None and other_bound is not None:
                halfwidth = own_bound + other_bound
                covered = abs(actual_gap - expected_gap) <= halfwidth
        out.append({"window": window.name, "driver_number": driver, "lap_number": lap,
                    "observed_utc": at, "timing_previous_driver": expected_driver,
                    "projected_previous_driver": number, "timing_gap_s": expected_gap,
                    "projected_gap_s": actual_gap, "matched": matched,
                    "gap_error_s": actual_gap - expected_gap if matched else None,
                    "heuristic_gap_covered": covered, "heuristic_gap_halfwidth_s": halfwidth})
    return out


def independent_marker_checks(
    geometry: TrackGeometry, markers: list[float], traces, laps: pd.DataFrame, window: TrafficWindow,
    reference_driver: int, used_drivers: set[int], tolerance: float, maximum_laps: int = 1,
) -> list[dict]:
    start, end = window.telemetry.start_utc, window.telemetry.end_utc
    eligible = laps[laps.is_clean_green_pace_lap & laps.sector_1_s.notna() & laps.sector_2_s.notna()
                    & laps.lap_start_utc.gt(start + timedelta(seconds=tolerance))
                    & laps.lap_end_estimate_utc.lt(end - timedelta(seconds=tolerance))]
    rows = []
    for lap in eligible.itertuples():
        if lap.driver_number == reference_driver or lap.driver_number in used_drivers:
            continue
        next_lap = laps[(laps.driver_number == lap.driver_number) & (laps.lap_number == lap.lap_number + 1)]
        if len(next_lap) != 1:
            continue
        line_end = next_lap.lap_start_utc.iloc[0]
        if line_end > end - timedelta(seconds=tolerance):
            continue
        own = traces[lap.driver_number]
        first = lap.lap_start_utc
        times = (first, first + timedelta(seconds=lap.sector_1_s),
                 first + timedelta(seconds=lap.sector_1_s + lap.sector_2_s), line_end)
        for label, marker, at in zip(("start", "sector_1", "sector_2", "finish"),
                                      (*markers, 0.), times, strict=True):
            crossings = forward_passages(own, marker, geometry.length_m)
            match = _nearest_passage(crossings, at, tolerance)
            rows.append({"window": window.name, "driver_number": int(lap.driver_number),
                         "lap_number": int(lap.lap_number), "marker": label, "timing_utc": at,
                         "projected_utc": match, "error_s": (match - at).total_seconds() if match else None})
        used_drivers.add(lap.driver_number)
        if len(rows) >= maximum_laps * 4:
            break
    return rows


def dropout_checks(geometry: TrackGeometry, traces, window: str, stride: int) -> list[dict]:
    rows = []
    for driver, trace in traces.items():
        for i in range(stride, len(trace), stride):
            heldout = trace[i]
            estimate = estimate_from_trace(geometry, trace[i - 2:i], heldout.fix.at_utc,
                                           LocalizationMode.POINT_IN_TIME)
            error = None
            if estimate.progress_m is not None and heldout.progress_m is not None:
                error = abs(signed_loop_delta(estimate.progress_m, heldout.progress_m, geometry.length_m))
            age = ((heldout.fix.at_utc - estimate.latest_observation_utc).total_seconds()
                   if estimate.latest_observation_utc else None)
            rows.append({"window": window, "driver_number": driver, "as_of_utc": heldout.fix.at_utc,
                         "source_age_s": age, "unsupported_reason": estimate.reason or heldout.reason,
                         "speed_mps": estimate.speed_mps, "error_m": error,
                         "inside_heuristic_bound": error <= estimate.uncertainty_m if error is not None else None})
    return rows


def apply_off_track_evidence(car: TrackPosition, at, record: dict | None) -> TrackPosition:
    if record is None or at < pd.Timestamp(record["available_by_utc"]):
        return car
    return TrackPosition(car.driver_number, PhysicalRoute.OFF_TRACK, car.completed_laps,
                         car.classification, reason=record["reason"])


def recent_speed_at(lookup, driver: int, cutoff, maximum_age_s: float):
    row = _asof(lookup, driver, cutoff)
    if row is None or (cutoff - row.date).total_seconds() > maximum_age_s:
        return None, None
    return row.speed / 3.6, row.date


def localize_window(
    geometry: TrackGeometry, window: TrafficWindow, location: pd.DataFrame, speed: pd.DataFrame, inputs: dict,
    markers: list[float], config: dict, period: float, reference_driver: int, used_drivers: set[int],
    off_track_evidence: dict[int, dict],
    *, coverage_replay: CoverageReplay | None = None, marker_laps_per_window: int = 1,
) -> tuple[list[dict], list[dict], dict, dict[str, list[dict]]]:
    drivers = inputs["drivers"].driver_number
    split = {int(driver): frame for driver, frame in location.groupby("driver_number")}
    traces = {driver: map_fixes(geometry, _fixes(split[driver])) if driver in split else ()
              for driver in drivers}
    times = {driver: tuple(point.fix.at_utc for point in trace) for driver, trace in traces.items()}
    laps = _driver_rows(inputs["laps"], "lap_start_utc")
    reported = _driver_rows(inputs["positions"], "date")
    pits = _driver_rows(inputs["pits"], "date")
    car_speed = _driver_rows(speed.dropna(subset=["speed"]), "date")
    positions, neighbors = [], []
    metrics, reasons = Counter(), Counter()
    start, end = window.telemetry.start_utc, window.telemetry.end_utc
    modes = [(mode.value, mode) for mode in LocalizationMode]
    if coverage_replay is not None:
        modes.extend(((NO_SPEED, LocalizationMode.POINT_IN_TIME), (MATCHED_RETRO, LocalizationMode.RETROSPECTIVE)))
    at = start + timedelta(seconds=period)
    while at < end:
        metadata = {"session_key": window.telemetry.session_key, "window": window.name,
                    "window_kind": window.kind, "layout_version": geometry.version, "as_of_utc": at,
                    "track_state": "GREEN"}
        indices = {driver: bisect_right(times[driver], at) for driver in traces}
        if coverage_replay is not None:
            contexts = {driver: observation_context(
                trace, indices[driver], _asof(car_speed, driver, at), at, start, end,
                markers, config["speed_fallback_max_age_s"],
            ) for driver, trace in traces.items()}
        for label, mode in modes:
            cars = []
            speed_clocks = {}
            for driver, trace in traces.items():
                i = indices[driver]
                after = int(mode is LocalizationMode.RETROSPECTIVE)
                # Projection is forward-only. The archive uses its sample clock
                # as availability, so only the local visible prefix is needed.
                prefix = trace[max(0, i - 2):i + after]
                fallback_speed, speed_utc = None, None
                if mode is LocalizationMode.POINT_IN_TIME and label != NO_SPEED:
                    fallback_speed, speed_utc = recent_speed_at(
                        car_speed, driver, at, config["speed_fallback_max_age_s"],
                    )
                car = _physical_row(geometry, prefix, at, driver, mode, laps, reported,
                                    pits.get(driver, ((), ()))[1], fallback_speed_mps=fallback_speed,
                                    route_mode=LocalizationMode.POINT_IN_TIME if label == MATCHED_RETRO else None)
                car = apply_off_track_evidence(car, at, off_track_evidence.get(driver))
                if car.reason == "CAR_SPEED_FALLBACK":
                    speed_clocks[driver] = speed_utc
                cars.append(car)
            snapshot = PhysicalSnapshot(window.telemetry.session_key, at, geometry.version,
                                        geometry.length_m, tuple(cars))
            groups = physical_neighborhoods(snapshot)
            if coverage_replay is not None:
                coverage_replay.capture(metadata, label, snapshot, groups, contexts, off_track_evidence)
            if label != mode.value:
                continue
            metrics[f"{mode.value}_snapshots"] += 1
            metrics[f"{mode.value}_complete"] += snapshot.complete
            for car in cars:
                group = groups[car.driver_number]
                ahead = group.ahead
                metrics[f"{mode.value}_resolved_ahead"] += ahead is not None and snapshot.complete
                if car.reason:
                    reasons[f"{mode.value}:{car.reason}"] += 1
                source_age = ((at - car.observed_at_utc).total_seconds() if car.observed_at_utc else None)
                position = metadata | {"mode": mode.value, "driver_number": car.driver_number,
                                       "route": car.route.value, "classification": car.classification,
                                       "completed_laps": car.completed_laps, "progress_m": car.progress_m,
                                       "uncertainty_m": car.uncertainty_m, "speed_mps": car.speed_mps,
                                       "lap_aligned": car.lap_aligned, "source_age_s": source_age,
                                       "observation_utc": car.observed_at_utc, "quality": car.quality.value,
                                       "fallback_speed_utc": speed_clocks.get(car.driver_number),
                                       "reason": car.reason}
                positions.append(position)
                neighbors.append(metadata | {
                    "mode": mode.value, "driver_number": car.driver_number, "route": car.route.value,
                    "coverage": group.coverage, "ahead": ahead.driver_number if ahead else None,
                    "behind": group.behind.driver_number if group.behind else None,
                    "reason": group.ahead_reason, "gap_m": ahead.gap_m if ahead else None,
                    "gap_lower_m": ahead.lower_m if ahead else None,
                    "gap_upper_m": ahead.upper_m if ahead else None,
                    "encounter": ahead.encounter if ahead else None,
                    "lap_advantage": ahead.lap_advantage if ahead else None,
                    "source_age_s": source_age, "speed_mps": car.speed_mps,
                    "fallback_speed_utc": speed_clocks.get(car.driver_number),
                    "completed_laps": car.completed_laps, "classification": car.classification,
                })
        at += timedelta(seconds=period)
    if coverage_replay is not None:
        coverage_replay.check_dropouts(geometry, traces, car_speed, window, config)
    checks = {
        "control_passages": control_passage_checks(geometry, traces, inputs["passes"], pits, window, config),
        "dropped_fixes": dropout_checks(geometry, traces, window.name, config["dropout_stride"]),
        "markers": independent_marker_checks(geometry, markers, traces, inputs["laps"], window,
                                             reference_driver, used_drivers,
                                             config["maximum_timing_marker_match_s"],
                                             min(marker_laps_per_window, config["independent_laps"] - len(used_drivers)))
        if len(used_drivers) < config["independent_laps"] else [],
    }
    quality = {
        "name": window.name, "kind": window.kind, "counts": dict(metrics), "reasons": dict(reasons),
        "locations": len(location), "mapped": sum(point.progress_m is not None
                                                  for trace in traces.values() for point in trace),
        "projection_reasons": dict(Counter(point.reason for trace in traces.values() for point in trace
                                           if point.reason is not None)),
    }
    return positions, neighbors, quality, checks


def localization_summary(windows: list[dict], checks: dict[str, list[dict]], driver_count: int) -> dict:
    counts = sum((Counter(window["counts"]) for window in windows), Counter())
    markers = [row["error_s"] for row in checks["markers"] if row["error_s"] is not None]
    control = checks["control_passages"]
    gaps = [row["gap_error_s"] for row in control if row["gap_error_s"] is not None]
    bounds = [row["heuristic_gap_covered"] for row in control if row["heuristic_gap_covered"] is not None]
    widths = [row["heuristic_gap_halfwidth_s"] for row in control if row["heuristic_gap_halfwidth_s"] is not None]
    dropped = [row for row in checks["dropped_fixes"] if row["error_m"] is not None]

    def errors(values):
        return {"n": len(values), "mae": float(np.mean(np.abs(values))) if values else None,
                "bias": float(np.mean(values)) if values else None}

    def absolute_errors(values):
        return {"n": len(values), "mae": float(np.mean(values)) if values else None,
                "p90": float(np.quantile(values, .9)) if values else None}

    return {
        "driver_count": driver_count,
        "locations": sum(row["locations"] for row in windows),
        "mapped": sum(row["mapped"] for row in windows),
        "point_in_time_complete": counts["POINT_IN_TIME_complete"],
        "point_in_time_snapshots": counts["POINT_IN_TIME_snapshots"],
        "retrospective_complete": counts["RETROSPECTIVE_complete"],
        "retrospective_snapshots": counts["RETROSPECTIVE_snapshots"],
        "point_in_time_complete_resolved_ahead": counts["POINT_IN_TIME_resolved_ahead"],
        "speed_fallback_samples": sum(window["reasons"].get("POINT_IN_TIME:CAR_SPEED_FALLBACK", 0)
                                      for window in windows),
        "timing_markers": {"requested": len(checks["markers"]), **errors(markers)},
        "control_order": {"eligible": len(control),
                          "resolved": sum(row["projected_previous_driver"] is not None for row in control),
                          "matched": len(gaps), "matched_gap_error_s": errors(gaps),
                          "gap_bounds_checked": len(bounds), "gap_bounds_covered": sum(bounds),
                          "median_heuristic_gap_halfwidth_s": float(np.median(widths)) if widths else None},
        "heldout_fix_requested": len(checks["dropped_fixes"]),
        "heldout_fix_error_m": absolute_errors([row["error_m"] for row in dropped]),
        "heldout_fix_inside_bound": sum(row["inside_heuristic_bound"] for row in dropped),
        "heldout_fix_by_age": [
            {"maximum_age_s": hi, "minimum_age_exclusive_s": lo, **absolute_errors([
                row["error_m"] for row in dropped if lo < row["source_age_s"] <= hi
            ])} for lo, hi in ((0., .25), (.25, .5), (.5, .9))
        ],
    }
