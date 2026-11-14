"""Matched engineering fixtures for separating latent and realized pace."""
from collections import Counter
from dataclasses import replace

from f1ri.simulator.events import Event, EventKind
from f1ri.simulator.pit_execution import ReplacementTyres
from f1ri.simulator.race_control import ControlCommand as C
from f1ri.simulator.strategy import compare_strategies
from f1ri.simulator.strategy_constraints import CheckpointConstraints, constraint_summary
from f1ri.simulator.strategy_pace import PaceCurve
from f1ri.simulator.strategy_policy import DriverResources, Policy, PolicyKind, Stop
from f1ri.validation.neutralization_scenarios import controls
from f1ri.validation.overtake_scenarios import field
from f1ri.validation.strategy_fixtures import strategy_families


def cases(length):
    f = strategy_families()
    green = f["green"]
    focus = ("continue", "pit-now-medium", "pit-now-hard", "pit-now-used-soft", "extend-one", "two-stop")
    f["clean-air"] = replace(green, scenario_id="clean-air",
                             initial=replace(green.initial, cars=(green.initial.cars[0],)),
                             physical=replace(green.physical, cars=(green.physical.cars[0],)),
                             resources=(green.resources[0],), pace=replace(green.pace, curves=tuple(
                                 p for p in green.pace.curves if p.driver == 63)))
    f["unblocked"] = replace(green, scenario_id="unblocked",
                             retirements=(Event(290_000_000, EventKind.RETIREMENT, 4, source="DECLARED_RETIREMENT"),))
    f["longer-horizon"] = replace(green, scenario_id="longer-horizon", horizon_laps=8,
                                  targets=((63, 26),), end_us=950_000_000)
    for gap in (5., 70., 240.):
        cars = tuple(replace(p, progress_m=650.-gap) if p.driver_number == 4 else p
                     for p in green.physical.cars)
        initial = replace(green.initial, cars=tuple(
            replace(c, completed_checkpoints=c.completed_laps*3+int(p.progress_m//300))
            for c in green.initial.cars for p in cars if p.driver_number == c.driver_number))
        key = f"gap-{int(gap)}"
        f[key] = replace(green, scenario_id=key, initial=initial, physical=replace(green.physical, cars=cars))
    for opponent in (78., 94.):
        key = f"opponent-{int(opponent)}"
        f[key] = replace(green, scenario_id=key, pace=replace(green.pace, curves=tuple(
            replace(p, lap_seconds=(opponent,)*len(p.lap_seconds))
            if p.driver == 4 and p.set_id == "worn" else p for p in green.pace.curves)))
    f["multi-car"] = _multi(green, length)
    selected = ("green", "clean-air", "unblocked", "longer-horizon", "gap-5", "gap-70", "gap-240",
                "opponent-78", "opponent-94", "vsc", "sc", "shared-box", "multi-car")
    return {key: (f[key], "continue", tuple(p.strategy_id for p in f[key].policies
                                         if key != "green" or p.strategy_id in focus)) for key in selected}


def _multi(base, length):
    state, layout = field(((63, 2, 658.), (4, 2, 649.), (1, 2, 525.),
                           (16, 2, 358.), (81, 2, 270.), (87, 1, 148.)), "multi-imola-declared")
    positions = tuple(replace(p, progress_m=p.progress_m*length/900) for p in layout.cars)
    team = {63: "A", 4: "A", 1: "B", 16: "B", 81: "C", 87: "C"}
    initial = replace(state, cars=tuple(replace(c, team=team[c.driver_number],
                      tyres=replace(c.tyres, set_id="starting", age_laps=5+i,
                                    compound=("HARD", "MEDIUM", "SOFT")[i%3])) for i, c in enumerate(state.cars)))
    resources = tuple(DriverResources(c.driver_number, c.team, (
        ReplacementTyres("MEDIUM", set_id="medium"),
    ), length*.81, replace(base.resources[0].route, box_id=c.team)) for c in initial.cars)
    curves = tuple(PaceCurve(c.driver_number, set_id, age, tuple(pace+j*slope for j in range(12)), "DECLARED")
                   for i, c in enumerate(initial.cars) for set_id, age, pace, slope in (
                       ("starting", 5+i, 89.+i*1.6, .6+i*.07), ("medium", 0, 86.+i*1.5, .8)))
    control = controls(initial, [(C.VSC, 8), (C.VSC_ENDING, 32), (C.GREEN, 42),
                                 (C.SC, 138), (C.SC_IN, 211), (C.SC_WITHDRAWN, 234), (C.GREEN, 235)])
    return replace(base, scenario_id="multi-car", initial=initial, physical=replace(
        layout, length_m=length, layout_version="imola-geometry-declared-field", cars=positions),
        resources=resources, pace=replace(base.pace, curves=curves),
        policies=(base.policies[0], Policy("stack", PolicyKind.TEAM, (Stop(63, "medium"), Stop(4, "medium"))),
                  Policy("stagger", PolicyKind.TEAM, (Stop(63, "medium"), Stop(4, "medium", delay_laps=1))),
                  Policy("react-sc", PolicyKind.SC, (Stop(63, "medium"),))),
        targets=((63, 20), (4, 20)), controls=control, end_us=675_000_000)


def evaluate(s, reference, candidates=None):
    return compare_strategies(s, reference, candidates, recorder_factory=CheckpointConstraints)


def discriminate(result, scenario, left, right):
    a, b = (result["results"][x] for x in (left, right))
    sides = []
    for d, marker in scenario.targets:
        def indexed(run, d, marker):
            return {r["checkpoint"]: r for r in run["checkpoint_constraints"]
                    if r["driver"] == d and r["latent_us"] is not None and r["checkpoint"] <= marker}
        x, y = indexed(a, d, marker), indexed(b, d, marker)
        matched = sorted(x.keys() & y.keys())
        different = [cp for cp in matched if x[cp]["latent_us"] != y[cp]["latent_us"]]
        suppression = [cp for cp in different if abs(x[cp]["at_us"]-y[cp]["at_us"]) <= 1 and
                       (x[cp]["category"] == "PHYSICAL_FOLLOWING_LIMITED" or
                        y[cp]["category"] == "PHYSICAL_FOLLOWING_LIMITED")]
        effective = [cp for cp in different if abs(x[cp]["effective_us"]-y[cp]["effective_us"]) <= 1]
        comparison = []
        for cp in matched:
            comparison.append({"checkpoint": cp, "left": x[cp], "right": y[cp],
                               "latent_delta_us": y[cp]["latent_us"]-x[cp]["latent_us"],
                               "effective_delta_us": y[cp]["effective_us"]-x[cp]["effective_us"],
                               "crossing_delta_us": y[cp]["at_us"]-x[cp]["at_us"]})
        sides.append({"driver": d, "marker": marker, "matched_checkpoints": len(matched),
                      "different_latent_checkpoints": len(different),
                      "equal_effective_traversal_checkpoints": effective, "following_equal_crossing_checkpoints": suppression,
                      "latent_delta_on_matched_us": sum(y[cp]["latent_us"]-x[cp]["latent_us"] for cp in matched),
                      "effective_delta_on_matched_us": sum(y[cp]["effective_us"]-x[cp]["effective_us"] for cp in matched),
                      "left_summary": constraint_summary(a["checkpoint_constraints"], d, marker),
                      "right_summary": constraint_summary(b["checkpoint_constraints"], d, marker),
                      "left_marker": _marker(a, d, marker, scenario), "right_marker": _marker(b, d, marker, scenario),
                      "matched": comparison})
    return {"left": left, "right": right, "drivers": sides,
            "pit_elapsed_delta_us": _elapsed(b["pit"])-_elapsed(a["pit"]),
            "grouped_constraint_counts": {key: dict(Counter(r["category"] for r in run["checkpoint_constraints"]))
                                          for key, run in ((left, a), (right, b))}}


def _marker(run, d, cp, scenario):
    value = run["targets"].get(d)
    race_distance = scenario.initial.ruleset.scheduled_laps*3
    if value is None:
        return {"reached": False, "finish_order": None,
                "remaining_checkpoints": run.get("remaining_checkpoints", {}).get(d)}
    return value | {"reached": True, "supported": run["status"] != "UNSUPPORTED",
                    "physical_marker_m": cp*scenario.physical.length_m/3,
                    "remaining_checkpoints": race_distance-cp, "finish_order": run["finish_order"]}


def _elapsed(pits):
    return sum(r["pit_elapsed_us"] or 0 for r in pits)
