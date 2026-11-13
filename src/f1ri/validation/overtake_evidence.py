"""Audit independent timing evidence and the earliest loss of pair support."""
from bisect import bisect_right
from datetime import datetime, timedelta
from itertools import pairwise

from f1ri.features.overtake_events import racing_position, relative_gap
from f1ri.ingestion.raw_cache import read_snapshot

PROJECTION = {"AMBIGUOUS_SEGMENT", "NO_PLAUSIBLE_SEGMENT", "UNMAPPED_LOCATION"}
MISSING = {"MISSING_LOCATION_PREFIX", "WINDOW_START_NO_FIX"}


def utc(value):
    at = datetime.fromisoformat(value) if isinstance(value, str) else value
    if at.utcoffset() is None:
        raise ValueError("Archived observations require a timezone")
    return at


def audit_event(event, config, manifest, history, root):
    key = event["session"]
    attacker, target = event["attacker"], event["target"]
    lo, hi = map(utc, (event["event_time_lower_utc"], event["event_time_upper_utc"]))
    rank_rows = config["official_pairs"][f"{key}/{attacker}-{target}"]
    lap_flip = rank_rows[0][1] > rank_rows[0][2] and rank_rows[-1][1] < rank_rows[-1][2]
    rank_target_remains_ahead = all(a > b for _, a, b in rank_rows)
    markers = event["evidence"]["timing_pair_order"]
    timing_flip = any(not r["attacker_first"] and max(utc(r["attacker_utc"]), utc(r["target_utc"])) <= lo
                      for r in markers) and any(
        r["attacker_first"] and min(utc(r["attacker_utc"]), utc(r["target_utc"])) >= hi for r in markers)
    contradictions = []
    for r in markers:
        sector_positions = [event[p]["target"]["progress_m"] for p in ("before", "after")]
        marker = r["marker"]
        if (r["lap"] == event["before"]["target"]["completed_laps"] + 1
                and marker > 0 and utc(r["target_utc"]) < lo
                and all(pos + event[p]["target"]["uncertainty_m"] < manifest["sector_markers_m"][marker]
                        for pos, p in zip(sector_positions, ("before", "after"), strict=True))):
            contradictions.append("TARGET_TIMING_MARKER_ALREADY_PASSED_WHILE_PROJECTION_STILL_BEHIND")
    target_flat = event["before"]["target"]["progress_m"] == event["after"]["target"]["progress_m"]
    if target_flat:
        contradictions.append("TARGET_PROJECTED_PROGRESS_FLAT_ACROSS_BRACKET")
    if rank_target_remains_ahead:
        contradictions.append("TARGET_AHEAD_AT_BOTH_SURROUNDING_CONTROL_LINES")
    nearby_pits = [{"driver": d, "record_utc": stamp, "estimated_interval": [a, b]}
                   for d in (attacker, target) for a, b, stamp in history.pits[d]
                   if a <= hi + timedelta(seconds=45) and b >= lo - timedelta(seconds=45)]
    if nearby_pits:
        contradictions.append("PIT_CYCLE_LIMITS_INDEPENDENT_RANK_ATTRIBUTION")
    # Official ranks and OpenF1 timing are both timing products. Count them
    # together, separately from the location-derived trajectories.
    wide = event["event_time_bracket_s"] > 4 * event["source_period_s"] + 1e-6
    full = event["field_coverage"] == ["COMPLETE", "COMPLETE"]
    interruptions = history.interruptions((attacker, target), lo, hi)
    if interruptions:
        decision = "EXCLUDED"
    elif any("PROJECTION_STILL_BEHIND" in r for r in contradictions):
        decision = "TIER_C_AMBIGUOUS"
    elif full and not wide and lap_flip and timing_flip and not nearby_pits:
        decision = "TIER_A_CONFIRMED"
    else:
        decision = "TIER_B_SUPPORTED"
    source_payloads = [r for r in manifest["payloads"] if utc(r["start_utc"]) <= hi and utc(r["end_utc"]) >= lo
                       and r["driver_number"] in (None, attacker, target)]
    raw_fixes = {}
    for payload in source_payloads:
        if payload["endpoint"] != "location":
            continue
        for fix in read_snapshot(root / payload["cache_path"]):
            at = utc(fix["date"])
            if fix["driver_number"] in (attacker, target) and lo - timedelta(seconds=1) <= at <= hi + timedelta(seconds=1):
                identity = fix["driver_number"], at, fix["x"], fix["y"], fix.get("z")
                raw_fixes.setdefault(identity, set()).add(payload["sha256"])
    target_coordinates = {fix[2:] for fix in raw_fixes if fix[0] == target}
    target_sample_count = sum(fix[0] == target for fix in raw_fixes)
    return event | {
        "decision": decision, "role": manifest["role"], "circuit": manifest["circuit"],
        "initial_physical_front_to_back": [target, attacker], "final_physical_front_to_back": [attacker, target],
        "official_pair_ranks": rank_rows, "official_rank_flip": lap_flip,
        "same_lap_timing_flip": timing_flip, "nearby_pits": nearby_pits,
        "sources": {"geometry_sha256": manifest["files"]["geometry.json"], "payloads": source_payloads,
                    "lap_chart": config["official_lap_charts"][key],
                    "pit_summary": config["official_pit_summaries"][key]},
        "contradictions_or_limits": sorted(set(contradictions + event["exclusions"])),
        "blocked_by_width": wide,
        "target_raw_location_samples": target_sample_count,
        "target_distinct_raw_coordinates": len(target_coordinates),
        "raw_location_timeline": [{"driver": d, "at_utc": at, "x": x, "y": y, "z": z,
                                   "payload_sha256": sorted(hashes)}
                                  for (d, at, x, y, z), hashes in sorted(raw_fixes.items())],
    }


def position_failure(row):
    if not row:
        return "MISSING_POSITION_ROW"
    reason = row.get("reason")
    if reason in MISSING:
        return "MISSING_LOCATION"
    if reason == "STALE_LOCATION":
        return "STALE_OBSERVATION"
    if reason in PROJECTION:
        return "GEOMETRY_PROJECTION_FAILURE"
    if reason == "MISSING_VALID_BRACKET":
        return "INTERPOLATION_SUPPORT_FAILURE"
    if reason == "LAP_EVIDENCE_MISSING_OR_STALE":
        return "MISSING_OR_STALE_TIMING"
    if row.get("route") != "RACING":
        return "PIT_ROUTE" if row.get("route") == "PIT" else "ROUTE_AMBIGUITY"
    if row.get("progress_m") is not None and not row.get("lap_aligned"):
        return "LAP_BOUNDARY_ALIGNMENT"
    return None if racing_position(row) else "UNRESOLVED_POSITION"


def sample_support(at, pair, frames, neighbors, length):
    frame = frames.get(at, {})
    d, target = pair
    a, b = frame.get(d, {}), frame.get(target, {})
    near = neighbors.get(at, {}).get(d, {})
    missing = sorted(n for n, r in frame.items()
                     if r["route"] not in ("PIT", "OFF_TRACK") and r["progress_m"] is None)
    failures = {role: reason for role, row in (("attacker", a), ("target", b))
                if (reason := position_failure(row)) is not None}
    if any(utc(stamp) > at for r in (a, b)
           if (stamp := r.get("observation_utc") or r.get("observed_at_utc"))):
        failures["clock"] = "INCONSISTENT_SOURCE_TIMESTAMP"
    reason = near.get("ahead_reason", near.get("reason"))
    if not failures:
        gap, error = relative_gap(a, b, length)
        if abs(gap) <= error:
            failures["pair"] = "PHYSICAL_UNCERTAINTY_OVERLAP"
        elif near.get("ahead") != target:
            failures["pair"] = (
                "OPPONENT_CHANGED" if near.get("ahead") is not None else
                "AMBIGUOUS_NEAREST_NEIGHBOR" if reason in ("NEAREST_NEIGHBOR_OVERLAP", "CIRCULAR_ORDER_OVERLAP") else
                "MISSING_ACTIVE_FIELD_MEMBER" if missing else "UNRESOLVED_NEIGHBOR"
            )
    return failures, {"pair_positions": [a, b], "neighbor_reason": reason, "missing_active_members": missing}


def first_loss(at, end, pair, frames, neighbors, times, period, length):
    """Record the last verified sample and the next unsupported observation."""
    last = at
    for tick in times[bisect_right(times, at):bisect_right(times, end)]:
        if tick > end:
            break
        if (tick - last).total_seconds() > period * 1.51:
            return last, last + timedelta(seconds=period), {"grid": "SAMPLING_GAP"}, {}
        flags, details = sample_support(tick, pair, frames, neighbors, length)
        if flags:
            return last, tick, flags, details
        last = tick
    if last < end:
        reason = "OBSERVATION_WINDOW_TERMINATION" if not times or last == times[-1] else "BETWEEN_ARCHIVE_TICKS"
        return last, end, {"grid": reason}, {}
    return last, None, {}, {}


def union_duration(spans):
    total = 0.
    end = None
    for lo, hi in sorted(spans):
        if end is None or lo > end:
            end = lo
        if hi > end:
            total += (hi - end).total_seconds()
            end = hi
    return total


def continuous_components(samples, multiplier):
    return 1 + sum((b[0] - a[0]).total_seconds() > multiplier * max(a[1], b[1])
                   for a, b in pairwise(samples)) if samples else 0
