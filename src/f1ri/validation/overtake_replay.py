"""Audit pairwise physical exchanges in approved cached windows."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
from itertools import pairwise

import pandas as pd

from f1ri.features.track_geometry import signed_loop_delta
from f1ri.simulator.overtaking import OvertakeAttempt, simulate_scripted_overtaking
from f1ri.simulator.proximity import PhysicalRoute, PhysicalSnapshot, PositionQuality, TrackPosition
from f1ri.simulator.state import (
    CarState,
    RacePhase,
    RaceRuleset,
    RaceState,
    SimulationMode,
    TrackState,
    TyreState,
)
from f1ri.validation.traffic_encounter_audit import window_rows


def pair_order_audit(folder, geometry, inputs, scenario, config):
    window = scenario["window"]
    attacker, target = scenario["attacker"], scenario["target"]
    key = scenario["session"]
    length = geometry["length_m"]
    pos = window_rows(folder, "physical_positions", [window], ["RETROSPECTIVE", "POINT_IN_TIME"])
    neighbor = window_rows(folder, "neighborhoods" if key == 9896 else "physical_neighbors",
                           [window], ["RETROSPECTIVE", "POINT_IN_TIME"])
    estimates = {(r["mode"], r["as_of_utc"], r["driver_number"]): r for r in pos}
    near = {(r["mode"], r["as_of_utc"], r["driver_number"]): r for r in neighbor}
    points = []
    excluded = Counter()
    for (mode, at, driver), a in sorted(estimates.items()):
        if driver != attacker or mode != "RETROSPECTIVE":
            continue
        b = estimates.get((mode, at, target))
        if not b or any(r["route"] != "RACING" or r["progress_m"] is None or not r["lap_aligned"] for r in (a, b)):
            excluded["ROUTE_OR_POSITION_UNRESOLVED"] += 1
            continue
        gap = signed_loop_delta(a["progress_m"], b["progress_m"], length)
        if abs(gap) <= a["uncertainty_m"] + b["uncertainty_m"] or abs(gap) > config["archive_max_endpoint_gap_m"]:
            excluded["GAP_UNCERTAIN_OR_OUTSIDE_SCREEN"] += 1
            continue
        follower, leader = (attacker, target) if gap > 0 else (target, attacker)
        if near[mode, at, follower]["ahead"] != leader:
            excluded["PAIR_NOT_RESOLVED_NEIGHBORS"] += 1
            continue
        points.append({"at_utc": at, "signed_gap_m": gap, "attacker": a, "target": b,
                       "field_coverage": near[mode, at, follower]["coverage"],
                       "relationship": near[mode, at, follower]["encounter"]})
    exchanges = []
    for before, after in pairwise(points):
        a, b = before["at_utc"], after["at_utc"]
        if not (before["signed_gap_m"] > 0 > after["signed_gap_m"]
                and (b - a).total_seconds() <= config["archive_max_bracket_s"]):
            continue
        pit = [r for r in inputs["pits"].itertuples() if r.driver_number in (attacker, target)
               and r.date >= a - timedelta(seconds=2)
               and r.date - timedelta(seconds=float(r.lane_duration) if pd.notna(r.lane_duration) else 0)
               <= b + timedelta(seconds=2)]
        green = any(r.start_utc <= a < b <= r.end_utc and r.status == "GREEN" and r.confidence == "HIGH"
                    for r in inputs["track"].itertuples())
        next_lines = {d: next((r.lap_start_utc for r in inputs["laps"].itertuples()
                               if r.driver_number == d and r.lap_start_utc > b), None) for d in (attacker, target)}
        # An official control-line order is a distinct timing check; position
        # messages provide the independent classification evidence if present.
        observed_ranks = {d: next((int(r.position) for r in inputs["positions"].itertuples()
                                   if r.driver_number == d and r.date > b), None) for d in (attacker, target)}
        exchanges.append({"before": before, "after": after, "green": green,
                          "pit_overlap": [r.date for r in pit], "next_timing_lines": next_lines,
                          "next_reported_classification": observed_ranks,
                          "physical_support": "FULL_FIELD_ENDPOINTS" if (
                              before["field_coverage"] == after["field_coverage"] == "COMPLETE"
                          ) else "RESOLVED_PAIR_SUBSET_FIELD",
                          "status": "SUPPORTED_PAIR_EXCHANGE" if green and not pit else "PIT_OR_TRACK_CONFOUNDED"})
    return {"session_key": key, "window": window, "attacker": attacker, "target": target,
            "resolved_pair_samples": len(points), "sample_exclusions": dict(excluded), "exchanges": exchanges}, estimates


def scripted_pair_replay(audit, estimates, geometry, gap_s):
    """Use the cutoff-visible pair, leaving the archived result for comparison."""
    supported = [row for row in audit["exchanges"] if row["status"] == "SUPPORTED_PAIR_EXCHANGE"]
    if not supported:
        return {"status": "NO_SUPPORTED_ARCHIVED_EXCHANGE"}, None, None
    observed = supported[0]
    at = observed["before"]["at_utc"]
    numbers = (audit["target"], audit["attacker"])
    rows = [estimates.get(("POINT_IN_TIME", at, d)) for d in numbers]
    length = geometry["length_m"]
    layout = geometry.get("version") or geometry["layout_version"]
    if any(not r or r["progress_m"] is None or not r["lap_aligned"] or not r["speed_mps"]
           or r["route"] != "RACING" or not r["completed_laps"] for r in rows):
        return {"status": "CUTOFF_PAIR_UNSUPPORTED"}, None, None
    separation = signed_loop_delta(rows[1]["progress_m"], rows[0]["progress_m"], length)
    if separation <= sum(r["uncertainty_m"] for r in rows):
        return {"status": "CUTOFF_ORDER_UNCERTAIN"}, None, None
    positions = tuple(TrackPosition(r["driver_number"], PhysicalRoute.RACING, r["completed_laps"],
                                    r["classification"], r["progress_m"], 0.,
                                    quality=PositionQuality.EXACT_SCENARIO) for r in rows)
    physical = PhysicalSnapshot(audit["session_key"], at, layout, length, positions)
    cars = tuple(CarState(r.driver_number, i+1, TyreState("UNKNOWN", None),
                          completed_checkpoints=r.completed_laps*3 + int(r.progress_m // (length/3)),
                          last_crossing_us=0) for i, r in enumerate(positions))
    state = RaceState(audit["session_key"], at, RaceRuleset(2025, 80), SimulationMode.CONDITIONAL_SIMULATION,
                      f"historical-pair-{audit['window']}", cars, phase=RacePhase.RUNNING, track_state=TrackState.GREEN)
    checkpoint = cars[1].completed_checkpoints + 1
    script = OvertakeAttempt("historical-pair", state.simulation_id, *reversed(numbers), layout,
                             0, 0, 45_000_000, checkpoint, "CACHE_OBSERVED_PAIR_SCRIPT",
                             "CUTOFF_LOCAL_SPEED_CONSTANT_SCENARIO")
    result = simulate_scripted_overtaking(
        state, physical, {r["driver_number"]: length / r["speed_mps"] for r in rows},
        (script,), laps=1, minimum_gap_s=gap_s,
    )
    outcome, = result.results
    return {"status": outcome.status.value, "reason": outcome.reason, "at_utc": at,
            "archived_reversal_bracket_s": (observed["after"]["at_utc"]-at).total_seconds(),
            "simulated_boundary_commit_s": outcome.committed_us / 1e6 if outcome.committed_us is not None else None,
            "scope": "TWO_CAR_SCENARIO_FROM_CUTOFF_LOCAL_SPEED_AND_ESTIMATED_POSITION",
            "checkpoint": checkpoint, "observed_completed_laps": [r["completed_laps"] for r in rows]}, result, script
