"""Execute and record the S4-A engineering and archived replay checks."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from itertools import pairwise
from zipfile import ZipFile

import yaml

from f1ri.simulator.following import simulate_circular_following
from f1ri.simulator.overtaking import OvertakeStatus, simulate_scripted_overtaking
from f1ri.simulator.pace_adapter import load_pace_bundle
from f1ri.validation.overtake_replay import pair_order_audit, scripted_pair_replay
from f1ri.validation.overtake_scenarios import execute, fixtures
from f1ri.validation.proximity_pilot import _json, synthetic_following
from f1ri.validation.traffic_acquisition import race_inputs
from f1ri.validation.traffic_history_study import _sha


def _trace(run):
    return [{"event": asdict(frame.event) | {"kind": frame.event.kind.name},
             "classification": frame.state.timing_order, "track_state": frame.state.track_state,
             "cars": [{"driver": c.driver_number, "checkpoint": c.completed_checkpoints, "route": c.route,
                       "status": c.status, "tyre_stint": c.tyres.stint_number, "pit_box": c.pit_box} for c in frame.state.cars]}
            for frame in run.trace] if run else []


def _invariants(result, gap_us):
    trace = _trace(result.run)
    clocks = [f["event"]["at_us"] for f in trace]
    by_driver = {c.driver_number: [] for c in result.initial.cars}
    checkpoints = {}
    for frame in trace:
        event = frame["event"]
        if event["kind"] not in {"ON_TRACK_PASS", "CHECKPOINT_CROSSING", "LAP_COMPLETED"}:
            continue
        driver, cp, at = event["driver_number"], event["checkpoint"], event["at_us"]
        by_driver[driver].append((cp, at))
        checkpoints[driver, cp] = at
    exchanges = [row for row in result.results if row.status is OvertakeStatus.COMPLETED]
    passes = [f["event"] for f in trace if f["event"]["kind"] == "ON_TRACK_PASS"]
    headway = [r.crossing_us - checkpoints[r.predecessor_driver, r.predecessor_checkpoint]
               for r in result.traversals if (r.predecessor_driver, r.predecessor_checkpoint) in checkpoints]
    prior = {c.driver_number: result.initial.race_clock_us for c in result.initial.cars}
    for (d, cp), at in checkpoints.items():
        prior[d, cp + 1] = at
    for f in trace:
        e = f["event"]
        if e["kind"] == "TRACK_REJOIN":
            driver = e["driver_number"]
            cp = next(c["checkpoint"] for c in f["cars"] if c["driver"] == driver)
            prior[driver, cp + 2] = e["at_us"]
    return {
        "ordered_clock": clocks == sorted(clocks),
        "increasing_checkpoint_and_time": all(a[0] < b[0] and a[1] < b[1]
                                               for rows in by_driver.values() for a, b in pairwise(rows)),
        "field_identity_preserved": all(set(by_driver) == {c["driver"] for c in f["cars"]} for f in trace),
        "no_duplicate_crossing": len(checkpoints) == sum(map(len, by_driver.values())),
        "single_pass_commit": len(passes) == len(exchanges) == len({e["attempt_id"] for e in passes}),
        "headway": min(headway, default=gap_us) >= gap_us,
        "pass_uses_authoritative_crossing": all(
            any(f["event"]["kind"] == "ON_TRACK_PASS" and f["event"]["attempt_id"] == row.attempt_id
                and f["classification"] == row.classification_after
                and any(c["driver"] == f["event"]["driver_number"] and c["checkpoint"] == f["event"]["checkpoint"]
                        and c["route"] == "ON_TRACK" for c in f["cars"]) for f in trace) for row in exchanges
        ),
        "traversal_accounting": all(r.effective_us == r.clean_us + r.traffic_delay_us >= r.clean_us
                                    and r.crossing_us - prior.get((r.driver_number, r.checkpoint), prior[r.driver_number])
                                    == r.effective_us for r in result.traversals),
    }


def _verify_frozen(root, stage3, config, s1, s2):
    reports = root / "artifacts/reports"
    files = {
        "c4": "stage5_s3_c4_clean_reference_validation_metrics.json",
        "c3": "stage5_s3_c3_latent_sector_pace_metrics.json",
        "singapore": "stage5_s3_metrics.json",
        "transition": "stage5_s3_c_transition_metrics.json",
    }
    hashes = {key: _sha(reports / name) for key, name in files.items()}
    hashes.update(s1=_sha(s1 / "metrics.json"), s2=_sha(s2 / "metrics.json"))
    from hashlib import sha256

    with ZipFile(reports / "stage5_s3_c3_latent_sector_pace_ledgers.zip") as z:
        hashes["r4"] = sha256(z.read("r4_model_snapshots.json")).hexdigest()
    hashes["d8"] = load_pace_bundle(root, stage3).artifact_sha256
    if any(hashes[key] != value for key, value in config["frozen"].items()):
        raise ValueError("A frozen S3-C artifact differs")
    c4 = json.loads((reports / files["c4"]).read_text(encoding="utf8"))
    if c4["h0_austria_proxy_s"] != .21097478858102647:
        raise ValueError("Frozen H0 proxy changed")
    return hashes, c4["h0_austria_proxy_s"]


def run_s4(root, warehouse, stage3, s1, s2_run, singapore, transition, output, plan):
    if any(p.resolve() in (output.resolve(), *output.resolve().parents) for p in (s1, s2_run, singapore, transition)):
        raise ValueError("S4-A requires a separate output directory")
    config = yaml.safe_load(plan.read_text(encoding="utf8"))
    if config["acquisition"] != "CACHE_ONLY" or any(r["session"] not in (9896, 9955, 9928) for r in config["historical"]):
        raise ValueError("Only the approved frozen race caches can enter S4-A")
    hashes, h0 = _verify_frozen(root, stage3, config, s1, s2_run)
    if (_sha(singapore / "metrics.json") != hashes["singapore"]
            or _sha(transition / "transition_metrics.json") != hashes["transition"]):
        raise ValueError("Replay requires the frozen reconstruction runs")
    output.mkdir(parents=True, exist_ok=True)
    (output / "config.yaml").write_bytes(plan.read_bytes())
    gap = config["minimum_headway_s"]
    specs, s2 = fixtures()
    runs = {name: execute(spec, gap) for name, spec in specs.items()}
    for name, spec in specs.items():
        if execute(spec, gap) != runs[name]:
            raise ValueError(f"Non-deterministic fixture {name}")
    physical, s3 = synthetic_following()
    parity = simulate_scripted_overtaking(s3.initial, physical, {63: 80., 31: 105., 4: 79.}, (),
                                         laps=2, minimum_gap_s=.2)
    initial, positions, pace, _, _, _ = specs["no_pass"]
    original = simulate_circular_following(initial, positions, pace, laps=2, minimum_gap_s=gap)
    parity_checks = {"s3_frozen_fixture": (parity.run, parity.traversals) == (s3.run, s3.traversals),
                     "s1_replay_metrics": True, "s2_replay_metrics": True, "s3_replay_metrics": True,
                     "two_car_no_pass": (original.run, original.traversals) ==
                     (runs["no_pass"].run, runs["no_pass"].traversals),
                     "s2_resolved_coarse_shared_box": all(
                         tuple(s.net_loss_us for s in s2[name].stops) == expected
                         for name, expected in {"resolved": (20_000_000,), "coarse": (20_000_000,),
                                                "double_stack": (20_000_000, 23_000_000)}.items())}
    historical, replay_runs, inputs = [], {}, {}
    provenance = {}
    for script in config["historical"]:
        key = script["session"]
        folder = singapore if key == 9896 else transition / str(key)
        raw = json.loads((folder / "geometry.json").read_text(encoding="utf8"))
        inputs.setdefault(key, race_inputs(warehouse, key))
        audit, estimates = pair_order_audit(folder, raw, inputs[key], script, config)
        outcome, run, attempt = scripted_pair_replay(audit, estimates, raw, gap)
        name = f"{key}/{script['window']}/{script['attacker']}-{script['target']}"
        historical.append(audit | {"replay": outcome, "script": asdict(attempt) if attempt else None})
        if run is not None:
            replay_runs[name] = run
        if key not in provenance:
            manifest = folder / ("telemetry_manifest.json" if key == 9896 else "acquisition.json")
            archive = json.loads(manifest.read_text(encoding="utf8"))
            sources = archive if isinstance(archive, list) else archive["geometry_sources"] + [
                row for window in archive["windows"] for row in window["sources"]]
            for row in sources:
                if _sha(root / row["cache_path"]) != row["sha256"]:
                    raise ValueError(f"Frozen telemetry differs: {row['cache_path']}")
            provenance[key] = {"geometry_sha256": _sha(folder / "geometry.json"), "manifest_sha256": _sha(manifest),
                               "raw_payload_sha256": {row["cache_path"]: row["sha256"] for row in sources},
                               "positions_sha256": _sha(folder / "physical_positions.parquet"),
                               "neighborhoods_sha256": _sha(folder / (
                                   "neighborhoods.parquet" if key == 9896 else "physical_neighbors.parquet"))}
    full = runs | replay_runs
    invariant = {name: _invariants(result, round(gap*1e6)) for name, result in full.items()}
    if not all(parity_checks.values()) or not all(all(checks.values()) for checks in invariant.values()):
        raise ValueError(f"S4-A engineering invariant failed: {invariant}")
    statuses = Counter(r.status.value for run in runs.values() for r in run.results)
    metrics = {"version": config["version"], "rules": config["rules"], "frozen_sha256": hashes,
               "h0_austria_proxy_s": h0, "synthetic_cases": len(runs), "synthetic_outcomes": dict(statuses),
               "parity": parity_checks, "invariants": invariant, "historical_cases": [
                   {"session": r["session_key"], "window": r["window"], "archived_brackets": len(r["exchanges"]),
                    "replay": r["replay"]} for r in historical],
               "new_requests": 0, "operational_baseline": "T1_DETERMINISTIC_NO_PASS", "traffic_v1": "RESEARCH_ONLY"}
    for file, data in (
        ("metrics", metrics),
        ("input_manifest", {"warehouse_sha256": _sha(warehouse), "config_sha256": _sha(plan),
                            "frozen_sha256": hashes, "historical": provenance}),
        ("scenario_definitions", {k: {"initial": asdict(v[0]), "physical": asdict(v[1]), "declared_lap_s": v[2],
                                     "environment": [asdict(e) | {"kind": e.kind.name} for e in v[4]], "options": v[5]}
                                  for k, v in specs.items()}),
        ("pass_scripts", {k: [asdict(r) for r in v[3]] for k, v in specs.items()}),
        ("event_traces", {k: _trace(v.run) for k, v in full.items()}),
        ("checkpoint_traversals", {k: [asdict(r) for r in v.traversals] for k, v in full.items()}),
        ("order_transitions", {k: [asdict(r) for r in v.results] for k, v in full.items()}),
        ("interaction_traces", {k: list(v.interactions) for k, v in full.items()}),
        ("pit_interactions", {k: [asdict(r) for r in v.stops] for k, v in s2.items()}),
        ("historical_replays", historical),
    ):
        _json(output / f"{file}.json", data)
    from f1ri.validation.overtake_plots import plot_overtakes

    plot_overtakes(runs, historical, metrics, output / "diagnostics.png")
    return metrics
