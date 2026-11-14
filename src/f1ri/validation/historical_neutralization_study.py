"""Run S5-B without changing the simulator or frozen research cohorts."""
from collections import Counter, defaultdict
from datetime import timedelta
from statistics import median

import yaml

from f1ri.simulator.race_control import RaceControlProfile
from f1ri.validation.historical_neutralization_compare import (
    error_stats,
    replay_declared,
    run_prediction_comparison,
)
from f1ri.validation.historical_neutralization_evidence import (
    SUPPORT,
    chronology,
    localization,
    route_events,
    timing_observations,
)
from f1ri.validation.historical_neutralization_sources import (
    audit_sources,
    clock,
    read_release,
    verify_releases,
)
from f1ri.validation.proximity_pilot import _json

PLAN = "config/experiments/stage5_s5_b_historical_neutralization_validation.yaml"


def _restart(episodes, archive, scenarios, support, predictions, declared):
    ledger = []
    for ep in episodes:
        if ep["status"] not in ("VSC", "SC"):
            continue
        name = ep["episode"]
        clear = ep["archived_release_utc"]
        if clear is None:
            continue
        by_car = defaultdict(list)
        for r in archive[name]["observed_lines"]:
            if clock(r["utc"]) >= clear:
                by_car[r["driver"]].append(r)
        for driver, observations in sorted(by_car.items()):
            target = min(observations, key=lambda x: clock(x["utc"]))
            observed_time = clock(target["utc"])
            checks = {r["variant"]: r["simulated_utc"] for r in predictions if r["episode"] == name and
                      r["driver"] == driver and r["lap"] == target["completed_laps"]}
            at_clear = next(r for r in support if r["episode"] == name and r["cutoff"] == "global_clear" and
                            r["driver"] == driver and r["source_set"] == "combined")
            started = next(c for c in scenarios[name]["initial"]["cars"] if c["driver_number"] == driver)
            permissions = [r for r in declared.restart_permissions if name == "imola_sc_4" and
                           r["driver"] == driver and r["status"] == "CROSSED_RESTART_LINE"]
            simulated_permission = permissions[0] if permissions else None
            epoch = declared.execution.initial.race_start_utc
            ledger.append({"episode": name, "driver": driver, "global_clear_utc": clear,
                           "physical_withdrawal_utc": None, "earliest_observed_line_after_clear_utc": observed_time,
                           "archived_lap_at_next_line": target["completed_laps"], "source_tier": "H2",
                           "next_line_elapsed_s": (observed_time-clear).total_seconds(),
                           "same_lap_simulated_line_utc": checks,
                           "same_lap_error_s": {k: (v-observed_time).total_seconds() if v else None
                                                for k, v in checks.items()},
                           "clear_snapshot_tier": at_clear["tier_event_clock"],
                           "route_at_clear": at_clear["route"],
                           "observed_laps_at_clear": at_clear["completed_laps"],
                           "declared_initial_laps": started["completed_checkpoints"]//3,
                           "simulated_driver_permission_utc": epoch+timedelta(microseconds=simulated_permission["at_us"])
                           if simulated_permission else None,
                           "simulated_driver_permission_lap": simulated_permission["required_checkpoint"]//3
                           if simulated_permission else None,
                           "actual_passing_permission_utc": None})
    return ledger


def _sc_line_spread(episode, evidence, predictions):
    rows = []
    lines = defaultdict(list)
    start, end = episode["start_utc"], episode["end_utc"]
    roster = {r["driver"] for r in evidence["observed_lines"]}
    for r in evidence["observed_lines"]:
        if start <= clock(r["utc"]) < end:
            lines[r["completed_laps"]].append(r)
    for lap, timing in sorted(lines.items()):
        by_driver = {r["driver"]: clock(r["utc"]) for r in timing}
        comparable = {r["driver"]: r["simulated_utc"] for r in predictions if r["lap"] == lap and r["common"]}
        matched = sorted(by_driver.keys() & comparable.keys())
        rows.append({"lap": lap, "observed_roster_count": len(by_driver), "episode_roster_count": len(roster),
                     "matched_driver_count": len(matched),
                     "full_observed_line_spread_s": (max(by_driver.values())-min(by_driver.values())).total_seconds()
                     if len(by_driver) > 1 else None,
                     "comparable_driver_set": matched,
                     "matched_observed_line_spread_s": (max(by_driver[d] for d in matched) -
                                                        min(by_driver[d] for d in matched)).total_seconds()
                     if len(matched) > 1 else None,
                     "matched_simulated_line_spread_s": (max(comparable[d] for d in matched) -
                                                         min(comparable[d] for d in matched)).total_seconds()
                     if len(matched) > 1 else None,
                     "physical_queue_formation_utc": None, "same_time_physical_gaps_m": None,
                     "scope": "SAME_LAP_LINE_TIME_SPREAD_NOT_SIMULTANEOUS_GAP"})
    return rows


def _movement(timing, relative, episodes):
    vsc = next(r for r in episodes if r["status"] == "VSC")
    rows = [r for r in timing if r["episode"] == vsc["episode"]]
    phases = defaultdict(list)
    for r in rows:
        if r["start_utc"] and r["duration_s"] is not None:
            phases[r["segment"], r["population"]].append(r["duration_s"])
    distribution = [{"segment": k[0], "population": k[1], "n": len(values), "min_s": min(values),
                     "median_s": median(values), "max_s": max(values)} for k, values in sorted(phases.items())]
    keys = {(r["driver"], r["lap"]): r for r in rows if r["segment"] == "LAP"}
    pairs = []
    for r in relative:
        if r["episode"] == vsc["episode"] and r["measure"] == "CONSECUTIVE_LAP_DELTA":
            source = keys.get((r["driver"], r["lap"]))
            if source:
                pairs.append(r | {"population": source["population"], "overlap_s": source["overlap_s"]})
    cutoffs = {vsc["start_utc"]: "DEPLOYMENT", vsc["announcement_utc"]: "ENDING", vsc["end_utc"]: "RELEASE"}
    segment_transitions = []
    for row in rows:
        if row["segment"] == "LAP" or row["end_utc"] is None:
            continue
        crossed = [label for at, label in cutoffs.items() if at and row["start_utc"] < at < row["end_utc"]]
        if crossed:
            segment_transitions.append(row | {"crossed_control_boundaries": crossed})
    return {"vsc_distributions": distribution, "vsc_matched_lap_durations": pairs,
            "vsc_sector_transition_observations": segment_transitions,
            "model_sector_comparison": "UNSUPPORTED_SIMULATOR_CHECKPOINTS_ARE_EQUAL_DISTANCE",
            "vsc_relative_error_s": {v: error_stats([r["error_s"] for r in pairs if r["variant"] == v])
                                     for v in sorted({r["variant"] for r in pairs})},
            "observed_vsc_driver_reaction_utc": None, "observed_marshal_delta_compliance": None}


def run_historical_neutralization(root, output, geometry_folder, warehouse):
    if output.exists():
        raise FileExistsError(output)
    plan = root / PLAN
    protocol = yaml.safe_load(plan.read_bytes())
    parity = verify_releases(root, protocol["frozen"])
    versions = {name: read_release(root, name) for name in ("s5_a", "s5_a1")}
    old, new = versions["s5_a"], versions["s5_a1"]
    source = old["source_manifest.json"]
    evidence = source["historical"]
    released = new["historical_timing_comparison.json"]
    scenarios = old["scenario_manifest.json"]["archived"]
    if {r["session"] for r in evidence.values()} != set(protocol["cohort"]):
        raise ValueError("Frozen archive differs from approved cohort")
    loaded = audit_sources(root, protocol, source, geometry_folder, warehouse)
    episodes, controls, intervals = chronology(evidence)
    routes, visits = route_events(evidence, episodes)
    timing, lap_rows, reconciliation = timing_observations(
        loaded, episodes, evidence, visits, protocol["timing_reconciliation_tolerance_s"])
    support, geometry = localization(loaded, episodes, evidence, scenarios, lap_rows, visits, routes,
                                     geometry_folder/"geometry.json", protocol["location_max_age_s"])
    profile = RaceControlProfile(**yaml.safe_load(
        (root/"config/experiments/stage5_s5_a_neutralization_mechanics.yaml").read_bytes())["profile"])
    comparisons, stats, relative, sensitivity, variants, live = run_prediction_comparison(
        episodes, evidence, released, scenarios, support, geometry, profile, protocol)
    sc_episode = next(e for e in episodes if e["status"] == "SC")
    sc_name = sc_episode["episode"]
    declared = replay_declared(scenarios[sc_name], sc_episode, profile)
    restart = _restart(episodes, evidence, scenarios, support, comparisons, declared)
    queue = _sc_line_spread(sc_episode, evidence[sc_name], [r for r in comparisons
                                                           if r["episode"] == sc_name and r["variant"] == "B"])
    movement = _movement(timing, relative, episodes)
    actual_route = [{"episode": ep["episode"], "driver": driver, "entry_utc": a, "exit_utc": b,
                     "recorded_lane_elapsed_s": (b-a).total_seconds() if a and b else None,
                     "simulated_rejoin_match": None, "historical_queue_support": None}
                    for ep in episodes for driver, a, b in visits[ep["episode"]]
                    if (a and ep["start_utc"] <= a < ep["end_utc"]) or
                       (b and ep["start_utc"] <= b < ep["end_utc"])]
    grid = defaultdict(Counter)
    retrospective = defaultdict(Counter)
    for r in support:
        if not r["cutoff"].startswith("next_line_"):
            grid[r["episode"], r["source_set"], r["cutoff"]][r["tier_event_clock"]] += 1
            retrospective[r["episode"], r["source_set"], r["cutoff"]][r["tier_retrospective"]] += 1
    counts = [{"episode": ep, "source_set": src, "cutoff": cutoff,
               "active_roster": sum(c.values()), **{tier: c[tier] for tier in SUPPORT}}
              for (ep, src, cutoff), c in sorted(grid.items())]
    for record in counts:
        key = (record["episode"], record["source_set"], record["cutoff"])
        record["retrospective_tiers"] = {tier: retrospective[key][tier] for tier in SUPPORT}
    marker_parity = all(abs(r["delta_s"]) <= protocol["timing_reconciliation_tolerance_s"] for r in reconciliation)
    local_parity = all(not r["command_timing"] for ep in episodes if ep["scope"] == "LOCAL"
                       for r in (released[ep["episode"]]["frozen"], released[ep["episode"]]["stabilized"]))
    invariants = {"stage1_boundaries_reproduced": all(e["stage1_boundary_parity"] for e in episodes),
                  "local_negatives_have_no_global_commands": local_parity,
                  "raw_laps_match_frozen_lines_on_shared_keys": marker_parity,
                  "no_future_fix_in_localized_state": all(not r["latest_observed_utc"] or
                                                         r["latest_observed_utc"] <= r["cutoff_utc"] for r in support),
                  "fixed_common_prediction_population": all(len({item["n"] for item in s["common"].values()}) == 1
                                                            for s in stats.values()),
                  "prior_synthetic_and_historical_invariants": all(new["metrics.json"]["scenario_invariants"].values()),
                  "prior_green_pit_160_217": new["regression_parity.json"]["green_pit_160_217"],
                  "prior_t1_no_pass": new["regression_parity.json"]["s1_s2_s3_and_t1"]["two_car_no_pass"]}
    if not all(invariants.values()):
        raise ValueError(f"S5-B study invariants failed: {invariants}")
    global_episodes = [r for r in episodes if r["scope"] == "TRACK"]
    metrics = {
        "study": protocol["version"], "baseline": protocol["baseline"],
        "independent_global_episodes": len(global_episodes),
        "independent_global_races": len({r["session"] for r in global_episodes}),
        "global_layouts": len({r["race"] for r in global_episodes}), "comparison_windows": len(episodes),
        "observed_red_episodes": sum(r["status"] == "RED" for r in episodes),
        "acquisition_responses": sum(r["source"] == "BOUNDED_S5_B" for r in loaded.census),
        "localization_counts": counts, "timing_record_counts": dict(Counter(r["segment"] for r in timing)),
        "route_pairs_in_episode": dict(Counter(r["episode"] for r in actual_route)),
        "comparison": stats, "invariants": invariants,
        "s5_c": "PATH_B_PARTIAL_HISTORICAL_SUPPORT" if timing and all(invariants.values())
        else "PATH_C_HISTORICAL_MOVEMENT_EVIDENCE_UNAVAILABLE",
    }
    decomposition = [
        {"contrast": "A_TO_B", "changed_component": "GLOBAL_RESTART_GATE_AND_DRIVER_PERMISSIONS",
         "support": "FIXED_DECLARED_FIELD_AND_PACE", "results": stats[sc_name]["frozen_pair_common"]},
        {"contrast": "B_TO_D", "changed_component": "PHYSICAL_LONGITUDINAL_INITIALIZATION",
         "support": "MATCHED_TARGETS_AND_SAME_PACE_SCENARIO", "results": stats[sc_name]["common"]
         if sc_name in live else None},
        {"contrast": "SC_DEPLOYMENT_BOUND_SENSITIVITY", "changed_component": "INITIAL_LONGITUDINAL_POSITION",
         "support": "EXACT_SCENARIO_POINTS_WITHIN_HEURISTIC_BOUNDS",
         "frozen_comparison_difference": "FROZEN_REPLAY_STARTS_FIVE_SECONDS_BEFORE_DEPLOYMENT",
         "results": [r for r in sensitivity if r["episode"] == sc_name]},
        {"contrast": "RESIDUAL", "unresolved": ["clean pace", "SC pacing and catch-up", "collection and queue",
                                                "unsupported unlapping", "historical pit and retirement replay",
                                                "route ambiguity", "clock and timing-marker alignment"]},
    ]
    output.mkdir(parents=True)
    (output/"config.yaml").write_bytes(plan.read_bytes())
    for name, product in {
        "protocol": protocol, "race_selection_manifest": {"cohort": protocol["cohort"], "episodes": episodes},
        "warehouse_runtime_provenance": loaded.runtime, "source_availability_census": loaded.census,
        "race_control_events": controls, "historical_neutralization_intervals": intervals,
        "physical_support_hierarchy": SUPPORT, "historical_localization_eligibility": support,
        "driver_timing_observations": timing, "source_timestamp_reconciliation": reconciliation,
        "sector_lap_timing_comparisons": relative, "vsc_movement_diagnostics": movement,
        "sc_collection_queue_support": {"same_lap_lines": queue,
                                        "snapshot_neighbors": [r for r in support if r["episode"] == sc_name and
                                                               r["source_set"] == "combined" and
                                                               r["cutoff"] in ("deployment","announcement","global_clear")],
                                        "declared_simulated_progress": declared.progress}, "restart_timing": restart,
        "neutralization_pit_events": {"events": routes, "visits": actual_route},
        "simulation_variants": variants, "replay_comparison_predictions": comparisons,
        "common_support_metrics": stats, "error_decomposition": decomposition, "sensitivity": sensitivity,
        "invariants": invariants, "regression_parity": parity, "metrics": metrics,
    }.items():
        _json(output/f"{name}.json", product)

    from f1ri.validation.historical_neutralization_plots import plot_history

    plot_history(output, metrics, loaded.census, episodes, support, timing, queue, restart, comparisons,
                 movement, sensitivity, actual_route, released, declared)
    return output/"metrics.json"
