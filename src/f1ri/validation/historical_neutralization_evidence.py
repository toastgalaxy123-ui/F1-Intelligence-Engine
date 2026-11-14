"""Build source-time observations and apply explicit historical support gates."""
import itertools
from bisect import bisect_right
from collections import defaultdict
from dataclasses import asdict
from datetime import timedelta
from math import isfinite

from f1ri.features.lap_context import build_track_status_intervals
from f1ri.features.physical_positions import LocalizationMode, estimate_from_trace
from f1ri.features.track_geometry import TrackGeometry, map_fixes, signed_loop_delta
from f1ri.simulator.proximity import (
    PhysicalRoute,
    PhysicalSnapshot,
    TrackPosition,
    physical_neighborhoods,
)
from f1ri.validation.historical_neutralization_sources import clock
from f1ri.validation.overtake_historical_study import _load_json

ONE_US = timedelta(microseconds=1)
SUPPORT = {
    "H0": "Direct localization under the archive event-clock convention. Requires geometry, lap and route support.",
    "H1": "Bounded interval from sector markers. Retrospective if the sector data would become known after cutoff.",
    "H2": "Lap-line or lap-start constraint. Within-lap position unresolved.",
    "H3": "Classification or active roster evidence only.",
    "H4": "Declared simulation field. No historical physical position inferred.",
}


def chronology(evidence):
    by_session = defaultdict(dict)
    for record in evidence.values():
        for row in record["observed_messages"]:
            key = (row["date"], row["category"], row["scope"], row["flag"], row["driver_number"], row["message"])
            by_session[record["session"]][key] = row | {"date": clock(row["date"])}
    events, intervals, episodes = [], [], []
    for name, record in evidence.items():
        frozen = record["interval"]
        start, end = clock(frozen["start_utc"]), clock(frozen["end_utc"])
        session = record["session"]
        candidates = list(by_session[session].values())
        baseline = start - ONE_US
        control = [m for m in candidates if start <= m["date"] <= end]
        # The source archive contains a selected message subset. On a local
        # window selected mid-incident we must not regenerate earlier states.
        rebuilt = build_track_status_intervals(session, control, baseline, end + ONE_US)
        matched = next((x for x in rebuilt if x.start_utc == start and x.status == frozen["status"]), None)
        if matched is None or matched.end_utc != end:
            raise ValueError(f"Stage 1 did not reproduce the archived boundary for {name}")
        intervals.extend({"episode": name, **asdict(x)} for x in rebuilt
                         if x.end_utc > start and x.start_utc < end)
        last_green = max((m["date"] for m in candidates if m["date"] < start and m["scope"] == "Track"
                          and m["flag"] in ("GREEN", "CLEAR")), default=None)
        announcement = next((m["date"] for m in control if
                             ("IN THIS LAP" in m["message"] or "SAFETY CAR ENDING" in m["message"])), None)
        release = next((m for m in control if m["date"] == end and m["scope"] == "Track"
                        and m["flag"] in ("GREEN", "CLEAR")), None)
        unlapping = [m for m in control if "LAPPED CAR" in m["message"] and "OVERTAKE" in m["message"]]
        first_line = min((clock(r["utc"]) for r in record["observed_lines"] if clock(r["utc"]) >= end), default=None)
        episodes.append({"episode": name, "session": session, "race": record["race"],
                         "status": frozen["status"], "start_utc": start, "end_utc": end,
                         "duration_s": (end-start).total_seconds(), "previous_status": record["previous"],
                         "last_observed_global_green_utc": last_green, "announcement_utc": announcement,
                         "archived_release_utc": release["date"] if release else None,
                         "next_timing_line_utc": first_line, "actual_withdrawal_utc": None,
                         "withdrawal_proxy_utc": end if frozen["status"] == "SC" and release else None,
                         "unlapping": unlapping, "confidence": frozen["confidence"],
                         "source_rule": frozen["source_rule"], "stage1_boundary_parity": True,
                         "scope": "TRACK" if record["supported_global_status"] else "LOCAL",
                         "message_set": "FROZEN_WINDOW_EXCERPT", "message_count": len(control)})
        events.extend(m | {"episode": name, "receipt_utc": None, "driver_response_utc": None}
                      for m in control)
    return episodes, events, intervals


def route_events(evidence, episodes):
    rows = []
    visits = defaultdict(list)
    for ep in episodes:
        name = ep["episode"]
        pending = {}
        for item in evidence[name]["observed_route_events"]:
            stamp, driver, kind = clock(item["utc"]), item["driver"], item["kind"]
            if kind == "PIT_ENTRY":
                if driver in pending:
                    visits[name].append((driver, pending[driver], None))
                pending[driver] = stamp
            elif kind == "PIT_EXIT":
                visits[name].append((driver, pending.pop(driver, None), stamp))
            rows.append({"episode": name, "driver": driver, "utc": stamp, "event": kind,
                         "in_intervention": ep["start_utc"] <= stamp < ep["end_utc"],
                         "archive_boundary_method": "FROZEN_S0_ROUTE_EVENT_OR_LANE_RECONSTRUCTION",
                         "stationary_service_s": None, "physical_rejoin_utc": None,
                         "pit_decision_utc": None, "stay_out_counterfactual": None})
        visits[name].extend((d, t, None) for d, t in pending.items())
    return rows, visits


def overlap(start, end, intervals):
    result = defaultdict(float)
    for lo, hi, state in intervals:
        seconds = (min(hi, end)-max(start, lo)).total_seconds()
        if seconds > 0:
            result[state] += seconds
    accounted = sum(result.values())
    if accounted > (duration := (end-start).total_seconds()) + 1e-6:
        raise ValueError("Historical track intervals must be disjoint")
    if duration - accounted > 1e-6:
        result["UNKNOWN"] += duration - accounted
    return dict(result)


def _duration(value):
    return value if value is not None and isfinite(value) and value > 0 else None


def _pit_affected(start, end, driver, is_pit_out, visits):
    return bool(is_pit_out or any(
        d == driver and (left is None or left < end) and (right is None or right > start)
        for d, left, right in visits
    ))


def timing_observations(sources, episodes, archive, visits, tolerance):
    result = []
    lap_rows = {}
    timestamp_checks = []
    state_windows = sorted((e for e in episodes if e["race"] == "Imola"), key=lambda e: e["start_utc"])
    spans = [(e["start_utc"], e["end_utc"], e["status"]) for e in state_windows]
    # Only add GREEN where Stage 1's adjacent archived boundary or track
    # message establishes it. Earlier unknown periods stay unknown.
    spans.extend((a["end_utc"], b["start_utc"], "GREEN") for a, b in itertools.pairwise(state_windows)
                 if a["archived_release_utc"] and b["previous_status"] == "GREEN")
    first = state_windows[0]
    if first["previous_status"] == "GREEN" and first["last_observed_global_green_utc"]:
        spans.append((first["last_observed_global_green_utc"], first["start_utc"], "GREEN"))
    last = state_windows[-1]
    if last["archived_release_utc"]:
        # Green as of clear, within the frozen post-episode observation horizon.
        spans.append((last["end_utc"], last["end_utc"] + timedelta(seconds=200), "GREEN"))
    for ep in [e for e in episodes if e["status"] in ("SC", "VSC")]:
        name = ep["episode"]
        timing = sources.laps.get(ep["status"].lower()+"_timing", ())
        seen = set()
        reference = {(r["driver"], r["completed_laps"]): clock(r["utc"])
                     for r in archive[name]["observed_lines"]}
        for r in timing:
            key = r["driver_number"], r["lap_number"]
            if key in seen:
                raise ValueError(f"Ambiguous source lap {key} in {name}")
            seen.add(key)
            start = clock(r["date_start"])
            elapsed = _duration(r.get("lap_duration"))
            end = start + timedelta(seconds=elapsed) if start and elapsed else None
            pieces = tuple(_duration(r.get(f"duration_sector_{i}")) for i in range(1, 4))
            sectors_valid = bool(elapsed and all(pieces) and abs(sum(pieces)-elapsed) <= tolerance)
            if end and key in reference:
                timestamp_checks.append({"episode": name, "driver": key[0], "lap": key[1], "raw_end_utc": end,
                                         "frozen_s0_line_utc": reference[key],
                                         "delta_s": (end-reference[key]).total_seconds(),
                                         "independent_sources": False})
            lap_rows[name, *key] = {"start": start, "end": end, "lap": key[1], "pieces": pieces if sectors_valid else (),
                                    "pit_out": r["is_pit_out_lap"], "driver": key[0]}
            segments = [("LAP", start, end, elapsed)]
            boundary = start
            for i, value in enumerate(pieces, 1):
                next_marker = boundary + timedelta(seconds=value) if boundary and value else None
                if not sectors_valid:
                    next_marker = None
                segments.append((f"S{i}", boundary if sectors_valid else None, next_marker, value))
                boundary = next_marker
            for segment, lo, hi, seconds in segments:
                states = overlap(lo, hi, spans) if lo and hi else {}
                state_seconds = states.get(ep["status"], 0.)
                if not lo or not hi:
                    phase = "INCOMPLETE_OR_UNRESOLVED"
                elif _pit_affected(start, end, key[0], r["is_pit_out_lap"], visits[name]):
                    phase = "PIT_AFFECTED"
                elif lo < ep["end_utc"] < hi or (ep["announcement_utc"] and
                        ep["announcement_utc"] <= lo < ep["end_utc"]):
                    phase = "RESTART_TRANSITION"
                elif len(states) == 1 and state_seconds:
                    phase = "ENTIRELY_NEUTRALIZED"
                elif state_seconds:
                    phase = "PARTIALLY_NEUTRALIZED"
                elif hi <= ep["start_utc"]:
                    phase = "PRE_GREEN" if set(states) == {"GREEN"} else "PRE_STATUS_UNRESOLVED_OR_LOCAL"
                else:
                    phase = "POST_GREEN" if set(states) == {"GREEN"} else "POST_STATUS_UNRESOLVED"
                result.append({"episode": name, "driver": key[0], "lap": key[1], "segment": segment,
                               "start_utc": lo, "end_utc": hi, "duration_s": seconds, "overlap_s": states,
                               "population": phase, "pit_out": r["is_pit_out_lap"],
                               "sector_clock_method": "LAP_START_PLUS_SECTOR_DURATIONS" if segment != "LAP" else None,
                               "observation_available_by_utc": end,
                               "receipt_time_known": False, "tire_compound": None, "tire_age_laps": None})
    return result, lap_rows, timestamp_checks


def _timing_bound(rows, start, driver, cutoff, markers, length):
    current = max((r for (ep, d, _), r in rows.items() if ep == start and d == driver and
                   r["start"] and r["start"] <= cutoff), key=lambda r: r["start"], default=None)
    if current is None:
        return None, None
    if current["end"] is None or current["end"] < cutoff or not current["pieces"] or not markers:
        return current, None
    ends = [0., *[sum(current["pieces"][:i]) for i in range(1, 4)]]
    boundaries = [*markers, length]
    elapsed = (cutoff-current["start"]).total_seconds()
    for i, (lo, hi) in enumerate(itertools.pairwise(ends)):
        if lo <= elapsed <= hi:
            return current, (boundaries[i], boundaries[i+1])
    return current, None


def position_at(trace, geometry, cutoff, age_limit, lap, route, last_fix):
    if last_fix is None:
        return None, "NO_LOCATION_BEFORE_CUTOFF"
    if (cutoff-last_fix).total_seconds() > age_limit:
        return None, "STALE_LOCATION_NO_CACHE_WINDOW"
    if geometry is None:
        return None, "GEOMETRY_UNAVAILABLE"
    if not lap:
        return None, "NO_LAP_ALIGNMENT_INPUT"
    if route != "RACING":
        return None, "PIT_OR_ROUTE_AMBIGUITY"
    estimate = estimate_from_trace(geometry, trace, cutoff, LocalizationMode.POINT_IN_TIME)
    if estimate.progress_m is None:
        return None, estimate.reason
    elapsed = (cutoff-lap["start"]).total_seconds()
    if estimate.progress_m > 105*elapsed + 35:
        return None, "CONTROL_LINE_ALIGNMENT_UNCERTAIN"
    straddles_line = min(estimate.progress_m, geometry.length_m-estimate.progress_m) < estimate.uncertainty_m
    aligned_at_line = 0 <= elapsed <= .9 and abs(signed_loop_delta(0, estimate.progress_m, geometry.length_m)) <= 35
    if straddles_line and not aligned_at_line:
        return None, "UNCERTAINTY_STRADDLES_TIMING_LINE"
    return estimate, None


def localization(sources, episodes, archive, scenario, lap_rows, visits, routes, geometry_file, age_limit):
    document = _load_json(geometry_file) if geometry_file.exists() else {}
    geometry = TrackGeometry(document["version"], document["length_m"], clock(document["available_at_utc"]),
                             tuple(map(tuple, document["points_xy"])), document["reference_end_gap_m"]) \
        if document else None
    markers = document.get("sector_markers_m")
    raw = {"existing": sources.old_location, "combined": {}}
    raw["combined"] = {d: sources.old_location.get(d, {}) | sources.new_location.get(d, {})
                       for d in sources.old_location.keys() | sources.new_location.keys()}
    traces = {group: {d: map_fixes(geometry, (v for v in fixes.values() if v)) if geometry else ()
                      for d, fixes in by_car.items()} for group, by_car in raw.items()}
    clocks = {group: {d: tuple(sorted(at for at, fix in fixes.items() if fix)) for d, fixes in by_car.items()}
              for group, by_car in raw.items()}
    result = []
    for ep in (e for e in episodes if e["status"] in ("VSC", "SC")):
        name = ep["episode"]
        roster = [c["driver_number"] for c in scenario[name]["initial"]["cars"]]
        cutoffs = {"initial": clock(scenario[name]["initial"]["race_start_utc"]),
                   "deployment": ep["start_utc"], "announcement": ep["announcement_utc"],
                   "global_clear": ep["archived_release_utc"], "withdrawal_proxy": ep["withdrawal_proxy_utc"]}
        relevant = archive[name]["observed_lines"]
        if ep["archived_release_utc"]:
            for driver in roster:
                future = [clock(m["utc"]) for m in relevant if m["driver"] == driver and
                          clock(m["utc"]) >= ep["end_utc"]]
                if future:
                    cutoffs[f"next_line_{driver}"] = min(future)
        for label, cutoff in cutoffs.items():
            if cutoff is None:
                continue
            for driver in roster:
                last_line = max((m for m in relevant if m["driver"] == driver and clock(m["utc"]) <= cutoff),
                                key=lambda r: clock(r["utc"]), default=None)
                lap, bound = _timing_bound(lap_rows, name, driver, cutoff, markers, geometry.length_m) \
                    if geometry else (None, None)
                route_rows = [x for x in routes if x["episode"] == name and x["driver"] == driver and x["utc"] <= cutoff]
                last_route = max(route_rows, key=lambda r: r["utc"], default=None)
                route = ("PIT_OR_OFF_TRACK" if last_route and last_route["event"] != "PIT_EXIT" else "RACING")
                if bound and _pit_affected(lap["start"], lap["end"], driver, lap["pit_out"], visits[name]):
                    bound = None
                for group in raw:
                    observations = clocks[group].get(driver, ())
                    i = bisect_right(observations, cutoff)
                    before = observations[i-1] if i else None
                    after = observations[i] if i < len(observations) else None
                    if before and (cutoff-before).total_seconds() <= age_limit:
                        prefix = traces[group].get(driver, ())[max(0, i-2):i]
                    else:
                        prefix = ()
                    estimate, reason = position_at(prefix, geometry, cutoff, age_limit, lap, route, before)
                    laps = lap["lap"]-1 if lap else last_line["completed_laps"] if last_line else None
                    tier = "H0" if estimate else "H2" if laps is not None else "H3"
                    result.append({"episode": name, "cutoff": label, "cutoff_utc": cutoff, "driver": driver,
                                   "source_set": group, "tier_event_clock": tier,
                                   "tier_retrospective": "H1" if bound and not estimate else tier,
                                   "first_exclusion": reason, "required_data": "FRESH_LOCATION_GEOMETRY_LAP_ROUTE",
                                   "location_before_utc": before, "location_after_utc_diagnostic_only": after,
                                   "age_s": (cutoff-before).total_seconds() if before else None,
                                   "location_prefix_0_9_s": i-bisect_right(
                                       observations, cutoff-timedelta(seconds=age_limit, microseconds=1)),
                                   "last_frozen_line_utc": clock(last_line["utc"]) if last_line else None,
                                   "completed_laps": laps, "route": route, "route_evidence": "ARCHIVED_EVENT_CLOCK",
                                   "retrospective_pit_out_flag": lap["pit_out"] if lap else None,
                                   "retrospective_sector_low_m": bound[0] if bound else None,
                                   "retrospective_sector_high_m": bound[1] if bound else None,
                                   "sector_information_available_by_utc": lap["end"] if bound else None,
                                   "estimated_progress_m": estimate.progress_m if estimate else None,
                                   "position_uncertainty_m": estimate.uncertainty_m if estimate else None,
                                   "latest_observed_utc": estimate.latest_observation_utc if estimate else None,
                                   "projection_quality": estimate.quality if estimate else None,
                                   "predecessor": None, "predecessor_reason": None,
                                   "ahead_gap_m": None, "ahead_gap_lower_m": None, "ahead_gap_upper_m": None,
                                   "receipt_time_known": False})
    if geometry:
        _add_neighbors(result, geometry)
    return result, document


def _add_neighbors(rows, geometry):
    snapshots = defaultdict(list)
    for row in rows:
        snapshots[row["episode"], row["cutoff"], row["source_set"]].append(row)
    for group in snapshots.values():
        if any(r["tier_event_clock"] != "H0" for r in group):
            for r in group:
                r["predecessor_reason"] = "INCOMPLETE_ROSTER_LOCALIZATION"
            continue
        cars = tuple(TrackPosition(r["driver"], PhysicalRoute.RACING, r["completed_laps"], None,
                                   r["estimated_progress_m"], r["position_uncertainty_m"],
                                   quality=r["projection_quality"], observed_at_utc=r["latest_observed_utc"])
                     for r in group)
        neighbors = physical_neighborhoods(PhysicalSnapshot(9987, group[0]["cutoff_utc"],
                                                            geometry.version, geometry.length_m, cars))
        for r in group:
            neighborhood = neighbors[r["driver"]]
            r["predecessor_reason"] = neighborhood.ahead_reason
            if ahead := neighborhood.ahead:
                r.update(predecessor=ahead.driver_number, ahead_gap_m=ahead.gap_m,
                         ahead_gap_lower_m=ahead.lower_m, ahead_gap_upper_m=ahead.upper_m)
