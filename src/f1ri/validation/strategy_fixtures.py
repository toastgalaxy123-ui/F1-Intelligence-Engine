"""Versioned deterministic strategy fixture families and bounded sensitivity cases."""
from dataclasses import replace

from f1ri.simulator.pit_execution import ReplacementTyres
from f1ri.simulator.race_control import ControlCommand as C
from f1ri.simulator.race_control import RaceControlProfile
from f1ri.simulator.strategy import StrategyScenario
from f1ri.simulator.strategy_pace import PaceCurve, PaceMode, PaceSource
from f1ri.simulator.strategy_policy import DriverResources, Policy, PolicyKind, Stop
from f1ri.validation.neutralization_scenarios import controls, synthetic_scenarios
from f1ri.validation.overtake_scenarios import field, fixtures

PROFILE = RaceControlProfile()
CONTINUE = Policy("continue", PolicyKind.CONTINUE)


def _policy(name, kind, *actions):
    return Policy(name, kind, actions)


def strategy_families():
    frozen, s2, _ = synthetic_scenarios(PROFILE)
    state, physical = frozen["pit_green"]["initial"], frozen["pit_green"]["physical"]
    route = s2["resolved"].stops[0].plan.route
    state = replace(state, ruleset=replace(state.ruleset, scheduled_laps=20),
                    cars=tuple(replace(c, team=f"Team {c.driver_number}",
                                             tyres=replace(c.tyres, compound="HARD", age_laps=8, set_id="worn"))
                                      for c in state.cars))
    resources = tuple(DriverResources(c.driver_number, c.team, (
        ReplacementTyres("MEDIUM", set_id="medium"), ReplacementTyres("HARD", set_id="hard"),
        ReplacementTyres("SOFT", 2, set_id="used-soft"),
    ), 725., replace(route, box_id=c.team)) for c in state.cars)
    curves = tuple(PaceCurve(c.driver_number, set_id, age, tuple(base+i*slope for i in range(14)), "DECLARED")
                   for c in state.cars for set_id, age, base, slope in (
                       ("worn", 8, 90. if c.driver_number == 63 else 92., .0),
                       ("medium", 0, 76., 1.5), ("hard", 0, 84., .3), ("used-soft", 2, 73., 5.),
                   ))
    pace = PaceSource(PaceMode.DECLARED_SYNTHETIC, curves, provenance="S5-C declared lap-by-age curves")
    early = _policy("pit-now-medium", PolicyKind.NEXT, Stop(63, "medium"))
    late = _policy("extend-one", PolicyKind.EXTEND, Stop(63, "medium", delay_laps=1))
    hard = _policy("pit-now-hard", PolicyKind.COMPOUND, Stop(63, "hard"))
    used = _policy("pit-now-used-soft", PolicyKind.COMPOUND, Stop(63, "used-soft"))
    two = _policy("two-stop", PolicyKind.SEQUENCE, Stop(63, "medium", lap=2), Stop(63, "used-soft", lap=4))
    green = StrategyScenario("green", "s5-c-family", state, physical, resources, pace,
                             (CONTINUE, early, late, hard, used, two), ((63, 17),), 700_000_000, 6)
    vsc = replace(green, scenario_id="vsc",
                  controls=controls(state, [(C.VSC, 156), (C.VSC_ENDING, 182), (C.GREEN, 194)]),
                  policies=(CONTINUE, _policy("react-vsc", PolicyKind.VSC, Stop(63, "medium")),
                            _policy("after-green", PolicyKind.NEXT, Stop(63, "medium", review_us=195_000_000))))
    sc = replace(green, scenario_id="sc", profile=replace(PROFILE, sc_speed_mps=8.),
                 controls=controls(state, [(C.SC, 156), (C.SC_IN, 192), (C.SC_WITHDRAWN, 205), (C.GREEN, 206)]),
                 policies=(CONTINUE, _policy("react-sc", PolicyKind.SC, Stop(63, "medium")),
                           _policy("after-green", PolicyKind.NEXT, Stop(63, "medium", review_us=207_000_000))))
    red = replace(green, scenario_id="red",
                  controls=controls(state, [(C.RED, 156), (C.PREPARE, 179), (C.ROLLING, 184),
                                            (C.SC_IN, 198), (C.SC_WITHDRAWN, 205), (C.GREEN, 206)]),
                  policies=(CONTINUE, early, late))
    multi = replace(vsc, scenario_id="vsc-then-sc", controls=(*vsc.controls, *tuple(
        replace(e, event_id=f"later-{e.event_id}") for e in
        controls(state, [(C.SC, 280), (C.SC_IN, 305), (C.SC_WITHDRAWN, 320), (C.GREEN, 321)]))))
    degradation = replace(green, scenario_id="degradation", pace=replace(pace, curves=tuple(
        replace(c, lap_seconds=tuple(v+5*i for i, v in enumerate(c.lap_seconds))) if c.set_id == "worn" else c
        for c in pace.curves)))
    team_state, team_phys = field(((63, 1, 675.), (4, 1, 668.)), "shared-strategy")
    team_state = replace(team_state, cars=tuple(replace(c, team="Shared", tyres=replace(c.tyres, set_id="worn",
                         age_laps=8)) for c in team_state.cars))
    prior = {p.plan.request.driver_number: p.plan.route for p in s2["double_stack"].stops}
    team_res = tuple(replace(r, team="Shared", route=replace(prior[r.driver], service_s=6.)) for r in resources)
    team = replace(green, scenario_id="shared-box", initial=team_state, physical=team_phys, resources=team_res,
                   targets=((63, 11), (4, 11)), end_us=400_000_000,
                   policies=(CONTINUE, _policy("stack", PolicyKind.TEAM, Stop(63, "medium"), Stop(4, "medium")),
                             _policy("63-first", PolicyKind.TEAM, Stop(63, "medium"), Stop(4, "medium", delay_laps=1)),
                             _policy("4-first", PolicyKind.TEAM, Stop(4, "medium"), Stop(63, "medium", delay_laps=1))))
    car_state, layout, old_pace, attempts, _, _ = fixtures()[0]["authorized_pair"]
    car_state = replace(car_state, cars=tuple(replace(c, tyres=replace(c.tyres, set_id="worn")) for c in car_state.cars))
    pass_res = tuple(replace(resources[0], driver=d, team=f"Team {d}", route=replace(route, box_id=f"Box {d}"))
                     for d in old_pace)
    pass_curves = tuple(PaceCurve(d, t, 2 if t == "worn" else 0, (p,)*14, "DECLARED")
                        for d, p in old_pace.items() for t in ("worn", "medium"))
    traffic = replace(green, scenario_id="traffic-no-pass", initial=car_state, physical=layout, resources=pass_res,
                      pace=replace(pace, curves=pass_curves), targets=((2, 15),), end_us=260_000_000,
                      policies=(CONTINUE, _policy("pit-car-2", PolicyKind.NEXT, Stop(2, "medium"))))
    scripted = replace(traffic, scenario_id="traffic-scripted-pass", passes=attempts)
    return {s.scenario_id: s for s in (green, vsc, sc, team, degradation, red, traffic, scripted, multi)}


def sensitivity_families(families):
    sc = families["sc"]
    green = families["green"]
    for label, change in (
        ("deploy-earlier", {"controls": (replace(sc.controls[0], at_us=155_500_000), *sc.controls[1:])}),
        ("deploy-later", {"controls": (replace(sc.controls[0], at_us=162_000_000), *sc.controls[1:])}),
        ("longer-sc", {"controls": (sc.controls[0], *(replace(c, at_us=c.at_us+22_000_000) for c in sc.controls[1:]))}),
        ("faster-sc", {"profile": replace(sc.profile, sc_speed_mps=13.)}),
        ("larger-queue-gap", {"profile": replace(sc.profile, queue_gap_m=16, train_gap_m=22)}),
        ("restart-pace", {"profile": replace(sc.profile, restart_speed_mps=27.)}),
        ("initial-gap", {"physical": replace(sc.physical, cars=tuple(
            replace(c, progress_m=c.progress_m-4.) if c.driver_number == 4 else c for c in sc.physical.cars))}),
        ("faster-and-later", {"profile": replace(sc.profile, sc_speed_mps=13.),
                              "controls": (replace(sc.controls[0], at_us=162_000_000), *sc.controls[1:])}),
        ("longer-service", {"resources": tuple(replace(r, route=replace(r.route, service_s=r.route.service_s+2.))
                                                for r in sc.resources)}),
        ("longer-exit", {"resources": tuple(replace(r, route=replace(r.route, merge_s=r.route.merge_s+3.))
                                             for r in sc.resources)}),
    ):
        yield label, replace(sc, scenario_id=f"sc-{label}", **change), "react-sc"
    for label, slope in (("flat", 0.), ("heavy", 12.)):
        pace = replace(green.pace, curves=tuple(replace(c, lap_seconds=tuple(v+slope*i
                       for i, v in enumerate(c.lap_seconds))) if c.driver == 63 and c.set_id == "worn" else c
                       for c in green.pace.curves))
        yield f"tyres-{label}", replace(green, scenario_id=f"tyres-{label}", pace=pace), "pit-now-medium"
