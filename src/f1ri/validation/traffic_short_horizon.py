"""Build archived short traversal targets without changing the lap benchmark."""
from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import timedelta
from itertools import pairwise

import pandas as pd

from f1ri.features.track_geometry import TrackGeometry, forward_passage_brackets, map_fixes
from f1ri.ingestion.telemetry import TelemetryWindow, load_telemetry_window
from f1ri.validation.proximity_pilot import _fixes
from f1ri.validation.traffic_acquisition import ReplaySpan
from f1ri.validation.traffic_coverage_pilot import merge_source_frames
from f1ri.validation.traffic_encounter_audit import window_rows
from f1ri.validation.traffic_exposures import LapEligibilityAudit, exposure_state
from f1ri.validation.traffic_history_study import _sha
from f1ri.validation.traffic_target_audit import audited_outcomes


def sector_boundaries(start, durations):
    boundaries = [start]
    for duration in durations:
        if pd.isna(duration) or duration <= 0:
            break
        boundaries.append(boundaries[-1] + timedelta(seconds=duration))
    return boundaries


def crossings(trace, marker, length):
    return [{"at": at, "available": max(left.fix.availability, right.fix.availability),
             "bracket_s": (right.fix.at_utc - left.fix.at_utc).total_seconds()}
            for at, left, right in forward_passage_brackets(trace, marker % length, length)]


def _nearest(events, expected, tolerance):
    matches = [r for r in events if abs((r["at"] - expected).total_seconds()) <= tolerance]
    return matches[0] if len(matches) == 1 else None


def cutoff_state(stream, clocks, cutoff, config):
    i = bisect_right(clocks, cutoff) - 1
    if i < 0 or (cutoff - clocks[i]).total_seconds() > config["sampling"]["maximum_cutoff_sample_age_seconds"]:
        return {}, "PHYSICAL_STATE_UNRESOLVED", None
    current = stream[i]
    raw = current["state"]
    if raw == "UNSUPPORTED":
        if current["track_state"] in ("SC", "VSC") and current["ahead"] is not None:
            return current, "QUEUE_CONSTRAINED", None
        return current, "PHYSICAL_STATE_UNRESOLVED", None
    j = bisect_right(clocks, cutoff - timedelta(seconds=3)) - 1
    prior = stream[j] if j >= 0 and (cutoff - clocks[j]).total_seconds() <= 3.5 else {}
    closing = (prior["gap_m"] - current["gap_m"]) / (clocks[i] - clocks[j]).total_seconds() if (
        prior.get("ahead") == current["ahead"] and prior.get("state") not in (None, "UNSUPPORTED")
    ) else None
    if raw == "CLOSE":
        return current, "CLOSE_SUPPORTED", closing
    if raw == "CLEAR":
        return current, "CLEAR", closing
    if current["gap_lower_m"] <= config["entry_upper_m"]:
        return current, "CLOSE_UNCERTAIN", closing
    if closing is not None and closing >= 10 / 3 and current["gap_upper_m"] <= 225:
        return current, "APPROACHING", closing
    return current, "INTERMEDIATE", closing


def screen_support(stream, clocks, start, end, cutoff, initial, group, period, config):
    span = (end - start).total_seconds()
    samples = stream[bisect_left(clocks, start):bisect_left(clocks, end)]
    usable = [r for r in samples if r["target_supported"]]
    follower = initial.get("ahead")
    follow = [r for r in usable if r["state"] == "CLOSE" and r["ahead"] == follower]
    gaps = [(b - a).total_seconds() for a, b in pairwise([start, *[r["as_of_utc"] for r in usable], end])]
    longest = max(gaps, default=span)
    ongoing = group == "CLOSE_SUPPORTED"
    continuous = 0.
    for row in stream[bisect_right(clocks, cutoff) - 1:bisect_left(clocks, end)] if ongoing else ():
        if not row["target_supported"] or row["state"] != "CLOSE" or row["ahead"] != follower:
            break
        continuous = max(0., (row["as_of_utc"] - cutoff).total_seconds() + period)
    changes = [r for r in usable if follower is not None and r["ahead"] != follower]
    route_breaks = [r for r in samples if r["follower_retro_route"] != "RACING"
                    or r["leader_retro_route"] not in ("RACING", None)]
    return {
        "support_fraction": min(1., len(usable) * period / span),
        "full_field_fraction": min(1., sum(r["coverage"] == "COMPLETE" for r in samples) * period / span),
        "clear_fraction": sum(r["state"] == "CLEAR" for r in usable) / len(usable) if usable else 0.,
        "close_exposure_s": len(follow) * period, "same_leader_close_at_start_s": min(span, continuous),
        "longest_gap_s": longest, "route_continuous": bool(samples) and not route_breaks,
        "first_route_interruption_utc": route_breaks[0]["as_of_utc"] if route_breaks else None,
        "neighbor_changed": bool(changes), "first_neighbor_change_utc": changes[0]["as_of_utc"] if changes else None,
        "first_unsupported_sample_utc": next((r["as_of_utc"] for r in samples if not r["target_supported"]), None),
        "mid_interval_close": any(r["state"] == "CLOSE" for r in usable) and group != "CLOSE_SUPPORTED",
        "gap_opened_after_cutoff": group == "CLOSE_SUPPORTED" and any(
            r["ahead"] == follower and r["gap_upper_m"] > 155 for r in usable if r["as_of_utc"] >= cutoff),
        "support_pass": bool(usable and len(usable) * period / span >= config["minimum_support_fraction"]
                             and longest <= config["maximum_unsupported_gap_seconds"]),
    }


def _stream(folder, window, exposure):
    pit = "POINT_IN_TIME"
    retro = "RETROSPECTIVE"
    neighbors = window_rows(folder, "physical_neighbors", [window], [pit, retro])
    positions = window_rows(folder, "physical_positions", [window], [pit, retro])
    route = {(r["driver_number"], r["as_of_utc"]): r["route"] for r in neighbors if r["mode"] == retro}
    local = {(r["driver_number"], r["as_of_utc"]): r for r in positions if r["mode"] == pit}
    streams = defaultdict(list)
    for r in neighbors:
        if r["mode"] != pit:
            continue
        at, driver, ahead = r["as_of_utc"], r["driver_number"], r["ahead"]
        own_route, other_route = route.get((driver, at)), route.get((ahead, at))
        state = exposure_state(r, exposure)
        own = local[driver, at]
        streams[driver].append(r | {"state": state, "follower_retro_route": own_route,
                                   "leader_retro_route": other_route, "quality": own["quality"],
                                   "progress_m": own["progress_m"], "uncertainty_m": own["uncertainty_m"],
                                   "target_supported": state != "UNSUPPORTED" and own_route == other_route == "RACING"})
    return {driver: sorted(rows, key=lambda r: r["as_of_utc"]) for driver, rows in streams.items()}


def _append_candidate(rows, key, geometry, window, lap, horizon, checkpoint, origin, cutoff, end,
                      available, clocks, stream, config, d8_index, timing):
    sampling = config["sampling"]
    initial, group, closing = cutoff_state(stream, clocks, cutoff, config)
    observed = end is not None and end <= clocks[-1] and end > cutoff
    support = screen_support(stream, clocks, origin, end, cutoff, initial, group, .5, sampling) if observed else {}
    dry_green = lap.track_status == "GREEN" and lap.confirmed_green_lap and not lap.rainfall
    clean_event = not (lap.is_pit_lap or lap.stint_pit_mismatch or lap.had_in_lap_on_track_pass)
    timing_pass = observed and timing["valid"]
    eligible = bool(timing_pass and dry_green and clean_event and support["route_continuous"] and support["support_pass"])
    clean = eligible and group == "CLEAR" and support["clear_fraction"] >= sampling["minimum_clear_fraction"]
    following = eligible and group == "CLOSE_SUPPORTED" and not support["neighbor_changed"] and (
        support["same_leader_close_at_start_s"] >= sampling["cutoff_close_continuation_seconds"])
    start_lap = pd.Timestamp(lap.lap_start_utc)
    d8 = d8_index.get((lap.driver_number, lap.lap_number), {})
    leader_lap = initial.get("completed_laps")
    leader_ref = (d8_index.get((initial.get("ahead"), leader_lap + 1), {})
                  if leader_lap is not None and initial.get("encounter") == "SAME_LAP" else {})
    leader_time = leader_ref.get("cutoff_utc")
    relative_pace = (leader_ref["expected_clean_s"] - d8["expected_clean_s"]
                     if leader_time and pd.Timestamp(leader_time) <= cutoff
                     and leader_ref.get("d8_status") == d8.get("d8_status") == "SUPPORTED" else None)
    identity = f"{key}/{lap.driver_number}/{lap.lap_number}/{horizon}/{checkpoint}"
    if horizon.startswith("H3"):
        identity += f"/{cutoff.isoformat()}"
    outcome = {"completed": observed, "timing_pass": timing_pass, "green_dry": bool(dry_green),
               "no_disqualifying_event": bool(clean_event), "observed_s": (end - origin).total_seconds() if observed else None,
               "end_utc": end if observed else None, "available_by_utc": available if observed else None,
               "clean_control": clean, "following_label": following, "archive_pass": bool(lap.had_in_lap_on_track_pass),
               "pit_or_stint_transition": bool(lap.is_pit_lap or lap.stint_pit_mismatch),
               "first_observed_interruption_utc": min((x for x in (
                   support.get("first_route_interruption_utc"), support.get("first_unsupported_sample_utc"),
                   support.get("first_neighbor_change_utc") if group == "CLOSE_SUPPORTED" else None,
               ) if x is not None), default=None)}
    failure = next((reason for passed, reason in (
        (group != "PHYSICAL_STATE_UNRESOLVED", "CUTOFF_UNSUPPORTED"),
        (observed, "INCOMPLETE_TRAVERSAL"), (timing_pass, "TIMING_UNRESOLVED"), (dry_green, "NON_GREEN_OR_WET"),
        (clean_event, "LAP_LEVEL_PIT_OR_PASS_CENSOR"), (support.get("route_continuous"), "ROUTE_INTERRUPTION"),
        (support.get("support_pass"), "PHYSICAL_SUPPORT"), (clean or following, "EXPOSURE_NOT_ELIGIBLE"),
    ) if not passed), None)
    if failure == "EXPOSURE_NOT_ELIGIBLE" and group == "CLOSE_SUPPORTED" and support["neighbor_changed"]:
        failure = "NEIGHBOR_CHANGED"
    rows.append({
        "opportunity_id": identity, "session_key": key, "driver_number": int(lap.driver_number),
        "lap_number": int(lap.lap_number), "stint_number": d8.get("stint_number"),
        "horizon_type": horizon, "checkpoint": checkpoint, "target_start_checkpoint": checkpoint,
        "target_end_checkpoint": checkpoint + 1, "window": window, "as_of_utc": cutoff,
        "target_start_utc": origin, "geometry_version": geometry.version,
        "expected_duration_s": config["expected_seconds"][horizon],
        "driver_route": initial.get("route"), "physical_leader": initial.get("ahead"),
        "initial_gap_m": initial.get("gap_m"), "gap_lower_m": initial.get("gap_lower_m"),
        "gap_upper_m": initial.get("gap_upper_m"), "lap_relationship": initial.get("encounter"),
        "localization_quality": initial.get("quality"), "source_staleness_s": initial.get("source_age_s"),
        "track_status": initial.get("track_state"), "cutoff_group": group, "closing_rate_mps": closing,
        "compound": d8.get("compound"), "tyre_age": d8.get("tire_age"),
        "track_temperature_c": float(lap.track_temperature_c) if pd.notna(lap.track_temperature_c) else None,
        "race_progress": lap.lap_number / config["scheduled_laps"], "lap_start_utc": start_lap,
        "prior_clean_history_count": d8.get("prior_clean"), "pace_reference_status": d8.get("d8_status") or "UNATTEMPTED",
        "d8_lap_s": d8.get("expected_clean_s"), "d8_lap_std_s": d8.get("clean_std_s"),
        "relative_clean_pace_s": relative_pace,
        "timing": timing, "support": support,
        "outcome": outcome | {"episode_id": initial.get("episode_id"),
                              "retrospective_stint_number": int(lap.stint_number) if pd.notna(lap.stint_number) else None,
                              "full_lap_observed_s": None if pd.isna(lap.observed_lap_time_s) else lap.observed_lap_time_s,
                              "stage1_clean_lap": bool(lap.is_clean_green_pace_lap)}, "first_failure": failure,
    })


def build_race(root, transition, history, key, config, protocol, d8_index, inputs, *, sectors_only=False):
    rows, geometry_manifest, source_hashes = [], {}, {}
    geometry_file = transition / str(key) / "geometry.json"
    raw = json.loads(geometry_file.read_text(encoding="utf8"))
    geometry = TrackGeometry(raw["version"], raw["length_m"], pd.Timestamp(raw["available_at_utc"]),
                             tuple(map(tuple, raw["points_xy"])), raw["reference_end_gap_m"])
    sector_markers = [0, *raw["sector_markers_m"][1:], 0]
    distance_markers = [geometry.length_m * f for f in config["sampling"]["distance_markers_fraction"]]
    geometry_manifest.update(version=geometry.version, sha256=_sha(geometry_file), sector_markers_m=sector_markers,
                             distance_markers_m=distance_markers, length_m=geometry.length_m)
    availability = lap_evidence_clocks(inputs)
    overlap = []
    for run in (transition, history):
        folder = run / str(key)
        if _sha(folder / "geometry.json") != geometry_manifest["sha256"]:
            raise ValueError("Cached development windows do not share frozen geometry")
        archive = json.loads((folder / "acquisition.json").read_text(encoding="utf8"))
        for window in archive["windows"]:
            name = window["name"]
            span = ReplaySpan(key, pd.Timestamp(window["telemetry"]["start_utc"]), pd.Timestamp(window["telemetry"]["end_utc"]))
            overlap.append((name, span.start_utc, span.end_utc))
            locations = []
            for source in window["sources"]:
                path = source["cache_path"]
                if path not in source_hashes:
                    source_hashes[path] = _sha(root / path)
                if source_hashes[path] != source["sha256"]:
                    raise ValueError(f"Frozen telemetry changed: {path}")
                if source["endpoint"] == "location":
                    frame, _ = load_telemetry_window(root, TelemetryWindow(
                        key, None, pd.Timestamp(source["start_utc"]), pd.Timestamp(source["end_utc"]),
                    ), "location")
                    locations.append(frame)
            fixes, _ = merge_source_frames(locations, span)
            streams = _stream(folder, name, protocol["exposure"])
            episode_at = {(r["driver_number"], r["as_of_utc"]): r["episode_id"]
                          for r in window_rows(folder, "traffic_segments", [name])}
            for driver, frame in fixes.groupby("driver_number"):
                if driver not in streams:
                    continue
                stream = streams[driver]
                clocks = [r["as_of_utc"] for r in stream]
                for r in stream:
                    r["episode_id"] = episode_at.get((driver, r["as_of_utc"]))
                trace = map_fixes(geometry, _fixes(frame))
                markers = set(sector_markers if sectors_only else sector_markers + distance_markers)
                crossing_index = {m: crossings(trace, m, geometry.length_m) for m in markers}
                alternate = [] if sectors_only else [
                    {m: crossings(trace[offset::2], m, geometry.length_m) for m in distance_markers}
                    for offset in (0, 1)
                ]
                own = inputs["laps"][inputs["laps"].driver_number.eq(driver)].sort_values("lap_start_utc")
                lap_rows = list(own.itertuples(index=False))
                for lap, next_lap in pairwise(lap_rows):
                    if lap.lap_start_utc >= span.end_utc or next_lap.lap_start_utc < span.start_utc:
                        continue
                    def add(horizon, checkpoint, start, cutoff, end, available, timing, *,
                            lap=lap, next_lap=next_lap, clocks=clocks, stream=stream, name=name, driver=driver):
                        if clocks[0] <= cutoff <= clocks[-1]:
                            _append_candidate(rows, key, geometry, name, lap, horizon, checkpoint, start, cutoff,
                                              end, max(available, availability.get((driver, lap.lap_number),
                                                                                  next_lap.lap_start_utc)) if available else None,
                                              clocks, stream, config, d8_index, timing)
                    timing_rows(lap, next_lap, sector_markers, [] if sectors_only else distance_markers,
                                crossing_index, config, add, alternate)
                if sectors_only:
                    continue
                starts = list(own.lap_start_utc)
                for i, at in enumerate(clocks):
                    if at.timestamp() % config["sampling"]["diagnostic_stride_seconds"]:
                        continue
                    j = bisect_right(starts, at) - 1
                    if not 0 <= j < len(lap_rows) - 1:
                        continue
                    lap, next_lap = lap_rows[j:j+2]
                    for seconds in config["sampling"]["diagnostic_seconds"]:
                        end = at + timedelta(seconds=seconds)
                        endpoint = stream[bisect_left(clocks, end)] if end <= clocks[-1] else {}
                        progress = (endpoint["progress_m"] - stream[i]["progress_m"]) % geometry.length_m if (
                            endpoint.get("progress_m") is not None and stream[i]["progress_m"] is not None) else None
                        _append_candidate(rows, key, geometry, name, lap, f"H3_{seconds}", 0, at, at, end,
                                          max(end, next_lap.lap_start_utc), clocks, stream, config, d8_index,
                                          {"valid": progress is not None and progress <= seconds * 105
                                           and end < next_lap.lap_start_utc,
                                           "progress_m": progress, "diagnostic_only": True})
    windows = {name: (start, end) for name, start, end in overlap}
    # Pick one replay by planned time margin only, never by the observed target result.
    chosen = {}
    for row in rows:
        identity = row["opportunity_id"]
        start, end = windows[row["window"]]
        priority = (end >= row["as_of_utc"] + timedelta(seconds=row["expected_duration_s"]),
                    min((row["as_of_utc"] - start).total_seconds(), 8), row["window"])
        if identity not in chosen or priority > chosen[identity][0]:
            chosen[identity] = (priority, row)
    return [chosen[k][1] for k in sorted(chosen)], geometry_manifest, source_hashes


def lap_evidence_clocks(inputs):
    laps = inputs["laps"].sort_values("lap_start_utc")
    own = {(r.driver_number, r.lap_number): r.lap_start_utc for r in laps.itertuples(index=False)}
    candidates, audit = [], LapEligibilityAudit()
    for lap in laps.itertuples(index=False):
        next_line = own.get((lap.driver_number, lap.lap_number + 1))
        if next_line is None or pd.isna(lap.lap_end_estimate_utc):
            continue
        identity = (lap.driver_number, lap.lap_number)
        candidates.append({"opportunity_id": identity, "completed_utc": lap.lap_end_estimate_utc})
        audit.outcomes.append({"opportunity_id": identity, "as_of_utc": lap.lap_start_utc,
                               "next_line_utc": next_line, "screen": "CLEAN_CONTROL"})
    return {r["opportunity_id"]: r["outcome_available_by_utc"] for r in audited_outcomes(candidates, audit, inputs)}


def timing_rows(lap, next_lap, sector_markers, distance_markers, index, config, add, alternate=()):
    start, end = lap.lap_start_utc, next_lap.lap_start_utc
    max_error = config["sampling"]["maximum_sector_marker_disagreement_seconds"]
    bounds = sector_boundaries(start, [lap.sector_1_s, lap.sector_2_s, lap.sector_3_s])
    for i, (a, b) in enumerate(pairwise(bounds)):
        if b <= a:
            continue
        first = _nearest(index[sector_markers[i]], a, max_error)
        last = _nearest(index[sector_markers[i + 1]], b, max_error)
        errors = [(event["at"] - clock).total_seconds() if event else None
                  for event, clock in ((first, a), (last, b))]
        timing = {"valid": bool(first and last and (i != 2 or abs((b - end).total_seconds()) <= max_error)),
                  "start_error_s": errors[0], "end_error_s": errors[1],
                  "timing_error_s": errors[1] - errors[0] if None not in errors else None,
                  "start_bracket_s": first["bracket_s"] if first else None,
                  "end_bracket_s": last["bracket_s"] if last else None}
        add("H1", i, a, a, b, max(b, last["available"]) if last else b, timing)
    duration = (end - start).total_seconds()
    if not 30 < duration < 160:
        return
    events = []
    for i, marker in enumerate(distance_markers):
        if i in (0, len(distance_markers) - 1):
            event = _nearest(index[marker], start if i == 0 else end, max_error)
        else:
            within = [r for r in index[marker] if start < r["at"] < end]
            event = within[0] if len(within) == 1 else None
        events.append(event)
    for i, (a, b) in enumerate(pairwise(events)):
        if a is None:
            continue
        valid = bool(b and b["at"] > a["available"])
        check = None
        if valid and alternate:
            estimates = []
            for sample in alternate:
                left = _nearest(sample[distance_markers[i]], a["at"], max_error)
                right = _nearest(sample[distance_markers[i + 1]], b["at"], max_error)
                if left and right:
                    estimates.append((right["at"] - left["at"]).total_seconds())
            if len(estimates) == 2:
                check = estimates[0] - estimates[1]
        add("H2", i, a["at"], a["available"], b["at"] if valid else None,
            b["available"] if valid else None,
            {"valid": valid, "start_bracket_s": a["bracket_s"], "end_bracket_s": b["bracket_s"] if b else None,
             "trigger_latency_s": (a["available"] - a["at"]).total_seconds(),
             "timing_error_s": check, "timing_check": "DISJOINT_FIX_REPEATABILITY"})
