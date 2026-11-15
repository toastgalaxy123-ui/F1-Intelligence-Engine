"""Receipt clock, capture journal, revision and decision-gating regression tests."""

from dataclasses import replace
from datetime import timedelta
from json import loads
from pathlib import Path

import httpx
import pytest

from f1ri.ingestion.openf1 import OpenF1Client
from f1ri.ingestion.source_capture import (
    CapturePlan,
    capture,
    capture_files,
    load_capture_files,
    retry_wait,
)
from f1ri.ingestion.source_receipt import CLOCK_ANOMALY, EXPERIMENT, clock_quality, identity, utc
from f1ri.ingestion.source_replay import ReplayIndex
from f1ri.validation.historical_field_sources import load_pilots
from f1ri.validation.source_availability_study import (
    _export,
    open_source,
    recovery_probe,
    strategy_delay,
)
from f1ri.validation.source_faults import BASE, ENDPOINTS, SUITE_ID, response, run_faults

ROOT = Path(__file__).resolve().parents[1]
POLICY = {"response_max_s": 10, "position": 15, "location": 2, "race_control": 30, "pit": 30, "laps": 150}


def at(seconds):
    return BASE+timedelta(seconds=seconds)


def row(second=0, driver=1, position=1):
    return {"session_key": 9987, "driver_number": driver, "date": at(second).isoformat(), "position": position}


def index(items):
    return ReplayIndex(SUITE_ID, 9987, ENDPOINTS, items, mode=EXPERIMENT, roster=(1, 4))


def raw_at(items, seconds, event=None):
    return index(items).replay(at(seconds), event_cutoff=at(seconds if event is None else event),
                               receipt_cutoff=at(seconds), policy=POLICY)


def test_clock_parsing_and_monotonic_elapsed():
    assert utc("2025-05-18T10:06:27-04:00") == BASE
    with pytest.raises(ValueError, match="explicit UTC offsets"):
        utc("2025-05-18T14:06:27")
    assert clock_quality(at(0), at(2), 1, 2_000_000_001) != CLOCK_ANOMALY
    assert clock_quality(at(0), at(-2), 1, 2_000_000_001) == CLOCK_ANOMALY
    e, _ = response(1, "position", 0, 2.3, [row()])
    assert e.duration_s == pytest.approx(2.3)


def test_schema_identity_is_conservative():
    rec = row()
    assert identity(9987, "position", rec)[0] == identity(
        9987, "position", rec | {"date": "2025-05-18T10:06:27-04:00"})[0]
    assert identity(9987, "position", rec)[0] != identity(9987, "position", rec | {"driver_number": 4})[0]
    with pytest.raises(ValueError, match="does not match"):
        identity(9987, "position", rec | {"session_key": 9896})
    assert identity(9987, "sessions", {"session_key": 9987, "date_start": BASE.isoformat()})[1] is None


def test_observed_journal_pins_raw_bytes_and_receipt_mode():
    manifest, responses, orphans = open_source(ROOT)
    assert not orphans and len(responses) == 10
    assert manifest["plan"]["mode"] == "RETROSPECTIVE_RETRIEVAL"
    assert all(e.http_status == 200 and e.provider_publication_utc is None and len(body) == e.body_bytes
               for e, body in responses)
    idx = ReplayIndex(manifest["capture_id"], 9896, manifest["plan"]["endpoints"], responses,
                      mode=manifest["plan"]["mode"])
    first = min(e.available_utc for e, _ in responses)
    out = loads(idx.replay(first, event_cutoff=at(0), receipt_cutoff=first, policy=POLICY))
    assert out["observations"]
    assert all(not e["field_input_allowed"] for e in out["endpoints"].values())


def test_capture_limits_and_http_response_preservation(tmp_path):
    with pytest.raises(ValueError, match="2.1 seconds"):
        CapturePlan(9987, ("position",), 10, 1, 2, 0)
    with pytest.raises(ValueError, match="bounded UTC source window"):
        CapturePlan(9987, ("location",), 10, 2.1, 2, 0)
    with pytest.raises(ValueError, match="up to 100"):
        CapturePlan(9987, ("position",), 10, 2.1, 101, 0)

    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(403, stream=httpx.ByteStream(b'{"error":"forbidden"}'),
                              headers={"Set-Cookie": "unsafe=example"})

    source = OpenF1Client("https://example.test", 2.1)
    source.client.close()
    source.client = httpx.Client(transport=httpx.MockTransport(handle))
    with source:
        plan = CapturePlan(9987, ("position",), 5, 2.1, 1, 3)
        out = tmp_path/"capture"
        result = capture(out, plan, client=source)
    _, records, pending = load_capture_files(capture_files(out))
    assert len(calls) == result["attempts"] == 1 and not pending
    receipt, raw = records[0]
    assert receipt.http_status == 403 and receipt.error == "HTTP_403"
    assert raw == b'{"error":"forbidden"}' and "set-cookie" not in receipt.headers
    assert retry_wait(receipt) is None
    raw_files = capture_files(out)
    raw_files[f"raw/{receipt.body_sha256}.bin"] = b"tampered"
    with pytest.raises(ValueError, match="checksum"):
        load_capture_files(raw_files)
    with pytest.raises(FileExistsError):
        capture(out, plan)


def test_retry_wait_honors_rate_limit_and_permanent_failures():
    rate, _ = response(1, "position", 0, .1, status=429, error="HTTP_429", headers={"retry-after": "15"})
    assert retry_wait(rate) == 15
    future_header = "Sun, 18 May 2025 14:06:42 GMT"
    assert retry_wait(replace(rate, headers={"retry-after": future_header})) == pytest.approx(14.9)
    assert retry_wait(replace(rate, http_status=503, error="HTTP_503", retry=2)) == 4
    assert retry_wait(replace(rate, http_status=401, error="HTTP_401")) is None


def test_receipt_event_and_processing_clocks_have_separate_gates():
    items = [response(1, "position", 0, 2, [row(1)], process_lag=1)]
    assert not loads(raw_at(items, 2.5))["observations"]
    assert not loads(raw_at(items, 4, event=.5))["observations"]
    data = loads(raw_at(items, 4, event=1))
    assert len(data["observations"]) == 1
    obs = data["observations"][0]
    assert utc(obs["first_seen_utc"]) == at(2)
    assert utc(obs["usable_utc"]) == at(3)
    assert utc(obs["source_event_utc"]) == at(1)


def test_serialized_prefix_is_stable_after_future_correction():
    first = response(1, "position", 0, .1, [row()])
    correction = response(2, "position", 4, 4.1, [row(position=3)])
    index_all = index([correction, first])
    assert raw_at([first], 2) == raw_at([first, correction], 2)
    revised = loads(index_all.replay(at(5), event_cutoff=at(5), receipt_cutoff=at(5), policy=POLICY))
    version = revised["observations"][0]
    assert version["changed_fields"] == ["position"] and version["payload"]["position"] == 3
    assert len(index_all.versions) == 2


def test_duplicate_delivery_omission_and_conflicting_versions():
    first = response(1, "position", 0, .1, [row()]*2)
    same = response(2, "position", 1, 1.1, [row()])
    omission = response(3, "position", 2, 2.1)
    conflict = response(4, "position", 3, 3.1, [row(position=2), row(position=3)])
    items = [first, same, omission, conflict]
    assert loads(raw_at(items, 2.5))["observations"][0]["first_seen_utc"] == str(at(.1))
    blocked = loads(raw_at(items, 4))
    assert not blocked["observations"]
    assert blocked["endpoints"]["position"]["conflicts"] == 1
    assert {r["kind"] for r in index(items).audit} >= {
        "DUPLICATE_ROW", "IDENTICAL_REDELIVERY", "OMISSION_NOT_RETRACTION", "SOURCE_CONFLICT",
    }


def test_older_response_does_not_refresh_a_later_failed_poll():
    early = response(1, "position", 0, 3, [row()])
    failed = response(2, "position", 1, 1.1, status=503, error="HTTP_503")
    snapshot = loads(raw_at([early, failed], 4))
    assert snapshot["observations"]
    assert "SOURCE_OUTAGE" in snapshot["endpoints"]["position"]["status"]
    assert not snapshot["endpoints"]["position"]["field_input_allowed"]


def test_replay_invalidates_a_clock_epoch_after_jump():
    a = response(1, "position", 0, .1, [row()])
    b, bb = response(2, "position", 1, 1.1, [row(0, 4)])
    c, cc = response(3, "position", 2, 2.1, [row(0, 4)])
    broken = (replace(b, request_ns=b.request_ns+8_000_000_000), bb)
    same_epoch = index([a, broken, (c, cc)])
    assert len(same_epoch.unordered) == 2
    snapshot = loads(same_epoch.replay(at(4), receipt_cutoff=at(4), event_cutoff=at(4), policy=POLICY))
    assert [r["payload"]["driver_number"] for r in snapshot["observations"]] == [1]


def test_known_absence_requires_complete_matching_query_and_success():
    uncertain, u = response(1, "position", 0, .1)
    failed = response(2, "position", 1, 1.1, status=503, error="HTTP_503")
    arrived = response(3, "position", 2, 2.1, [row(1)])
    no_bound = loads(raw_at([(replace(uncertain, coverage="UNVERIFIED"), u), failed, arrived], 3))
    assert not no_bound["observations"][0]["appearance"]["bounded_both_sides"]
    bounded = loads(raw_at([(uncertain, u), failed, arrived], 3))["observations"][0]["appearance"]
    assert bounded["bounded_both_sides"] and bounded["failed_polls_between"] == 1
    assert bounded["provider_publication_utc"] is None


def test_partial_or_stale_driver_coverage_blocks_field():
    sample = [response(1, "location", 0, .1, [
        {"session_key": 9987, "driver_number": 1, "date": at(0).isoformat(), "x": 4, "y": 0},
    ])]
    early = loads(raw_at(sample, .2))["endpoints"]["location"]
    late = loads(raw_at(sample, 4))["endpoints"]["location"]
    assert early["missing_or_unverified_drivers"] == [4] and not early["field_input_allowed"]
    assert "SOURCE_STALE" in late["status"] and late["missing_or_unverified_drivers"] == [1, 4]


def test_fault_harness_and_receipt_filtered_reconstruction():
    session = load_pilots(ROOT)[0][9987]
    recovery = recovery_probe(POLICY)
    faults, demos = run_faults(session, POLICY, recovery)
    assert len(faults) == 18 and all(r["passed"] for r in faults.values())
    assert recovery["request_budget_respected"] and recovery["orphaned_after"] == 1
    assert demos["missing_pit"]["field"] is None
    assert demos["stale_field"]["field"] is None
    data = demos["receipt_filtered_field"]
    assert data["field"]["supported_positions"] == 1
    assert data["field"]["supported_predecessors"] == 0 and data["historical_strategy"] == "UNSUPPORTED"
    assert not data["receipt_qualified_operational"]


def test_receipt_delay_changes_policy_timing_under_fixed_physics():
    demo = strategy_delay()
    assert [r["pit_entries"][0]["target_lap"] if r["pit_entries"] else None for r in demo["runs"]] == [2, 3, None]
    assert len({repr(r["simulator_controls"]) for r in demo["runs"]}) == 1
    assert all(all(p["at_us"] >= r["policy_receipt_us"] for p in r["policy_decisions"]) for r in demo["runs"])


def test_verified_export_reimports_exact_capture_prefix(tmp_path):
    manifest, responses, _ = open_source(ROOT)
    index = ReplayIndex(manifest["capture_id"], 9896, manifest["plan"]["endpoints"], responses,
                        mode=manifest["plan"]["mode"], roster=(4, 63))
    t = max(e.available_utc for e, _ in responses)
    settings = {"decision_utc": t, "receipt_cutoff": t,
                "event_cutoff": utc("2025-10-05T12:52:29.272Z"), "policy": POLICY, "roster": (4, 63)}
    payload = index.replay(t, receipt_cutoff=t, event_cutoff=settings["event_cutoff"], policy=POLICY)
    assert _export(ROOT, tmp_path/"replay.zip", settings, payload)["reproduced"]
    with pytest.raises(ValueError, match="differs after reimport"):
        _export(ROOT, tmp_path/"altered.zip", settings, b"{}")


def test_dashboard_cutoff_changes_visible_source_prefix():
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(ROOT/"src/f1ri/dashboard/app.py"), default_timeout=30).run()
    next(iter(app.get("segmented_control"))).set_value("Source Availability & Replay").run()
    assert not app.exception
    assert any("Historical receipt and provider publication" in info.value for info in app.info)
    next(r for r in app.radio if r.label == "Observation mode").set_value("Observed collector receipts").run()
    assert not app.exception
    assert next(m for m in app.metric if m.label == "Visible record versions").value == "0"
    slider = next(s for s in app.select_slider if s.label == "Virtual receipt cutoff (UTC)")
    slider.set_value(slider.options[-1]).run()
    assert not app.exception
    assert int(next(m for m in app.metric if m.label == "Visible record versions").value) > 0
    assert any("HISTORICAL_STRATEGY_UNSUPPORTED" in row.value for row in app.caption)
