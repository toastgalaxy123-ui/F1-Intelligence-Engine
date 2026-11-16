"""Isolated private API security, storage and process recovery controls."""

from hashlib import sha256
from logging import makeLogRecord
from os import getpid
from pathlib import Path
from subprocess import PIPE, Popen, run
from sys import executable
from time import monotonic, sleep
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("itsdangerous")
psutil = pytest.importorskip("psutil")

from fastapi.testclient import TestClient

from f1ri.api.app import create_app
from f1ri.api.contracts import Context
from f1ri.api.research import snapshot
from f1ri.private.auth import COOKIE
from f1ri.private.config import PrivateConfig
from f1ri.private.logging import ServiceFormatter
from f1ri.private.storage import Store, lease

ROOT = Path(__file__).resolve().parents[2]
API = "/api/v1"


def configuration(path, **overrides):
    return PrivateConfig(
        environment="staging", origin="https://localhost:8443",
        oidc_issuer="https://127.0.0.1:9443/realms/f1ri", oidc_client_id="research",
        oidc_client_secret="client-fixture-"+"c"*32, flow_secret="flow-fixture-"+"s"*32,
        edge_secret="edge-fixture-"+"e"*32, database=path, **overrides,
    )


@pytest.fixture
def private(tmp_path):
    cfg = configuration(tmp_path/"private.sqlite3")
    store = Store(cfg.database)
    store.migrate()
    for key in ("a", "b"):
        store.enroll(cfg.oidc_issuer, key, f"Researcher {key.upper()}")
    tokens = {key: store.new_session(store.principal(cfg.oidc_issuer, key)["id"], 600) for key in ("a", "b")}
    with TestClient(create_app(ROOT, private_config=cfg), base_url=cfg.origin) as client:
        yield cfg, store, tokens, client


def headers(cfg, token=None):
    values = {"Origin": cfg.origin, "X-F1RI-Edge": cfg.edge_secret.get_secret_value()}
    if token:
        values["Cookie"] = f"{COOKIE}={token}"
    return values


def args(preset, operation, **extra):
    state = snapshot(ROOT, Context(preset=preset))
    return state, {"context": state.context.model_dump(mode="json"), "snapshot_id": state.snapshot_id,
                   "operation": operation, **extra}


def completed(client, cfg, token, row):
    deadline = monotonic()+42
    while row["state"] in ("QUEUED", "RUNNING"):
        assert monotonic() < deadline
        sleep(.06)
        row = client.get(f"{API}/analyses/{row['id']}", headers=headers(cfg, token)).json()
    return row


def document(state, request=None, receipt=None):
    return {"version": "s8b-workspace-v1", "id": str(uuid4()), "title": "Research capsule",
            "created_at": "2026-11-16T00:00:00Z", "updated_at": "2026-11-16T00:00:00Z",
            "context": state.context.model_dump(mode="json"), "snapshot_id": state.snapshot_id,
            "model_sha256": state.fields["model"]["value"]["sha256"], "support": state.support,
            "source_hashes": state.source_hashes, "analysis": request, "receipt": receipt, "reference": "reference"}


def test_gateway_anonymous_host_and_origin_checks(private):
    cfg, _, tokens, client = private
    assert client.get(f"{API}/presets").status_code == 403
    assert client.get(f"{API}/presets", headers=headers(cfg)).status_code == 401
    assert client.get("/auth/check", headers=headers(cfg), follow_redirects=False).headers["location"] == "/auth/sign-in"
    assert client.get(f"{API}/presets", headers=headers(cfg, tokens["a"]) | {"Host": "invalid.example"}).status_code == 400
    assert client.get("/auth/session", headers=headers(cfg, tokens["a"])).json()["label"] == "Researcher A"
    assert client.get("/openapi.json", headers=headers(cfg, tokens["a"])).status_code == 404
    _, query = args("archive", "forecast")
    bad = headers(cfg, tokens["a"]) | {"Origin": "https://untrusted.example"}
    assert client.post(f"{API}/analyses", json=query, headers=bad).status_code == 403
    assert client.post(f"{API}/analyses", json=query, headers={
        k: v for k, v in headers(cfg, tokens["a"]).items() if k != "Origin"}).status_code == 403
    assert client.options(f"{API}/analyses", headers=bad).status_code == 403
    assert client.get("/auth/callback?state=invalid", headers=headers(cfg)).status_code == 400


def test_session_expiry_signout_and_revocation(private):
    cfg, store, tokens, client = private
    token_hash = sha256(tokens["a"].encode()).hexdigest()
    with store.connect(write=True) as db:
        db.execute("UPDATE sessions SET expires=1 WHERE token_hash=?", (token_hash,))
    _, query = args("archive", "forecast")
    assert client.post(f"{API}/analyses", json=query, headers=headers(cfg, tokens["a"])).status_code == 401
    assert client.get("/auth/session", headers=headers(cfg, "X"*43)).status_code == 401
    signout = client.post("/auth/logout", json={}, headers=headers(cfg, tokens["b"]))
    assert signout.status_code == 200
    cookie = signout.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert client.get(f"{API}/workspaces", headers=headers(cfg, tokens["b"])).status_code == 401
    owner = store.principal(cfg.oidc_issuer, "b")["id"]
    second_device = store.new_session(owner, 600)
    store.revoke(cfg.oidc_issuer, "b")
    assert store.principal(cfg.oidc_issuer, "b") is None
    assert client.get("/auth/session", headers=headers(cfg, second_device)).status_code == 401


def test_workspace_ownership_and_same_identity_on_another_session(private):
    cfg, store, tokens, client = private
    state, request = args("archive", "forecast")
    doc = document(state, request)
    a, b = (headers(cfg, tokens[key]) for key in ("a", "b"))
    assert client.post(f"{API}/workspaces", json=doc, headers=a).status_code == 201
    uri = f"{API}/workspaces/{doc['id']}"
    assert client.get(f"{API}/workspaces", headers=b).json() == []
    assert client.get(uri, headers=b).status_code == 404
    assert client.get(f"{uri}/export", headers=b).status_code == 404
    assert client.put(uri, json=doc, headers=b).status_code == 404
    assert client.delete(uri, headers=b).status_code == 404
    assert client.post(f"{uri}/duplicate", json={}, headers=b).status_code == 404
    assert client.get(f"{API}/workspaces/not-a-uuid", headers=a).status_code == 422
    assert client.get(f"{API}/snapshots/{state.snapshot_id}", headers=b).status_code == 404
    assert client.get("/artifacts/reports/stage8_s8_b_evidence.zip", headers=a).status_code == 404
    assert client.post(f"{API}/workspaces", json=doc | {"owner": "b"}, headers=a).status_code == 422
    assert client.post(f"{API}/workspaces", json=doc | {"source_hashes": {"../outside": "0"*64}},
                       headers=a).json()["error"]["code"] == "WORKSPACE_SOURCE_MISMATCH"
    assert client.post(f"{API}/workspaces", json=doc | {"model_sha256": "0"*64},
                       headers=a).json()["error"]["code"] == "WORKSPACE_MODEL_MISMATCH"
    assert client.post(f"{API}/workspaces", content=b"x"*17_000,
                       headers=a | {"Content-Type": "application/json"}).status_code == 413
    assert client.put(uri, json=doc | {"title": "Renamed"}, headers=a).json()["title"] == "Renamed"
    same_owner = store.new_session(store.principal(cfg.oidc_issuer, "a")["id"], 600)
    assert client.get(f"{uri}/export", headers=headers(cfg, same_owner)).json()["title"] == "Renamed"
    assert client.post(f"{uri}/duplicate", json={}, headers=a).status_code == 201
    assert client.delete(uri, headers=a).status_code == 204
    assert client.get(uri, headers=a).status_code == 404


def test_scoped_jobs_keep_same_science_and_cannot_cancel_each_other(private):
    cfg, store, tokens, client = private
    a, b = (headers(cfg, tokens[key]) for key in ("a", "b"))
    _, request = args("archive", "forecast")
    first = client.post(f"{API}/analyses", json=request, headers=a).json()
    repeated = client.post(f"{API}/analyses", json=request, headers=a).json()
    other = client.post(f"{API}/analyses", json=request, headers=b).json()
    assert first["id"] == repeated["id"] != other["id"]
    assert client.get(f"{API}/analyses/{first['id']}", headers=b).status_code == 404
    assert client.get(f"{API}/analyses/{first['id']}/details", headers=b).status_code == 404
    assert client.get(f"{API}/analyses/{first['id']}/export", headers=b).status_code == 404
    assert client.delete(f"{API}/analyses/{first['id']}", headers=b).status_code == 404
    home, away = [completed(client, cfg, token, row) for token, row in ((tokens["a"], first), (tokens["b"], other))]
    assert home["result"] == away["result"]
    assert home["result"]["analysis_id"] not in (home["id"], away["id"])
    assert not away["cache_hit"]
    assert client.post(f"{API}/analyses", json=request, headers=a).json()["cache_hit"]
    with store.connect(write=True) as db:
        db.execute("UPDATE jobs SET updated=0 WHERE id=?", (home["id"],))
    assert client.get(f"{API}/analyses/{away['id']}/export", headers=a).status_code == 404
    # A submission prunes expired results, but cannot hand out the other owner's retained result.
    next_run = client.post(f"{API}/analyses", json=request, headers=a).json()
    assert next_run["id"] not in (home["id"], away["id"])
    assert client.get(f"{API}/analyses/{home['id']}", headers=a).status_code == 410


def test_per_owner_and_submission_limits_and_owned_cancellation(private):
    cfg, store, tokens, client = private
    a, b = (headers(cfg, tokens[key]) for key in ("a", "b"))
    _, timing = args("timing", "timing")
    _, forecast = args("archive", "forecast")
    _, manual = args("two-car", "manual", policies=["63-L31-hard"])
    running = client.post(f"{API}/analyses", json=timing, headers=a).json()
    queued = client.post(f"{API}/analyses", json=forecast, headers=a).json()
    assert client.post(f"{API}/analyses", json=manual, headers=a).json()["error"]["code"] == "QUEUE_FULL"
    assert client.delete(f"{API}/analyses/{queued['id']}", headers=b).status_code == 404
    assert client.delete(f"{API}/analyses/{queued['id']}", headers=a).json()["state"] == "CANCELED"
    asked = client.delete(f"{API}/analyses/{running['id']}", headers=a).json()
    assert completed(client, cfg, tokens["a"], asked)["state"] == "CANCELED"
    owner_a = store.principal(cfg.oidc_issuer, "a")["id"]
    for _ in range(cfg.rate_submissions-3):
        store.limit(owner_a, "submit", cfg.rate_submissions)
    assert client.post(f"{API}/analyses", json=forecast, headers=a).json()["error"]["code"] == "RATE_LIMIT"
    assert client.post(f"{API}/analyses", json=forecast, headers=b).status_code == 202


def test_migration_backup_interruption_and_database_lease(tmp_path):
    cfg = configuration(tmp_path/"source.sqlite3")
    store = Store(cfg.database)
    store.migrate()
    store.migrate()
    store.enroll(cfg.oidc_issuer, "a", "Researcher A")
    owner = store.principal(cfg.oidc_issuer, "a")["id"]
    token = store.new_session(owner, 600)
    state, request = args("archive", "forecast")
    doc = document(state, request)
    from f1ri.api.workspace import WorkspaceFile

    store.save(owner, WorkspaceFile.model_validate(doc), 10)
    with store.connect(write=True) as db:
        for key, status in (("1"*64, "QUEUED"), ("2"*64, "RUNNING"), ("3"*64, "RUNNING")):
            db.execute("INSERT INTO jobs(id,owner,computation,expected,state,created,updated) VALUES(?,?,?,?,?,?,?)",
                       (key, owner, "0"*64, "", status, 1, 1))
    with lease(cfg.database), pytest.raises(OSError), lease(cfg.database):
        pytest.fail("Two processes claimed the same persistent job queue.")
    out = tmp_path/"backup.sqlite3"
    store.copy(out)
    with pytest.raises(FileExistsError):
        store.copy(out)
    restored = tmp_path/"restored.sqlite3"
    Store(out).copy(restored)
    assert Store(restored).integrity()
    with TestClient(create_app(ROOT, private_config=cfg), base_url=cfg.origin) as client:
        for key in ("1"*64, "2"*64, "3"*64):
            job = client.get(f"{API}/analyses/{key}", headers=headers(cfg, token)).json()
            assert job["state"] == "FAILED" and job["code"] == "SERVICE_RESTART"
            assert job["result"] is None
        assert client.get(f"{API}/workspaces/{doc['id']}", headers=headers(cfg, token)).status_code == 200
    assert Store(restored).workspace(owner, doc["id"]).title == doc["title"]


def test_worker_guard_exits_without_supervisor_and_on_deadline():
    guard = ("from f1ri.api.worker import _guard; from time import monotonic,sleep; import sys; "
             "_guard(int(sys.argv[1]), float(sys.argv[2]), monotonic()+float(sys.argv[3])); "
             "print('ready', flush=True); sleep(15)")
    with Popen([executable, "-c", "from time import sleep; sleep(15)"]) as supervisor:
        args = [executable, "-c", guard, str(supervisor.pid), str(psutil.Process(supervisor.pid).create_time()), "10"]
        with Popen(args, stdout=PIPE, text=True) as worker:
            try:
                assert worker.stdout.readline().strip() == "ready"
                supervisor.terminate()
                supervisor.wait(timeout=5)
                assert worker.wait(timeout=5) == 125
            finally:
                if worker.poll() is None:
                    worker.kill()

    own = psutil.Process(getpid())
    deadline = run([executable, "-c", guard, str(own.pid), str(own.create_time()), "0.5"],
                   capture_output=True, timeout=7, check=False)
    assert deadline.returncode == 124


def test_private_configuration_and_log_redaction(tmp_path):
    from pydantic import ValidationError

    cfg = configuration(tmp_path/"db.sqlite3", log_level="WARNING")
    assert cfg.log_level == "WARNING"
    with pytest.raises(ValidationError, match="loopback"):
        PrivateConfig.model_validate(dict(cfg) | {"origin": "https://remote.example.invalid"})
    with pytest.raises(ValidationError):
        configuration(tmp_path/"db.sqlite3", log_level="DEBUG")
    error = makeLogRecord({"msg": "request failed", "exc_info": (ValueError, ValueError("hidden payload"), None)})
    assert ServiceFormatter().format(error) == "request failed"
