"""Bounded read-only study and export verification for S7-C0."""

from collections import Counter
from dataclasses import fields
from json import loads
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from f1ri.features.shadow_snapshot import (
    ShadowConfiguration,
    ShadowSnapshot,
    check_seal,
    make_snapshot,
    seal_decision,
)
from f1ri.features.temporal_maturity import FieldValidity
from f1ri.ingestion.source_capture import load_capture_files
from f1ri.ingestion.source_receipt import canonical, digest, utc
from f1ri.ingestion.source_replay import ReplayIndex
from f1ri.validation.historical_field_sources import checked_members, load_pilots, write_archive
from f1ri.validation.historical_field_study import _parquet, _seal, write_json
from f1ri.validation.shadow_controls import control_experiments, shadow_cases
from f1ri.validation.shadow_polling_study import compare, feasibility, measured
from f1ri.validation.shadow_temporal_audit import d8r1_cutoff_audit, matrix, maturity_experiments
from f1ri.validation.source_availability_study import CAPTURE, CAPTURE_SHA, _frozen, open_source

REPORTS = Path("artifacts/reports")
PLAN = Path("config/experiments/stage7_s7_c0.json")
REHEARSAL = REPORTS/"stage7_s7_c0_rehearsal.zip"
REHEARSAL_SHA = "d5d58a7991b5ba5444caf8a68304263361e2b92513b14e5e4ed169c2e943a44c"
CLOCK = REPORTS/"stage7_s7_c0_clock.json"
CLOCK_SHA = "74b62b4dfc9c981a6a875c6415b3bb3ca9bced90bf7492b142cf39266717d576"
RELEASE = "SHADOW_PREFLIGHT_ENGINEERING_VALIDATED_EMPIRICAL_LIMITATIONS"
PINNED = {
    REPORTS/"stage7_s7_b_evidence.zip": "a9e8f4ee0de1df0de359eb6b2f6f5e67ff768b9d7d74c17b72db36a403cee799",
    CAPTURE: CAPTURE_SHA,
    REPORTS/"stage7_s7_b_metrics.json": "ba36c2bab5804aa479a54e723e0f641e532d436bbe1f073f61893ea705eba767",
    REPORTS/"stage7_s7_b_dashboard.json": "367a2b3a55d48a1813132245c3112668f542a07792dd0ae7f6dde0448c452413",
    REHEARSAL: REHEARSAL_SHA, CLOCK: CLOCK_SHA,
}


def _baseline(root, previous):
    for path, expected in PINNED.items():
        if digest((root/path).read_bytes()) != expected:
            raise ValueError(f"Pinned preflight input differs: {path.name}")
    path = root/REPORTS/"stage7_s7_b_evidence.zip"
    checked_members(path, PINNED[REPORTS/"stage7_s7_b_evidence.zip"])
    with ZipFile(path) as z:
        baseline = loads(z.read("release_verification.json"))
    frozen = _frozen(root, {})
    checks = baseline["deterministic_replay_files"]
    if previous and any(digest((previous/n).read_bytes()) != expected for n, expected in checks.items()):
        raise ValueError("Independent S7-B replay diverged.")
    return {"s7b_pins": {p.as_posix(): h for p, h in PINNED.items() if "s7_b_" in p.name},
            "s7b_expected_outputs": len(checks)-1, "s7b_deterministic_replay_files": checks,
            "earlier_release": frozen, "prior_sealed_parity": baseline["frozen_s6_c"]
            +baseline["frozen_s7_a"]+baseline["frozen_s7_a1"]}


def _capture(root, plan):
    names = checked_members(root/REHEARSAL, REHEARSAL_SHA)
    with ZipFile(root/REHEARSAL) as source:
        files = {n: source.read(n) for n in names}
    m, receipts, incomplete = load_capture_files(files)
    if m["plan"] != plan:
        raise ValueError("Rehearsal differs from the frozen scheduler plan.")
    choices = [loads(raw) for name, raw in sorted(files.items()) if name.endswith(".scheduler.json")]
    if [e.request_id for e, _ in receipts] != [v["request_id"] for v in choices]:
        raise ValueError("Scheduler diagnostics do not match the committed request journal.")
    return m, receipts, incomplete, choices


def _canonical_file_hashes(directory):
    return {p.relative_to(directory).as_posix(): digest(p.read_bytes()) for p in sorted(directory.rglob("*"))
            if p.is_file() and p.name not in ("SHA256SUMS", "shadow_replay.zip")}


def verify_export(root, path):
    members = checked_members(path)
    if set(members) != {"manifest.json"}:
        raise ValueError("Unsupported shadow replay archive.")
    with ZipFile(path) as z:
        spec = loads(z.read("manifest.json"))
    if spec["schema"] != "s7-c0-shadow-replay-v1" or spec["input_sha256"] != REHEARSAL_SHA:
        raise ValueError("Shadow export source binding differs.")
    with TemporaryDirectory() as directory:
        run(root, None, Path(directory)/"replay", export=False)
        actual = _canonical_file_hashes(Path(directory)/"replay")
    if actual != spec["outputs"]:
        differing = sorted(k for k in actual.keys() | spec["outputs"].keys() if actual.get(k) != spec["outputs"].get(k))
        raise ValueError(f"Shadow export replay bytes differ at {differing}")
    return {"reproduced": True, "files": len(actual), "export_sha256": digest(path.read_bytes())}


def run(root, previous, out, *, export=True):
    frozen = _baseline(root, previous)
    protocol = loads((root/PLAN).read_bytes())
    policy = loads((root/"config/experiments/stage7_s7_b.json").read_bytes())["freshness_policy"]
    _, old, _ = open_source(root)
    polling = measured(old, protocol["freshness_limits_s"])
    outcomes = compare(protocol)
    estimated = feasibility(protocol)
    manifest, receipts, incomplete, choices = _capture(root, protocol["rehearsal"])
    capture_stats = measured(receipts, protocol["freshness_limits_s"])
    clock = loads((root/CLOCK).read_bytes())
    if not all((polling["request_rate_floor_respected"], capture_stats["request_rate_floor_respected"],
                clock["synchronization"] == "UNSYNCHRONIZED")):
        raise ValueError("Preflight timing evidence fails a protocol gate.")
    original, mutant, next_line, audit = d8r1_cutoff_audit(root)
    maturity = maturity_experiments(original, mutant, next_line, audit)
    sessions, _ = load_pilots(root)
    controlled, controls = shadow_cases(sessions[9987], policy)
    delay = control_experiments(policy)
    temporal_tests = maturity["controls"] | controls["temporal_receipt_controls"]

    plan = manifest["plan"]
    roster = sessions[plan["session"]].roster
    index = ReplayIndex(manifest["capture_id"], plan["session"], plan["endpoints"], receipts, mode=plan["mode"],
                        roster=roster)
    stops = sorted({e.available_utc for e, _ in receipts})
    snapshots, sealed_actual = [], []
    event = utc(protocol["maturity"]["cutoff"])
    for stop in stops:
        raw = index.replay(stop, receipt_cutoff=stop, event_cutoff=event, policy=policy)
        independent = ReplayIndex(manifest["capture_id"], plan["session"], plan["endpoints"],
                                  [(e, body) for e, body in receipts if e.available_utc <= stop],
                                  mode=plan["mode"], roster=roster)
        if independent.replay(stop, receipt_cutoff=stop, event_cutoff=event, policy=policy) != raw:
            raise ValueError("A future capture response changed earlier replay bytes.")
        state = loads(raw)
        snapshot = make_snapshot(state, ShadowConfiguration(2025), observed_geometry=sessions[9896])
        sealed_actual.append(seal_decision(snapshot))
        snapshots.append({"clock": snapshot.receipt_cutoff, "source_event_cutoff": snapshot.event_cutoff,
                          "earned_versions": len(snapshot.versions), "maturity": snapshot.maturity,
                          "withheld": snapshot.withholding_reasons, "endpoints": snapshot.endpoint_support,
                          "seal_sha256": sealed_actual[-1]["sha256"]})
    decisions = {f"rehearsal_{i+1:02d}": item for i, item in enumerate(sealed_actual)} | controlled
    capture_type = "ARCHIVE_REPLAY"
    known_stage = {"release": RELEASE, "empirical_racing": "INCOMPLETE", "LIVE": "LIVE_UNSUPPORTED",
                   "location_freshness_feasible": False, "clock_synchronized": False,
                   "ready_for_live_strategy": False, "regulation_2026_supported": False,
                   "next_stage": "Bounded receipt capture with a verified clock and transport appropriate for location.",
                   "recommendations": "WITHHELD"}
    ledger_rows = [{"case": name, "sha256": doc["sha256"],
                    **{k: doc["snapshot"][k] for k in ("capture_id", "session", "decision_utc", "event_cutoff",
                                                      "receipt_cutoff", "capture_mode", "status")},
                    "withholding_count": len(doc["snapshot"]["withholding_reasons"]),
                    "supported_route_positions": doc["snapshot"]["physical"]["localized"]}
                   for name, doc in decisions.items()]
    freshness_rows, coverage_rows = [], []
    for trace in outcomes:
        for cp in trace["checkpoints"]:
            for ep, frame in cp["feeds"].items():
                freshness_rows.append({"method": trace["method"], "experiment": trace["experiment"],
                                       "endpoint": ep, **frame})
                coverage_rows.append({"method": trace["method"], "experiment": trace["experiment"],
                                      "virtual_s": cp["at"], "endpoint": ep, "missing": frame["missing_driver_count"],
                                      "fresh": frame["fresh"]})
    for snapshot in snapshots:
        for ep, frame in snapshot["endpoints"].items():
            freshness_rows.append({"method": "S4", "experiment": capture_type, "endpoint": ep,
                                   "utc": snapshot["clock"], "response_age_s": frame["response_age_s"]})
            coverage_rows.append({"method": "S4", "experiment": capture_type, "endpoint": ep,
                                  "utc": snapshot["clock"], "missing": len(frame["missing_or_unverified_drivers"]),
                                  "fresh": None})
    metrics = {"release": RELEASE, "decision": "B", "observed_attempts": len(receipts)+len(incomplete),
               "observed_successful": sum(not e.error for e, _ in receipts), "nominal_and_fault_traces": len(outcomes),
               "temporal_controls": len(temporal_tests), "shadow_cases": len(controlled), "sealed": len(decisions),
               "withheld": len(decisions), "contemporary_racing_windows": 0, "live": "LIVE_UNSUPPORTED"}
    dashboard = {"release": RELEASE, "mode": capture_type, "capture_id": manifest["capture_id"],
                 "session": manifest["plan"]["session"], "feasibility": estimated, "baseline": polling,
                 "capture": capture_stats, "clock": clock, "checkpoints": snapshots, "temporal": maturity,
                 "synthetic": [{"Policy": r["method"], "Scenario": r["experiment"],
                                "Location fresh checkpoints (%)": round(100*r["freshness"]["location"]["duty_cycle"], 1),
                                "Withheld checkpoints": r["withheld_checkpoints"]} for r in outcomes],
                 "delay": delay, "readiness": known_stage, "controls": [
                     {"case": row["case"], "sha256": row["sha256"], "status": row["status"],
                      "reasons": decisions[row["case"]]["snapshot"]["withholding_reasons"]}
                     for row in ledger_rows if not row["case"].startswith("rehearsal_")],
                 "later_evaluation": controls["later_evaluation"]}
    documents = {
        "protocol": protocol, "frozen_baseline": frozen,
        "source_inventory": matrix()["fields"],
        "scheduler_contract": {"method": "S4", "global_spacing": 2.1, "global_429": True,
                               "scoped_retry_cooldown": True, "single_inflight": True, "budget_per_endpoint": True,
                               "soft_starvation_guard": "Overdue eligible endpoint precedes weighted/dependency work. "
                                                       "Rate limits, retries and capped budgets dominate."},
        "scheduler_feasibility": estimated, "endpoint_priority_plan": plan["scheduler"],
        "polling_baseline": polling, "polling_comparison": outcomes, "clock_quality": clock,
        "temporal_maturity_schema": [f.name for f in fields(FieldValidity)], "field_availability_matrix": matrix(),
        "lap_leakage_audit": maturity, "d8r1_cutoff_audit": audit,
        "shadow_snapshot_schema": [f.name for f in fields(ShadowSnapshot)],
        "decision_support": {"readiness": known_stage, "cases": ledger_rows,
                             "withholding_reason_counts": dict(Counter(r for item in decisions.values()
                                                                      for r in item["snapshot"]["withholding_reasons"]))},
        "shadow_decisions": decisions,
        "sealed_decision_manifest": {name: doc["sha256"] for name, doc in decisions.items()},
        "race_control_delay": delay, "fault_tests": {"temporal": temporal_tests, "shadow": controls},
        "capture_quality": {"mode": capture_type, "manifest": manifest, "raw_sha256": REHEARSAL_SHA,
                            "scheduler": choices, "capture_metrics": capture_stats, "unordered_attempts": index.unordered,
                            "revisions": dict(Counter(a["kind"] for a in index.audit)),
                            "observation_versions": len(index.versions), "provider_publication": None,
                            "racing_event_age": None},
        "dashboard_integration": dashboard, "live_readiness": known_stage,
        "regression_parity": frozen,
        "invariant_checks": {"all_temporal_checks_passed": all(temporal_tests.values()), "receipt_prefix_stable": True,
                             "all_actions_withheld": all(v["snapshot"]["action"] is None for v in decisions.values()),
                             "all_seals_verify": all(check_seal(canonical(s)) == s["sha256"] for s in decisions.values()),
                             "fixed_race_control_physics": True, "empirical_racing_capture_claimed": False},
        "metrics": metrics, "later_evaluation": controls["later_evaluation"],
    }
    if not all(val for key, val in documents["invariant_checks"].items() if key != "empirical_racing_capture_claimed"):
        raise ValueError("Shadow preflight did not satisfy static evidence gates.")

    out.mkdir(parents=True, exist_ok=False)
    diag = out/"diagnostics"
    diag.mkdir()
    for name, value in documents.items():
        write_json(out/f"{name}.json", value)
    _parquet(out/"shadow_snapshot_ledger.parquet", ledger_rows)
    _parquet(out/"source_freshness.parquet", freshness_rows)
    _parquet(out/"source_coverage.parquet", coverage_rows)
    _parquet(diag/"request_response_ledger.parquet", [
        {"request": e.request_id, "sequence": e.sequence, "endpoint": e.endpoint,
         "request_utc": e.request_utc, "receipt_utc": e.completed_utc, "available_utc": e.available_utc,
         "clock_quality": e.clock_quality, "body_sha256": e.body_sha256, "seconds": e.duration_s,
         "http_status": e.http_status, "error": e.error} for e, _ in receipts])
    _parquet(diag/"observation_versions.parquet", [
        {key: row[key] for key in ("key", "endpoint", "version", "body_sha256", "source_event_utc",
                                  "first_seen_utc", "processed_utc", "usable_utc")}
        for row in index.versions.values()])
    from f1ri.validation.shadow_preflight_plots import render

    render(diag, documents)
    if export:
        spec = {"schema": "s7-c0-shadow-replay-v1", "input_sha256": REHEARSAL_SHA,
                "capture_id": manifest["capture_id"], "outputs": _canonical_file_hashes(out)}
        write_archive(diag/"shadow_replay.zip", {"manifest.json": canonical(spec)})
    _seal(out)
    return metrics
