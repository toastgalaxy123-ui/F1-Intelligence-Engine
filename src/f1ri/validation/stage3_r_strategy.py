"""Exercise the fitted successor through the existing S5-C.1 boundary."""
from dataclasses import asdict, replace
from datetime import datetime
from hashlib import sha256
from math import isfinite

import numpy as np

from f1ri.simulator.events import Event, EventKind
from f1ri.simulator.pace_adapter import forecast_new_stint, forecast_pace
from f1ri.simulator.pit_execution import PitStopRequest, ReplacementTyres
from f1ri.simulator.pit_pace import replacement_pace_request
from f1ri.simulator.race_control import ControlCommand as C
from f1ri.simulator.strategy import compare_strategies, fingerprint
from f1ri.simulator.strategy_constraints import CheckpointConstraints
from f1ri.simulator.strategy_forecast import StrategyForecastAdapter, read_forecast_inputs
from f1ri.simulator.strategy_policy import Policy, PolicyKind, Stop
from f1ri.validation.neutralization_scenarios import controls
from f1ri.validation.overtake_historical_study import _load_json
from f1ri.validation.stage3_r_validation import forecast_document
from f1ri.validation.strategy_sources import audit_model_assets, frozen_pace_fixture
from f1ri.validation.strategy_study import _invariants


def load_controls(root):
    prior, _, _ = audit_model_assets(root)
    base = frozen_pace_fixture(root, prior)
    folder = root/prior["frozen_pace"]["path"]
    snapshot = _load_json(folder/"snapshot.json")
    identities = {d["driver_number"]: d["request"] for d in snapshot["drivers"]}
    timestamp = snapshot["as_of_utc"]
    clock = base.initial.race_clock_us
    length = base.physical.length_m
    published_source = sha256((folder/"snapshot.json").read_bytes()).hexdigest()
    cars = tuple(replace(c, team="DECLARED_SHARED_TEAM", tyres=replace(c.tyres,
                         compound_absolute=identities[c.driver_number]["compound_absolute"],
                         stint_number=identities[c.driver_number]["stint_number"]))
                 for c in base.initial.cars)
    initial = replace(base.initial, cars=cars)
    start = next(c.completed_laps+1 for c in cars if c.driver_number == 63)
    sets = (ReplacementTyres("MEDIUM", 0, "C4", "medium"),
            ReplacementTyres("HARD", 0, "C3", "hard"),
            ReplacementTyres("SOFT", 0, "C5", "soft"),
            ReplacementTyres("SOFT", 3, "C5", "used-soft"))
    resources = tuple(replace(r, team="DECLARED_SHARED_TEAM", tyres=sets,
                              route=replace(r.route, box_id="DECLARED_SHARED_BOX")) for r in base.resources)
    ref = Policy("continue", PolicyKind.CONTINUE)
    stops = tuple(Policy(t.set_id, PolicyKind.TARGET, (Stop(63, t.set_id, lap=start),)) for t in sets)
    later = Policy("later", PolicyKind.TARGET, (Stop(63, "medium", lap=start+1),))
    twice = Policy("two-stop", PolicyKind.SEQUENCE, (Stop(63, "medium", lap=start), Stop(63, "used-soft", lap=start+2)))
    repeat = Policy("identical-medium", PolicyKind.TARGET, (Stop(63, "medium", lap=start),))
    marker = (start+3)*3+2
    standard = replace(base, initial=initial, resources=resources, controls=(), targets=((63, marker),),
                       policies=(ref, *stops, later, twice, repeat), horizon_laps=7, end_us=clock+850_000_000)
    single = replace(standard, scenario_id="clean-air", initial=replace(initial, cars=tuple(
        c for c in cars if c.driver_number == 63)), physical=replace(base.physical, cars=tuple(
        c for c in base.physical.cars if c.driver_number == 63)), resources=tuple(r for r in resources if r.driver == 63))
    blocked_positions = tuple(replace(c, progress_m=.50*length if c.driver_number == 63 else .505*length)
                              for c in standard.physical.cars)
    blocked = replace(standard, scenario_id="two-car", physical=replace(standard.physical, cars=blocked_positions))
    removed = replace(blocked, scenario_id="retirement", retirements=(
        Event(clock+150_000_000, EventKind.RETIREMENT, 4, source="DECLARED_RETIREMENT"),))
    vsc = replace(blocked, scenario_id="vsc", controls=controls(initial, [
        (C.VSC, clock/1e6+5), (C.VSC_ENDING, clock/1e6+70), (C.GREEN, clock/1e6+80)]))
    sc = replace(blocked, scenario_id="sc", controls=controls(initial, [
        (C.SC, clock/1e6+5), (C.SC_IN, clock/1e6+85), (C.SC_WITHDRAWN, clock/1e6+96), (C.GREEN, clock/1e6+97)]))
    vsc = replace(vsc, policies=(ref, stops[0], Policy("react", PolicyKind.VSC, (Stop(63, "medium"),))))
    sc = replace(sc, policies=(ref, stops[0], Policy("react", PolicyKind.SC, (Stop(63, "medium"),))))
    other_start = next(c.completed_laps+1 for c in cars if c.driver_number == 4)
    team = replace(blocked, scenario_id="shared-box", targets=((63, marker), (4, marker)), policies=(
        ref, Policy("double-stack", PolicyKind.TEAM, (Stop(63, "medium", start), Stop(4, "medium", other_start))),
        Policy("stagger", PolicyKind.TEAM, (Stop(63, "medium", start), Stop(4, "medium", other_start+1)))))
    varied = replace(blocked, scenario_id="wider-gap", physical=replace(blocked.physical, cars=tuple(
        replace(c, progress_m=.64*length) if c.driver_number == 4 else c for c in blocked.physical.cars)),
        policies=(ref, *stops[:2]))
    feature_rows = {driver: {"request": req, "source_sha256": published_source,
                             "evidence_as_of": {k: timestamp for k in (
                                 "identity", "timing", "tyres", "clean_history", "previous_stint", "grid_qualifying")}
                             | {"weather": snapshot["weather_observed_at_utc"]}}
                    for driver, req in identities.items() if req}
    original_opponent = replace(blocked, scenario_id="original-opponent", policies=(ref, *stops[:2]))
    return (single, blocked, removed, vsc, sc, team, varied, original_opponent), feature_rows


def _slower_opponent(record):
    # Declare a slower clean-air history while keeping each observation clock and tyre state fixed.
    request = dict(record["request"])
    shift = 4.0
    for name in ("last_clean_lap_time_s", "median_last_3_clean_laps_s", "median_last_5_clean_laps_s",
                 "median_stint_clean_laps_s"):
        if request[name] is not None:
            request[name] += shift
    request["observed_stint_clean_times_s"] = [v+shift for v in request["observed_stint_clean_times_s"]]
    return record | {"request": request, "source_sha256": fingerprint((record["source_sha256"], "history_plus_s", shift))}


def execute_strategy(root, bundle, samples, seed):
    scenarios, features = load_controls(root)
    results = {}
    checks = {}
    clean = scenarios[0]
    for scene in scenarios:
        inputs = read_forecast_inputs([
            _slower_opponent(features[4]) if c.driver_number == 4 and scene.scenario_id != "original-opponent"
            else features[c.driver_number] for c in scene.initial.cars
        ])
        adapter = StrategyForecastAdapter(bundle, inputs, seed=seed, samples=samples, sample=None)
        run = compare_strategies(scene, "continue", recorder_factory=CheckpointConstraints,
                                 forecast_adapter=adapter)
        results[scene.scenario_id] = {
            "scenario": asdict(scene), "result": run,
            "opponent_history": "DECLARED_PLUS_4_SECONDS" if len(inputs) > 1 and scene.scenario_id != "original-opponent"
            else "SAVED_SINGAPORE_CUTOFF",
        }
        checks[scene.scenario_id] = _invariants(scene, run)
    focal = results["clean-air"]["result"]
    sample_adapter = StrategyForecastAdapter(bundle, read_forecast_inputs([features[63]]), seed=seed,
                                             samples=samples, sample=0)
    policies = ["medium", "hard", "identical-medium", "two-stop"]
    pair = compare_strategies(clean, "continue", policies, forecast_adapter=sample_adapter)
    shorter = compare_strategies(replace(clean, policies=tuple(reversed(clean.policies))), "continue",
                                 ["medium", "identical-medium"], forecast_adapter=sample_adapter)
    equivalents = pair["results"]["medium"]["trajectory"] == pair["results"]["identical-medium"]["trajectory"]
    material = {k: pair["results"][k] for k in policies}
    other_unchanged = True
    two = results["two-car"]["result"]
    reference = [r for r in two["results"]["continue"]["forecast_requests"] if r["request"]["driver_number"] == 4]
    for run in two["results"].values():
        other_unchanged &= [r for r in run["forecast_requests"] if r["request"]["driver_number"] == 4] == reference
    paired = {"matched": pair["matched"], "same_policy_exact": equivalents,
              "candidate_order_and_population_independent": shorter["results"]["medium"] == pair["results"]["medium"],
              "unrelated_driver_stream_unchanged": other_unchanged, "strategy_draws": material,
              "cross_driver_covariance": "NOT_MODELED",
              "two_stop_stream_ids": [r.get("stream_id") for r in pair["results"]["two-stop"]["forecast_requests"]]}
    medium_requests = pair["results"]["medium"]["forecast_requests"]
    hard_requests = pair["results"]["hard"]["forecast_requests"]
    paired["same_stop_replacement_stream_matched"] = (len(medium_requests) == len(hard_requests) == 2 and
                                                        medium_requests[1]["stream_id"] == hard_requests[1]["stream_id"])
    good = (all(all(v.values()) for branch in checks.values() for v in branch.values())
            and all(branch["status"] == "MARKERS_REACHED" and not branch["unsupported"]
                    for scene in results.values() for branch in scene["result"]["results"].values()))
    clean_marks = [focal["pairs"][key]["combined_delta_us"] for key in ("medium", "hard", "used-soft")]
    following = any((r["following_only_us"] or 0) > 0 for run in two["results"].values()
                    for r in run["checkpoint_constraints"] if r["driver"] == 63)
    stack_wait = any(p["wait_us"] > 0 for p in results["shared-box"]["result"]["results"]["double-stack"]["pit"])
    integrity = (good and following and stack_wait and all(v is not None for v in clean_marks) and len(set(clean_marks)) > 1
                 and equivalents and paired["same_stop_replacement_stream_matched"] and
                 paired["candidate_order_and_population_independent"] and other_unchanged and
                 len(set(paired["two_stop_stream_ids"])) == 3)
    return results, checks, paired, integrity


def replacement_requests(root, bundle, horizons, samples):
    scenes, features = load_controls(root)
    input_row, = read_forecast_inputs([features[63]])
    r = input_row.request
    r = replace(r, n_trajectory_samples=samples)
    out = []
    for horizon in horizons:
        req = replace(r, horizon_laps=horizon)
        batch = forecast_pace(bundle, req)
        row = forecast_document(req, batch)
        row["draw_lap1_lap2_correlation"] = float(np.corrcoef(batch.lap_samples_s[:, :2].T)[0, 1]) if horizon >= 2 else None
        out.append(row)
    for tyre in scenes[0].resources[0].tyres:
        for delay in (0, 2):
            stop = PitStopRequest(r.session_key, 63, r.as_of_utc, r.planned_start_lap+delay,
                                  tyre, "stage3-r1-hypothetical")
            req = replacement_pace_request(r, stop, horizon=10,
                                           seed=int(fingerprint((r.random_seed, delay))[:16], 16), samples=samples)
            batch = forecast_new_stint(bundle, req)
            out.append(forecast_document(req, batch) | {"fitted_set": tyre.set_id, "hypothetical_delay_laps": delay})
    replacement_state_valid = all(
        row["request"]["tyre_age_at_start"] == (3 if row["fitted_set"] == "used-soft" else 0)
        and row["request"]["planned_start_lap"] == r.planned_start_lap+row["hypothetical_delay_laps"]+1
        and row["request"]["as_of_utc"] == r.as_of_utc
        and row["request"]["clean_laps_observed_current_stint"] == 0
        and not row["request"]["observed_stint_clean_times_s"]
        for row in out if "fitted_set" in row
    )
    return {"cutoff": r.as_of_utc, "rows": out, "replacement_state_valid": replacement_state_valid,
            "all_numeric_integrity": all(x["numerical_integrity"] for x in out),
            "weather_valid": datetime.fromisoformat(features[63]["evidence_as_of"]["weather"]) <= r.as_of_utc,
            "min_max_expected_lap_s": [min(lap["expected_lap_time_s"] for f in out for lap in f["forecast"]["lap_forecasts"]
                                          if isfinite(lap["expected_lap_time_s"])),
                                       max(lap["expected_lap_time_s"] for f in out for lap in f["forecast"]["lap_forecasts"]
                                           if isfinite(lap["expected_lap_time_s"]))]}
