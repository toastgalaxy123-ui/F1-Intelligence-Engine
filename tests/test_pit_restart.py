from dataclasses import replace

import pytest

from f1ri.simulator.neutralization import simulate_neutralization
from f1ri.simulator.race_control import RaceControlProfile
from f1ri.validation.neutralization_study import _checks
from f1ri.validation.pit_restart_scenarios import fixtures, pit_accounting


@pytest.fixture(scope="module")
def study():
    specs = fixtures(RaceControlProfile())
    return specs, {k: simulate_neutralization(**s) for k, s in specs.items()}


def test_pit_progress_and_counterfactual(study):
    specs, runs = study
    accounting = {r["scenario"]: r for r in pit_accounting(specs, runs)}
    assert accounting["dynamic_green"]["entry_us"] == 160_000_000
    assert accounting["dynamic_green"]["merge_us"] == 217_000_000
    for name in ("p2_vsc", "p3_vsc_ends", "p4_sc", "p5_red"):
        assert accounting[name]["entry_us"] > 160_000_000
        assert accounting[name]["elapsed_pit_us"] == 57_000_000
        assert accounting[name]["service_us"] == 3_000_000
        assert accounting[name]["strategic_loss_us"] == 57_000_000 - accounting[name]["stay_out_from_entry_us"]
    assert accounting["p5_red"]["entry_us"] >= 184_000_000


@pytest.mark.parametrize(("name", "expected"), [("p6_exact", 160_000_000), ("p7_before", 160_000_001),
                                                ("p8_after", 160_000_000)])
def test_boundary_race_control_tie(name, expected, study):
    _, runs = study
    entries = [r for r in runs[name].pit_ledger if r["status"] == "PIT_ENTRY_COMMITTED"]
    assert [r["at_us"] for r in entries] == [expected]
    assert entries[0]["unwrapped_progress_m"] == pytest.approx(1625., abs=1e-4)


@pytest.mark.parametrize(("name", "status"), [("p9_missed", "MISSED_ENTRY"), ("p10_cancelled", "CANCELLED"),
                                              ("expired_entry", "MISSED_ENTRY"),
                                              ("geometry_unsupported", "UNSUPPORTED")])
def test_declined_stops_leave_no_pit_events(name, status, study):
    _, runs = study
    run = runs[name]
    assert any(r["status"] == status for r in run.pit_ledger)
    assert not any(r["status"] == "PIT_ENTRY_COMMITTED" for r in run.pit_ledger)


@pytest.mark.parametrize(("name", "order"), [("p11_box_order", [63, 4]), ("p12_box_reverse", [4, 63]),
                                             ("p13_box_committed", [63, 4])])
def test_actual_arrival_owns_box_queue(name, order, study):
    _, runs = study
    rows = [r for r in runs[name].pit_ledger if r["status"] == "SERVICE_QUEUE"]
    assert [r["driver"] for r in rows] == order
    assert rows[0]["wait_us"] == 0
    assert rows[1]["wait_us"] > 0
    assert rows[1]["service_start_us"] == rows[0]["service_end_us"]
    if name == "p13_box_committed":
        assert rows[0]["service_start_us"] == 27_000_000
    assert sum(r["status"] == "REJOINED" for r in runs[name].pit_ledger) == 2


def test_global_green_and_driver_permissions_are_independent(study):
    _, runs = study
    run = runs["r2_train"]
    green = next(r for r in run.controls if r["event"]["command"] == "GREEN_RELEASE")
    crossings = [r["at_us"] for r in run.restart_permissions if r["status"] == "CROSSED_RESTART_LINE"]
    assert green["clock_us"] == 62_000_000
    assert len(crossings) == 3 and len(set(crossings)) > 1 and max(crossings) > green["clock_us"]
    passes = runs["r4_r5_permission"].execution.results
    assert [(a.status, a.reason) for a in passes] == [
        ("REJECTED", "DRIVER_RESTART_LINE_PENDING"), ("COMPLETED", None)]


def test_pit_rejoin_requires_next_racing_line(study):
    _, runs = study
    rows = [r for r in runs["r8_pit_exit"].restart_permissions if r["driver"] == 63]
    assert [r["status"] for r in rows] == ["WITHDRAWAL", "REJOIN_RESTRICTED", "CROSSED_RESTART_LINE"]
    assert rows[0]["required_checkpoint"] is None
    assert rows[1]["required_checkpoint"] == rows[2]["required_checkpoint"] == 9


def test_lapped_release_and_restart_interruption(study):
    _, runs = study
    lapped = [r for r in runs["r3_lapped"].restart_permissions if r["status"] == "WITHDRAWAL"]
    assert len({r["required_checkpoint"] for r in lapped}) == 2
    assert any(r["status"] == "SUPERSEDED_BY_RACE_CONTROL" for r in runs["r7_interruption"].restart_permissions)
    assert any(r["outcome"] == "REJECTED" for r in runs["r10_incomplete"].controls)


def test_race_control_precedes_same_tick_restart_crossing(study):
    _, runs = study
    run = runs["r9_same_tick"]
    green = next(r for r in run.controls if r["event"]["command"] == "GREEN_RELEASE")
    lead = [r for r in run.restart_permissions if r["driver"] == 1]
    assert green["clock_us"] == 70_000_000
    assert [(r["status"], r["at_us"]) for r in lead] == [
        ("WITHDRAWAL", green["clock_us"]), ("CROSSED_RESTART_LINE", green["clock_us"])]


def test_cross_module_invariants_and_determinism(study):
    specs, runs = study
    assert all(all(_checks(r).values()) for r in runs.values())
    assert runs["p12_box_reverse"] == simulate_neutralization(**specs["p12_box_reverse"])
    assert runs["x1_green"] == simulate_neutralization(**(specs["x1_green"] | {"individual_restart": False}))


def test_duplicate_decision_identity_is_rejected(study):
    specs, _ = study
    spec = specs["dynamic_green"]
    decision, = spec["pit_decisions"]
    with pytest.raises(ValueError, match="unique"):
        simulate_neutralization(**(spec | {"pit_decisions": (decision, replace(decision))}))
