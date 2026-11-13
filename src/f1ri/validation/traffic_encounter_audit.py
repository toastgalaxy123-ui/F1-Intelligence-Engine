"""Explain observed encounter timing and reproduce the frozen lap support mask."""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from itertools import groupby, pairwise
from statistics import mean

import duckdb
import pandas as pd

from f1ri.validation.traffic_exposures import exposure_state
from f1ri.validation.traffic_transition import approach_signal


def window_rows(folder, name, windows, modes=None):
    with duckdb.connect() as con:
        con.execute("SET TimeZone = 'UTC'")
        sql = 'SELECT * FROM read_parquet(?) WHERE "window" IN (SELECT unnest(?))'
        params = [str(folder / f"{name}.parquet"), list(windows)]
        if modes is not None:
            sql += " AND mode IN (SELECT unnest(?))"
            params.append(list(modes))
        cursor = con.execute(sql, params)
        keys = [col[0] for col in cursor.description]
        return [dict(zip(keys, row, strict=True)) for row in cursor.fetchall()]


def support_sample(neighbor, follower, leader, retrospective_follower, retrospective_leader, snapshot, exposure):
    state = exposure_state(neighbor, exposure)
    routes = (retrospective_follower.get("route"), retrospective_leader.get("route"))
    reasons = []
    if not follower.get("localized"):
        reasons.append("FOLLOWER_" + (follower.get("failure") or "POSITION_UNAVAILABLE"))
    if neighbor["ahead"] is not None and not leader.get("localized"):
        reasons.append("LEADER_" + (leader.get("failure") or "POSITION_UNAVAILABLE"))
    if not snapshot["complete"]:
        reasons.append("INCOMPLETE_FIELD")
    if neighbor["ahead"] is None:
        reasons.append(follower.get("ahead_reason") or "NEIGHBOR_UNRESOLVED")
    if routes != ("RACING", "RACING"):
        reasons.append("RETROSPECTIVE_ROUTE_MASK")
    if neighbor["track_state"] != "GREEN":
        reasons.append("NON_GREEN")
    if state == "UNSUPPORTED" and not reasons:
        reasons.append("POINT_IN_TIME_ROUTE_OR_LAP_RELATION_UNSUPPORTED")
    if (state == "INTERMEDIATE" and neighbor["gap_lower_m"] <= exposure["entry_upper_m"]
            < neighbor["gap_upper_m"]):
        reasons.append("CLOSE_THRESHOLD_STRADDLED")
    return {
        "as_of_utc": neighbor["as_of_utc"], "leader": neighbor["ahead"], "state": state,
        "gap_m": neighbor["gap_m"], "gap_lower_m": neighbor["gap_lower_m"],
        "gap_upper_m": neighbor["gap_upper_m"], "lap_relation": neighbor["encounter"],
        "field_complete": snapshot["complete"], "missing_required_cars": snapshot["missing_required"],
        "follower_localized": bool(follower.get("localized")), "leader_localized": bool(leader.get("localized")),
        "neighbor_resolved": neighbor["ahead"] is not None, "retrospective_routes": routes,
        "point_in_time_routes": (follower.get("route"), leader.get("route")),
        "track_state": neighbor["track_state"], "neighbor_reason": follower.get("ahead_reason"),
        "follower_position_reason": follower.get("position_reason"),
        "leader_position_reason": leader.get("position_reason"),
        "follower_fix_age_s": follower.get("location_age_s"), "leader_fix_age_s": leader.get("location_age_s"),
        "route_supported": routes == ("RACING", "RACING"),
        "target_supported": state != "UNSUPPORTED" and routes == ("RACING", "RACING"),
        "reasons": reasons,
    }


def support_summary(rows, start, end, period):
    covered = [r for r in rows if start <= r["as_of_utc"] < end]
    checks = ("field_complete", "follower_localized", "leader_localized",
              "neighbor_resolved", "route_supported", "target_supported")
    runs = [(valid, len(list(group)) * period) for valid, group in groupby(r["target_supported"] for r in covered)]
    elapsed = (end - start).total_seconds()
    return {
        "samples": len(covered), "fractions": {check: mean(r[check] for r in covered) if covered else None
                                               for check in checks},
        "frozen_fraction": min(1., sum(r["target_supported"] for r in covered) * period / elapsed) if elapsed > 0 else None,
        "longest_supported_s": max((seconds for valid, seconds in runs if valid), default=0.),
        "longest_gap_s": max((seconds for valid, seconds in runs if not valid), default=0.),
        "gaps": sum(not valid for valid, _ in runs),
        "failure_counts": dict(Counter(reason for row in covered if not row["target_supported"]
                                       for reason in row["reasons"])),
        "missing_required_cars": dict(Counter(car for row in covered for car in row["missing_required_cars"])),
    }


def close_runs(rows, period):
    """Build strict entry-condition runs independently of the frozen hysteretic episodes."""
    runs = []
    for row in rows:
        at, leader = row["as_of_utc"], row["leader"]
        if row["state"] != "CLOSE":
            continue
        if (runs and runs[-1]["leader"] == leader
                and at - runs[-1]["last_utc"] == timedelta(seconds=period)):
            runs[-1]["last_utc"] = at
        else:
            runs.append({"leader": leader, "start_utc": at, "last_utc": at})
    return [row | {"duration_s": (row["last_utc"] - row["start_utc"]).total_seconds() + period} for row in runs]


def encounter_states(rows, signal, entry_upper_m, period, minimum_duration_s):
    """Annotate sample states using only samples at or before each timestamp."""
    by_clock = {}
    close_since, last_close, close_leader = None, None, None
    for row in rows:
        at, leader = row["as_of_utc"], row["leader"]
        previous = by_clock.get(at - timedelta(seconds=3))
        state = row["state"]
        current = {"ahead": leader, "gap_upper_m": row["gap_upper_m"]} if state != "UNSUPPORTED" else None
        prior = ({"ahead": previous["leader"], "gap_upper_m": previous["gap_upper_m"]}
                 if previous and previous["state"] != "UNSUPPORTED" else None)
        approach = approach_signal(current, prior, row["gap_upper_m"], prior["gap_upper_m"] if prior else None,
                                   None, None)
        if state == "CLOSE":
            if last_close is None or at - last_close != timedelta(seconds=period) or leader != close_leader:
                close_since = at
            label = ("FOLLOWING_EPISODE_ACTIVE" if (at - close_since).total_seconds() >= minimum_duration_s
                     else "CLOSE_SUPPORTED")
            last_close, close_leader = at, leader
        elif last_close is not None and at - last_close == timedelta(seconds=period):
            label = "ENCOUNTER_TERMINATED"
        elif state == "UNSUPPORTED":
            label = "PHYSICAL_STATE_UNRESOLVED"
        elif row["gap_lower_m"] <= entry_upper_m < row["gap_upper_m"]:
            label = "CLOSE_BUT_UNCERTAIN"
        elif approach == "PHYSICAL_APPROACH":
            label = "PHYSICAL_APPROACH_SUPPORTED"
        elif at == signal:
            label = "APPROACH_DETECTED"
        else:
            label = "NO_APPROACH_EVIDENCE"
        row["online_diagnostic_state"] = label
        by_clock[at] = row


def anchor_audits(folder, windows, anchors, laps, config, outcomes):
    names = [w["name"] for w in windows]
    modes = ("POINT_IN_TIME", "RETROSPECTIVE")
    neighbors = window_rows(folder, "physical_neighbors", names, modes)
    diagnostics = window_rows(folder, "coverage_driver_snapshots", names, modes)
    positions = {(r["window"], r["as_of_utc"], r["driver_number"]): r
                 for r in window_rows(folder, "physical_positions", names, ["POINT_IN_TIME"])}
    diagnostic = {(r["window"], r["as_of_utc"], r["mode"], r["driver_number"]): r for r in diagnostics}
    snapshots = {(r["window"], r["as_of_utc"]): r
                 for r in window_rows(folder, "coverage_snapshots", names, ["POINT_IN_TIME"])}
    grouped = defaultdict(list)
    for row in neighbors:
        if row["mode"] == "POINT_IN_TIME":
            grouped[row["window"], row["driver_number"]].append(row)
    episodes = window_rows(folder, "traffic_episodes", names)
    period = config["sampling"]["sample_period_seconds"]
    timelines, failures = [], []
    for window, anchor in zip(windows, anchors, strict=True):
        name, driver = window["name"], anchor["driver_number"]
        identity = anchor["opportunity_id"]
        own = laps[laps.driver_number.eq(driver)].set_index("lap_number")
        lap = own.loc[anchor["lap_number"]]
        start, end = lap.lap_start_utc, lap.lap_end_estimate_utc
        next_line = own.loc[anchor["lap_number"] + 1].lap_start_utc if anchor["lap_number"] + 1 in own.index else None
        rows = []
        for neighbor in sorted(grouped[name, driver], key=lambda r: r["as_of_utc"]):
            at, leader = neighbor["as_of_utc"], neighbor["ahead"]
            own_pt = diagnostic.get((name, at, modes[0], driver), {})
            other_pt = diagnostic.get((name, at, modes[0], leader), {})
            own_rt = diagnostic.get((name, at, modes[1], driver), {})
            other_rt = diagnostic.get((name, at, modes[1], leader), {})
            sample = support_sample(neighbor, own_pt, other_pt, own_rt, other_rt,
                                    snapshots[name, at], config["exposure"])
            sample["follower_position"] = positions.get((name, at, driver))
            sample["leader_position"] = positions.get((name, at, leader))
            own_progress = (sample["follower_position"] or {}).get("progress_m")
            leader_progress = (sample["leader_position"] or {}).get("progress_m")
            sample["forward_gap_crosses_loop_origin"] = (None if own_progress is None or leader_progress is None
                                                        else leader_progress < own_progress)
            rows.append(sample)
        selection = rows[bisect_right([r["as_of_utc"] for r in rows], start) - 1]
        outcome = outcomes[identity]
        support = support_summary(rows, start, end, period)
        coverage = outcome.get("physical_coverage")
        if coverage is not None and abs(support["frozen_fraction"] - coverage) > 1e-9:
            raise ValueError(f"Frozen physical mask did not reproduce for {identity}")
        common = {"opportunity_id": identity, "window": name, "cutoff_utc": start,
                  "target_end_utc": end, "next_line_utc": next_line, "screen": outcome["screen"]}
        if outcome["screen"] == "PHYSICAL_SUPPORT_BELOW_MINIMUM":
            failures.append(common | {"cutoff_sample": selection, "support": support,
                                      "first_decisive_exclusion": ("CUTOFF_UNSUPPORTED" if selection["state"] == "UNSUPPORTED"
                                                                   else "LAP_COVERAGE_BELOW_90_PERCENT"),
                                      "threshold_sensitivity": {
                                          str(level): selection["state"] != "UNSUPPORTED"
                                          and support["frozen_fraction"] >= level for level in (.85, .80)
                                      }, "samples": rows})
        if window["kind"] != "transition":
            continue
        local = sorted((r for r in episodes if r["window"] == name and r["driver_number"] == driver),
                       key=lambda r: r["start_utc"])
        approach_at = anchor.get("physical_as_of_utc") if anchor["approach"] == "PHYSICAL_APPROACH" else anchor["interval_source_utc"]
        signal = datetime.fromisoformat(approach_at)
        encounter_states(rows, signal, config["exposure"]["entry_upper_m"], period,
                         config["exposure"]["minimum_duration_s"])
        from_signal = [r for r in rows if r["as_of_utc"] >= signal]
        runs = close_runs(from_signal, period)
        first = next((r for r in from_signal if r["state"] == "CLOSE"), None)
        first_nominal = next((r for r in from_signal if r["gap_m"] is not None
                              and r["gap_m"] <= config["exposure"]["entry_upper_m"]), None)
        next_id = f"{lap.session_key}/{driver}/{anchor['lap_number'] + 1}"
        next_outcome = outcomes.get(next_id)
        leaders = [r["leader"] for r in from_signal if r["leader"] is not None]
        first_boundary = own.loc[own.lap_start_utc.ge(first["as_of_utc"]), "lap_start_utc"].min() if first else None
        physical_signals = [r["as_of_utc"] for r in rows if r["as_of_utc"] <= start
                            and r["online_diagnostic_state"] == "PHYSICAL_APPROACH_SUPPORTED"]
        gap_statistics = support_summary(rows, signal, rows[-1]["as_of_utc"] + timedelta(seconds=period), period)
        timelines.append(common | {
            "selection_evidence": anchor, "observed_cutoff": selection, "first_approach_utc": approach_at,
            "first_cached_physical_approach_by_cutoff_utc": min(physical_signals, default=None),
            "first_nominal_close": first_nominal, "first_supported_close": first,
            "approach_to_close_s": (first["as_of_utc"] - signal).total_seconds() if first else None,
            "first_boundary_at_or_after_close": first_boundary if pd.notna(first_boundary) else None,
            "close_to_next_boundary_s": ((first_boundary - first["as_of_utc"]).total_seconds()
                                         if first and pd.notna(first_boundary) else None),
            "observed_leader_changes": sum(a != b for a, b in pairwise(leaders)),
            "strict_close_runs": runs, "episodes": local, "support": support,
            "support_from_signal_to_window_end": gap_statistics,
            "episode_spans_next_boundary": any(r["start_utc"] <= next_line <= r["end_utc"] for r in local)
            if next_line is not None else None,
            "next_lap_observed_screen": next_outcome["screen"] if next_outcome else None,
            "earliest_observed_eligible_full_lap": next_id if next_outcome and next_outcome["screen"] == "FOLLOWING"
            and next_outcome["pace_status"] == "SUPPORTED" and next_outcome.get("signed_proxy_s") is not None else None,
            "close_outcome": "OBSERVED" if first else "RIGHT_CENSORED_NO_SUPPORTED_CLOSE",
            "samples": rows,
        })
    return timelines, failures
