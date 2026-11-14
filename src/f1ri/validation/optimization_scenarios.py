"""Declared S6-A scenarios with archived, cutoff-known Singapore input features."""

from dataclasses import replace

from f1ri.simulator.overtaking import OvertakeAttempt
from f1ri.simulator.race_control import ControlCommand as C
from f1ri.simulator.strategy_forecast import read_forecast_inputs
from f1ri.simulator.strategy_optimization import OptimizationRequest, PaceSensitivity, SearchWindow
from f1ri.simulator.strategy_policy import Policy, PolicyKind, Stop
from f1ri.validation.neutralization_scenarios import controls
from f1ri.validation.stage3_r_strategy import _slower_opponent, load_controls


def scenario_requests(root):
    cases, evidence = load_controls(root)
    scenes = {s.scenario_id: s for s in cases}
    blocked, single = scenes["two-car"], scenes["clean-air"]
    clock = single.initial.race_clock_us/1e6
    vsc_late = replace(single, scenario_id="late-vsc", controls=controls(single.initial, [
        (C.VSC, clock+89), (C.VSC_ENDING, clock+148), (C.GREEN, clock+159)]))
    opponent = replace(blocked, scenario_id="opponent-pits", opponents=(
        Policy("opponent-pit", PolicyKind.NEXT, (Stop(4, "medium"),)),))
    passing = replace(blocked, scenario_id="scripted-pass", passes=(OvertakeAttempt(
        "declared-attempt", blocked.initial.simulation_id, 63, 4, blocked.physical.layout_version,
        blocked.initial.race_clock_us+1, blocked.initial.race_clock_us+1,
        blocked.initial.race_clock_us+100_000_000,
        next(c.completed_checkpoints+1 for c in blocked.initial.cars if c.driver_number == 63),
        "S6_A_DECLARED_PASS_SENSITIVITY"),))
    scenes.update({x.scenario_id: x for x in (vsc_late, opponent, passing)})
    return scenes, evidence


def fixture_request(root, name, *, horizon=5, sets=("hard", "medium", "soft", "used-soft"),
                    stop_options=3, max_stops=1, budget=64, information="POINT_IN_TIME",
                    objective=None, cache=True, fixtures=None, driver=63):
    scenes, evidence = fixtures or scenario_requests(root)
    scene = scenes[name]
    team = name == "shared-box"
    target_drivers = (63, 4) if team else (driver,)
    cars = {c.driver_number: c for c in scene.initial.cars}
    # Use the same absolute marker for both teammates, inside both physical budgets.
    common_marker = min(cars[d].completed_checkpoints+horizon*3 for d in target_drivers)
    scene = replace(scene, experiment_id="s6-a-v1", targets=tuple((d, common_marker) for d in target_drivers),
                    horizon_laps=horizon, end_us=scene.initial.race_clock_us+(horizon*175+60)*1_000_000)
    window = tuple(SearchWindow(d, cars[d].completed_laps+1, cars[d].completed_laps+stop_options, tuple(sets))
                   for d in target_drivers)
    features = read_forecast_inputs([
        _slower_opponent(evidence[c.driver_number]) if c.driver_number == 4 and name != "original-opponent"
        else evidence[c.driver_number] for c in scene.initial.cars
    ])
    return OptimizationRequest(f"{name}-h{horizon}", scene, features, window, max_stops=max_stops,
                               objective=objective or ("TEAM_COMBINED" if team else "COMMON_MARKER_TIME"),
                               lead_driver=63 if team else None, weights=((63, 2), (4, 1)) if team else (),
                               budget=budget, information_mode=information, cache_forecasts=cache)


def perturbations(request):
    scene = request.scenario
    for tag, change in (
        ("mean-pace-plus-0.3s", PaceSensitivity(level_s=.3)),
        ("soft-plus-0.3s", PaceSensitivity(compound="SOFT", compound_s=.3)),
        ("degradation-plus-0.06s-per-age-lap", PaceSensitivity(age_s_per_lap=.06)),
    ):
        yield tag, replace(request, request_id=tag, sensitivity=change)
    for tag, travel, service in (("pit-travel-plus-2s", 2., 0.), ("service-plus-1s", 0., 1.)):
        resources = tuple(replace(r, route=replace(r.route, service_s=r.route.service_s+service,
                                 lane_exit_s=r.route.lane_exit_s+travel+service, merge_s=r.route.merge_s+travel+service,
                                 out_sector_2_s=r.route.out_sector_2_s+travel+service,
                                 out_line_s=r.route.out_line_s+travel+service)) for r in scene.resources)
        yield tag, replace(request, request_id=tag, scenario=replace(scene, resources=resources))


def control_perturbation(request, tag):
    profile = request.scenario.profile
    if tag == "vsc-speed-minus-10pct":
        profile = replace(profile, vsc_speed_mps=profile.vsc_speed_mps*.9)
    elif tag == "restart-speed-plus-10pct":
        profile = replace(profile, restart_speed_mps=profile.restart_speed_mps*1.1)
    else:
        raise ValueError("Unsupported declared neutralization perturbation")
    return replace(request, request_id=tag, scenario=replace(request.scenario, profile=profile))
