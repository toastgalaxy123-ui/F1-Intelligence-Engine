"""Physical order reversal labels from supported local pair observations."""
from collections import defaultdict
from itertools import combinations, pairwise

from f1ri.features.track_geometry import signed_loop_delta

PASS_TYPES = frozenset({"SAME_LAP_RACING_PASS", "LAPPING_PASS", "UNLAPPING_PASS"})


def racing_position(row):
    return (row.get("route") == "RACING" and row.get("lap_aligned")
            and all(row.get(k) is not None for k in ("progress_m", "uncertainty_m", "completed_laps")))


def relative_gap(a, b, length):
    gap = signed_loop_delta(a["progress_m"], b["progress_m"], length)
    return gap, a["uncertainty_m"] + b["uncertainty_m"]


def lap_advantage(a, b, gap, length):
    return a["completed_laps"] - b["completed_laps"] + round(
        (a["progress_m"] + gap - b["progress_m"]) / length
    )


def track_segment(progress, geometry):
    markers = geometry["sector_markers_m"]
    sector = sum(marker <= progress for marker in markers)
    boundary = markers[sector] if sector < len(markers) else geometry["length_m"]
    return {"progress_m": progress, "sector": sector, "distance_to_boundary_m": boundary - progress,
            "near_start_finish": min(progress, geometry["length_m"] - progress) <= 100,
            "straight_or_corner": None, "drs_state": None}


def pair_points(positions, neighbors, geometry, limit):
    """Keep local pair samples, including uncertain estimates and unresolved adjacency."""
    length = geometry["length_m"]
    by_pair = defaultdict(list)
    for at, frame in sorted(positions.items()):
        localized = [d for d in sorted(frame) if racing_position(frame[d])]
        for a, b in combinations(localized, 2):
            gap, error = relative_gap(frame[a], frame[b], length)
            if abs(gap) > limit:
                continue
            follower, leader = (a, b) if gap >= 0 else (b, a)
            near = neighbors.get(at, {}).get(follower, {})
            by_pair[a, b].append({
                "at": at, "gap": gap, "error": error, "rows": (frame[a], frame[b]),
                "adjacent": near.get("ahead") == leader, "coverage": near.get("coverage"),
                "neighbor_reason": near.get("ahead_reason", near.get("reason")),
                "lap_advantage": lap_advantage(frame[a], frame[b], gap, length),
            })
    return by_pair


def _motion_supported(before, after, length, max_speed):
    dt = (after["at"] - before["at"]).total_seconds()
    for a, b in zip(before["rows"], after["rows"], strict=True):
        delta = (b["completed_laps"] - a["completed_laps"]) * length + b["progress_m"] - a["progress_m"]
        error = a["uncertainty_m"] + b["uncertainty_m"]
        if delta < -error or delta > dt * max_speed + error:
            return False
    return True


def reversal_candidates(points, geometry, config):
    """Bracket sign changes using the nearest resolved endpoints.

    Preserve unresolved center sign changes as diagnostic candidates when no
    resolved bracket covers them. Never use an uncertainty-overlapping sign
    or a center-only adjacency to support a pass.
    """
    limit = config["event_max_bracket_s"]
    for pair, series in sorted(points.items()):
        supported = [row for row in series if abs(row["gap"]) > row["error"] and row["adjacent"]]
        firm = [(lo, hi) for lo, hi in pairwise(supported)
                if lo["gap"] * hi["gap"] < 0 and (hi["at"] - lo["at"]).total_seconds() <= limit]
        uncertain = [(lo, hi) for lo, hi in pairwise(series)
                     if lo["gap"] * hi["gap"] < 0 and (hi["at"] - lo["at"]).total_seconds() <= limit
                     and (lo["adjacent"] or hi["adjacent"])
                     and not any(a["at"] <= lo["at"] < hi["at"] <= b["at"] for a, b in firm)]
        for lo, hi in sorted(firm + uncertain, key=lambda bracket: (bracket[0]["at"], bracket[1]["at"])):
            index = 0 if lo["gap"] > 0 else 1
            direction = 1 if index == 0 else -1
            lower, upper = lo["at"], hi["at"]
            middle = [row for row in series if lower <= row["at"] <= upper]
            reasons = []
            resolved = (lo, hi) in firm
            if not resolved:
                reasons.append("ORDER_BOUNDS_OR_NEIGHBOR_UNRESOLVED")
            if lo["lap_advantage"] != hi["lap_advantage"]:
                reasons.append("LAP_ALIGNMENT_CHANGED")
            if not all(_motion_supported(a, b, geometry["length_m"], config["event_max_speed_mps"])
                       for a, b in pairwise(middle)):
                reasons.append("IMPLAUSIBLE_PROJECTION_JUMP")
            if any(abs(r.get("source_age_s") or 0) > config["event_max_fix_age_s"] for r in (*lo["rows"], *hi["rows"])):
                reasons.append("STALE_ENDPOINT")
            yield {
                "attacker": pair[index], "target": pair[1 - index],
                "event_time_lower_utc": lower, "event_time_upper_utc": upper,
                "event_time_bracket_s": (upper - lower).total_seconds(),
                "last_confirmed_behind_utc": lower if resolved else None,
                "first_confirmed_ahead_utc": upper if resolved else None,
                "event_time_quality": "RESOLVED_INTERVAL" if resolved else "APPARENT_INTERVAL",
                "lap_advantage": lo["lap_advantage"] * direction,
                "field_coverage": [lo["coverage"], hi["coverage"]],
                "gap_trajectory": [{"at_utc": r["at"], "gap_m": r["gap"] * direction,
                                    "uncertainty_m": r["error"], "adjacent": r["adjacent"]} for r in middle],
                "before": {"gap_m": abs(lo["gap"]), "gap_uncertainty_m": lo["error"],
                           "attacker": lo["rows"][index], "target": lo["rows"][1 - index]},
                "after": {"gap_m": -abs(hi["gap"]), "gap_uncertainty_m": hi["error"],
                          "attacker": hi["rows"][index], "target": hi["rows"][1 - index]},
                "segment": track_segment(lo["rows"][index]["progress_m"], geometry),
                "exclusions": reasons,
            }


def classify_reversal(event, interruptions, corroborated, source_period_s):
    reasons = event["exclusions"] + [reason for _, reason in interruptions]
    if interruptions:
        category = {
            "CENSORED_PIT": "PIT_INDUCED_ORDER_CHANGE",
            "CENSORED_RETIREMENT": "RETIREMENT_INDUCED_CHANGE",
            "CENSORED_TRACK_STATE": "RACE_CONTROL_ORDER_CHANGE",
        }[interruptions[0][1]]
        return category, "EXCLUDED", reasons
    if {"LAP_ALIGNMENT_CHANGED", "IMPLAUSIBLE_PROJECTION_JUMP"} & set(reasons):
        return "ARTIFACT_OR_UNRESOLVED", "EXCLUDED", reasons
    if reasons:
        return "UNCERTAIN_PHYSICAL_REVERSAL", "C", reasons
    laps = event["lap_advantage"]
    category = "LAPPING_PASS" if laps > 0 else "UNLAPPING_PASS" if laps < 0 else "SAME_LAP_RACING_PASS"
    limits = []
    if event["field_coverage"] != ["COMPLETE", "COMPLETE"]:
        limits.append("SUBSET_FIELD")
    if not corroborated:
        limits.append("LIMITED_INDEPENDENT_TIMING")
    if event["event_time_bracket_s"] > 4 * source_period_s + 1e-6:
        limits.append("WIDE_OBSERVATION_BRACKET")
    return category, "B" if limits else "A", limits
