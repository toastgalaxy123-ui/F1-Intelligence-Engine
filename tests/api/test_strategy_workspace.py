"""S8-B additive API controls and authoritative team/trace checks."""

from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("psutil")

from fastapi.testclient import TestClient

from f1ri import decision_board as board
from f1ri.api.app import create_app
from f1ri.api.contracts import AnalysisRequest, Context
from f1ri.api.research import assets, prepare_analysis, run_analysis, snapshot
from f1ri.api.workspace import AnalysisDetails

ROOT = Path(__file__).resolve().parents[2]
API = "/api/v1"
PAIRED = ("4-L30-hard__63-L31-hard", "4-L30-hard__63-L32-soft")


@pytest.fixture(scope="module")
def http():
    with TestClient(create_app(ROOT), base_url="http://localhost") as client:
        yield client


def done(http, row):
    deadline = monotonic() + 40
    while row["state"] in ("RUNNING", "QUEUED"):
        assert monotonic() < deadline
        sleep(.08)
        row = http.get(f"{API}/analyses/{row['id']}").json()
    return row


def queries(preset, **options):
    view = snapshot(ROOT, Context(preset=preset))
    args = AnalysisRequest(context=view.context, snapshot_id=view.snapshot_id, operation="manual", **options)
    return view, args


def test_team_catalog_and_matched_physics(http):
    view, request = queries("shared-box", policies=PAIRED, max_stops=2)
    first = {"context": view.context.model_dump(mode="json"), "snapshot_id": view.snapshot_id}
    single = http.post(f"{API}/policy-catalog", json=first).json()
    pair = http.post(f"{API}/policy-catalog", json=first | {"max_stops": 2}).json()
    assert not set(PAIRED) & {v["id"] for v in single["policies"]}
    assert set(PAIRED) <= {v["id"] for v in pair["policies"]}
    job = done(http, http.post(f"{API}/analyses", json=request.model_dump(mode="json")).json())
    assert job["state"] == "COMPLETED"
    result = job["result"]
    _, _, settings, _ = prepare_analysis(ROOT, request)
    original = board.execute(ROOT, settings, assets(ROOT))["strategy"]["manual"]
    assert board.canonical(result["payload"]["decision"]["rows"]) == board.canonical(original["rows"])
    assert len(result["evaluation_marker"]) == 2
    visits = result["payload"]["decision"]["timelines"]
    assert [(p["driver"], p["wait_us"]) for p in visits[PAIRED[0]]["pit"]] == [(4, 0), (63, 2_903_848)]
    assert [p["wait_us"] for p in visits[PAIRED[1]]["pit"]] == [0, 0]
    response = http.get(f"{API}/analyses/{job['id']}/details")
    trace = AnalysisDetails.model_validate(response.json())
    assert trace.result_sha256 == result["result_sha256"]
    for policy in (*PAIRED, "reference"):
        points = trace.branches["decision"][policy].checkpoints
        raw = original["paired"]["results"][policy]["trajectory"]
        assert points and all(any(r["driver"] == p.driver == r["event_driver"] and
                                  p.checkpoint == r["checkpoint"] and p.clock_us == r["clock_us"] and
                                  p.progress_m == r["progress_m"] and p.event == r["event"] for r in raw) for p in points)
    assert trace.branches["decision"][PAIRED[1]].mounted_tyres[63]["soft"]["initial_age_laps"] == 0
    assert http.get(f"{API}/analyses/{job['id']}/export").json() == result


def test_trace_extension_preserves_canonical_s8a_identity():
    _, request = queries("two-car", policies=("63-L31-hard", "63-L31-medium", "63-L31-soft"))
    diagnostic = {}
    first = run_analysis(ROOT, request)
    second = run_analysis(ROOT, request, diagnostics=diagnostic)
    assert first == second and diagnostic["branches"]["decision"]["reference"]["checkpoints"]
    assert len(first.recommendation.alternatives) == 4
    assert first.recommendation.unique_action is None


def _workspace(view, result):
    return {
        "version": "s8b-workspace-v1", "id": str(uuid4()), "title": "Pit stop research",
        "created_at": "2026-11-16T02:00:00Z", "updated_at": "2026-11-16T02:00:00Z",
        "context": view.context.model_dump(mode="json"), "snapshot_id": view.snapshot_id,
        "model_sha256": result.model["sha256"], "source_hashes": view.source_hashes, "support": view.support,
        "analysis": result.request.model_dump(mode="json"), "reference": result.executed_policies[0],
        "receipt": {"analysis_id": result.analysis_id, "result_sha256": result.result_sha256},
    }


def test_workspace_verified_before_replay_and_import_validation(http):
    view, request = queries("two-car", policies=("63-L31-hard",))
    result = run_analysis(ROOT, request)
    doc = _workspace(view, result)
    response = http.post(f"{API}/workspaces/verify", json=doc)
    assert response.json() == {"status": "INPUTS_VERIFIED", "analysis_id": result.analysis_id,
                               "snapshot_id": view.snapshot_id, "replay_available": True}
    mutations = [
        (doc | {"model_sha256": "0"*64}, 409, "WORKSPACE_MODEL_MISMATCH"),
        (doc | {"source_hashes": {"../secret": "3"*64}}, 409, "WORKSPACE_SOURCE_MISMATCH"),
        (doc | {"snapshot_id": "2"*64}, 409, "WORKSPACE_SNAPSHOT_MISMATCH"),
        (doc | {"receipt": doc["receipt"] | {"analysis_id": "1"*64}}, 409, "WORKSPACE_ANALYSIS_MISMATCH"),
        (doc | {"command": "unexpected"}, 422, "INVALID_REQUEST"),
        (doc | {"version": "unsupported"}, 422, "INVALID_REQUEST"),
    ]
    for payload, status, code in mutations:
        denied = http.post(f"{API}/workspaces/verify", json=payload)
        assert denied.status_code == status and denied.json()["error"]["code"] == code
    replay = done(http, http.post(f"{API}/replays", json={
        "request": doc["analysis"], "expected_sha256": result.result_sha256}).json())
    assert replay["result"]["result_sha256"] == result.result_sha256
    corrupt = done(http, http.post(f"{API}/replays", json={
        "request": doc["analysis"], "expected_sha256": "9"*64}).json())
    assert corrupt["state"] == "FAILED" and corrupt["code"] == "REPLAY_CHECKSUM_MISMATCH"


def test_queued_and_active_http_cancellation_and_local_origin(http):
    timing = snapshot(ROOT, Context(preset="timing"))
    active = AnalysisRequest(context=timing.context, snapshot_id=timing.snapshot_id, operation="timing")
    _, queued = queries("two-car", policies=("63-L31-medium",))
    job_a = http.post(f"{API}/analyses", json=active.model_dump(mode="json")).json()
    job_b = http.post(f"{API}/analyses", json=queued.model_dump(mode="json")).json()
    assert job_b["state"] == "QUEUED"
    que = http.delete(f"{API}/analyses/{job_b['id']}")
    assert que.json()["state"] == "CANCELED"
    assert http.delete(f"{API}/analyses/{job_a['id']}", headers={"Origin": "https://invalid.example"}).status_code == 403
    running_uri = f"{API}/analyses/{job_a['id']}"
    assert http.get(running_uri).json()["state"] == "RUNNING"
    canceled = done(http, http.delete(running_uri).json())
    assert canceled["state"] == "CANCELED" and canceled["result"] is None
