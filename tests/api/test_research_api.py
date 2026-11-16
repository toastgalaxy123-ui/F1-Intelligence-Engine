"""Public contract, canonical engine parity and bounded local execution tests."""

from pathlib import Path
from time import monotonic, sleep

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("psutil")

from fastapi.testclient import TestClient

from f1ri import decision_board as board
from f1ri.api.app import create_app
from f1ri.api.contracts import MODEL_SHA, AnalysisRequest, Context
from f1ri.api.jobs import MAX_PENDING, Jobs
from f1ri.api.research import ResearchError, prepare_analysis, run_analysis, snapshot

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "/api/v1"


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app(ROOT), base_url="http://localhost") as http:
        yield http


def body(preset, operation, **options):
    view = snapshot(ROOT, Context(preset=preset))
    return AnalysisRequest(context=view.context, snapshot_id=view.snapshot_id,
                           operation=operation, **options)


def finished(client, job, limit=32):
    deadline = monotonic()+limit
    while job["state"] in ("QUEUED", "RUNNING"):
        assert monotonic() < deadline
        sleep(.09)
        job = client.get(f"{PREFIX}/analyses/{job['id']}").json()
    return job


def engine(result):
    _, _, settings, _ = prepare_analysis(ROOT, result.request)
    return board.execute(ROOT, settings)


def test_presets_snapshot_id_and_field_support(client):
    presets = client.get(f"{PREFIX}/presets").json()
    assert {r["id"] for r in presets} == {"archive", "two-car", "neutralization", "timing", "shared-box",
                                         "unsupported", "shadow"}
    for preset in presets:
        assert client.get(f"{PREFIX}/presets/{preset['id']}").json() == preset
    historical = client.get(f"{PREFIX}/presets/archive/snapshot").json()
    again = client.post(f"{PREFIX}/snapshots", json=historical["context"]).json()
    other = client.get(f"{PREFIX}/presets/archive/snapshot?driver=4&horizon=3").json()
    assert again["snapshot_id"] == historical["snapshot_id"] != other["snapshot_id"]
    assert historical["operations"] == ["forecast"] and historical["legal_policies"] == []
    assert historical["fields"]["model"]["value"]["sha256"] == MODEL_SHA
    assert historical["fields"]["drivers"][0]["inventory"]["value"] is None
    assert historical["fields"]["historical_field"]["supported_predecessors"] == 0
    declared = client.get(f"{PREFIX}/presets/two-car/snapshot").json()
    assert len(declared["driver_options"]) == 2
    assert "manual" in declared["operations"] and declared["legal_policies"]
    assert declared["fields"]["drivers"][0]["inventory"]["provenance"] == "DECLARED"
    assert declared["evaluation_marker"]


def test_cutoff_era_live_and_historical_gate(client):
    for context, reason in (
        ({"preset": "two-car", "model_era": "2026"}, "MODEL_ERA_UNSUPPORTED"),
        ({"preset": "archive", "cutoff": "2025-10-05T12:52:28Z"}, "CUTOFF_NOT_IN_VERIFIED_CATALOG"),
        ({"preset": "archive", "session": 10001}, "SESSION_NOT_IN_VERIFIED_CATALOG"),
        ({"preset": "two-car", "mode": "LIVE"}, "LIVE_UNSUPPORTED"),
    ):
        s = client.post(f"{PREFIX}/snapshots", json=context).json()
        assert reason in s["support"] and not s["operations"]
        blocked = client.post(f"{PREFIX}/analyses", json={"context": s["context"], "snapshot_id": s["snapshot_id"],
                                                        "operation": "manual"}).json()
        assert blocked["state"] == "UNSUPPORTED"
    archived = body("unsupported", "manual").model_dump(mode="json")
    assert client.post(f"{PREFIX}/analyses", json=archived).json()["state"] == "UNSUPPORTED"
    s7 = client.get(f"{PREFIX}/evidence/shadow").json()
    assert len(s7["checkpoints"]) == 12 and s7["mode"] == "ARCHIVE_REPLAY"
    assert client.get(f"{PREFIX}/evidence/shadow/11").json()["seal_sha256"] == s7["checkpoints"][11]["seal_sha256"]


def test_forecast_matches_d8r1_with_and_without_legal_replacement():
    result = run_analysis(ROOT, body("archive", "forecast"))
    assert board.canonical(result.payload["forecasts"]) == board.canonical(engine(result)["forecasts"])
    assert len(result.payload["forecasts"]) == 1 and result.model["sha256"] == MODEL_SHA
    assert result.completion == "MEAN_ONLY_VALIDATED"
    declared = run_analysis(ROOT, body("two-car", "forecast", tyre_sets=["soft"]))
    assert len(declared.payload["forecasts"]) == 3
    assert board.canonical(declared.payload["forecasts"]) == board.canonical(engine(declared)["forecasts"])


def test_strategy_conditional_robust_and_timing_engine_parity():
    manual = run_analysis(ROOT, body("two-car", "manual", policies=["63-L31-soft", "63-L31-hard"], future="SC"))
    source = engine(manual)["strategy"]
    for view, key in (("decision", "manual"), ("declared", "manual_future")):
        assert board.canonical(manual.payload[view]["rows"]) == board.canonical(source[key]["rows"])
        for name, timeline in manual.payload[view]["timelines"].items():
            assert board.canonical(timeline["pit"]) == board.canonical(source[key]["paired"]["results"][name]["pit"])
    assert manual.recommendation.unique_action is None
    assert "EXACT_SIMULATION_TIE" in manual.recommendation.statuses
    conditional = run_analysis(ROOT, body("two-car", "conditional", budget=4, future="VSC"))
    opt = engine(conditional)["strategy"]["conditional"]
    assert conditional.completion == opt["completion"] == "BOUNDED_SEARCH_INCOMPLETE"
    assert conditional.payload["search"] == opt["search"]
    assert board.canonical(conditional.payload["conditional"]["rows"]) == board.canonical(opt["conditional"]["rows"])
    robust = run_analysis(ROOT, body("neutralization", "robust", budget=128, robust_budget=240))
    original = engine(robust)
    family = original["strategy"]["robust"]
    assert robust.payload["analysis"]["regret"] == family["analysis"]["regret"]
    assert board.canonical(robust.payload["analysis"]["rows"]) == board.canonical(family["analysis"]["rows"])
    assert robust.recommendation.unique_action == original["recommendation"]["unique_action"]
    assert set(robust.executed_policies) <= set(family["policies"])
    timing = run_analysis(ROOT, body("timing", "timing"))
    frozen = board.frozen_timing(ROOT)
    assert timing.payload["reproduced_regret_us"] == frozen["case"]["sc-63-soft"]["regret_us"] == 46_754_185
    assert timing.payload["case"]["p5-scheduled-conditional"] == frozen["case"]["p5-scheduled-conditional"]


def test_job_dedup_cache_export_recompute(client):
    request = body("two-car", "manual", policies=["63-L31-soft"]).model_dump(mode="json")
    first = client.post(f"{PREFIX}/analyses", json=request).json()
    second = client.post(f"{PREFIX}/analyses", json=request).json()
    assert first["id"] == second["id"]
    complete = finished(client, first)
    assert complete["state"] == "COMPLETED" and complete["measurements"]["peak_rss_mib"] > 0
    result = complete["result"]
    assert len(result["executed_policies"]) == 2
    cached = client.post(f"{PREFIX}/analyses", json=request).json()
    assert cached["cache_hit"] and cached["result"] == result
    exported = client.get(f"{PREFIX}/analyses/{first['id']}/export")
    assert "attachment" in exported.headers["content-disposition"] and exported.json() == result
    assert result["result_sha256"] == board.digest(board.canonical({
        k: v for k, v in result.items() if k != "result_sha256"}))
    replay = client.post(f"{PREFIX}/replays", json={"request": result["request"],
                                                  "expected_sha256": result["result_sha256"]}).json()
    assert replay["id"] != first["id"]
    verified = finished(client, replay)
    assert verified["state"] == "COMPLETED" and verified["result"] == result


def test_invalid_body_origin_snapshot_and_policy(client):
    manual = body("two-car", "manual").model_dump(mode="json")
    url = f"{PREFIX}/analyses"
    invalid = {
        "too_many_policies": manual | {"policies": ["A", "B", "C", "D", "E", "F"]},
        "unknown_policy": manual | {"policies": ["invented-policy"]},
        "illegal_tyre": manual | {"policies": ["63-L31-soft"], "tyre_sets": ["hard"]},
        "policy_path": manual | {"policies": ["../model"]},
        "large_budget": manual | {"robust_budget": 241},
        "invalid_cutoff": manual | {"context": manual["context"] | {"cutoff": "2025-10-05T12:52:29"}},
    }
    assert all(client.post(url, json=payload).status_code == 422 for payload in invalid.values())
    assert client.post(url, json=manual | {"snapshot_id": "1"*64}).status_code == 409
    assert client.post(url, json=manual, headers={"Origin": "https://random.example"}).status_code == 403
    assert client.post(url, json=manual, headers={"Host": "random.example"}).status_code == 400
    assert client.post(url, content="z"*17000, headers={"Content-Type": "application/json"}).status_code == 413
    assert client.post(url, data={"q": "model"}).status_code == 415
    assert client.get(f"{url}/{'a'*64}/export").status_code == 404


@pytest.mark.parametrize(("limits", "code"), [({"wall_seconds": .06}, "TIME_LIMIT"),
                                              ({"rss_bytes": 1}, "MEMORY_LIMIT")])
def test_worker_limits(limits, code):
    queue = Jobs(ROOT, **limits)
    request = body("two-car", "manual")
    _, _, _, identity = prepare_analysis(ROOT, request)
    try:
        queue.submit(identity, request)
        deadline = monotonic()+12
        row = queue.get(identity)
        while row.state in {"QUEUED", "RUNNING"}:
            assert monotonic() < deadline
            sleep(.05)
            row = queue.get(identity)
        assert row.state == "FAILED" and row.code == code and row.result is None
    finally:
        queue.close()


def test_queue_capacity_and_cancellation():
    queue = Jobs(ROOT)
    request = body("timing", "timing")
    identities = [f"{i:064x}" for i in range(MAX_PENDING+1)]
    try:
        for key in identities[:MAX_PENDING]:
            queue.submit(key, request)
        with pytest.raises(ResearchError, match="queue is full"):
            queue.submit(identities[-1], request)
        for key in identities[:MAX_PENDING]:
            queue.cancel(key)
        deadline = monotonic()+12
        for key in identities[:MAX_PENDING]:
            while queue.get(key).state in {"QUEUED", "RUNNING"}:
                assert monotonic() < deadline
                sleep(.05)
            assert queue.get(key).state == "CANCELED" and queue.get(key).result is None
    finally:
        queue.close()
