"""Paired localization diagnostics on a shared field, clock and route mask."""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from f1ri.features.physical_positions import LocalizationMode, estimate_from_trace
from f1ri.features.track_geometry import map_fixes, signed_loop_delta
from f1ri.simulator.proximity import PhysicalRoute
from f1ri.validation.proximity_pilot import _asof, _parquet

CURRENT = "POINT_IN_TIME"
NO_SPEED = "POINT_IN_TIME_NO_SPEED"
MATCHED_RETRO = "RETROSPECTIVE_MATCHED_MASK"
NATIVE_RETRO = "RETROSPECTIVE"
_EXCLUDED = {PhysicalRoute.PIT, PhysicalRoute.OFF_TRACK}


def age_band(age):
    if age is None or pd.isna(age):
        return "MISSING"
    if age <= .25:
        return "0–0.25 s"
    if age <= .50:
        return "0.25–0.50 s"
    if age <= .90:
        return "0.50–0.90 s"
    return ">0.90 s"


def observation_context(trace, index, speed, at, start, end, markers, speed_limit):
    left = trace[index - 1] if index else None
    right = trace[index] if index < len(trace) else None
    left_age = (at - left.fix.at_utc).total_seconds() if left else None
    speed_age = (at - speed.date).total_seconds() if speed is not None else None
    speed_status = "MISSING"
    if speed_age is not None:
        if speed_age > speed_limit:
            speed_status = "STALE"
        else:
            speed_status = "FRESH" if 0 <= speed.speed / 3.6 <= 105 else "INVALID"
    edge = "START" if at - start <= timedelta(seconds=1) else "INTERIOR"
    if end - at <= timedelta(seconds=1):
        edge = "END"
    # Attribute only from the observed left fix. An unmapped or absent fix has no sector.
    sector = bisect_right(markers, left.progress_m) if left and left.progress_m is not None else None
    return {"location_age_s": left_age, "age_band": age_band(left_age), "speed_age_s": speed_age,
            "speed_status": speed_status, "window_edge": edge, "last_observed_sector": sector,
            "right_fix_present": right is not None}


def loss_detail(reason, context, mode):
    if reason == "NO_RECENT_VELOCITY":
        status = "DISABLED" if mode == NO_SPEED else context["speed_status"]
        return f"NO_RECENT_VELOCITY_SPEED_{status}"
    if reason == "MISSING_VALID_BRACKET":
        if not context["right_fix_present"]:
            return "WINDOW_END_NO_RIGHT_FIX" if context["window_edge"] == "END" else "NO_RIGHT_FIX"
        return "UNUSABLE_INTERPOLATION_BRACKET"
    if reason == "MISSING_LOCATION_PREFIX" and context["window_edge"] == "START":
        return "WINDOW_START_NO_FIX"
    return reason or "POSITION_UNSUPPORTED"


class CoverageReplay:
    def __init__(self):
        self.snapshots = []
        self.drivers = []
        self.dropouts = []

    def capture(self, metadata, mode, snapshot, groups, contexts, off_track_evidence):
        cars = snapshot.cars
        required = [car for car in cars if car.route not in _EXCLUDED]
        missing = [car.driver_number for car in required if car.progress_m is None]
        self.snapshots.append(metadata | {
            "mode": mode, "roster": [car.driver_number for car in cars],
            "window_edge": next(iter(contexts.values()))["window_edge"],
            "required": [car.driver_number for car in required],
            "evidence_off_track": [car.driver_number for car in cars if car.route is PhysicalRoute.OFF_TRACK],
            "pit": [car.driver_number for car in cars if car.route is PhysicalRoute.PIT],
            "unknown_route": [car.driver_number for car in cars if car.route is PhysicalRoute.UNKNOWN],
            "supported": [car.driver_number for car in required if car.progress_m is not None],
            "missing_required": missing, "complete": snapshot.complete,
            "resolved_ahead": [car.driver_number for car in required if groups[car.driver_number].ahead],
            "unresolved_ahead": [car.driver_number for car in required if not groups[car.driver_number].ahead],
        })
        for car in cars:
            driver = car.driver_number
            group, context = groups[driver], contexts[driver]
            ahead = group.ahead
            evidence = off_track_evidence.get(driver) if car.route is PhysicalRoute.OFF_TRACK else None
            self.drivers.append(metadata | context | {
                "mode": mode, "driver_number": driver, "complete": snapshot.complete, "route": car.route.value,
                "required": car.route not in _EXCLUDED, "localized": car.progress_m is not None,
                "position_reason": car.reason,
                "failure": loss_detail(car.reason, context, mode) if driver in missing else None,
                "blocks_alone": len(missing) == 1 and driver in missing,
                "position_bound_width_m": 2 * car.uncertainty_m if car.uncertainty_m is not None else None,
                "ahead": ahead.driver_number if ahead else None, "ahead_reason": group.ahead_reason,
                "lap_relation": ahead.encounter if ahead else None,
                "gap_m": ahead.gap_m if ahead else None,
                "gap_lower_m": ahead.lower_m if ahead else None, "gap_upper_m": ahead.upper_m if ahead else None,
                "off_track_source": evidence["source"] if evidence else None,
                "off_track_available_by_utc": evidence["available_by_utc"] if evidence else None,
            })

    def check_dropouts(self, geometry, traces, car_speed, window, config):
        for driver, trace in traces.items():
            for i in range(config["dropout_stride"], len(trace), config["dropout_stride"]):
                heldout = trace[i]
                at = heldout.fix.at_utc
                before = trace[i - 2:i]
                age = (at - before[-1].fix.at_utc).total_seconds()
                speed = _asof(car_speed, driver, at)
                fallback = (speed.speed / 3.6 if speed is not None
                            and (at - speed.date).total_seconds() <= config["speed_fallback_max_age_s"] else None)
                # Reproject the right bracket without the hidden fix.
                remapped = map_fixes(geometry, [p.fix for p in (*before, *trace[i + 1:i + 2])])
                for label, mode, prefix, velocity in (
                    (CURRENT, LocalizationMode.POINT_IN_TIME, before, fallback),
                    (NO_SPEED, LocalizationMode.POINT_IN_TIME, before, None),
                    (MATCHED_RETRO, LocalizationMode.RETROSPECTIVE, remapped, None),
                ):
                    estimate = estimate_from_trace(geometry, prefix, at, mode, fallback_speed_mps=velocity)
                    error = (abs(signed_loop_delta(estimate.progress_m, heldout.progress_m, geometry.length_m))
                             if estimate.progress_m is not None and heldout.progress_m is not None else None)
                    self.dropouts.append({
                        "window": window.name, "window_kind": window.kind, "mode": label, "driver_number": driver,
                        "as_of_utc": at, "source_age_s": age, "age_band": age_band(age),
                        "error_m": error, "position_bound_width_m": (
                            2 * estimate.uncertainty_m if estimate.uncertainty_m is not None else None
                        ),
                        "inside_heuristic_bound": error <= estimate.uncertainty_m if error is not None else None,
                        "used_speed_fallback": estimate.velocity_source is not None,
                        "unsupported_reason": estimate.reason or heldout.reason,
                    })

    def finish(self, output: Path, exposure: dict) -> dict:
        snapshots, drivers, drops = map(pd.DataFrame, (self.snapshots, self.drivers, self.dropouts))
        failures = drivers[drivers.failure.notna()]
        _parquet(output / "coverage_snapshots.parquet", self.snapshots)
        _parquet(output / "coverage_driver_snapshots.parquet", self.drivers)
        _parquet(output / "coverage_dropped_fixes.parquet", self.dropouts)
        slice_keys = ["mode", "window", "window_kind", "driver_number", "last_observed_sector", "age_band", "failure"]
        slices = failures.groupby(slice_keys, dropna=False).agg(
            missing=("failure", "size"), sole_blocker=("blocks_alone", "sum"),
        ).reset_index()
        _parquet(output / "coverage_loss_slices.parquet", slices.to_dict("records"))
        modes = {}
        for label, frame in snapshots.groupby("mode", sort=False):
            losses = failures[failures["mode"] == label]
            visible = drivers[drivers["mode"] == label]
            modes[label] = summarize_support(frame) | {
                "missing_by_reason": dict(Counter(losses.failure)),
                "missing_by_driver": {str(k): int(v) for k, v in losses.driver_number.value_counts().items()},
                "missing_by_age_band": dict(Counter(losses.age_band)),
                "missing_by_last_observed_sector": dict(Counter(
                    "UNKNOWN" if pd.isna(sector) else str(int(sector)) for sector in losses.last_observed_sector
                )),
                "sole_blocker_by_driver": {str(k): int(v) for k, v in
                                           losses[losses.blocks_alone].driver_number.value_counts().items()},
                "ahead_resolved_complete": int((visible.complete & visible.ahead.notna()).sum()),
                "ahead_unresolved_complete": int((visible.complete & visible.required & visible.ahead.isna()).sum()),
                "position_bound_width_m": distribution(visible.position_bound_width_m),
                "edge": summarize_support(frame[frame.window_edge.ne("INTERIOR")]),
                "interior": summarize_support(frame[frame.window_edge.eq("INTERIOR")]),
                "strata": {kind: summarize_support(part) for kind, part in frame.groupby("window_kind")},
                "windows": {name: summarize_support(part) for name, part in frame.groupby("window")},
            }
        pairs = {other: paired_results(snapshots, drivers, other, exposure)
                 for other in (NO_SPEED, MATCHED_RETRO, NATIVE_RETRO)}
        dropped = []
        if not drops.empty:
            for (mode, age, used_speed), group in drops.groupby(["mode", "age_band", "used_speed_fallback"]):
                valid = group[group.error_m.notna()]
                dropped.append({
                    "mode": mode, "age_band": age, "used_speed_fallback": bool(used_speed),
                    "requested": len(group), "error_m": distribution(group.error_m),
                    "bound_width_m": distribution(valid.position_bound_width_m),
                    "inside_bound": int(valid.inside_heuristic_bound.sum()),
                })
        return {"availability": "OBSERVATION_TIMESTAMP_PROXY", "modes": modes, "paired_against_current": pairs,
                "dropped_fix_by_age_and_mode": dropped}


def distribution(values):
    clean = np.asarray(pd.Series(values).dropna(), dtype=float)
    return {"n": len(clean), "median": float(np.median(clean)) if len(clean) else None,
            "p90": float(np.quantile(clean, .90)) if len(clean) else None}


def summarize_support(frame):
    return {"snapshots": len(frame), "complete": int(frame.complete.sum()),
            "one_missing": int(frame.missing_required.map(len).eq(1).sum()),
            "multiple_missing": int(frame.missing_required.map(len).gt(1).sum())}


def paired_results(snapshots, drivers, other, exposure):
    keys = ["window", "as_of_utc"]
    base = snapshots[snapshots["mode"] == CURRENT]
    comparison = snapshots[snapshots["mode"] == other]
    paired = base.merge(comparison, on=keys, suffixes=("_current", "_other"), validate="one_to_one")
    if len(paired) != len(base) or len(paired) != len(comparison):
        raise ValueError("Coverage modes differ in sampling clocks")
    if not paired.apply(lambda row: row.roster_current == row.roster_other, axis=1).all():
        raise ValueError("Coverage modes differ in declared rosters")
    same_mask = paired.apply(lambda row: (
        row.required_current == row.required_other and row.pit_current == row.pit_other
        and row.evidence_off_track_current == row.evidence_off_track_other
    ), axis=1)
    if other != NATIVE_RETRO and not same_mask.all():
        raise ValueError("Paired localization modes must use the same required field")
    keys.append("driver_number")
    fields = ["ahead", "complete", "gap_m", "gap_lower_m", "gap_upper_m"]
    left = drivers[drivers["mode"] == CURRENT][keys + fields]
    right = drivers[drivers["mode"] == other][keys + fields]
    neighbors = left.merge(right, on=keys, suffixes=("_current", "_other"), validate="one_to_one")
    both = neighbors[neighbors.ahead_current.notna() & neighbors.ahead_other.notna()]

    def compare(frame):
        agree = frame[frame.ahead_current == frame.ahead_other]
        return {"both_resolved": len(frame), "different_identity": len(frame) - len(agree),
                "same_identity_absolute_gap_difference_m": distribution((agree.gap_m_current - agree.gap_m_other).abs()),
                "close_threshold_disagreement": int((
                    frame.gap_upper_m_current.le(exposure["entry_upper_m"])
                    != frame.gap_upper_m_other.le(exposure["entry_upper_m"])
                ).sum()),
                "clean_threshold_disagreement": int((
                    frame.gap_lower_m_current.ge(exposure["clean_lower_m"])
                    != frame.gap_lower_m_other.ge(exposure["clean_lower_m"])
                ).sum())}

    return {"paired_snapshots": len(paired), "different_required_mask": int((~same_mask).sum()),
            "current_only_complete": int((paired.complete_current & ~paired.complete_other).sum()),
            "other_only_complete": int((~paired.complete_current & paired.complete_other).sum()),
            "both_complete": int((paired.complete_current & paired.complete_other).sum()),
            "all_localized_pairs": compare(both),
            "complete_field_pairs": compare(both[both.complete_current & both.complete_other])}
