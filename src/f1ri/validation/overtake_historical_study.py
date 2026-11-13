"""Cache-only S4-B census, observation labels and statistical support audit."""
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from itertools import pairwise
from statistics import median
from zipfile import ZipFile

import duckdb
import yaml

from f1ri.features.overtake_events import (
    PASS_TYPES,
    classify_reversal,
    pair_points,
    reversal_candidates,
)
from f1ri.features.overtake_opportunities import group_encounters, outcome, sample_opportunities
from f1ri.simulator.pace_adapter import load_pace_bundle
from f1ri.validation.overtake_history import RaceHistory
from f1ri.validation.overtake_replay import scripted_pair_replay
from f1ri.validation.traffic_encounter_audit import window_rows
from f1ri.validation.traffic_history_study import _sha

APPROVED = {9896: "frozen_pilot", 9955: "development", 9928: "development", 9987: "reconstruction_only"}
BASELINE = "371d6c4ccc7557205848f4c55030907b184661b1"


def _load_json(path):
    import json

    return json.loads(path.read_bytes())


def _source_manifest(root, folder, session):
    files = ["geometry.json", "physical_positions.parquet",
             "neighborhoods.parquet" if session == 9896 else "physical_neighbors.parquet"]
    files.extend(name for name in ("markers.parquet", "timing_reconciliation.json", "localization.json")
                 if (folder / name).exists())
    index = "telemetry_manifest.json" if session == 9896 else "acquisition.json"
    manifest = _load_json(folder / index)
    payloads = manifest if isinstance(manifest, list) else manifest["geometry_sources"] + [
        r for window in manifest["windows"] for r in window["sources"]]
    cached = []
    for row in payloads:
        path = root / row["cache_path"]
        cached.append(row | {"cache_present": path.exists(), "cache_hash_matches": path.exists()
                             and _sha(path) == row["sha256"]})
    with duckdb.connect() as db:
        db.execute("SET TimeZone = 'UTC'")
        span = db.execute(
            'SELECT "window", mode, min(as_of_utc), max(as_of_utc), count(DISTINCT as_of_utc) '
            'FROM read_parquet(?) GROUP BY ALL ORDER BY ALL', [str(folder / "physical_positions.parquet")]
        ).fetchall()
    return {
        "folder": folder.relative_to(root).as_posix() if folder.is_relative_to(root) else str(folder),
        "files": {f: _sha(folder / f) for f in [*files, index]},
        "window_spans": [{"window": w, "mode": m, "first_utc": lo, "last_utc": hi, "snapshots": n}
                         for w, m, lo, hi, n in span if m in ("POINT_IN_TIME", "RETROSPECTIVE")],
        "payloads": cached,
    }


def _frames(rows):
    data = defaultdict(dict)
    for row in rows:
        data[row["as_of_utc"]][row["driver_number"]] = row
    return dict(data)


def _source_windows(folder, key, manifest, *, outcome_only=False):
    geometry = _load_json(folder / "geometry.json")
    names = sorted({span["window"] for span in manifest["window_spans"]})
    for name in names:
        source = "neighborhoods" if key == 9896 else "physical_neighbors"
        layers = {}
        modes = ("RETROSPECTIVE",) if outcome_only else ("POINT_IN_TIME", "RETROSPECTIVE")
        for mode in modes:
            layers[mode] = tuple(_frames(window_rows(folder, table, [name], [mode]))
                                 for table in ("physical_positions", source))
        times = sorted(layers["RETROSPECTIVE"][0])
        if len(times) > 1:
            yield name, geometry, layers, median((b - a).total_seconds() for a, b in pairwise(times))


def _retirements(root):
    filename = root / "config/experiments/stage5_s3_c_transition_pilot.yaml"
    plan = yaml.safe_load(filename.read_bytes())
    return {r["session_key"]: {v["driver_number"]: datetime.fromisoformat(v["available_by_utc"])
                              for v in r.get("known_off_track", ())} for r in plan["cohorts"]}


def _frozen(root, stage3, replay, config):
    reports = root / "artifacts/reports"
    frozen = {key: _sha(reports / f"{prefix}_metrics.json") for key, prefix in {
        "s4_a": "stage5_s4_a_overtaking_mechanics",
        "c4": "stage5_s3_c4_clean_reference_validation",
    }.items()}
    frozen["d8"] = load_pace_bundle(root, stage3).artifact_sha256
    if frozen != config["frozen"] or _sha(replay / "metrics.json") != frozen["s4_a"]:
        raise ValueError("Frozen S4-A, S3-C4 or D8 hash differs")
    with ZipFile(reports / "stage5_s4_a_overtaking_mechanics_ledgers.zip") as z:
        for name in ("order_transitions.json", "event_traces.json", "historical_replays.json",
                     "pass_scripts.json", "scenario_definitions.json", "input_manifest.json"):
            if z.read(name) != (replay / name).read_bytes():
                raise ValueError(f"S4-A frozen replay differs for {name}")
    return frozen


def ordering_audit(transitions, traces):
    """Version the interpretation of the frozen circular and linear fields."""
    output = []
    for scenario, rows in sorted(transitions.items()):
        frames = traces[scenario]
        for row in rows:
            if row["status"] != "COMPLETED":
                continue
            i, = [i for i, f in enumerate(frames) if f["event"]["kind"] == "ON_TRACK_PASS"
                  and f["event"]["attempt_id"] == row["attempt_id"]]
            event = frames[i]["event"]
            prior_rank = frames[i-1]["classification"]
            if (i == 0 or prior_rank != row["classification_before"]
                    or frames[i]["classification"] != row["classification_after"]
                    or row["pair_at_boundary_after"] != row["pair_at_boundary_before"][::-1]
                    or row["pair_at_boundary_after"][0] != event["driver_number"]):
                raise ValueError(f"S4-A trace phases disagree for {scenario}")
            output.append({"scenario": scenario, "attempt_id": row["attempt_id"], "version": "s4-a-order-view-v2",
                           "commit_utc_offset_us": event["at_us"],
                           "attacker_anchored_cycle_pre": row["physical_before"],
                           "attacker_anchored_cycle_post": row["physical_after"],
                           "checkpoint_pair_front_to_back_pre": row["pair_at_boundary_before"],
                           "checkpoint_pair_front_to_back_post": row["pair_at_boundary_after"],
                           "timing_order_pre": prior_rank, "timing_order_post": frames[i]["classification"]})
    return output


def _label_candidates(context, history, config):
    source, session, window, geometry, layers, period = context
    retro, neighbors = layers["RETROSPECTIVE"]
    points = pair_points(retro, neighbors, geometry, config["event_max_gap_m"])
    for row in reversal_candidates(points, geometry, config):
        lo, hi = row["event_time_lower_utc"], row["event_time_upper_utc"]
        independent = history.event_evidence(row)
        interruptions = history.interruptions((row["attacker"], row["target"]), lo, hi)
        category, tier, reasons = classify_reversal(row, interruptions, independent["corroborated"], period)
        track = history.green_interval(lo)
        yield row | {"session": session, "source": source, "window": window,
                     "layout_version": geometry.get("version", geometry.get("layout_version")),
                     "geometry_source": geometry["length_source"], "source_period_s": period,
                     "track_state": "GREEN" if track else "NOT_SUPPORTED_GREEN",
                     "high_confidence_green_throughout": bool(track and hi < track.end_utc),
                     "interruptions": interruptions, "evidence": independent,
                     "event_type": category, "tier": tier, "exclusions": reasons}


def _route_order_changes(context, history):
    source, session, window, geometry, layers, period = context
    frames, neighbors = layers["RETROSPECTIVE"]
    times = sorted(neighbors)
    if not times:
        return
    exits = [(d, lo, hi, observed, "PIT") for d, spans in history.pits.items() for lo, hi, observed in spans]
    exits.extend((d, at, at + timedelta(seconds=10), at, "OFF_TRACK")
                 for d, at in history.retirements.items())
    # Reconstructed route transitions include an uncertainty band. Use
    # supported endpoints across the band rather than requiring consecutive fixes.
    for target, start, end, observed, route in sorted(exits):
        if not times[0] < start < times[-1]:
            continue
        boundary = bisect_left(times, start)
        before_range = times[bisect_left(times, start - timedelta(seconds=5)):boundary]
        after_range = times[boundary:bisect_right(times, min(end, start + timedelta(seconds=10)))]
        followers = {}
        for before_at in before_range:
            for d, near in neighbors[before_at].items():
                if near.get("ahead") == target and near["gap_upper_m"] <= 120:
                    followers[d] = before_at, near
        for d, (before_at, before) in sorted(followers.items()):
            found = next((at for at in after_range
                          if frames.get(at, {}).get(target, {}).get("route") == route
                          and neighbors[at].get(d, {}).get("ahead") not in (None, target)), None)
            if found is None:
                continue
            target_before = frames.get(before_at, {}).get(target, {})
            target_after = frames[found][target]
            after = neighbors[found][d]
            expected = "CENSORED_PIT" if route == "PIT" else "CENSORED_RETIREMENT"
            yield {
                "order_change_id": f"route/{session}/{d}-{target}/{observed.isoformat()}",
                "session": session, "source": source, "window": window, "driver": d,
                "previous_neighbor": target, "new_neighbor": int(after["ahead"]),
                "before_utc": before_at, "after_utc": found, "target_before": target_before,
                "target_after": target_after, "before_gap_m": before.get("gap_m"), "after_gap_m": after.get("gap_m"),
                "source_period_s": period, "source_observation_utc": observed,
                "event_type": "PIT_INDUCED_ORDER_CHANGE" if route == "PIT" else "RETIREMENT_INDUCED_CHANGE",
                "evidence": history.interruptions((target,), before_at, found),
                "layout_version": geometry.get("version", geometry.get("layout_version")),
                "tier": "EXCLUDED", "exclusions": [expected],
            }


def _deduplicate_events(events):
    pairs = defaultdict(list)
    for event in events:
        pairs[event["session"], event["attacker"], event["target"]].append(event)
    kept = []
    for key, rows in sorted(pairs.items()):
        groups = []
        end = None
        for row in sorted(rows, key=lambda r: (r["event_time_lower_utc"], r["event_time_upper_utc"])):
            lo, hi = row["event_time_lower_utc"], row["event_time_upper_utc"]
            if end is None or lo >= end:
                groups.append([])
                end = hi
            groups[-1].append(row)
            end = max(end, hi)
        for group in groups:
            best = min(group, key=lambda r: (
                r["event_type"] not in PASS_TYPES, {"A": 0, "B": 1, "C": 2, "EXCLUDED": 3}[r["tier"]],
                r["event_time_bracket_s"], r["event_time_lower_utc"], r["source"], r["window"],
            ))
            lo = best["event_time_lower_utc"]
            session, d, target = key
            kept.append(best | {
                "pass_event_id": f"event/{session}/{d}-{target}/{lo.isoformat()}",
                "supporting_observations": [{k: r[k] for k in (
                    "source", "window", "event_time_lower_utc", "event_time_upper_utc", "tier", "event_type"
                )} for r in group],
            })
    return kept


def _outcomes(opportunity, context, times, history, events, config):
    _, _, _, geometry, layers, period = context
    retro, neighbors = layers["RETROSPECTIVE"]
    at, d, target = (opportunity[key] for key in ("cutoff_utc", "attacker", "target"))
    horizons = [(str(s), at + timedelta(seconds=s)) for s in config["horizons_s"]]
    horizons.append(("checkpoint", history.checkpoint(d, at, opportunity["track_segment"],
                                                      config["checkpoint_limit_s"])))
    for horizon, end in horizons:
        stop = history.interruptions((d, target), at, end) if end is not None else []
        yield outcome(opportunity, end, retro, neighbors, times, events, stop, period, geometry["length_m"]) | {
            "horizon": horizon, "competing_event": stop[0][1] if stop else None,
            "competing_event_utc": stop[0][0] if stop else None,
        }


def _replay_event(event, context):
    _, session, window, geometry, layers, _ = context
    point, _ = layers["POINT_IN_TIME"]
    lower, upper = event["event_time_lower_utc"], event["event_time_upper_utc"]
    audit = {key: event[key] for key in ("attacker", "target")} | {
        "window": window, "session_key": session,
        "exchanges": [{"status": "SUPPORTED_PAIR_EXCHANGE", "before": {"at_utc": lower},
                       "after": {"at_utc": upper}}],
    }
    estimates = {("POINT_IN_TIME", lower, d): row for d, row in point.get(lower, {}).items()}
    replay, run, script = scripted_pair_replay(audit, estimates, geometry, .1)
    commit = replay.get("simulated_boundary_commit_s")
    direction = (run is not None and run.results[0].pair_at_boundary_after
                 == (event["attacker"], event["target"]))
    return {"pass_event_id": event["pass_event_id"], **replay,
            "direction_matches": direction,
            "timing_outside_bracket_s": max(0., commit - event["event_time_bracket_s"])
            if commit is not None else None,
            "script_checkpoint": script.checkpoint if script else None}


def run_historical_overtakes(root, warehouse, stage3, folders, replay, output, plan):
    config = yaml.safe_load(plan.read_bytes())
    if config["acquisition"] != "CACHE_ONLY" or any(APPROVED.get(r["session"]) != r["role"] for r in config["cohorts"]):
        raise ValueError("S4-B accepts only the approved cache cohorts and roles")
    if output.exists():
        raise FileExistsError(f"Use a new S4-B run directory: {output}")
    frozen = _frozen(root, stage3, replay, config)
    order = ordering_audit(_load_json(replay / "order_transitions.json"), _load_json(replay / "event_traces.json"))
    replays = _load_json(replay / "historical_replays.json")
    retirements = _retirements(root)
    histories, census = {}, []
    # Complete the source census before evaluating any labels.
    for entry in config["cohorts"]:
        key, source = entry["session"], entry["source"]
        folder = folders[source] if source == "singapore" else folders[source] / str(key)
        manifest = _source_manifest(root, folder, key)
        if not all(r["cache_hash_matches"] for r in manifest["payloads"]):
            raise ValueError(f"Frozen telemetry payload missing or changed in {folder}")
        census.append(entry | manifest)
        if key not in histories:
            histories[key] = RaceHistory(warehouse, key, retirements.get(key, {}))
    events, seen, funnel, contexts, route_changes = [], {}, Counter(), {}, {}
    for entry in census:
        key, source = entry["session"], entry["source"]
        history = histories[key]
        for window, geometry, layers, period in _source_windows(root / entry["folder"], key, entry):
            context = source, key, window, geometry, layers, period
            contexts[key, source, window] = entry
            events.extend(_label_candidates(context, history, config))
            for change in _route_order_changes(context, history):
                route_changes.setdefault(change["order_change_id"], change)
            pit, neighbors = layers["POINT_IN_TIME"]
            for row in sample_opportunities(key, window, pit, neighbors, geometry, history, config, funnel):
                seen.setdefault(row["opportunity_id"], row | {"source": source})
    events = _deduplicate_events(events)
    supported = [r for r in events if r["event_type"] in PASS_TYPES]
    for reference in replays:
        for exchange in reference["exchanges"]:
            if exchange["status"] != "SUPPORTED_PAIR_EXCHANGE":
                continue
            if not any((r["session"], r["attacker"], r["target"], str(r["event_time_lower_utc"]),
                        str(r["event_time_upper_utc"])) == (
                            reference["session_key"], reference["attacker"], reference["target"],
                            exchange["before"]["at_utc"], exchange["after"]["at_utc"]
                        ) for r in supported):
                raise ValueError(f"S4-A archived bracket did not reproduce for {reference['window']}")
    opportunities = sorted(seen.values(), key=lambda r: r["opportunity_id"])
    tyre = {r["opportunity_id"]: r.pop("retrospective_tyre_context") for r in opportunities}
    encounters = group_encounters(opportunities, config["encounter_link_s"])
    event_pairs = defaultdict(list)
    for event in events:
        event_pairs[event["session"], event["attacker"], event["target"]].append(event)
    by_context, support_by_context = defaultdict(list), defaultdict(list)
    for row in opportunities:
        by_context[row["session"], row["source"], row["window"]].append(row)
    for row in supported:
        support_by_context[row["session"], row["source"], row["window"]].append(row)
    results, comparisons = [], []
    for (key, source, window), entry in sorted(contexts.items()):
        if not (by_context[key, source, window] or support_by_context[key, source, window]):
            continue
        matching = {"window_spans": [{"window": window}]}
        (_, geometry, layers, period), = _source_windows(
            root / entry["folder"], key, matching, outcome_only=not support_by_context[key, source, window],
        )
        context = source, key, window, geometry, layers, period
        times = sorted(layers["RETROSPECTIVE"][0])
        for row in by_context[key, source, window]:
            results.extend(_outcomes(row, context, times, histories[key],
                                     event_pairs[key, row["attacker"], row["target"]], config))
        comparisons.extend(_replay_event(r, context) for r in support_by_context[key, source, window])
    output.mkdir(parents=True)
    from f1ri.validation.overtake_readiness import write_results

    write_results(output, config, plan, frozen, _sha(warehouse), census, histories, order, replays, events,
                  supported, opportunities, results, encounters, comparisons, tyre, funnel, list(route_changes.values()))
    return output / "metrics.json"
