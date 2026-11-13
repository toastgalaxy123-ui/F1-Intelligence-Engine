"""Synthetic pit-entry and restart fixtures for S5-A.1."""
from dataclasses import replace

from f1ri.simulator.neutralization import simulate_neutralization
from f1ri.simulator.overtaking import OvertakeAttempt
from f1ri.simulator.pit_restart import PitDecision
from f1ri.simulator.race_control import ControlCommand as C
from f1ri.validation.neutralization_scenarios import controls, synthetic_scenarios
from f1ri.validation.overtake_scenarios import field


def fixtures(profile):
    frozen, s2, _ = synthetic_scenarios(profile)
    base = frozen["pit_green"] | {"environment": (), "individual_restart": True}
    state = base["initial"]
    s2_plan = s2["resolved"].stops[0].plan
    # Synthetic track fixture declares the branch independently in metres.
    # The S2 route supplies only elapsed offsets once the branch is reached.
    decision = PitDecision("63-stop", state.simulation_id, state.race_clock_us, 270_000_000, s2_plan,
                           base["physical"].layout_version, 725., "DECLARED_SYNTHETIC_900M_BRANCH",
                           reference_entry_us=160_000_000)
    run = simulate_neutralization(**(base | {"pit_decisions": (decision,)}))
    green_entry = next(r["at_us"] for r in run.pit_ledger if r["status"] == "PIT_ENTRY_COMMITTED")
    specs = {"p1_frozen_green": frozen["pit_green"]}

    def pit(name, seq, row=None, **extra):
        specs[name] = base | {"controls": controls(state, seq), "pit_decisions": (row or decision,)} | extra

    pit("dynamic_green", [])
    pit("p2_vsc", [(C.VSC, 156)])
    pit("p3_vsc_ends", [(C.VSC, 156), (C.VSC_ENDING, 157), (C.GREEN, 158)])
    pit("p4_sc", [(C.SC, 156)], clean_lap_s={63: 60., 4: 92.},
        profile=replace(profile, sc_speed_mps=8))
    pit("p5_red", [(C.RED, 156), (C.PREPARE, 179), (C.ROLLING, 184)])
    for name, delta in (("p6_exact", 0), ("p7_before", -1), ("p8_after", 1)):
        pit(name, [(C.VSC, (green_entry+delta)/1e6)])
    pit("p9_missed", [], replace(decision, at_us=green_entry+1_000_000))
    pit("p10_cancelled", [], replace(decision, cancel_us=156_000_000))
    pit("p14_rejoin", [(C.SC, 156), (C.SC_IN, 176), (C.SC_WITHDRAWN, 193), (C.GREEN, 194)])
    pit("geometry_unsupported", [], replace(decision, entry_progress_m=None))
    pit("expired_entry", [(C.RED, 156)], replace(decision, valid_until_us=160_000_000))

    left, physical = field(((63, 1, 675.), (4, 1, 668.)), "shared-dynamic")
    shared_plans = {visit.plan.request.driver_number: visit.plan for visit in s2["double_stack"].stops}
    # Different route travel to the same box permits order reversal without an
    # on-track pass. Use a distinct declared fork for this synthetic teammate.
    faster_path = replace(shared_plans[4].route, box_arrival_s=91, service_s=3, timing_line_s=99)
    plans = (replace(shared_plans[63], route=replace(shared_plans[63].route, service_s=6)),
             replace(shared_plans[4], route=faster_path))
    shared = tuple(PitDecision(f"box-{plan.request.driver_number}", left.simulation_id, 0, 90_000_000,
                               plan, physical.layout_version, 730 if plan.request.driver_number == 4 else 725,
                               "DECLARED_SYNTHETIC_SHARED_BOX") for plan in plans)
    for label, seq, pace in (
        ("p11_box_order", [(C.VSC, 1)], {63: 90., 4: 90.}),
        ("p12_box_reverse", [(C.SC, 1)], {63: 90., 4: 45.}),
        ("p13_box_committed", [(C.SC, 27)], {63: 90., 4: 135.}),
    ):
        specs[label] = {"initial": left, "physical": physical, "clean_lap_s": pace,
                        "controls": controls(left, seq), "pit_decisions": shared, "individual_restart": True,
                        "laps": 4, "end_us": 150_000_000, "profile": profile}
    for name, source in (("r1_two", "sc_two"), ("r2_train", "sc_train"), ("r3_lapped", "sc_lapped"),
                         ("r6_finish", "finish_restart"), ("r7_interruption", "restart_interrupted"),
                         ("x1_green", "green"), ("x2_vsc", "vsc"),
                         ("x3_sc", "sc_train"), ("x4_red", "red_restart"),
                         ("x5_committed_pass", "completed_pass")):
        specs[name] = frozen[source] | {"individual_restart": True}
    specs["r8_pit_exit"] = specs["p14_rejoin"]
    for name, time in (("r9_same_tick", 70.), ("r10_incomplete", 30.)):
        first, positions = field(((1, 3, 200), (2, 3, 150)), name)
        seq = [(C.SC, 1), (C.SC_IN, 2), (C.SC_WITHDRAWN, time), (C.GREEN, time)]
        if name == "r10_incomplete":
            seq = [(C.SC, 1), (C.GREEN, time)]
        specs[name] = {"initial": first, "physical": positions, "clean_lap_s": {1: 90., 2: 90.},
                       "controls": controls(first, seq), "individual_restart": True,
                       "laps": 3, "end_us": 145_000_000, "profile": profile}
    start, physical = field(((1, 3, 850), (2, 3, 800)), "two-pass-permissions")
    # Car 2's first proposal occurs after global clear but before its line.
    # A later proposal reaches the same subsequent racing checkpoint after release.
    def attempt(name, at, cp):
        return OvertakeAttempt(name, start.simulation_id, 2, 1, physical.layout_version,
                               at, at, 40_000_000, cp, "S5_A1_RESTART")
    seq = [(C.SC, .1), (C.SC_IN, .2), (C.SC_WITHDRAWN, .3), (C.GREEN, .3)]
    specs["r4_r5_permission"] = {"initial": start, "physical": physical, "clean_lap_s": {1: 90., 2: 45.},
                                 "controls": controls(start, seq), "individual_restart": True,
                                 "attempts": (attempt("restricted", 400_000, 12), attempt("released", 5_200_000, 13)),
                                 "laps": 3, "end_us": 110_000_000, "profile": profile}
    return specs


def pit_accounting(specs, results):
    output = []
    for name in ("dynamic_green", "p2_vsc", "p3_vsc_ends", "p4_sc", "p5_red", "p14_rejoin"):
        result = results[name]
        stay = simulate_neutralization(**(specs[name] | {"pit_decisions": ()}))
        row = specs[name]["pit_decisions"][0]
        d = row.driver
        entered = next(r for r in result.pit_ledger if r["status"] == "PIT_ENTRY_COMMITTED")
        box = next(r for r in result.pit_ledger if r["status"] == "SERVICE_QUEUE")
        merged = next(r for r in result.pit_ledger if r["status"] == "REJOINED")
        marker = next((f for f in stay.execution.run.trace if f.event.driver_number == d
                       and f.event.checkpoint == row.line+1), None)
        merge = box["merge_us"]
        entry = entered["at_us"]
        lane_entry = next(r["at_us"] for r in result.pit_ledger if r["status"] == "PIT_LANE_TRAVERSAL")
        output.append({"scenario": name, "driver": d, "reference_entry_us": row.reference_entry_us,
                       "entry_us": entry, "entry_shift_us": entry-row.reference_entry_us,
                       "arrival_us": box["at_us"], "service_start_us": box["service_start_us"],
                       "service_end_us": box["service_end_us"], "merge_us": merge,
                       "elapsed_pit_us": merge-entry, "service_us": box["service_us"], "wait_us": box["wait_us"],
                       "lane_motion_us": box["lane_exit_us"]-lane_entry-box["service_us"]-box["wait_us"],
                       "transition_us": lane_entry-entry+merge-box["lane_exit_us"],
                       "stay_out_from_entry_us": marker.event.at_us-entry if marker else None,
                       "strategic_loss_us": merge-marker.event.at_us if marker else None,
                       "pit_rank_at_merge": merged["classification"].index(d)+1,
                       "stay_out_rank_at_marker": marker.state.timing_order.index(d)+1 if marker else None,
                       "merge_predecessor": merged["neighbors"].get(d)})
    return output
