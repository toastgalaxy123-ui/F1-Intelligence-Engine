"""Regression controls for priority polling, maturity and shadow decision seals."""

from datetime import timedelta
from json import loads
from pathlib import Path

import pytest

from f1ri.features.shadow_snapshot import check_seal
from f1ri.features.temporal_maturity import Boundary, visible_fields
from f1ri.ingestion.priority_polling import PriorityScheduler
from f1ri.ingestion.source_receipt import canonical
from f1ri.validation.historical_field_sources import load_pilots
from f1ri.validation.shadow_controls import control_experiments, shadow_cases
from f1ri.validation.shadow_polling_study import compare, feasibility, measured
from f1ri.validation.shadow_preflight import PLAN, _baseline, _capture
from f1ri.validation.shadow_temporal_audit import d8r1_cutoff_audit, maturity_experiments
from f1ri.validation.source_faults import BASE

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = loads((ROOT/PLAN).read_bytes())
POLICY = loads((ROOT/"config/experiments/stage7_s7_b.json").read_bytes())["freshness_policy"]


def test_global_throttle_retry_scope_and_endpoint_budget():
    rule = {"weight": 1, "period_s": 2, "max_gap_s": 5, "budget": 1, "dependency": True}
    scheduler = PriorityScheduler(("location", "race_control"), {"method": "S4", "rules": {
        "location": rule | {"budget": 2}, "race_control": rule,
    }}, 2.1)
    assert scheduler.choose(0) == ("location", 0)
    scheduler.started("location", 0)
    scheduler.completed("location", .4, False, status=429, retry_after=5, poll=1)
    assert scheduler.choose(2.1) == (None, 5.4)
    assert scheduler.retry["location"] == (1, 1)
    scheduler.started("race_control", 5.4)
    scheduler.completed("race_control", 5.8, True)
    assert scheduler.choose(7.5) == ("location", 7.5)
    scheduler.started("location", 7.5)
    scheduler.completed("location", 7.9, True, poll=1, retry=1)
    assert scheduler.choose(30) == (None, None)


def test_comparison_respects_spacing_and_location_bound():
    bound = feasibility(PROTOCOL)
    assert bound["location_continuous"] == "POLLING_INFEASIBLE"
    assert bound["rate_ceiling_steady_requests_per_min"] < bound["required_requests_per_min"]["location"]
    results = {(doc["method"], doc["experiment"]): doc for doc in compare(PROTOCOL)}
    sequential, priority = (results[m, "NOMINAL"] for m in ("S0", "S4"))
    assert priority["freshness"]["location"]["duty_cycle"] > sequential["freshness"]["location"]["duty_cycle"]
    fault = results["S4", "FAULTS"]
    failure = next(i for i, p in enumerate(fault["schedule"]) if p["status"] == 429)
    failed, after = fault["schedule"][failure:failure+2]
    assert after["start"] >= failed["usable"] + 5
    assert all(r["spacing_violations"] == 0 and r["requests"] <= r["request_budget"] for r in results.values())


def test_rehearsal_transport_and_frozen_evidence():
    frozen = _baseline(ROOT, None)
    assert len(frozen["s7b_pins"]) == 4 and frozen["s7b_expected_outputs"] == 52
    manifest, receipts, incomplete, decisions = _capture(ROOT, PROTOCOL["rehearsal"])
    assert not incomplete and len(decisions) == len(receipts) == 12
    assert manifest["plan"]["mode"] == "RETROSPECTIVE_RETRIEVAL"
    assert all(e.http_status == 200 and e.provider_publication_utc is None for e, _ in receipts)
    stats = measured(receipts, PROTOCOL["freshness_limits_s"])
    assert stats["min_spacing_s"] >= 2.1
    assert stats["endpoints"]["location"]["max_usable_interval_s"] > 2


def test_field_completion_and_usable_receipt_are_independent():
    start = BASE
    finish = BASE+timedelta(seconds=90)
    available = finish+timedelta(seconds=2)
    row = {"session_key": 9987, "driver_number": 1, "lap_number": 4,
           "date_start": start.isoformat(), "lap_duration": 90.}
    args = {"first_seen": finish, "processed": finish, "usable": available,
            "boundaries": {"finish": Boundary(finish, "next_line", available)}}
    assert visible_fields("laps", row, finish, receipt=finish, **args) == ({}, {})
    values, fields = visible_fields("laps", row, available, receipt=available, **args)
    assert values["lap_duration"] == 90.
    assert fields["lap_duration"]["processed"] == finish
    assert fields["lap_duration"]["usable"] == available
    assert "lap_duration" not in visible_fields("laps", row, available)[0]


def test_frozen_d8r1_request_excludes_lap31_outcomes():
    record, mutated, following, audit = d8r1_cutoff_audit(ROOT)
    study = maturity_experiments(record, mutated, following, audit)
    assert audit["same_frozen_request"] and audit["mutated_request_unchanged"]
    assert audit["outcome"] == "NO_LEAKAGE_FOUND_IN_AUDITED_PATH"
    assert "lap_duration" not in study["lap_start_visible"]
    assert study["later_visible_event_time_only"]["lap_duration"] == record["lap_duration"]


def test_shadow_gate_and_seal_detect_tampering():
    sessions, _ = load_pilots(ROOT)
    cases, temporal = shadow_cases(sessions[9987], POLICY)
    assert len(cases) == 16 and all(temporal["temporal_receipt_controls"].values())
    assert all(case["snapshot"]["action"] is None for case in cases.values())
    snapshot = cases["complete_receipt_fixture"]
    assert snapshot == cases["receipt_future_mutation"]
    assert check_seal(canonical(snapshot)) == snapshot["sha256"]
    modified = snapshot | {"candidate_library": ["new"]}
    with pytest.raises(ValueError, match="seal mismatch"):
        check_seal(canonical(modified))


def test_race_control_visibility_only_changes_at_usable_receipt():
    trace = control_experiments(POLICY)
    assert trace["physical_controls_unmodified"]
    assert trace["green_delivery"]["visible_after"] and not trace["green_delivery"]["visible_before"]
    assert trace["correction"]["prefix_stable"]
    assert trace["deadline_controls"][0]["committed_laps"] != trace["deadline_controls"][1]["committed_laps"]


def test_shadow_page_presents_maturity_and_changes_sealed_checkpoint():
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(ROOT/"src/f1ri/dashboard/app.py"), default_timeout=30).run()
    next(iter(app.get("segmented_control"))).set_value("Shadow-Mode Readiness").run()
    assert not app.exception
    assert any("LIVE_UNSUPPORTED" in w.value for w in app.warning)

    def tab(name):
        next(w for w in app.get("segmented_control") if w.label == "Shadow evidence").set_value(name).run()
        assert not app.exception

    tab("Lap maturity")
    assert any("Original receipt evidence" in c.value for c in app.caption)
    tab("Sealed checkpoints")
    initial = app.code[0].value
    choices = next(s for s in app.selectbox if s.label == "Receipt cutoff (UTC)")
    choices.set_value(len(choices.options)-1).run()
    assert not app.exception and app.code[0].value != initial
    tab("Race control")
