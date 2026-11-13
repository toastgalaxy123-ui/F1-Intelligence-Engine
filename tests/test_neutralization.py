from dataclasses import replace

import pytest

from f1ri.simulator.neutralization import simulate_neutralization
from f1ri.simulator.overtaking import simulate_scripted_overtaking
from f1ri.simulator.race_control import ControlCommand as C
from f1ri.simulator.race_control import ControlPhase as P
from f1ri.simulator.race_control import RaceControlProfile
from f1ri.validation.neutralization_scenarios import controls, run_synthetic
from f1ri.validation.overtake_scenarios import field


@pytest.fixture(scope="module")
def study():
    return run_synthetic(RaceControlProfile())


def test_green_delegate_and_determinism(study):
    scenarios, results, _ = study
    spec = scenarios["green"]
    assert results["green"].execution == simulate_scripted_overtaking(
        spec["initial"], spec["physical"], spec["clean_lap_s"], (), laps=2
    )
    assert results["sc_train"] == simulate_neutralization(**scenarios["sc_train"])


def test_checkpoint_boundary_is_continuous(study):
    _, runs, _ = study
    clocks = []
    for name in ("checkpoint_minus", "checkpoint", "checkpoint_plus"):
        rows = [r.crossing_us for r in runs[name].execution.traversals if r.driver_number == 1]
        assert len(rows) == len(set(rows))
        clocks.append(rows)
    assert clocks[1][0] == 10_000_000
    assert max(abs(a-b) for a, b in zip(clocks[0], clocks[2], strict=True)) <= 3


def test_vsc_preserves_gaps_and_sc_collects(study):
    _, runs, _ = study
    vsc = [f for f in runs["vsc"].progress if f["phase"] in (P.VSC_ACTIVE, P.VSC_ENDING)]
    assert all(f["gaps_m"][2] == pytest.approx(vsc[0]["gaps_m"][2]) for f in vsc)
    train = next(f for f in runs["sc_train"].progress if f["kind"] == "TRAIN_FORMED")
    assert max(g for d, g in train["gaps_m"].items() if d != train["pacer"]) <= 18
    assert train["pacer"] in train["positions_m"]
    assert set(train["classification"]) == {1, 2, 3}
    assert train["virtual_sc_m"] >= train["positions_m"][train["pacer"]]


def test_rolling_release_is_line_gated(study):
    _, runs, _ = study
    sc = runs["sc_two"]
    deferred, released = [r for r in sc.controls if r["event"]["command"] is C.GREEN]
    assert deferred["outcome"] == "DEFERRED"
    assert released["clock_us"] > deferred["clock_us"]
    withdrawal = next(r["clock_us"] for r in sc.controls if r["event"]["command"] is C.SC_WITHDRAWN)
    laps = {f.event.driver_number for f in sc.execution.run.trace
            if f.event.kind.name == "LAP_COMPLETED" and withdrawal < f.event.at_us < released["clock_us"]}
    assert laps == {1, 2}


def test_restart_allows_green_pass_and_blocks_early_attempt():
    from f1ri.simulator.overtaking import OvertakeAttempt

    state, physical = field(((1, 3, 200), (2, 3, 150)), "restart-pass")
    script = controls(state, [(C.VSC, .1), (C.VSC_ENDING, .5), (C.GREEN, 1)])
    early = OvertakeAttempt("early", state.simulation_id, 2, 1, physical.layout_version,
                            500_000, 500_000, 30_000_000, 10, "S5")
    late = replace(early, attempt_id="late", proposed_us=1_000_000, window_start_us=1_000_000)
    result = simulate_neutralization(state, physical, {1: 90, 2: 45}, script,
                                     laps=2, attempts=(early, late))
    assert [(r.attempt_id, r.status) for r in result.execution.results] == [
        ("early", "REJECTED"), ("late", "COMPLETED")]


def test_pending_and_completed_passes(study):
    _, runs, _ = study
    assert runs["cancel_pass"].execution.results[0].status == "CANCELLED"
    assert runs["completed_pass"].execution.results[0].status == "COMPLETED"
    assert runs["completed_pass"].execution.results[0].committed_us == 7_500_000


def test_red_freezes_distance_checkpoints_and_tires(study):
    _, runs, _ = study
    hold = runs["red_hold"]
    a, b = hold.progress[-2:]
    assert a["positions_m"] == b["positions_m"]
    assert a["checkpoint_counts"] == b["checkpoint_counts"]
    assert b["suspended_us"] == 55_000_000 and b["active_elapsed_us"] == 5_000_000
    assert not hold.execution.traversals
    restart = runs["red_restart"]
    resume = next(f for f in restart.progress if f["phase"] is P.RESTART_ROLLING)
    assert resume["suspended_us"] == 30_000_000 and resume["active_elapsed_us"] == 5_000_000
    assert restart.execution.traversals


def test_escalation_and_unsupported_operations(study):
    _, runs, _ = study
    assert [r["after"] for r in runs["escalation"].controls[:3]] == [
        P.VSC_ACTIVE, P.SC_COLLECTING_FIELD, P.RED_SUSPENDED]
    assert {r["reason"] for r in runs["exceptions"].controls if r["outcome"] == "UNSUPPORTED"} == {
        "UNLAPPING_PROCEDURE_UNSUPPORTED", "STANDING_RESTART_UNSUPPORTED", "STATIONARY_RED_SERVICE_UNSUPPORTED"}
    assert sum(r["outcome"] == "REJECTED" for r in runs["invalid"].controls) == 2
    assert {f["phase"] for f in runs["local_only"].progress} == {P.GREEN_RUNNING}
    assert any(r["outcome"] == "SUPERSEDED" for r in runs["restart_interrupted"].controls)


def test_pit_service_and_counterfactual_are_separate(study):
    _, runs, pit = study
    assert all(r["status"] == "RESOLVED" for r in pit)
    assert {r["elapsed_route_us"] for r in pit[:3]} == {57_000_000}
    assert {r["service_us"] for r in pit} == {3_000_000}
    assert pit[1]["strategic_loss_us"] < pit[0]["strategic_loss_us"]
    assert [r["queue_us"] for r in pit if r["scenario"] == "shared_box"] == [0, 2_000_000]
    for n in ("pit_coarse", "pit_red_entry", "pit_missed_entry"):
        assert any(r["phase"] == "PIT_UNSUPPORTED" for r in runs[n].execution.interactions)


@pytest.mark.parametrize("name", ["finish_sc", "finish_restart", "finish_red", "finish_vsc"])
def test_race_finishes_via_original_classification(name, study):
    _, runs, _ = study
    final = runs[name].execution.run.final_state
    assert final.phase == "FINISHED"
    assert final.timing_order == (1, 3, 2)
    assert {c.driver_number: c.completed_laps for c in final.cars} == {1: 20, 2: 19, 3: 20}


def test_retirement_does_not_reinsert_car(study):
    _, runs, _ = study
    retired = runs["retirement"]
    assert 2 not in retired.progress[-1]["neighbors"]
    assert next(c for c in retired.execution.run.final_state.cars if c.driver_number == 2).status == "RETIRED"
