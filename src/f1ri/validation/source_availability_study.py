"""Read-only verification and deterministic S7-B availability study."""

from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from json import loads
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from f1ri.decision_board import replay as replay_s6c
from f1ri.features.historical_field import reconstruct_field
from f1ri.ingestion.source_capture import (
    CapturePlan,
    _manifest,
    _names,
    _write_new,
    capture,
    capture_files,
    load_capture_files,
)
from f1ri.ingestion.source_receipt import (
    EVENT_ONLY,
    EXPERIMENT,
    RETROSPECTIVE,
    SCHEMAS,
    canonical,
    digest,
    utc,
)
from f1ri.ingestion.source_replay import ReplayIndex
from f1ri.validation.historical_field_sources import (
    S6C,
    checked_members,
    load_pilots,
    write_archive,
)
from f1ri.validation.historical_field_study import _parquet, _seal, write_json
from f1ri.validation.pit_branch_sources import S7A_EVIDENCE
from f1ri.validation.source_faults import BASE, response, run_faults

REPORTS = Path("artifacts/reports")
PROTOCOL = Path("config/experiments/stage7_s7_b.json")
CAPTURE = REPORTS/"stage7_s7_b_capture.zip"
DASHBOARD = REPORTS/"stage7_s7_b_dashboard.json"
CAPTURE_SHA = "7fea556d9aff6906879bf872d9ca02ecb0f2673d3dec14da4427809bb3b12218"
RELEASE = "RECEIPT_CAPTURE_ENGINEERING_VALIDATED"
MODEL = "f98e8444a5982dd482a29444ce2c06848a39829cc248cb6cfceb760b59eae16a"
FROZEN = {
    "stage7_s7_a1_evidence.zip": "c688ddf1732fcea5ed31b63cc14ee01640752d9f610e1667a51be8630547a0ee",
    "stage7_s7_a1_inputs.zip": "bbb29533153a7849fb782d039beb55a5a41b380be550dbc16981ed4c9b5eb3ad",
    "stage7_s7_a1_metrics.json": "a28bb2103e8bee7f7001380c99b3eea0689c18b4e1ae17a0f5552210e1893563",
    "stage7_s7_a1_dashboard.json": "0119a3aa044dd606331158a8e8c456c6cf852d66cd178c5c9f73e4ed249f394d",
}


def open_source(root):
    entries = checked_members(root/CAPTURE, CAPTURE_SHA)
    with ZipFile(root/CAPTURE) as archive:
        files = {name: archive.read(name) for name in entries}
    manifest, envelopes, orphaned = load_capture_files(files)
    return manifest, envelopes, orphaned


def _frozen(root, previous):
    if any(digest((root/REPORTS/p).read_bytes()) != expected for p, expected in FROZEN.items()):
        raise ValueError("Frozen S7-A.1 input artifact differs.")
    verified = {}
    for key, file, member, field in (
        ("s6c", S6C, "regression_parity.json", "exact_s6_c_reproduction"),
        ("s7a", S7A_EVIDENCE, "release_verification.json", "deterministic_s7a_files"),
        ("s7a1", REPORTS/"stage7_s7_a1_evidence.zip", "release_verification.json", "deterministic_run_files"),
    ):
        checked_members(root/file)
        with ZipFile(root/file) as z:
            reference = loads(z.read(member))[field]
        folder = previous.get(key)
        if folder and any(digest((folder/p).read_bytes()) != h for p, h in reference.items()):
            raise ValueError(f"Independent frozen run differs: {key}")
        verified[key] = {"outputs": len(reference)-int("SHA256SUMS" in reference),
                         "verified": bool(folder), "manifest": reference if folder else None}
    with ZipFile(root/S6C) as z:
        sealed = replay_s6c(root, z.read("diagnostics/neutralization_replay.zip"))
    if sealed["snapshot"]["model"]["value"]["sha256"] != MODEL:
        raise ValueError("Frozen D8R1 identity differs.")
    return {"pins": FROZEN, "independent_reruns": verified, "d8r1_sha256": MODEL,
            "d8r1_release": "MEAN_ONLY_VALIDATED", "s6c_sealed_export_reproduced": True}


def recovery_probe(policy):
    """Exercise the actual resumed collector with a transport fixture and one orphan."""
    with TemporaryDirectory() as temp:
        directory = Path(temp)/"receipt"
        plan = CapturePlan(9987, ("position",), 20, 2.1, 3, 0)
        manifest = _manifest(directory, plan, False)
        capture_id = manifest["capture_id"]
        raw_row = {"session_key": 9987, "date": BASE.isoformat(), "driver_number": 1, "position": 2}
        first, raw = response(1, "position", 0, .1, [raw_row])
        first = replace(first, capture_id=capture_id, request_id=f"{capture_id}:1", mode=RETROSPECTIVE)
        request_keys = ("provider", "endpoint", "session", "parameters", "capture_id", "sequence", "poll", "retry",
                        "request_id", "clock_epoch", "request_utc", "request_ns")
        intent, receipt = _names(1)
        pending = {key: first.document()[key] for key in request_keys}
        journal = directory/"journal"
        _write_new(journal/intent, canonical(pending))
        _write_new(journal/receipt, canonical(first.document()))
        _write_new(directory/"raw"/f"{first.body_sha256}.bin", raw)
        intent2, _ = _names(2)
        _write_new(journal/intent2, canonical(pending | {"sequence": 2, "poll": 2,
                                                        "request_id": f"{capture_id}:2"}))
        (journal/".interrupted.tmp").write_bytes(b'{"incomplete":')

        class RecoveredTransport:
            def capture_attempt(self, endpoint, params, *, deadline, max_bytes, started):
                now = datetime.now(UTC)
                started(now, 10)
                return {"request_utc": now, "request_ns": 10, "first_byte_utc": now,
                        "completed_utc": now, "completed_ns": 10, "http_status": 200,
                        "headers": {}, "error": None}, canonical([raw_row | {"position": 1}])

        _, before, orphan = load_capture_files(capture_files(directory))
        finish = capture(directory, plan, resume=True, client=RecoveredTransport())
        _, after, orphan_after = load_capture_files(capture_files(directory))
        at = BASE+timedelta(seconds=2)

        def state(rows):
            idx = ReplayIndex(capture_id, 9987, plan.endpoints, rows, mode=RETROSPECTIVE)
            return idx.replay(at, event_cutoff=at, receipt_cutoff=at, policy=policy)

        return {"before": len(before), "after": len(after), "orphaned": len(orphan),
                "orphaned_after": len(orphan_after), "request_budget_respected": finish["attempts"] == 3,
                "raw_commit_prefix_intact": before[0][1] == after[0][1],
                "stable_prefix": state(before) == state(after), "mode": EXPERIMENT}


def strategy_delay():
    """Apply only a policy review gate. Preserve the simulator's physical SC event."""
    from f1ri.simulator.strategy import run_strategy
    from f1ri.validation.strategy_fixtures import strategy_families

    scene = strategy_families()["sc"]
    source = next(p for p in scene.policies if p.strategy_id == "react-sc")
    physical_clock = scene.controls[0].at_us
    rows = []
    for delay in (0, 40, 60):
        raw = {"session_key": 9987, "date": BASE.isoformat(), "category": "SafetyCar",
               "message": "SAFETY CAR DEPLOYED"}
        pair = response(1, "race_control", -1, delay, [raw])
        index = ReplayIndex(pair[0].capture_id, 9987, ("race_control",), (pair,), mode=EXPERIMENT)
        delivered = BASE+timedelta(seconds=delay)
        before = delivered-timedelta(microseconds=1)
        rules = {"response_max_s": 10, "race_control": 30}
        if loads(index.replay(before, event_cutoff=min(before, BASE), receipt_cutoff=before,
                              policy=rules))["observations"]:
            raise ValueError("The SC signal was visible before receipt.")
        arrived = loads(index.replay(delivered, event_cutoff=BASE, receipt_cutoff=delivered,
                                     policy=rules))["observations"][0]
        receipt_clock = physical_clock+round((utc(arrived["usable_utc"])-BASE).total_seconds()*1_000_000)
        policy = replace(source, stops=tuple(replace(stop, review_us=receipt_clock) for stop in source.stops))
        actual = run_strategy(replace(scene, policies=(policy,)), policy)
        actions = [row for row in actual["decisions"] if row["outcome"] == "REQUESTED"]
        if any(row["at_us"] < receipt_clock for row in actions):
            raise ValueError("A reactive policy ran before its declared receipt.")
        rows.append({"delay_s": delay, "physical_effect_us": physical_clock, "source_event_us": physical_clock,
                     "policy_receipt_us": receipt_clock, "policy_decisions": actions,
                     "pit_entries": [{"entry_us": v["entry_us"], "target_lap": v["target_lap"]} for v in actual["pit"]],
                     "simulator_controls": [{"command": c.command, "at_us": c.at_us} for c in scene.controls]})
    if [v["pit_entries"][0]["target_lap"] if v["pit_entries"] else None for v in rows] != [2, 3, None]:
        raise ValueError("The declared SC receipt-delay control did not distinguish pit decisions.")
    return {"mode": EXPERIMENT, "source": "Frozen S5-C synthetic strategy fixture", "runs": rows,
            "provider_latency_estimate": None}


def _summary(snapshot):
    return {"decision_utc": snapshot["decision_utc"], "event_cutoff": snapshot["event_cutoff"],
            "receipt_cutoff": snapshot["receipt_cutoff"], "mode": snapshot["mode"],
            "prefix_sha256": snapshot["prefix_sha256"], "records": len(snapshot["observations"]),
            "endpoints": snapshot["endpoints"],
            "examples": [{key: row[key] for key in ("endpoint", "key", "version", "source_event_utc", "first_seen_utc",
                                                   "usable_utc", "changed_fields", "payload")}
                         for ep in snapshot["endpoints"] for row in sorted(
                             (o for o in snapshot["observations"] if o["endpoint"] == ep),
                             key=lambda o: (o["source_event_utc"] or "", o["usable_utc"]), reverse=True
                         )[:3]]}


def _export(root, out, settings, expected):
    document = {"version": "s7-b-export-v1", "capture_sha256": CAPTURE_SHA, "settings": settings}
    write_archive(out, {"manifest.json": canonical(document), "snapshot.json": expected})
    return verify_export(root, out)


def verify_export(root, archive):
    names = checked_members(archive)
    if set(names) != {"manifest.json", "snapshot.json"}:
        raise ValueError("Unrecognized receipt replay export.")
    with ZipFile(archive) as z:
        spec = loads(z.read("manifest.json"))
        expected = z.read("snapshot.json")
    if spec["version"] != "s7-b-export-v1" or spec["capture_sha256"] != CAPTURE_SHA:
        raise ValueError("Receipt replay export source revision differs.")
    manifest, env, _ = open_source(root)
    settings = spec["settings"]
    idx = ReplayIndex(manifest["capture_id"], manifest["plan"]["session"], manifest["plan"]["endpoints"],
                      env, mode=manifest["plan"]["mode"], roster=settings["roster"])
    actual = idx.replay(settings["decision_utc"], event_cutoff=settings["event_cutoff"],
                        receipt_cutoff=settings["receipt_cutoff"], policy=settings["policy"])
    if actual != expected:
        raise ValueError("Receipt replay export differs after reimport.")
    return {"reproduced": True, "snapshot_sha256": digest(actual), "export_sha256": digest(archive.read_bytes())}


def run(root, out, previous=None):
    frozen = _frozen(root, previous or {})
    protocol = loads((root/PROTOCOL).read_bytes())
    policy = protocol["freshness_policy"]
    manifest, envelopes, orphaned = open_source(root)
    if manifest["plan"] != protocol["capture"]:
        raise ValueError("Capture does not match the frozen request plan.")
    sessions, _ = load_pilots(root)
    roster = sessions[9896].roster
    plan = manifest["plan"]
    idx = ReplayIndex(manifest["capture_id"], plan["session"], plan["endpoints"], envelopes, mode=plan["mode"],
                      roster=roster)
    times = [min(e.request_utc for e, _ in envelopes)] + sorted({e.available_utc for e, _ in envelopes})
    event = utc(protocol["historical_events"]["singapore_cutoff"])
    snapshot_bytes = [idx.replay(t, event_cutoff=event, receipt_cutoff=t, policy=policy) for t in times]
    snapshots = [loads(raw) for raw in snapshot_bytes]
    for t, old in zip(times, snapshot_bytes, strict=True):
        prefix = [(e, raw) for e, raw in envelopes if e.available_utc <= t]
        new = ReplayIndex(manifest["capture_id"], plan["session"], plan["endpoints"], prefix,
                          mode=plan["mode"], roster=roster)
        if new.replay(t, event_cutoff=event, receipt_cutoff=t, policy=policy) != old:
            raise ValueError("New genuine responses changed an earlier receipt snapshot.")
    historical = {}
    for name, at in protocol["historical_events"].items():
        session = sessions[9896 if name == "singapore_cutoff" else 9987]
        field = reconstruct_field(session, utc(at))
        historical[name] = {
            "session": session.key, "source_event_cutoff": at, "mode": EVENT_ONLY,
            "receipt": "RECEIPT_UNAVAILABLE", "provider_publication_utc": None,
            "physical_scope": "FROZEN_S7_A_AND_S7_A1_SUPPORT_ONLY", "localized": field["supported_positions"],
            "roster": len(session.roster), "reported_race_control": field["track_status"],
        }
    recovery = recovery_probe(policy)
    faults, demos = run_faults(sessions[9987], policy, recovery)
    delay = strategy_delay()
    statuses = Counter(e["kind"] for e in idx.audit)
    durations = [e.duration_s for e, _ in envelopes]
    capture_quality = {
        "mode": RETROSPECTIVE, "engineering_receipts": "FIRST_SEEN_OBSERVED",
        "actual_racing_receipt_windows": 0, "independent_live_racing_sessions": 0,
        "historical_collection_windows": 1, "clock_sync": "UNKNOWN", "unordered_attempts": idx.unordered,
        "provider_publication_latency": None, "event_to_first_seen_age": None,
        "independent_operational_availability": None,
        "response_duration_seconds": {"min": min(durations), "median": median(durations), "max": max(durations)},
        "capture_duration_s": (times[-1]-times[0]).total_seconds(),
        "polling_intervals_s": [(b.request_utc-a.request_utc).total_seconds()
                                for (a, _), (b, _) in pairwise(envelopes)],
        "endpoint_first_seen_versions": dict(Counter(r["endpoint"] for r in idx.versions.values())),
    }
    live = {"release": "LIVE_UNSUPPORTED", "empirical_racing_freshness": "UNMEASURED",
            "provider_publication": "UNVERIFIED", "historical_receipt": "UNAVAILABLE",
            "shadow_operational_reliability": "UNVERIFIED", "capture_software": RELEASE}
    settings = {"decision_utc": times[-1], "receipt_cutoff": times[-1], "event_cutoff": event,
                "policy": policy, "roster": roster}
    final_snapshot = idx.replay(settings["decision_utc"], event_cutoff=event, receipt_cutoff=times[-1], policy=policy)
    out.mkdir(parents=True, exist_ok=False)
    diag = out/"diagnostics"
    diag.mkdir()
    reproduction = _export(root, diag/"availability_replay.zip", settings, final_snapshot)
    for key in ("before", "after", "stable_prefix", "orphaned_after"):
        if key not in recovery:
            raise ValueError("Incomplete recovery verification.")
    metrics = {"release": RELEASE, "decision_path": "B_ENGINEERING_VALIDATED_EMPIRICAL_AVAILABILITY_INCOMPLETE",
               "capture_attempts": len(envelopes)+len(orphaned), "successful": sum(not e.error for e, _ in envelopes),
               "failed": sum(bool(e.error) for e, _ in envelopes), "retried": sum(e.retry > 0 for e, _ in envelopes),
               "endpoints": len(plan["endpoints"]), "observations": len(idx.history), "versions": len(idx.versions),
               "bytes_observed": sum(e.body_bytes for e, _ in envelopes),
               "raw_bodies": len({e.body_sha256 for e, _ in envelopes}), "fault_scenarios": len(faults),
               "empirical_racing_windows": 0, "live": "LIVE_UNSUPPORTED"}
    docs = {
        "protocol": protocol, "frozen_baseline": frozen, "capture_manifest": manifest | {"sha256": CAPTURE_SHA},
        "source_inventory": {ep: {"event_clock": clock, "identity": ["session_key", *keys],
                                    "driver_specific": "driver_number" in keys, "provider_publication": None}
                             for ep, (clock, keys) in SCHEMAS.items()},
        "clock_contract": {"source_event": "Normalized provider event field; original value retained",
                           "first_byte": "Local arrival of the first nonempty body chunk",
                           "first_seen": "Local response completion for that payload version",
                           "usable": "Later of request, completion and processing; no earlier application use",
                           "duration": "Monotonic nanoseconds within the same host collection epoch",
                           "provider_publication": "UNVERIFIED", "decision_consumption": "Virtual decision cutoff",
                           "historical_archive": EVENT_ONLY},
        "capture_quality": capture_quality, "revision_audit": {"capture": dict(statuses), "rows": idx.audit},
        "replay_protocol": {"modes": [EVENT_ONLY, RETROSPECTIVE, EXPERIMENT],
                            "captured_scope": "CURRENT_COLLECTOR_CLOCK_ONLY", "freshness": policy,
                            "older_response_overwrite": "SUPPRESSED", "omission": "NO_RETRACTION",
                            "export": reproduction},
        "receipt_replay": {"snapshots": [_summary(s) for s in snapshots], "export": reproduction},
        "fault_injection": {"scenarios": faults, "fixtures": demos["fixture_inputs"],
                            "raw_capture_modified": False},
        "outage_recovery": {"journal": recovery, "controlled_outage": [_summary(s) for s in demos["outage"]]},
        "historical_receipt_limitations": historical, "race_control_delay_demo": delay,
        "snapshot_support": {key: demos[key] for key in ("missing_pit", "receipt_filtered_field", "stale_field")},
        "dashboard_integration": {"capture": {"capture_id": manifest["capture_id"], "session": plan["session"],
                                             "summary": [_summary(s) for s in snapshots], "quality": capture_quality},
                                  "historical": historical,
                                  "synthetic": {key: [_summary(s) for s in demos[key]] for key in (
                                      "correction", "outage", "delayed_sc")},
                                  "delay": delay, "live": live, "strategy_support": "HISTORICAL_STRATEGY_UNSUPPORTED"},
        "live_readiness": live, "regression_parity": frozen,
        "invariant_checks": {"capture_prefix_stable": True, "synthetic_prefix_stable": demos["prefix_stability"],
                             "all_faults_passed": len(faults) == 18, "export_reproduced": reproduction["reproduced"],
                             "geometry_support_unchanged": True, "review_after_sc_receipt": True,
                             "simulator_change": False, "provider_publication_inferred": False},
        "metrics": metrics,
    }
    for name, data in docs.items():
        write_json(out/f"{name}.json", data)
    version_rows = sorted(idx.versions.values(), key=lambda r: (r["key"], r["usable_utc"]))
    common = [{"capture_id": e.capture_id, "sequence": e.sequence, "endpoint": e.endpoint,
               "request_id": e.request_id} for e, _ in envelopes]
    tables = {
        "request_ledger": [row | {"request_utc": e.request_utc, "monotonic_start_ns": e.request_ns,
                                  "poll": e.poll, "retry": e.retry, "epoch": e.clock_epoch,
                                  "params": canonical(e.parameters).decode()}
                           for row, (e, _) in zip(common, envelopes, strict=True)],
        "response_ledger": [row | {"first_byte_utc": e.first_byte_utc, "completed_utc": e.completed_utc,
                                   "processed_utc": e.processed_utc, "duration_s": e.duration_s, "status": e.http_status,
                                   "error": e.error, "bytes": e.body_bytes, "sha256": e.body_sha256,
                                   "clock_quality": e.clock_quality, "rows": e.record_count}
                            for row, (e, _) in zip(common, envelopes, strict=True)],
        "first_seen_ledger": [{key: row[key] for key in ("key", "endpoint", "version", "first_seen_utc",
                                                         "usable_utc", "source_event_utc", "request_id")}
                              for row in version_rows],
        "record_versions": [{key: value for key, value in row.items() if key not in ("payload", "appearance")} |
                            {"canonical_payload": canonical(row["payload"]).decode()} for row in version_rows],
        "availability_windows": [row["appearance"] | {"key": row["key"], "endpoint": row["endpoint"],
                                                      "mode": RETROSPECTIVE} for row in version_rows if row["appearance"]]
                                + [demos["appearance"] | {"key": "controlled-appearance", "endpoint": "position",
                                                          "mode": EXPERIMENT}],
        "endpoint_freshness": [{"decision_utc": s["decision_utc"], "endpoint": ep, **row |
                                {"driver_age_s": canonical(row["driver_age_s"]).decode(),
                                 "error_kinds": canonical(row["error_kinds"]).decode()}}
                               for s in snapshots for ep, row in s["endpoints"].items()],
    }
    for name, data in tables.items():
        _parquet(out/f"{name}.parquet", data)
    from f1ri.validation.source_availability_plots import render

    render(diag, docs, tables)
    _seal(out)
    return metrics
