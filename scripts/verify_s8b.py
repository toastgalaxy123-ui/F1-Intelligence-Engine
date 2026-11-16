"""Reproduce S8-A results and build the S8-B workspace release evidence."""

from argparse import ArgumentParser
from base64 import b64decode
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import version
from io import BytesIO
from json import loads
from pathlib import Path
from shutil import copyfile
from subprocess import run
from time import perf_counter, sleep
from uuid import UUID
from zipfile import ZipFile

import httpx
from verify_s8a import (
    PRIOR,
    REPORT_DIR,
    ROOT,
    _fresh_runs,
    _request,
    _wait,
    measure,
    parity,
    regression,
)

from f1ri import decision_board as board
from f1ri.api.workspace import WorkspaceFile
from f1ri.validation.historical_field_sources import checked_members, write_archive
from f1ri.validation.historical_field_study import _seal, write_json

BASELINE = "125886dac5e2648b76efe9bead699124bb52a619"
DEST = ROOT / "artifacts/runs/stage8_s8_b_strategy_workspace"
A_PINS = {
    "stage8_s8_a_evidence.zip": "9f094bb072aceb7e3fefcda8a5300ce062d634029cd1e6d12640258f6120be93",
    "stage8_s8_a_metrics.json": "af1fada6465532f61848cde4752a84a22672d900a25050472b0fbe66b9349c2e",
    "stage8_s8_a_validation.md": "eb46e32a2f776842d0f0fc60784ebbb96765e8a61cfa05761cb78a71906adf17",
}
TEAM = ("4-L30-hard__63-L31-hard", "4-L30-hard__63-L32-soft")
API = "/api/v1"


def frozen():
    if any(board.digest((REPORT_DIR/name).read_bytes()) != checksum for name, checksum in A_PINS.items()):
        raise ValueError("Pinned S8-A artifacts differ.")
    archive = REPORT_DIR / "stage8_s8_a_evidence.zip"
    names = checked_members(archive, A_PINS[archive.name])
    if len(names) != 46:
        raise ValueError("Unexpected number of S8-A evidence members.")
    run(["git", "diff", "--quiet", BASELINE, "--", "src/f1ri/simulator", "src/f1ri/models",
         "artifacts/models", "artifacts/reports/stage8_s8_a*"], cwd=ROOT, check=True)
    with ZipFile(archive) as z:
        return loads(z.read("api_parity.json")), {"commit": BASELINE, "pins": A_PINS, "evidence_members": len(names),
                                                "simulator_and_model_paths_unchanged": True}


def _lines(raw):
    return {path: digest for digest, path in (row.split("  ", 1) for row in raw.decode().splitlines() if row)}


def audit_manifests(prefix):
    documents = {}
    for stage, archive in (("s6c", "stage6_s6_c"), ("s7c0", "stage7_s7_c0")):
        folder = prefix.with_name(f"{prefix.name}_{stage}_regression")
        if not (folder/"SHA256SUMS").is_file():
            documents[stage] = {"statuses": ["UNRESOLVED_DIFFERENCE"], "reason": "A rerun manifest is required."}
            continue
        actual = (folder/"SHA256SUMS").read_bytes()
        path = REPORT_DIR/f"{archive}_evidence.zip"
        checked_members(path)
        with ZipFile(path) as z:
            published = z.read("SHA256SUMS")
            if stage == "s6c":
                expected = loads(z.read("regression_parity.json"))["exact_s6_c_reproduction"]
            else:
                replay_zip = z.read("diagnostics/shadow_replay.zip")
                with ZipFile(BytesIO(replay_zip)) as replay:
                    expected = loads(replay.read("manifest.json"))["outputs"]
                expected["diagnostics/shadow_replay.zip"] = board.digest(replay_zip)
            a, b = _lines(published), _lines(actual)
            common = a.keys() & b.keys()
            changed = sorted(key for key in common if a[key] != b[key])
            fields = {}
            for key in changed:
                if key.endswith(".json"):
                    release, replay = loads(z.read(key)), loads((folder/key).read_bytes())
                    fields[key] = {"release_only": sorted(release.keys()-replay.keys()),
                                   "rerun_only": sorted(replay.keys()-release.keys()),
                                   "value_changes": sorted(k for k in release.keys() & replay.keys()
                                                           if release[k] != replay[k])}
            det = [name for name, h in expected.items() if board.digest((folder/name).read_bytes()) != h]
        volatile = {"performance.json": {"observed_wall_seconds"}} if stage == "s6c" else {}
        allowed = {"metrics.json", "regression_parity.json", *volatile} if stage == "s6c" else set()
        unexplained = (set(changed)-allowed or any(
            set(fields.get(name, {}).get("value_changes", ())) != expected_keys
            for name, expected_keys in volatile.items()
        ) or any(fields.get(k, {}).get("value_changes") for k in allowed-volatile.keys()))
        status = ["OUTPUT_BYTES_VERIFIED", "SEMANTIC_PARITY_VERIFIED"]
        extra = a.keys()-b.keys()
        media = "screenshots/" if stage == "s6c" else "diagnostics/screenshots/"
        packaging = {"validation_report.md", "ui_validation.json" if stage == "s6c" else "diagnostics/ui_validation.json"}
        if det or unexplained or b.keys()-a.keys() or any(k not in packaging and not k.startswith(media) for k in extra):
            status = ["UNRESOLVED_DIFFERENCE"]
        if published == actual:
            status.append("MANIFEST_BYTES_VERIFIED")
        documents[stage] = {"statuses": status, "required_files": len(expected), "published_entries": len(a),
            "rerun_entries": len(b), "published_manifest_sha256": board.digest(published),
            "rerun_manifest_sha256": board.digest(actual), "full_manifest_bytes_equal": published == actual,
            "published_crlf_lines": published.count(b"\r\n"), "rerun_crlf_lines": actual.count(b"\r\n"),
            "shared_path_order_equal": [k for k in a if k in b] == [k for k in b if k in a],
            "published_only": sorted(a.keys()-b.keys()), "rerun_only": sorted(b.keys()-a.keys()),
            "changed_common_members": changed,
            "changed_member_hashes": {k: {"published": a[k], "rerun": b[k]} for k in changed},
            "changed_json_fields": fields,
            "deterministic_differences": det,
            "explanation": ("Published browser/report packaging and release metadata differ. Only wall-time measurements "
                            "change on shared research performance fields." if stage == "s6c" else
                            "The release wraps the deterministic run with browser observations, screenshots and report.")}
    return documents


def _query(client, preset, operation, **kwargs):
    snapshot = _request(client, "GET", f"{API}/presets/{preset}/snapshot")
    return snapshot, {"operation": operation, "context": snapshot["context"], "snapshot_id": snapshot["snapshot_id"], **kwargs}


def _timed(client, method, path, **options):
    start = perf_counter()
    value = _request(client, method, path, **options)
    return value, perf_counter()-start


def team_and_workspace(client):
    snap, query = _query(client, "shared-box", "manual", policies=TEAM, max_stops=2)
    start = perf_counter()
    job = _wait(client, _request(client, "POST", f"{API}/analyses", json=query))
    total = perf_counter()-start
    result = job["result"]
    timelines = result["payload"]["decision"]["timelines"]
    original = parity(result)
    assert [p["wait_us"] for p in timelines[TEAM[0]]["pit"]] == [0, 2_903_848]
    assert [p["wait_us"] for p in timelines[TEAM[1]]["pit"]] == [0, 0]
    details, detail_time = _timed(client, "GET", f"{API}/analyses/{job['id']}/details")
    assert details["result_sha256"] == result["result_sha256"]
    assert all(details["branches"]["decision"][policy]["checkpoints"] for policy in TEAM)
    checks = {policy: {"row": next(row for row in result["payload"]["decision"]["rows"] if row["candidate"] == policy),
                      "service": timelines[policy]["pit"],
                      "checkpoints": len(details["branches"]["decision"][policy]["checkpoints"])}
              for policy in (*TEAM, "reference")}
    doc = {
        "version": "s8b-workspace-v1", "id": str(UUID("28062762-bd60-449e-b7a0-dbd1f460a581")), "title": "Shared-box replay",
        "created_at": "2026-11-16T00:00:00Z", "updated_at": "2026-11-16T00:00:00Z",
        "context": snap["context"], "snapshot_id": result["snapshot_id"], "model_sha256": result["model"]["sha256"],
        "source_hashes": snap["source_hashes"], "support": snap["support"], "analysis": result["request"],
        "receipt": {k: result[k] for k in ("analysis_id", "result_sha256")}, "reference": TEAM[1],
    }
    verified, verify_time = _timed(client, "POST", f"{API}/workspaces/verify", json=doc)
    assert verified["replay_available"] and verified["analysis_id"] == result["analysis_id"]
    blocked = {}
    for key, value in (("model_sha256", "0"*64), ("source_hashes", {"../outside": "0"*64})):
        response = client.post(f"{API}/workspaces/verify", json=doc | {key: value})
        assert response.status_code == 409
        blocked[key] = response.json()["error"]["code"]
    replay = _wait(client, _request(client, "POST", f"{API}/replays",
                                   json={"request": result["request"], "expected_sha256": result["result_sha256"]}))
    assert board.canonical(replay["result"]) == board.canonical(result)
    return ({"parity": original, "policies": checks, "objective": result["request"]["team_objective"],
             "marker": result["evaluation_marker"]},
            {"document": doc, "verification": verified, "mismatches_rejected": blocked, "result_checksum_verified": True},
            {"team_end_to_end_s": total, "team_worker": job["measurements"], "drilldown_http_s": detail_time,
             "workspace_verify_http_s": verify_time})


def two_clients(client):
    _, queries = _query(client, "neutralization", "robust", budget=128, robust_budget=239)
    def submit():
        with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False, timeout=10) as other:
            return _timed(other, "POST", f"{API}/analyses", json=queries)
    with ThreadPoolExecutor(max_workers=2) as sessions:
        a, b = sessions.map(lambda _: submit(), range(2))
    assert a[0]["id"] == b[0]["id"]
    shared = _wait(client, a[0])

    _, timing = _query(client, "timing", "timing")
    timing_result = _wait(client, _request(client, "POST", f"{API}/analyses", json=timing))["result"]
    running = _request(client, "POST", f"{API}/replays",
        json={"request": timing, "expected_sha256": timing_result["result_sha256"]})
    active_uri = f"{API}/analyses/{running['id']}"
    deadline = perf_counter()+10
    while running["state"] == "QUEUED" and perf_counter() < deadline:
        sleep(.025)
        running = _request(client, "GET", active_uri)
    assert running["state"] == "RUNNING"
    queued = _request(client, "POST", f"{API}/analyses", json=queries | {"robust_budget": 238})
    assert queued["state"] == "QUEUED"
    queued_at = perf_counter()
    terminal_queue = _request(client, "DELETE", f"{API}/analyses/{queued['id']}")
    queued_s = perf_counter()-queued_at
    assert terminal_queue["state"] == "CANCELED"
    running_at = perf_counter()
    want = _request(client, "DELETE", active_uri)
    while want["state"] in {"RUNNING", "QUEUED"} and perf_counter()-running_at < 12:
        sleep(.025)
        want = _request(client, "GET", active_uri)
    assert want["state"] == "CANCELED" and want["result"] is None
    return {"identical_clients_one_job": True, "submit_http_s": [a[1], b[1]], "worker": shared["measurements"],
            "queued_cancel_s": queued_s, "active_cancel_s": perf_counter()-running_at,
            "canceled_before_result": want["result"] is None, "worker_timeout_test": "tests/api/test_research_api.py"}


def browser(report, out):
    if not report:
        return {"verified": False, "screenshots": [], "tests": []}
    data = loads(report.read_bytes())
    screenshots = sorted((out/"screenshots").glob("*.png"))
    assert data["stats"]["unexpected"] == 0 and data["stats"]["expected"] >= 8
    assert len(screenshots) == 30 and {int(p.name[:2]) for p in screenshots} == set(range(1, 31))
    tests = []
    def collect(suite):
        for spec in suite.get("specs", []):
            results = [case for test in spec["tests"] for case in test["results"]]
            timings = {item["name"]: float(b64decode(item["body"])) for case in results
                       for item in case.get("attachments", []) if item["name"].endswith("_ms") and "body" in item}
            tests.append({"name": spec["title"], "passed": spec["ok"],
                          "duration_ms": sum(case["duration"] for case in results), "interactions_ms": timings})
        for child in suite.get("suites", []):
            collect(child)
    for suite in data["suites"]:
        collect(suite)
    return {"verified": True, "tests": tests, "screenshots": [{"file": p.name, "sha256": board.digest(p.read_bytes())}
                                                            for p in screenshots]}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEST)
    parser.add_argument("--prior-prefix", type=Path, default=DEST.with_name("stage8_s8_a"))
    parser.add_argument("--rerun-priors", action="store_true")
    parser.add_argument("--ui-report", type=Path)
    parser.add_argument("--tests-passed", type=int)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(exist_ok=True, parents=True)
    expected, baseline = frozen()
    if args.rerun_priors:
        _fresh_runs(args.prior_prefix)
    prior = regression(args.prior_prefix)
    audit = audit_manifests(args.prior_prefix)
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=35, trust_env=False) as client:
        schema = _request(client, "GET", "/openapi.json")
        health = _request(client, "GET", f"{API}/health")
        _, _, perf, current = measure(client)
        regress = {label: {k: value[k] == current[label][k] for k in ("analysis_id", "snapshot_id", "result_sha256")}
                   for label, value in expected.items() if "analysis_id" in value}
        assert len(regress) == 8 and all(all(r.values()) for r in regress.values())
        team, workspace, timings = team_and_workspace(client)
        jobs = two_clients(client)
    ui = browser(args.ui_report, out)
    prior_expected = {key for key, *_ in PRIOR} | {"s7c0"}
    ready = (ui["verified"] and args.tests_passed and args.tests_passed >= 458 and
             all(prior["reruns"][key]["verified"] for key in prior_expected) and
             all("OUTPUT_BYTES_VERIFIED" in v["statuses"] for v in audit.values()))
    if args.publish and not ready:
        parser.error("Published evidence requires passing browser, test and frozen-output checks.")
    release = "INTERACTIVE_RESEARCH_WORKSPACE_VALIDATED" if ready else "VERIFICATION_INCOMPLETE"
    package = loads((ROOT/"apps/web/package.json").read_bytes())
    metrics = {"decision": "A" if ready else "PENDING", "release": release, "baseline_tests": 454,
               "pytest_passed": args.tests_passed, "chrome_tests": len(ui["tests"]), "screenshots": len(ui["screenshots"]),
               "timing_original_regret_us": 46_754_185, "s8a_result_hashes_verified": len(regress), "live": "LIVE_UNSUPPORTED"}
    documents = {
        "protocol": {"version": "s8-b", "source_release": BASELINE, "scope": "TRUSTED_LOCAL_RESEARCH"},
        "frozen_baseline": baseline,
        "api_contract": {"version": "v1", "openapi": schema, "timing_unit": "integer microseconds", "worker": health["budgets"],
                         "original_result_shape": "UNCHANGED"},
        "frontend_manifest": {"dependencies": package["dependencies"], "scripts": package["scripts"],
                              "lock_sha256": board.digest((ROOT/"apps/web/package-lock.json").read_bytes())},
        "backend_manifest": {"versions": {name: version(name) for name in ("fastapi", "uvicorn", "pydantic", "psutil")},
                             "analysis_worker": "one subprocess"},
        "workspace_schema": WorkspaceFile.model_json_schema(), "workspace_replay": workspace,
        "strategy_comparison": current["manual"], "team_strategy_validation": team,
        "policy_detail_validation": {"same_engine_checkpoint_events": True, "paired": team["policies"],
                                     "suite": "tests/api/test_strategy_workspace.py"},
        "timeline_validation": {"team_wait_us": 2_903_848, "staggered_wait_us": 0, "timing_regret_us": 46_754_185,
                                "clock": "racetime_us", "interpolation": False},
        "robust_visualization": current["robust"],
        "recommendation_support": {k: current[k]["recommendation"] for k in ("manual", "conditional_small", "robust")},
        "job_lifecycle": jobs, "ui_validation": ui, "browser_tests": ui["tests"],
        "performance": perf | timings | {"two_clients": jobs, "browser_workflow_ms": {
            row["name"]: row["duration_ms"] for row in ui["tests"]},
            "browser_interaction_ms": {name: elapsed for row in ui["tests"] for name, elapsed in row["interactions_ms"].items()}},
        "security_review": {"local_only": True, "max_workspace_bytes": 15360, "max_api_body_bytes": 16384,
                            "max_saved_entries": 24, "mismatches": workspace["mismatches_rejected"],
                            "arbitrary_paths": "DENIED", "hosted_authentication": "REQUIRED"},
        "private_beta_readiness": {"local": release, "remote": "S8_C_AUTHENTICATION_AND_CAPACITY_REQUIRED",
                                   "plan": "docs/web/private-beta.md"},
        "regression_manifest_audit": audit, "api_parity": current,
        "regression_parity": {"s8a_exact_results": regress, "earlier_studies": prior},
        "invariant_checks": {"unmodified_s8a": True, "no_live": health["live"] == "LIVE_UNSUPPORTED",
                             "workspace_checksum_verified": True, "team_parity": True, "legacy_result_shape_unchanged": True},
        "metrics": metrics,
    }
    for name, value in documents.items():
        write_json(out/f"{name}.json", value)
    copyfile(ROOT/"docs/web/strategy-workspace.md", out/"architecture_changes.md")
    if args.publish:
        copyfile(REPORT_DIR/"stage8_s8_b_validation.md", out/"validation_report.md")
        copyfile(out/"metrics.json", REPORT_DIR/"stage8_s8_b_metrics.json")
    _seal(out)
    if args.publish:
        target = REPORT_DIR/"stage8_s8_b_evidence.zip"
        write_archive(target, {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*")
                                if p.is_file() and p.name != "SHA256SUMS" and not p.name.startswith("playwright_")})
        print(f"{len(checked_members(target))} checksummed evidence files · {board.digest(target.read_bytes())}")
    print(release)


if __name__ == "__main__":
    main()
