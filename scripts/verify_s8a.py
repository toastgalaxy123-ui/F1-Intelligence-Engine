"""Exercise the local production API and write the checksummed S8-A release ledger."""

from argparse import ArgumentParser
from importlib.metadata import version
from io import BytesIO
from json import loads
from pathlib import Path
from shutil import copyfile
from subprocess import run
from sys import executable
from time import perf_counter, sleep
from zipfile import ZipFile

import httpx

from f1ri import decision_board as board
from f1ri.api.contracts import MODEL_SHA, Result
from f1ri.api.research import PINS, REPORTS, evidence, prepare_analysis
from f1ri.validation.historical_field_sources import checked_members, write_archive
from f1ri.validation.historical_field_study import _seal, write_json

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / REPORTS
PRIOR_BASE = "93c4cbea3b241b5e662dcdc7874f6bb9a98be183"
DEFAULT = ROOT / "artifacts/runs/stage8_s8_a_web_foundation"
PRIOR = (
    ("s6b1", "stage6_s6_c", "regression_parity.json", "exact_s6_b1_reproduction"),
    ("s6c", "stage6_s6_c", "regression_parity.json", "exact_s6_c_reproduction"),
    ("s7a", "stage7_s7_a", "release_verification.json", "deterministic_s7a_files"),
    ("s7a1", "stage7_s7_a1", "release_verification.json", "deterministic_run_files"),
    ("s7b", "stage7_s7_b", "release_verification.json", "deterministic_replay_files"),
)
WORKFLOWS = (
    ("forecast", "archive", "forecast", {}),
    ("replacement", "two-car", "forecast", {"tyre_sets": ["soft"]}),
    ("manual", "two-car", "manual", {"policies": ["63-L31-hard", "63-L31-medium", "63-L31-soft"]}),
    ("shared_box", "shared-box", "manual", {}),
    ("conditional_small", "two-car", "conditional", {"future": "VSC", "budget": 4}),
    ("conditional_larger", "two-car", "conditional", {"future": "SC", "budget": 128}),
    ("robust", "neutralization", "robust", {"budget": 128, "robust_budget": 240}),
    ("timing", "timing", "timing", {}),
)


def _member(stage, member):
    archive = REPORT_DIR / f"{stage}_evidence.zip"
    checked_members(archive)
    with ZipFile(archive) as z:
        return loads(z.read(member))


def _fresh_runs(prefix):
    def path(stage):
        return str(prefix.with_name(f"{prefix.name}_{stage}_regression"))

    commands = (
        ["stage6-s6-b1", "--output", path("s6b1")],
        ["stage6-s6-c", "--output", path("s6c")],
        ["stage7-s7-a", "--s6-c-run", path("s6c"), "--output", path("s7a")],
        ["stage7-s7-a1", "--s7-a-run", path("s7a"), "--output", path("s7a1")],
        ["stage7-s7-b", "study", "--s6-c-run", path("s6c"), "--s7-a-run", path("s7a"),
         "--s7-a1-run", path("s7a1"), "--output", path("s7b")],
        ["stage7-s7-c0", "study", "--s7-b-run", path("s7b"), "--output", path("s7c0")],
    )
    for command in commands:
        run([executable, "-m", "f1ri.cli", *command], cwd=ROOT, check=True)


def _compare(folder, expected):
    differences = [name for name, pin in expected.items()
                   if not (folder / name).is_file() or board.digest((folder / name).read_bytes()) != pin]
    if differences:
        raise ValueError(f"Frozen regression differs for {folder.name} at {differences}.")
    return {"verified": True, "outputs": len(expected) - int("SHA256SUMS" in expected),
            "checksum_manifest": "SHA256SUMS" in expected,
            "expected_manifest_sha256": board.digest(board.canonical(expected))}


def regression(prefix):
    run(["git", "diff", "--quiet", PRIOR_BASE, "--", "src/f1ri/simulator", "src/f1ri/models",
         "artifacts/models", "artifacts/reports/stage7_s7_c0*"], cwd=ROOT, check=True)
    result = {}
    for stage, source, member, field in PRIOR:
        expected = _member(source, member)[field]
        folder = prefix.with_name(f"{prefix.name}_{stage}_regression")
        result[stage] = _compare(folder, expected) if folder.is_dir() else {"verified": None, "outputs": len(expected)}
    c0 = REPORT_DIR / "stage7_s7_c0_evidence.zip"
    names = checked_members(c0, PINS[c0.name])
    with ZipFile(c0) as z:
        raw = z.read("diagnostics/shadow_replay.zip")
        inherited = loads(z.read("frozen_baseline.json"))
    with ZipFile(BytesIO(raw)) as z:
        expected = loads(z.read("manifest.json"))["outputs"]
    c0_folder = prefix.with_name(f"{prefix.name}_s7c0_regression")
    expected["diagnostics/shadow_replay.zip"] = names["diagnostics/shadow_replay.zip"]
    result["s7c0"] = _compare(c0_folder, expected) if c0_folder.is_dir() else {"verified": None, "outputs": len(expected)}
    return {
        "baseline": PRIOR_BASE, "simulator_and_model_paths_unchanged": True,
        "reruns": result,
        "s7c0_evidence_members": len(names),
        "s7c0_rehearsal_members": len(checked_members(REPORT_DIR / "stage7_s7_c0_rehearsal.zip")),
        "s7c0_export_manifest_outputs": len(expected)-1,
        "inherited_s7c0_earlier_rerun_flags": inherited["earlier_release"]["independent_reruns"],
        "flag_interpretation": "The inherited baseline flags describe that document only. S8-A verification is in reruns.",
        "pinned_s7c0_sha256": PINS,
    }


def _request(client, method, path, **options):
    response = client.request(method, path, **options)
    response.raise_for_status()
    return response.json()


def _wait(client, task):
    start = perf_counter()
    while task["state"] in ("QUEUED", "RUNNING"):
        if perf_counter()-start > 65:
            raise TimeoutError("Local analysis did not reach a terminal state.")
        sleep(.125)
        task = _request(client, "GET", f"/api/v1/analyses/{task['id']}")
    if task["state"] != "COMPLETED":
        raise ValueError(f"Unexpected job status {task['state']} {task.get('code')}")
    return task


def parity(document):
    result = Result.model_validate(document)
    _, _, settings, _ = prepare_analysis(ROOT, result.request)
    engine = board.execute(ROOT, settings)
    operation, payload = result.request.operation, result.payload
    from_engine = engine["strategy"]
    pairs = []
    if operation == "forecast":
        pairs = [(payload["forecasts"], engine["forecasts"])]
    elif operation == "manual":
        for view, branch in (("decision", "manual"), ("declared", "manual_future")):
            original = from_engine[branch]
            pairs.append((payload[view]["rows"], original["rows"]))
            pairs.append((payload[view]["best"], original["best"]))
            pairs.extend((timeline["pit"], original["paired"]["results"][policy]["pit"])
                         for policy, timeline in payload[view]["timelines"].items())
    elif operation == "conditional":
        pairs = [(payload["search"], from_engine["conditional"]["search"])]
        pairs.extend((payload[key]["rows"], from_engine["conditional"][key]["rows"])
                     for key in ("decision", "conditional"))
    elif operation == "robust":
        pairs = [(payload["analysis"], from_engine["robust"]["analysis"]),
                 (payload["sensitivity"], engine["recommendation"]["case_winners"])]
    elif operation == "timing":
        pairs = [(payload["case"], engine["timing"]["case"]), (payload["reproduced_regret_us"], 46_754_185),
                 (payload["marker_gap"], engine["timing"]["baseline"]["marker_gap"])]
    expected_sha = board.digest(board.canonical({k: v for k, v in document.items() if k != "result_sha256"}))
    if not pairs or any(board.canonical(a) != board.canonical(b) for a, b in pairs) or expected_sha != result.result_sha256:
        raise ValueError(f"API parity failed for {operation}.")
    return {"verified": True, "canonical_comparisons": len(pairs), "analysis_id": result.analysis_id,
            "snapshot_id": result.snapshot_id, "result_sha256": result.result_sha256,
            "model_sha256": result.model["sha256"], "policies_executed": result.executed_policies,
            "completion": result.completion, "recommendation": result.recommendation.model_dump(),
            "evaluation_marker": result.evaluation_marker, "source": "Direct existing engine invocation"}


def measure(client):
    prefix = "/api/v1"
    t = perf_counter()
    presets = _request(client, "GET", f"{prefix}/presets")
    performance = {"presets_http_wall_s": perf_counter()-t, "analysis": {}}
    snapshots, checks, manual = {}, {}, None
    for label, preset, operation, params in WORKFLOWS:
        if preset not in snapshots:
            started = perf_counter()
            snapshots[preset] = _request(client, "GET", f"{prefix}/presets/{preset}/snapshot")
            performance.setdefault("snapshot_http_wall_s", {})[preset] = perf_counter()-started
        snapshot = snapshots[preset]
        request = {"context": snapshot["context"], "snapshot_id": snapshot["snapshot_id"], "operation": operation} | params
        t = perf_counter()
        job = _wait(client, _request(client, "POST", f"{prefix}/analyses", json=request))
        elapsed = perf_counter()-t
        checks[label] = parity(job["result"])
        performance["analysis"][label] = {"end_to_end_s": elapsed, "cache_hit": job["cache_hit"]} | job["measurements"]
        if label == "manual":
            manual = job
            t = perf_counter()
            cached = _request(client, "POST", f"{prefix}/analyses", json=request)
            performance["identical_manual_cache_http_s"] = perf_counter()-t
            assert cached["cache_hit"] and cached["result"] == job["result"]
        print(f"{label}: {job['state']} {job['result']['completion']} {elapsed:.3f}s", flush=True)
    assert manual
    result = _request(client, "GET", f"{prefix}/analyses/{manual['id']}/export")
    assert result == manual["result"]
    replay = _wait(client, _request(client, "POST", f"{prefix}/replays",
                                   json={"request": result["request"], "expected_sha256": result["result_sha256"]}))
    assert replay["result"] == result and replay["id"] != manual["id"]
    checks["export_replay"] = {"verified": True, "result_sha256": result["result_sha256"],
                              "replay_job_id": replay["id"], "cache_hit": replay["cache_hit"]}
    historic = _request(client, "GET", f"{prefix}/presets/unsupported/snapshot")
    withheld = _request(client, "POST", f"{prefix}/analyses", json={"context": historic["context"],
        "snapshot_id": historic["snapshot_id"], "operation": "manual"})
    assert withheld["state"] == "UNSUPPORTED" and not withheld["result"]
    checks["historical_negative"] = {"verified": True, "state": withheld["state"], "code": withheld["code"]}
    return presets, snapshots, performance, checks


def ui_evidence(path, directory):
    if not path:
        return {"verified": None, "screenshots": [], "reason": "No browser run supplied."}
    data = loads(path.read_bytes())
    shots = sorted(directory.glob("*.png"))
    assert data["stats"]["unexpected"] == 0 and data["stats"]["expected"] >= 3
    assert len(shots) == 26 and {int(p.name[:2]) for p in shots} == set(range(1, 27))
    timing = []

    def collect(suite):
        for spec in suite.get("specs", []):
            timing.append({"test": spec["title"], "milliseconds": sum(
                r["duration"] for test in spec["tests"] for r in test["results"]), "passed": spec["ok"]})
        for child in suite.get("suites", []):
            collect(child)

    for suite in data["suites"]:
        collect(suite)
    return {"verified": True, "browser": "Playwright · installed Chrome", "tests": timing,
            "viewport": {"desktop": [1440, 1000], "mobile": [390, 844]},
            "screenshots": [{"file": p.name, "sha256": board.digest(p.read_bytes())} for p in shots]}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT)
    parser.add_argument("--prior-prefix", type=Path, default=DEFAULT.with_name("stage8_s8_a"))
    parser.add_argument("--rerun-priors", action="store_true")
    parser.add_argument("--ui-report", type=Path)
    parser.add_argument("--tests-passed", type=int)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    if args.rerun_priors:
        _fresh_runs(args.prior_prefix)
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    evidence(ROOT)
    pins = regression(args.prior_prefix)
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=30, trust_env=False) as client:
        api_schema = _request(client, "GET", "/openapi.json")
        health = _request(client, "GET", "/api/v1/health")
        validation = _request(client, "GET", "/api/v1/evidence")
        presets, snapshots, performance, checks = measure(client)
    ui = ui_evidence(args.ui_report, out / "screenshots")
    if ui["verified"]:
        performance["browser_workflow_ms"] = {row["test"]: row["milliseconds"] for row in ui["tests"]}
    promotable = ui["verified"] and args.tests_passed and all(row["verified"] for row in pins["reruns"].values())
    release = "WEB_RESEARCH_APPLICATION_FOUNDATION_VALIDATED" if promotable else "VERIFICATION_INCOMPLETE"
    if args.publish and not promotable:
        parser.error("Publishing the validation bundle requires browser, test and prior-release evidence.")
    package = loads((ROOT/"apps/web/package.json").read_bytes())
    metrics = {"release": release, "decision": "A" if promotable else "PENDING", "api_version": "v1",
               "presets": len(presets), "browser_cases": len(ui.get("tests", ())), "screenshots": len(ui["screenshots"]),
               "pytest_baseline": 445, "pytest_passed": args.tests_passed, "live": "LIVE_UNSUPPORTED",
               "timing_original_regret_us": 46_754_185}
    documents = {
        "protocol": {"version": "s8-a-v1", "baseline": PRIOR_BASE, "scope": "BOUNDED_LOCAL_RESEARCH",
                     "engine_authoritative": True, "reproduction_command": "python scripts/verify_s8a.py --output <directory>"},
        "api_contract": health | {"openapi": "/openapi.json", "canonical_times": "integer microseconds and UTC ISO 8601",
                                   "result_identity": "SHA-256 of canonical sorted-key JSON excluding result_sha256"},
        "openapi_schema": api_schema,
        "frontend_manifest": {"dependencies": package["dependencies"], "scripts": package["scripts"],
                              "lock_sha256": board.digest((ROOT/"apps/web/package-lock.json").read_bytes())},
        "backend_manifest": {"api": "f1ri.api.app:app", "model_sha256": MODEL_SHA,
                             "versions": {name: version(name) for name in ("fastapi", "pydantic", "uvicorn", "psutil")}},
        "preset_manifest": presets, "model_support": validation["model"],
        "scenario_support": {"snapshots": {k: {"operations": v["operations"], "snapshot_id": v["snapshot_id"],
                                "support": v["support"]} for k, v in snapshots.items()},
                             "unsupported_historical": checks["historical_negative"]},
        "api_parity": checks, "ui_validation": ui,
        "integration_tests": {"baseline_pytest": 445, "final_pytest": args.tests_passed,
                              "browser": ui.get("tests", []), "positive_supported": checks["manual"]["verified"],
                              "negative_withheld": checks["historical_negative"]["verified"]},
        "performance": performance,
        "security_review": {"loopback_default": True, "cross_origin_mutations_denied": True,
                            "trusted_hosts": ["127.0.0.1", "localhost"], "body_limit_bytes": 16_384,
                            "resource_budgets": health["budgets"], "public_access": "BLOCKED_PENDING_ACCESS_CONTROL",
                            "arbitrary_artifact_reads": False, "browser_secrets": False},
        "research_modes": validation["modes"], "deployment_readiness": {"local": release,
                           "remote": "ACCESS_CONTROL_REQUIRED", "live": "LIVE_UNSUPPORTED"},
        "regression_parity": pins,
        "invariant_checks": {"model_digest_matches": validation["model"]["sha256"] == MODEL_SHA,
                             "no_live_strategy": validation["live"] == "LIVE_UNSUPPORTED",
                             "api_comparisons_exact": all(v["verified"] for v in checks.values())},
        "metrics": metrics,
    }
    for name, value in documents.items():
        write_json(out/f"{name}.json", value)
    copyfile(ROOT/"docs/web/architecture.md", out/"architecture.md")
    if args.publish:
        copyfile(REPORT_DIR/"stage8_s8_a_validation.md", out/"validation_report.md")
        copyfile(out/"metrics.json", REPORT_DIR/"stage8_s8_a_metrics.json")
    _seal(out)
    if args.publish:
        target = REPORT_DIR/"stage8_s8_a_evidence.zip"
        write_archive(target, {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*")
                                if p.is_file() and p.name not in ("SHA256SUMS", "playwright_raw.json")})
        print(f"Evidence members {len(checked_members(target))} · SHA-256 {board.digest(target.read_bytes())}")
    print(release)


if __name__ == "__main__":
    main()
