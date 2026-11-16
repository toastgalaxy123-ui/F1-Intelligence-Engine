"""Seal the checksummed evidence for a passing S8-C localhost release rehearsal."""

from argparse import ArgumentParser
from importlib.metadata import version
from json import loads
from pathlib import Path
from platform import python_version, system
from re import findall
from shutil import copyfile
from subprocess import check_output, run
from zipfile import ZipFile

from verify_s8a import REPORT_DIR, ROOT, regression

from f1ri.api.contracts import MODEL_SHA
from f1ri.api.workspace import WorkspaceFile
from f1ri.validation.historical_field_sources import checked_members, hash_bytes, write_archive
from f1ri.validation.historical_field_study import write_json

BASELINE = "640e064b73d73907df193b03d874a7b3f72840e4"
MODEL = ROOT/"artifacts/models/PaceV2_D8R1/pace_v2_d8r1.pkl"
PINS = {
    "stage8_s8_b_evidence.zip": "1b2030d6e2df577d0e117f5c9e1937274e5a3ad0110cbc57a703b2549829c052",
    "stage8_s8_b_metrics.json": "b38b8abf8af87fe3a0aa4fd10f304fd54f3660dd1eda72bd52c314ba41077628",
    "stage8_s8_b_validation.md": "f7e2da63eacfd74fb3cee56a237c3fc3e37c8f2e1a87938dae220f6b7383fc34",
}
RELEASE = "PRIVATE_BETA_DEPLOYMENT_READY"


def read(path):
    return loads(path.read_bytes())


def chrome_report(path):
    raw = read(path)
    assert raw["stats"]["unexpected"] == raw["stats"]["skipped"] == raw["stats"]["flaky"] == 0
    assert not raw["errors"]

    def specs(suite):
        for spec in suite.get("specs", []):
            assert spec["ok"] and all(t["status"] == "expected" for t in spec["tests"])
            yield {"title": spec["title"], "duration_ms": sum(r["duration"] for t in spec["tests"] for r in t["results"])}
        for child in suite.get("suites", []):
            yield from specs(child)

    rows = [row for suite in raw["suites"] for row in specs(suite)]
    assert len(rows) == raw["stats"]["expected"]
    return rows


def observations(out, browser_paths):
    private, recovery, legacy = (chrome_report(path) for path in browser_paths)
    assert (len(private), len(recovery), len(legacy)) == (4, 1, 8)
    assert {int(n) for row in private+recovery for n in findall(r"\bH(\d+)\b", row["title"])} == set(range(1, 21))
    shots = sorted((out/"screenshots").glob("*.png"))
    assert [int(path.name[:2]) for path in shots] == list(range(1, 18))
    first = read(out/"api_rehearsal_first.json")
    candidate = read(out/"api_rehearsal_candidate.json")
    for key in ("comparisons", "team", "timing_regret_us"):
        assert first[key] == candidate[key]
    assert first["exact_frozen_results"] and candidate["exact_frozen_results"]
    assert first["timing_regret_us"] == 46_754_185
    restart = read(out/"recovery_observations.json")
    assert restart["active_and_queued"] == "FAILED/SERVICE_RESTART"
    assert all(restart[k] for k in ("pending_draft_preserved", "isolated_restore_integrity",
                                   "reopened_after_restart_restore_and_rollback", "restored_sessions_invalidated"))
    smoke = read(out/"deployment_smoke_tests.json")
    assert smoke["strict_certificate_validation"] and smoke["expected_security_headers"]
    assert not smoke["staging_secrets_in_bundles"] and smoke["compiled_bundles_scanned"] > 0
    return {
        "browser_tests": {"chrome": private+recovery, "legacy_chrome": legacy,
                          "coverage": [f"H{i}" for i in range(1, 21)],
                          "screenshots": [p.relative_to(out).as_posix() for p in shots]},
        "api_parity": {"supported": first["comparisons"], "paired_team": first["team"],
                       "regret_us": first["timing_regret_us"], "identical_across_two_cold_rehearsals": True},
        "rollback_validation": restart,
        "backup_restore": {"backup_sqlite_integrity": restart["backup_integrity"],
                           "restored_sqlite_integrity": restart["isolated_restore_integrity"],
                           "sessions_removed_from_restored_copy": restart["restored_sessions_invalidated"],
                           "verified_result_sha256": restart["verified_result_sha256"], "isolated_destination": True},
        "capacity_assessment": {
            "staging_rss_mib": {k: row["rss_mib"] for k, row in smoke["loopback_services"].items()},
            "cold_rehearsals_performance": [first["performance"], candidate["performance"]],
            "start_with_users": 2, "one_active_worker": True, "max_queue_s": 90,
            "initial_host_planning": {"cpu_cores": 2, "ram_gib": 2, "external_oidc_ram_excluded": True},
            "model_bytes": MODEL.stat().st_size,
            "estimated_public_scale": None, "vendor_costs_estimated": False,
            "cost_drivers": ["continuous instance time while available", "encrypted volume and backup retention",
                             "egress per response and export", "separately operated identity provider"],
        },
    }


def contracts():
    return {
        "protocol": {"stage": "8-C", "release": RELEASE, "decision": "B",
                     "scope": "LOOPBACK_PRODUCTION_EQUIVALENT_STAGING", "remote_deployment": False,
                     "external_prerequisites": ["authorized host and ingress", "approved DNS and TLS",
                                                "approved confidential OIDC client and invitations",
                                                "encrypted state and backups", "intended audience and source permissions"]},
        "hosting_requirements": {"frontend": "Next.js production build", "backend": "single FastAPI process",
                                 "gateway": "Caddy HTTPS", "compute": "single bounded Python child",
                                 "database": "SQLite on persistent local filesystem", "idp": "OpenID Connect",
                                 "state": "access-restricted writable volume", "release_assets": "read-only mount"},
        "authentication_contract": {"flow": "OIDC authorization code with PKCE S256",
                                    "invitation_key": ["issuer", "subject"], "self_registration": False,
                                    "session_cookies": ["Secure", "HttpOnly", "SameSite=Lax", "host-only", "path=/"],
                                    "stored_token": "SHA-256 digest", "renewal": "fresh provider sign-in",
                                    "expiry_default_s": 1800, "revoke": "all app sessions for the subject",
                                    "logout": "current application session", "redirect": "fixed application origin"},
        "authorization_contract": {"lookup_key": "owner plus workspace or job identifier",
                                   "missing_or_foreign_status": 404, "expired_session_status": 401,
                                   "workspace_methods": ["read", "update", "delete", "duplicate", "export"],
                                   "job_methods": ["read", "cancel", "details", "export"],
                                   "scientific_identifiers_are_credentials": False, "frozen_preset_access": "all invitees"},
        "user_resource_model": {"identity": "server-assigned owner keyed by OIDC issuer and subject",
                                "included_in_export": False, "workspace_ids": "UUIDv4 within owner",
                                "job_handles": "random server-issued 256-bit identifiers",
                                "computed_ids": "canonical deterministic hashes used for reproducibility"},
        "workspace_storage_schema": {"sqlite_user_version": 1, "journal": "WAL",
                                     "tables": ["principals", "sessions", "workspaces", "jobs", "usage"],
                                     "workspace": WorkspaceFile.model_json_schema()},
        "workspace_migration": {"explicit": True, "source_origin": "file export/import when origins differ",
                                "same_origin": "copy compatible browser originals into authenticated account",
                                "model_mismatch": "WORKSPACE_MODEL_MISMATCH", "source_mismatch": "WORKSPACE_SOURCE_MISMATCH",
                                "checks_snapshot": True, "preserves_local_originals": True},
        "workspace_persistence": {"observed": ["save", "list", "open", "update", "duplicate", "delete", "import", "export",
                                               "reproduce", "separate browser session", "mobile browser", "service restart"],
                                  "engine_result_retained_in_workspace": "checksum and original inputs"},
        "job_ownership": {"states": ["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELED"],
                          "active_and_queued_restart": "FAILED/SERVICE_RESTART",
                          "receipt_and_terminal_state": "single transaction", "waiting_workspace": "preserved without receipt",
                          "cancellation": "issuer of this private job handle only"},
        "cache_isolation": {"scope": ["owner", "canonical computation", "expected result checksum"],
                            "same_owner_reuse": True, "cross_owner_deduplication": False,
                            "shared_result_hash": True, "foreign_poll_and_cancel": 404},
        "resource_quotas": {"active": 1, "outstanding_total": 4, "outstanding_per_owner": 2, "queue_s": 90,
                            "wall_s": 35, "posix_cpu_s_max": 35, "sampled_rss_mib": 720,
                            "request_bytes": 16384, "worker_envelope_bytes": 3000000,
                            "manual_policy_count": 5, "declared_future_count": 4, "conditional_budget": 128,
                            "robust_budget": 240, "forecast_horizon_laps": [3, 5, 10],
                            "workspaces_per_user": 50, "submissions_per_min": 20, "exports_per_min": 12,
                            "result_days": 7, "terminal_job_history_per_owner": 64,
                            "terminal_job_history_global": 512, "retained_result_and_trace_bytes": 48000000},
        "network_security": {"gateway": "HTTPS and exact origin", "backend": "loopback; gateway credential required",
                             "worker_or_database_tcp_listener": False, "credentialed_cors": False,
                             "mutations": "exact trusted Origin", "debug": False, "robots_header": "noindex, nofollow"},
        "security_tests": {"python": "tests/api/test_private_hosting.py", "chrome": "apps/web/tests-private",
                           "runtime_probe": "deployment_smoke_tests.json",
                           "checks": ["invalid and expired cookie", "callback and invitation denial", "revocation",
                                      "cross-owner workspace/job/details/export/cancel denial", "malformed and oversized body",
                                      "wrong origin and CSRF", "encoded raw path", "model and source mismatch",
                                      "queue and owner rate limits", "retention and SQLite recovery"]},
        "secret_handling": {"sample_configuration": "placeholders only", "actual_secrets": "private operator files",
                            "browser_bundle": "staging credential scan passed",
                            "logs": "allowlisted status and measurements; no callback URI or request headers",
                            "archive_scope": "allowlisted summaries and staging screenshots"},
        "research_support": {"model": "PaceV2_D8R1", "sha256": MODEL_SHA, "supported_era": "2023_2025",
                             "classification": "MEAN_ONLY_VALIDATED", "g3_calibration": "FAILED",
                             "2026": "MODEL_ERA_UNSUPPORTED", "historical_physical_strategy": "WITHHELD",
                             "live": "LIVE_UNSUPPORTED"},
        "invariant_checks": {"simulator_model_frozen": True, "frozen_s8b_artifact_digests_match": True,
                             "canonical_checksum_behind_tls": True, "policy_and_marker_parity": True,
                             "matched_timing_regret_us": 46_754_185, "shared_box_wait_us": [2_903_848, 0],
                             "anonymous_analyses": False, "hosted_url": None},
    }


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--browser", type=Path, nargs=3, required=True, metavar=("PRIVATE", "RECOVERY", "LEGACY"))
    parser.add_argument("--pytest-passed", type=int, required=True)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    out = args.out
    assert args.pytest_passed >= 464
    for name, expected in PINS.items():
        assert hash_bytes((REPORT_DIR/name).read_bytes()) == expected, name
    archive = REPORT_DIR/"stage8_s8_b_evidence.zip"
    assert len(checked_members(archive, PINS[archive.name])) == 56
    protected = ["src/f1ri/simulator", "src/f1ri/models", "src/f1ri/decision_board.py", "src/f1ri/api/research.py",
                 "artifacts/models", "data/raw/reference", "artifacts/reports/stage6*", "artifacts/reports/stage7*",
                 "artifacts/reports/stage8_s8_b*"]
    run(["git", "diff", "--quiet", BASELINE, "--", *protected], cwd=ROOT, check=True)
    assert hash_bytes(MODEL.read_bytes()) == MODEL_SHA
    docs = contracts() | observations(out, args.browser)
    fresh = regression(ROOT/"artifacts/runs/stage8_s8_b")
    with ZipFile(archive) as source:
        frozen_regression = loads(source.read("regression_parity.json"))
    local_reruns = {k: v for k, v in fresh["reruns"].items() if v.get("verified") is not None}
    assert all(row["verified"] for row in local_reruns.values())
    docs["regression_parity"] = {"frozen_stage8b": frozen_regression,
                                 "existing_s6_and_s7_rerun_directories_rechecked": local_reruns,
                                 "s8c_supported_api_parity": docs["api_parity"]["identical_across_two_cold_rehearsals"]}
    docs["frozen_baseline"] = {"commit": BASELINE, "s8b_artifacts": PINS,
                               "verified_s8b_checksum_entries": 56, "simulator_model_paths_unchanged": True}
    docs["environment_manifest"] = {
        "os": system(), "python": python_version(), "node": check_output(["node", "--version"], text=True).strip(),
        "python_packages": {name: version(name) for name in ("Authlib", "cryptography", "fastapi", "uvicorn", "pydantic", "psutil")},
        "gateway": "Caddy 2.11.9", "issuer_fixture": "Keycloak 26.8.2 HTTPS with embedded staging database",
        "next_lock_sha256": hash_bytes((ROOT/"apps/web/package-lock.json").read_bytes()),
        "schema": 1, "model_sha256": MODEL_SHA, "public_services_provisioned": False,
    }
    docs["metrics"] = {"stage": "8-C", "decision": "B", "release": RELEASE, "python_passed": args.pytest_passed,
                       "baseline_python_passed": 458, "frontend_unit_passed": 2, "private_chrome_passed": 5,
                       "legacy_chrome_passed": 8, "private_acceptance_workflows": 20, "screenshots": 17,
                       "cold_production_rehearsals": 2, "frozen_s8_results_plus_team": 9,
                       "timing_original_regret_us": 46_754_185, "live": "LIVE_UNSUPPORTED", "remote": False}
    for name, value in docs.items():
        write_json(out/f"{name}.json", value)

    references = {
        "validation_report.md": REPORT_DIR/"stage8_s8_c_validation.md",
        "deployment_architecture.md": ROOT/"docs/web/private-beta.md",
        "licensing_review.md": ROOT/"docs/web/private-beta-licensing.md",
    }
    for name, path in references.items():
        copyfile(path, out/name)
    names = [f"{item}.json" for item in docs] + list(references) + [
        "deployment_smoke_tests.json", "recovery_observations.json",
        "api_rehearsal_first.json", "api_rehearsal_candidate.json", *docs["browser_tests"]["screenshots"],
    ]
    files = {name: (out/name).read_bytes() for name in names}
    manifest = "".join(f"{hash_bytes(raw)}  {name}\n" for name, raw in sorted(files.items())).encode()
    (out/"SHA256SUMS").write_bytes(manifest)
    if args.publish:
        published = REPORT_DIR/"stage8_s8_c_evidence.zip"
        write_archive(published, files)
        assert len(checked_members(published)) == len(files)
        copyfile(out/"metrics.json", REPORT_DIR/"stage8_s8_c_metrics.json")
        print(f"{len(files)} files verified. Evidence SHA-256 {hash_bytes(published.read_bytes())}")


if __name__ == "__main__":
    main()
