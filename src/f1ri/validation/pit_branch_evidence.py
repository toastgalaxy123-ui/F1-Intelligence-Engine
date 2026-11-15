"""Separated fit, held route controls and geometric transition windows."""

from datetime import timedelta
from itertools import pairwise
from math import hypot

from f1ri.features.historical_field import FieldMode, _position, reconstruct_field
from f1ri.features.pit_branch import Route, certificate, match_fix, matched_at
from f1ri.validation.historical_field_sources import stamp


def held_witnesses(graph, session, visit, timing):
    """Use timing labels on a separate visit, plus same-window non-pitting controls."""
    start, end = visit["entry"], visit["exit"]
    if not end or not 10 <= (end-start).total_seconds() <= 75:
        return [], None
    trim = timedelta(seconds=graph.policy["fit_lane_trim_s"])
    low, high = start+trim, end-trim
    other_pits = set()
    time_guard = timedelta(seconds=55)
    for row in timing:
        driver = row.get("driver", row.get("driver_number"))
        at = stamp(row.get("utc", row.get("date")))
        if start-time_guard <= at <= end+time_guard:
            other_pits.add(driver)
    result = []
    for driver, series in sorted(session.fixes.items()):
        is_pit = driver == visit["driver"]
        if not is_pit and driver in other_pits:
            continue
        for row in series.window(low, high):
            if row.conflict:
                continue
            observed = match_fix(graph, row.point)
            if not is_pit and not observed["core_alternative_covered"]:
                continue
            truth = Route.LANE if is_pit else Route.MAIN
            result.append(observed | {"visit": visit["id"], "session": session.key, "driver": driver,
                                      "timing_label": truth, "reference_role": "INDEPENDENT_REFERENCE_HELD_VISIT",
                                      "status": ("IDENTIFIED" if observed["route"] == truth else
                                                "ABSTAINED" if observed["route"] == Route.UNKNOWN else "WRONG_ROUTE")})
    # The witness labels depend on a two-sided timing screen, so its far end is
    # part of the event-time availability of the offline geometry qualification.
    return result, end+time_guard


def qualification(rows, policy):
    stats = {}
    for route, expected_key in ((Route.LANE, "min_pit_witnesses"), (Route.MAIN, "min_racing_witnesses")):
        group = [r for r in rows if r["timing_label"] == route]
        count = len(group)
        identified = sum(r["status"] == "IDENTIFIED" for r in group)
        wrong = sum(r["status"] == "WRONG_ROUTE" for r in group)
        stats[route] = {"witnesses": count, "identified": identified, "wrong": wrong, "fraction": identified/count
                        if count else None, "passed": count >= policy[expected_key] and
                        identified/count >= policy["min_fraction_identified"] and wrong <= policy["max_wrong_route_rows"]}
    return stats


def trajectory(session, visit, graph, mode=FieldMode.RETROSPECTIVE, cutoff=None):
    """Retain both hypotheses for each fix without asserting a geometric boundary."""
    entry, exit_at = visit["entry"], visit["exit"]
    if not graph or not graph.compatible(session) or visit["driver"] not in session.fixes:
        return []
    if (mode is FieldMode.RECEIPT or mode is FieldMode.CAUSAL and
            (cutoff is None or entry > cutoff or graph.verified_through is None or graph.verified_through > cutoff)):
        return []
    if mode is FieldMode.CAUSAL and exit_at and exit_at > cutoff:
        exit_at = None
    before = entry-timedelta(seconds=20)
    after = (exit_at or entry)+timedelta(seconds=20)
    if cutoff is not None:
        after = min(after, cutoff)
    records = session.fixes[visit["driver"]].window(before, after)
    rows = []
    for row in records:
        fix = row.point
        compared = match_fix(graph, fix)
        if row.conflict:
            qualified = Route.UNKNOWN
        else:
            matched = matched_at(graph, session, visit["driver"], fix.at_utc, FieldMode.RETROSPECTIVE)
            qualified = matched["route"]
            # Outside the guard use the existing S7-A directional main-loop
            # projection with a visible prefix, independent of pit timing.
            if qualified == Route.UNKNOWN and (
                min(compared["track_m"], graph.loop.length_m-compared["track_m"]) > 650+11 and
                compared["racing_residual_m"] <= graph.policy["route_radius_m"]
            ):
                physical, _ = _position(session, visit["driver"], fix.at_utc, FieldMode.CAUSAL)
                if physical.progress_m is not None:
                    qualified = Route.MAIN
        rows.append(compared | {"visit": visit["id"], "session": session.key, "driver": visit["driver"],
                                "utc": fix.at_utc, "x": fix.x, "y": fix.y,
                                "seconds_from_lane_entry": (fix.at_utc-entry).total_seconds(),
                                "qualified": qualified, "conflict": row.conflict,
                                "stationary_candidate": (qualified == Route.LANE and
                                                         matched.get("stationary_candidate", False)),
                                "source": ";".join(row.sources)})
    return rows


def transition(rows, visit, kind, graph):
    """Bound a route change using confirmed runs and a continuous observed trace."""
    entry = kind == "entry"
    before, after = (Route.MAIN, Route.LANE) if entry else (Route.LANE, Route.MAIN)
    pivot = visit["entry"] if entry else visit["exit"]
    base = {"visit": visit["id"], "session": visit["session"], "driver": visit["driver"],
            "timing_report_utc": pivot, "earliest_utc": None, "latest_utc": None, "width_s": None,
            "point_estimate_utc": None, "boundary_source": None, "quality": "UNAVAILABLE",
            "independent_geometric_reference": None}
    if not pivot or not graph or graph.verified_through is None:
        return base | {"reason": "QUALIFIED_BRANCH_OR_TIMING_UNAVAILABLE"}
    last_entry_sample = visit["exit"] or visit["entry"]+timedelta(seconds=30)
    sequence = [i for i, r in enumerate(rows) if r["qualified"] == after and
                ((entry and r["utc"] <= last_entry_sample) or (not entry and r["utc"] >= visit["entry"]))]
    for right in sequence:
        # Main-to-lane changes must select a before/after pair on the same visit.
        choices = [i for i in range(right) if rows[i]["qualified"] == before and
                   (entry or rows[i]["utc"] >= visit["entry"])]
        if not choices:
            continue
        left = choices[-1]
        width = (rows[right]["utc"]-rows[left]["utc"]).total_seconds()
        if not 0 < width <= 30:
            continue
        segment = rows[left:right+1]
        if any(r["conflict"] for r in segment) or any(
            not 0 < (b["utc"]-a["utc"]).total_seconds() <= graph.policy["max_fix_span_s"] or
            hypot(b["x"]-a["x"], b["y"]-a["y"])*graph.lane.metres_per_unit >
            graph.policy["max_racing_speed_mps"]*(b["utc"]-a["utc"]).total_seconds()+6
            for a, b in pairwise(segment)
        ):
            continue
        return base | {"earliest_utc": rows[left]["utc"], "latest_utc": rows[right]["utc"], "width_s": width,
                       "quality": "OBSERVED_ROUTE_CHANGE_WINDOW", "boundary_source": "LOOP_AND_HELD_CHECKED_LANE_FIXES",
                       "left_route": before, "right_route": after,
                       "left_source": rows[left]["source"], "right_source": rows[right]["source"],
                       "reason": "SURVEYED_GEOMETRIC_BOUNDARY_UNAVAILABLE"}
    return base | {"reason": "NO_CONTINUOUS_CONFIRMED_CHANGE_BRACKET"}


def _bounds(session, row, at):
    last = row["location_utc"]
    if row["reason"] != "STALE_LOCATION" or not last:
        return None
    age = (at-last).total_seconds()
    if not .9 < age <= 4:
        return None
    old, source = _position(session, row["driver_number"], last, FieldMode.CAUSAL)
    if old.progress_m is None or old.unwrapped_m(session.geometry.length_m) is None:
        return None
    local = old.progress_m
    spread = old.uncertainty_m+105*age
    # Reaching the ambiguous branching corridor would invalidate the envelope.
    if min(local, session.geometry.length_m-local) <= 650+spread:
        return None
    return local, spread, {"as_of": last, "age_s": age, "source": source["location_sources"],
                           "max_speed_mps": 105, "model": "BIDIRECTIONAL_RACING_LOOP_ENVELOPE"}


def field_certificates(session, field):
    off_route = {r["driver_number"]: r["route_comparison"] for r in field["drivers"]
                 if r["route"] == "PIT" and r.get("route_comparison")}
    positions = {r["driver_number"]: (r["progress_m"], r["uncertainty_m"], r["unwrapped_m"])
                 if r["progress_m"] is not None else None for r in field["drivers"]}
    bounds = {r["driver_number"]: bound for r in field["drivers"] if (bound := _bounds(session, r, field["cutoff"]))}
    return [certificate(driver, positions, field["length_m"], off_route=off_route, movement_bounds=bounds) |
            {"session": field["session"], "cutoff": field["cutoff"], "mode": field["mode"]}
            for driver in positions]


def merge_neighbor_candidates(session, graph, window, route_matcher):
    """Use observed pit-car loop projections only as hypotheses during the bracket."""
    if not window["earliest_utc"] or not graph:
        return {"status": "MERGE_NEIGHBORS_UNAVAILABLE", "ahead": [], "behind": [],
                "ambiguous_cars": list(session.roster), "unique_predecessor": None}
    series = session.fixes[window["driver"]]
    support = []
    for record in series.window(window["earliest_utc"], window["latest_utc"]):
        fix = record.point
        compared = match_fix(graph, fix)
        if record.conflict or compared["racing_residual_m"] > 25:
            continue
        at = fix.at_utc
        frame = reconstruct_field(session, at, route_matcher=route_matcher)
        points = {r["driver_number"]: (r["progress_m"], r["uncertainty_m"], r["unwrapped_m"])
                  if r["progress_m"] is not None else None for r in frame["drivers"]}
        d = window["driver"]
        # A projection of the lane car onto the loop is a hypothesis, hence the
        # separate diagnostic category and no unique result at any sampling time.
        points[d] = (compared["track_m"], 25., 0.)
        support.append((at, certificate(d, points, frame["length_m"]),
                        certificate(d, points, frame["length_m"], forward=False)))
    return {"status": "HYPOTHETICAL_MERGE_HORIZON_CANDIDATES" if support else "MERGE_NEIGHBORS_UNAVAILABLE",
            "ahead": sorted({d for _, a, _ in support for d in a["candidates"]}),
            "behind": sorted({d for _, _, b in support for d in b["candidates"]}),
            "ambiguous_cars": sorted({d for _, a, b in support for f in (a, b)
                                      for d in f["possible_interveners"]}),
            "times_evaluated": len(support), "unique_predecessor": None,
            "sampled_hypotheses": [{"utc": t, "ahead": a["candidates"], "behind": b["candidates"],
                                    "ahead_interval_m": a["separation_interval_m"],
                                    "behind_interval_m": b["separation_interval_m"]} for t, a, b in support]}
