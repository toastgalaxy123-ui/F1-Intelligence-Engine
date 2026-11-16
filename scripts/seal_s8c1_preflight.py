"""Seal S8-C.1 local preflight evidence while external authorization is pending."""

from argparse import ArgumentParser
from importlib.metadata import version
from json import loads
from pathlib import Path
from platform import python_version, release, system
from re import findall
from sqlite3 import sqlite_version
from subprocess import check_output, run
from xml.etree.ElementTree import parse
from zipfile import ZipFile

from seal_s8c import chrome_report, read
from verify_s8a import REPORT_DIR, ROOT

from f1ri.api.contracts import MODEL_SHA
from f1ri.api.research import PINS as RESEARCH_PINS
from f1ri.validation.historical_field_sources import checked_members, hash_bytes, write_archive
from f1ri.validation.historical_field_study import write_json

BASE = "8080c1fc8d9163230283cfed4e9739b77db948ef"
STEM = "stage8_s8_c1"
MODEL = "artifacts/models/PaceV2_D8R1/pace_v2_d8r1.pkl"
LOCK = "apps/web/package-lock.json"
LOCAL = "WINDOWS_LOOPBACK_HTTPS"
RELEASE = "PRIVATE_BETA_DEPLOYMENT_READY"
PENDING = "PENDING_AUTHORIZED_REMOTE_HOST"
FROZEN = {
    "stage8_s8_c_evidence.zip": "3e0d75ba1d672cf9043736a9a6bad20295fe8bf398cc093edb079934281e177d",
    "stage8_s8_c_metrics.json": "bc4dd0b90cf35a0af06603f339a02077f31de06bb56782c001c0aa5f4d99065f",
    "stage8_s8_c_validation.md": "b4d20c114331d7ad08a7dcade4dd0b778ac32da160c5525a0ea79a53ef932439",
}
REMOTE = (
    "HTTPS from another device", "anonymous sign-in boundary", "invited account sign-in",
    "uninvited account denied", "authenticated overview", "D8R1 forecast",
    "declared strategy", "robust analysis", "frozen Safety Car timing", "shared-box comparison",
    "save workspace", "second physical device replay", "cross-user workspace denial",
    "foreign-job cancellation denial", "logout invalidation", "session expiry",
    "restart persistence", "worker timeout", "historical physical withholding", "live and 2026 gates",
)


def digest(path):
    return hash_bytes(path.read_bytes())


def frozen_artifacts():
    for name, checksum in FROZEN.items():
        assert digest(REPORT_DIR/name) == checksum, name
    assert len(checked_members(REPORT_DIR/"stage8_s8_c_evidence.zip", FROZEN["stage8_s8_c_evidence.zip"])) == 49
    protected = ["src/f1ri", "deploy", "artifacts/models", "data/raw/reference", LOCK,
                 *(f"artifacts/reports/{name}" for name in FROZEN)]
    run(["git", "diff", "--quiet", BASE, "--", *protected], cwd=ROOT, check=True)
    assert digest(ROOT/MODEL) == MODEL_SHA
    for name, checksum in RESEARCH_PINS.items():
        assert digest(REPORT_DIR/name) == checksum, name


def pytest_total(path):
    report = parse(path).getroot()
    suites = [report] if report.tag == "testsuite" else report.findall("testsuite")
    assert suites
    assert not any(int(suite.get(key, "0")) for suite in suites for key in ("errors", "failures", "skipped"))
    total = sum(int(suite.get("tests")) for suite in suites)
    assert total == 466
    return total


def local_checks(out, hosting, recovery):
    runs = [read(out/f"api_rehearsal_{suffix}.json") for suffix in ("first", "candidate")]
    with ZipFile(REPORT_DIR/"stage8_s8_c_evidence.zip") as z:
        reference = loads(z.read("api_parity.json"))
    for row in runs:
        assert row["exact_frozen_results"]
        assert row["comparisons"] == reference["supported"]
        assert row["team"] == reference["paired_team"]
        assert row["timing_regret_us"] == reference["regret_us"] == 46_754_185
    assert [len(hosting), len(recovery)] == [4, 1]
    assert {int(n) for row in hosting+recovery for n in findall(r"\bH(\d+)\b", row["title"])} == set(range(1, 21))
    shots = sorted((out/"screenshots").glob("*.png"))
    assert [int(p.name[:2]) for p in shots] == list(range(1, 18))
    tls = read(out/"local_tls_network.json")
    assert tls["strict_certificate_validation"] and tls["expected_security_headers"]
    assert tls["checks"]["anonymous_api"] == 401 and tls["checks"]["anonymous_page"] == 303
    assert tls["checks"]["origin_boundary"] == tls["checks"]["direct_api"] == tls["checks"]["direct_ui"] == 403
    assert not tls["staging_secrets_in_bundles"]
    db = read(out/"local_operator_status.json")
    assert db["schema"] == 1 and db["model_sha256"] == MODEL_SHA
    restart = read(out/"recovery_observations.json")
    assert restart["active_and_queued"] == "FAILED/SERVICE_RESTART" and restart["incomplete_result_absent"]
    assert restart["backup_integrity"] and restart["isolated_restore_integrity"]
    assert restart["reopened_after_restart_restore_and_rollback"] and restart["restored_sessions_invalidated"]
    assert restart["previous_build_sha256"] != restart["candidate_build_sha256"]
    return runs, tls, db, restart, shots


def record(tls, db, restart, runs):
    def pending(**details):
        return {"remote_status": PENDING, **details}

    return {
        "protocol": {"stage": "S8-C.1", "scope": LOCAL, "source_release": BASE, "decision": "B",
                     "external_acceptance_required_for_path_a": True},
        "authorization_record": {
            "decision": "LOCAL_PREFLIGHT_ONLY", "approved_remote_destination_found": False,
            "external_actions_performed": [],
            "pending_approvals": ["destination and operations access", "ingress and DNS/TLS",
                                  "resource and spending limits", "production OIDC and invitees",
                                  "initial audience and source permissions", "encrypted state and backup policy"],
        },
        "hosting_manifest": pending(topology="Caddy HTTPS, Next.js, loopback FastAPI, single worker and SQLite",
                                    app_planning={"cpu_cores": 2, "ram_gib": 2, "initial_invitees": 2,
                                                  "identity_provider_separate": True}),
        "environment_validation": pending(local={
            "os": system(), "os_version": release(), "python": python_version(), "sqlite": sqlite_version,
            "pandas": version("pandas"), "node": check_output(["node", "--version"], text=True).strip(),
            "packages": {name: version(name) for name in ("fastapi", "uvicorn", "Authlib", "cryptography", "psutil")},
            "linux_systemd_executed": False, "local_sqlite_vendor_backport_verified": False,
        }, target_sqlite_gate="SQLite WAL-reset fix in 3.51.3+ or documented backport"),
        "identity_provider_validation": pending(local="Keycloak 26.8.2 fixture with two invitees and one denied identity",
                                                 production_enrollment_verified=False),
        "tls_validation": pending(local_ca_hostname_chain_verified=tls["strict_certificate_validation"],
                                  production_chain_verified=False, public_dns_verified=False),
        "network_security": pending(local_services=tls["loopback_services"], local_checks=tls["checks"],
                                    response_security_headers=tls["expected_security_headers"],
                                    staging_credentials_in_compiled_bundles=tls["staging_secrets_in_bundles"]),
        "authentication_tests": pending(local=["invitee", "uninvited", "expiry", "revocation", "logout",
                                               "invalid callback", "overlapping session poll during logout"]),
        "authorization_tests": pending(local=["anonymous protected routes", "foreign workspace and export denial",
                                              "foreign job polling and cancellation denial", "owner-only cache"]),
        "workspace_cross_device": pending(local="Two authenticated Chrome contexts, one mobile-sized, exact replay",
                                          physical_external_device_verified=False),
        "job_lifecycle": pending(local_restart=restart["active_and_queued"],
                                 local_interrupted_result_absent=restart["incomplete_result_absent"],
                                 local_queued_and_active_cancellation=True, local_cache_reuse=True,
                                 baseline_worker_deadline_seconds=35, baseline_sampled_rss_mib=720,
                                 baseline_max_pending_global=4, baseline_max_pending_owner=2),
        "resource_metrics": pending(local_process_rss_mib={k: v["rss_mib"] for k, v in tls["loopback_services"].items()},
                                    local_cold_runs=[row["performance"] for row in runs],
                                    local_operator_database_bytes=db["storage_bytes"]),
        "api_parity": pending(local_matches_s8c=True, cold_runs=2, frozen=read_only_parity(runs[0])),
        "scientific_regression_parity": {
            "scope": LOCAL, "frozen_model_sha256": MODEL_SHA, "all_frozen_research_files_match": True,
            "model": "MEAN_ONLY_VALIDATED", "era": "2023_2025", "model_2026": "MODEL_ERA_UNSUPPORTED",
            "historical_physical": "WITHHELD", "live": "LIVE_UNSUPPORTED",
            "timing_regret_us": 46_754_185, "shared_box_wait_us": [2_903_848, 0],
        },
        "backup_restore": pending(local_verified=all(restart[k] for k in (
            "backup_integrity", "isolated_restore_integrity", "restored_sessions_invalidated")),
                                   local_receipt_sha256=restart["verified_result_sha256"],
                                   remote_backup_schedule_approved=False),
        "rollback_validation": pending(local=restart),
        "licensing_gate": pending(review="docs/web/private-beta-licensing.md",
                                  authorized_initial_audience_confirmed=False, redistribution_approved=False),
        "monitoring_validation": pending(local_ready_endpoint=True, local_aggregate_operator_status=db,
                                         external_health_notifier_configured=False),
        "deployment_status": {"release": RELEASE, "decision": "B", "scope": LOCAL,
                              "remote_deployed": False, "remote_origin": None, "remote_cases_executed": 0},
    }


def read_only_parity(row):
    return {key: row[key] for key in ("comparisons", "team", "timing_regret_us")}


def main():
    args = ArgumentParser(description=__doc__)
    args.add_argument("--out", type=Path, required=True)
    args.add_argument("--pytest-junit", type=Path, required=True)
    args.add_argument("--browser", type=Path, nargs=2, required=True, metavar=("HOSTING", "RECOVERY"))
    args.add_argument("--legacy-browser", type=Path, required=True)
    args.add_argument("--publish", action="store_true")
    opts = args.parse_args()
    frozen_artifacts()
    total = pytest_total(opts.pytest_junit)
    hosting, recovery = map(chrome_report, opts.browser)
    legacy = chrome_report(opts.legacy_browser)
    assert len(legacy) == 8
    runs, tls, db, restart, shots = local_checks(opts.out, hosting, recovery)
    docs = record(tls, db, restart, runs)
    docs["remote_browser_tests"] = {
        "remote_cases": [{"id": i, "check": name, "status": PENDING} for i, name in enumerate(REMOTE, 1)],
        "remote_cases_executed": 0, "remote_screenshots": 0, "local_private_chrome": hosting+recovery,
        "local_legacy_chrome": legacy,
    }
    docs["model_and_artifact_hashes"] = {
        "base": BASE, "s8c_release": FROZEN, "s8c_checksum_entries": 49, "model_path": MODEL,
        "model_sha256": MODEL_SHA, "pinned_research_archives": RESEARCH_PINS,
    }
    docs["release_manifest"] = {
        "release": RELEASE, "base": BASE, "frontend_lock_sha256": digest(ROOT/LOCK), "schema": 1,
        "local_production_build_sha256": [restart["previous_build_sha256"], restart["candidate_build_sha256"]],
        "session_ui_sha256": digest(ROOT/"apps/web/components/private-session.tsx"),
        "api_client_sha256": digest(ROOT/"apps/web/lib/api.ts"),
        "approved_host_production_build": None,
    }
    docs["metrics"] = {
        "release": RELEASE, "stage": "S8-C.1", "decision": "B", "local_python_passed": total,
        "local_private_chrome_passed": len(hosting+recovery), "local_screenshots": len(shots),
        "local_legacy_chrome_passed": len(legacy), "local_frontend_unit_passed": 2,
        "local_frozen_results_verified": 9, "local_cold_rehearsals": 2, "remote_deployed": False,
        "remote_browser_cases_executed": 0, "live": "LIVE_UNSUPPORTED", "timing_regret_us": 46_754_185,
    }
    for key, value in docs.items():
        write_json(opts.out/f"{key}.json", value)
    report = (REPORT_DIR/f"{STEM}_validation.md").read_bytes()
    (opts.out/"validation_report.md").write_bytes(report)
    files = {f"{name}.json": (opts.out/f"{name}.json").read_bytes() for name in docs}
    files.update({p.relative_to(opts.out).as_posix(): p.read_bytes() for p in shots})
    files["validation_report.md"] = report
    (opts.out/"SHA256SUMS").write_bytes("".join(f"{hash_bytes(raw)}  {name}\n"
                                                for name, raw in sorted(files.items())).encode())
    if opts.publish:
        archive = REPORT_DIR/f"{STEM}_evidence.zip"
        write_archive(archive, files)
        assert len(checked_members(archive)) == len(files)
        (REPORT_DIR/f"{STEM}_metrics.json").write_bytes(files["metrics.json"])
        print(f"{len(files)} verified members. SHA-256 {digest(archive)}")


if __name__ == "__main__":
    main()
