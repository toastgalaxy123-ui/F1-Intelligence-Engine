"""Synthetic mechanical scenarios and shared S2 itinerary accounting."""
from dataclasses import replace
from datetime import timedelta

from f1ri.simulator.events import Event, EventKind
from f1ri.simulator.neutralization import simulate_neutralization
from f1ri.simulator.pit_execution import PitCarPlan, simulate_pit_scenario
from f1ri.simulator.proximity import PhysicalRoute, PositionQuality, TrackPosition
from f1ri.simulator.race_control import ControlCommand as C
from f1ri.simulator.race_control import RaceControlEvent
from f1ri.simulator.state import TrackState
from f1ri.validation.overtake_scenarios import field, fixtures


def controls(state, items):
    return tuple(RaceControlEvent(f"{i:03d}-{cmd.name}", state.simulation_id, cmd, round(at*1e6), "S5_SYNTHETIC",
                                  **(options[0] if options else {}))
                 for i, (cmd, at, *options) in enumerate(items))


def synthetic_scenarios(profile):
    specs, s2 = fixtures()
    scenarios = {}

    def add(name, sequence, *, source="no_pass", end=120, environment=None, attempts=None):
        state, physical, pace, passes, env, _ = specs[source]
        scenarios[name] = {"initial": state, "physical": physical, "clean_lap_s": pace,
                               "controls": controls(state, sequence), "laps": 7,
                               "attempts": passes if attempts is None else attempts,
                               "environment": env if environment is None else environment,
                               "end_us": round(end*1e6), "profile": profile}

    vsc = [(C.VSC, 5), (C.VSC_ENDING, 20), (C.GREEN, 31)]
    sc = [(C.SC, 5), (C.SC_IN, 44), (C.SC_WITHDRAWN, 61), (C.GREEN, 62)]
    red = [(C.RED, 5), (C.PREPARE, 30), (C.ROLLING, 35),
           (C.SC_IN, 48), (C.SC_WITHDRAWN, 53), (C.GREEN, 55)]
    scenarios["green"] = {"initial": specs["no_pass"][0], "physical": specs["no_pass"][1],
                              "clean_lap_s": specs["no_pass"][2], "controls": (), "laps": 2, "profile": profile}
    for label, when in (("checkpoint_minus", 9.999999), ("checkpoint", 10), ("checkpoint_plus", 10.000001)):
        add(label, [(C.VSC, when), *vsc[1:]])
    add("vsc", vsc)
    add("sc_two", sc)
    add("sc_train", sc, source="queue", attempts=())
    add("sc_lapped", sc, source="lapping", attempts=())
    add("cancel_pass", vsc, source="authorized_pair")
    add("completed_pass", [(C.SC, 9), *sc[1:]], source="authorized_pair")
    add("red_hold", [(C.RED, 5)], end=60)
    add("red_restart", red, end=175)
    add("escalation", [(C.VSC, 5), (C.SC, 20), (C.RED, 25), *red[1:]], end=175)
    add("exceptions", [(C.SC, 5), (C.UNLAP, 6), (C.STANDING, 7), (C.RED, 12), (C.RED_SERVICE, 13)])
    add("invalid", [(C.GREEN, 2), (C.VSC, 5), (C.VSC, 10), *vsc[1:]])
    add("restart_interrupted", [*sc, (C.RED, 64), (C.PREPARE, 80), (C.ROLLING, 90)], end=160)
    add("local_only", [(C.LOCAL, 4, {"scope": "SECTOR", "local_flag": TrackState.YELLOW}),
                       (C.LOCAL, 8, {"scope": "SECTOR", "local_flag": TrackState.GREEN})])
    add("retirement", sc, source="queue", attempts=(), environment=(Event(40_000_000, EventKind.RETIREMENT, 2),))

    state, physical = field(((1, 19, 790), (2, 18, 745), (3, 19, 710)), "finish")
    for label, seq in (("finish_sc", [(C.SC, 1)]),
                       ("finish_restart", [(C.SC, 1), (C.SC_IN, 2), (C.SC_WITHDRAWN, 3), (C.GREEN, 4)]),
                       ("finish_vsc", [(C.VSC, 1), (C.VSC_ENDING, 2), (C.GREEN, 3)]),
                       ("finish_red", [(C.RED, 1), (C.PREPARE, 20), (C.ROLLING, 22)])):
        scenarios[label] = {"initial": state, "physical": physical, "clean_lap_s": {1: 60., 2: 70., 3: 65.},
                                "controls": controls(state, seq), "laps": 3, "end_us": 150_000_000, "profile": profile}
    state, physical, pace, _, itinerary, _ = specs["dynamic_rejoin"]
    for label, seq in (("green", []), ("vsc", [(C.VSC, 160)]), ("sc", [(C.SC, 160)])):
        scenarios[f"pit_{label}"] = {"initial": state, "physical": physical, "clean_lap_s": pace,
                                        "controls": controls(state, seq), "environment": itinerary,
                                        "laps": 3, "end_us": 350_000_000, "profile": profile}
        scenarios[f"stay_{label}"] = scenarios[f"pit_{label}"] | {"environment": ()}
    scenarios["pit_coarse"] = scenarios["pit_vsc"] | {"environment": (
        Event(160_000_000, EventKind.COARSE_PIT_TRANSITION, 63, source="S2_COARSE"),)}
    scenarios["pit_red_entry"] = scenarios["pit_vsc"] | {"controls": controls(state, [(C.RED, 160)])}
    earlier_state = replace(state, cars=tuple(
        replace(c, completed_checkpoints=4) if c.driver_number == 63 else c for c in state.cars
    ))
    earlier_physical = replace(physical, cars=tuple(
        replace(c, progress_m=590.) if c.driver_number == 63 else c for c in physical.cars
    ))
    missed = tuple(replace(e, at_us=e.at_us-4_100_000) for e in itinerary)
    scenarios["pit_missed_entry"] = scenarios["pit_vsc"] | {
        "initial": earlier_state, "physical": earlier_physical, "environment": missed,
        "controls": controls(state, [(C.VSC, 155.1)]),
    }

    # Build a shared-box itinerary with distinct route-entry clocks using S2.
    pair_stop = s2["double_stack"].stops
    grid = s2["double_stack"].run.trace[0].state
    grid = replace(grid, phase=grid.phase.PRESTART)
    offset_pace = (91., 90., 90., 90., 90.)
    shared = simulate_pit_scenario(grid, (
        PitCarPlan(63, 0, (90.,)*5, (pair_stop[0].plan,)),
        PitCarPlan(4, 0, offset_pace, (pair_stop[1].plan,)),
    ))
    clock = 155_000_000
    before = [f for f in shared.run.trace if f.event.at_us <= clock][-1].state
    before = replace(before, race_clock_us=clock)
    phys = replace(physical, as_of_utc=before.race_start_utc+timedelta(microseconds=clock),
                   cars=tuple(TrackPosition(
                       c.driver_number, PhysicalRoute.RACING, c.completed_laps, c.grid_position,
                       (((clock/1e6 - (1 if c.driver_number == 4 else 0))/90) % 1)*900, 0.,
                       quality=PositionQuality.EXACT_SCENARIO,
                   ) for c in before.cars))
    pit_events = tuple(f.event for f in shared.run.trace if f.event.at_us >= 160_000_000
                       and (f.event.kind.name.startswith("PIT_") or f.event.kind is EventKind.TRACK_REJOIN
                            or f.event.source in ("PIT_ROUTE_LINE", "COMMON_MERGE")))
    scenarios["pit_shared_box"] = {"initial": before, "physical": phys, "clean_lap_s": {63: 90., 4: 90.},
                                       "controls": controls(before, [(C.SC, 160)]), "environment": pit_events,
                                       "laps": 3, "end_us": 350_000_000, "profile": profile}
    return scenarios, s2, shared


def pit_comparison(runs, stops, shared):
    output = []
    for label in ("green", "vsc", "sc", "shared_box"):
        actual = runs[f"pit_{label}"].execution.run
        reference = None if label == "shared_box" else runs[f"stay_{label}"].execution.run
        for visit in shared.stops if label == "shared_box" else stops["resolved"].stops:
            driver = visit.plan.request.driver_number
            frames = [f for f in actual.trace if f.event.driver_number == driver]
            by_kind = {f.event.kind.name: f.event.at_us for f in frames if f.event.kind.name.startswith("PIT_")
                       or f.event.kind is EventKind.TRACK_REJOIN}
            if "TRACK_REJOIN" not in by_kind:
                output.append({"scenario": label, "driver": driver, "status": "UNSUPPORTED"})
                continue
            merge = next(f for f in frames if f.event.source == "COMMON_MERGE")
            entry = by_kind["PIT_ROUTE_ENTRY"]
            rejoin = by_kind["TRACK_REJOIN"]
            cp = merge.event.checkpoint
            stay = next((f for f in reference.trace if f.event.driver_number == driver
                         and f.event.checkpoint == cp), None) if reference else None
            crossing_rank = merge.state.timing_order.index(driver)+1
            stay_rank = stay.state.timing_order.index(driver)+1 if stay else None
            service = by_kind["PIT_SERVICE_END"] - by_kind["PIT_SERVICE_START"]
            lane = by_kind["PIT_LANE_EXIT"] - by_kind["PIT_LANE_ENTRY"]
            output.append({"scenario": label, "driver": driver, "status": "RESOLVED",
                           "entry_us": entry, "merge_us": rejoin, "comparison_checkpoint": cp,
                           "lane_motion_us": lane-service-visit.queue_wait_us,
                           "service_us": service, "queue_us": visit.queue_wait_us,
                           "route_transition_us": rejoin-entry-lane, "elapsed_route_us": rejoin-entry,
                           "stay_out_crossing_us": stay.event.at_us if stay else None,
                           "stay_out_from_entry_us": stay.event.at_us-entry if stay else None,
                           "strategic_loss_us": rejoin-stay.event.at_us if stay else None,
                           "crossing_rank": crossing_rank, "stay_out_crossing_rank": stay_rank,
                           "crossing_rank_delta": crossing_rank-stay_rank if stay_rank is not None else None})
    return output


def run_synthetic(profile):
    scenarios, s2, shared = synthetic_scenarios(profile)
    runs = {name: simulate_neutralization(**spec) for name, spec in scenarios.items()}
    return scenarios, runs, pit_comparison(runs, s2, shared)
