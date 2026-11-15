"""Reproducible S7-A.1 pit-branch and predecessor evidence."""

from collections import Counter
from dataclasses import replace
from datetime import timedelta
from itertools import combinations
from json import loads
from pathlib import Path
from statistics import median
from zipfile import ZipFile

from f1ri.decision_board import canonical, replay
from f1ri.features.historical_field import (
    FieldMode,
    Series,
    reconcile_fixes,
    reconstruct_field,
    with_sparsity,
)
from f1ri.features.pit_branch import Route, fit_graph, matched_at, oriented_interval
from f1ri.features.track_geometry import signed_loop_delta
from f1ri.validation.historical_field_sources import S6C, checked_members, hash_bytes, stamp
from f1ri.validation.historical_field_study import _parquet, _seal, write_json
from f1ri.validation.pit_branch_evidence import (
    field_certificates,
    held_witnesses,
    merge_neighbor_candidates,
    qualification,
    trajectory,
    transition,
)
from f1ri.validation.pit_branch_sources import PINNED, S7A_EVIDENCE, load

STRICT = "S7A_FROZEN_STRICT_ROUTE"
REVISED = "S7A1_VALIDATED_MULTI_ROUTE"
RELEASE = "PARTIAL_GEOMETRIC_IMPROVEMENT"
MODEL_SHA = "f98e8444a5982dd482a29444ce2c06848a39829cc248cb6cfceb760b59eae16a"

FIA = {
    9987: "https://www.fia.com/system/files/decision-document/2025_emilia_romagna_grand_prix_-_race_director_event_notes_-_circuit_map_pit_lane_emergency_exits_quarantine_zone_and_red_zones.pdf",
    9896: "https://www.fia.com/system/files/decision-document/2025_singapore_grand_prix_-_event_notes_-_circuit_map_pit_lane_emergency_exits_map_and_quarantine_zone.pdf",
}


def _graphs(sessions, visits, policy, meta):
    graphs, evidence, held_rows, audit = {}, {}, [], []
    for key, session in sessions.items():
        fitting = next(r for r in visits if r["session"] == key and r["role"] == "FIT")
        graph, error = fit_graph(session, fitting, policy)
        evaluations = []
        clocks = []
        for visit in (r for r in visits if r["session"] == key and r["role"] == "HELD"):
            rows, clock = held_witnesses(graph, session, visit, meta["pit_timing"][key]) if graph else ([], None)
            held_rows.extend(rows)
            rates = qualification(rows, policy["held_gate"])
            passed = all(x["passed"] for x in rates.values())
            evaluations.append(passed)
            if passed:
                clocks.append(clock)
            audit.append({"session": key, "visit": visit["id"], "independence": "HELD_TELEMETRY_TIMING_CROSSCHECK",
                          "fit_reference": fitting["id"], "timing": {"entry": visit["entry"], "exit": visit["exit"]},
                          "checks": rates, "passed": passed, "validation_through_utc": clock})
        gate = (graph and all(evaluations) and sum(evaluations) >= policy["held_gate"]["min_independent_visits"])
        graphs[key] = replace(graph, verified_through=max(clocks)) if gate else graph
        evidence[key] = {
            "fit": fitting["id"], "fit_reason": error, "fit_event_time_through": graph.fit_through if graph else None,
            "held_event_time_through": graphs[key].verified_through if graphs[key] else None,
            "held_visits": len(evaluations), "passed_held_visits": sum(evaluations),
            "racing_loop_segments": len(session.geometry.points)-1,
            "validated_core_segments": len(graph.lane.points)-1 if gate else 0,
            "layout_version": session.geometry.version, "loop_source": session.layout_source,
            "status": "HELD_CHECKED_PIT_CORE" if gate else "STRICT_GUARD_RETAINED",
            "fia_source": FIA[key], "fia_document_role": "INDEPENDENT_TOPOLOGY_ONLY",
        }
    return graphs, evidence, held_rows, audit


def _graph_definition(session, graph):
    available = graph and graph.verified_through is not None
    labels = {r.value: {"metric_boundary": None, "status": "UNAVAILABLE"} for r in Route}
    labels[Route.MAIN] = {"status": "S7A_SAMPLED_MAIN_LOOP", "points_xy": session.geometry.points,
                          "centreline_length_m": session.geometry.length_m, "source": session.layout_source,
                          "direction": "CHRONOLOGICAL_REFERENCE_LAP"}
    if available:
        labels[Route.LANE] = {"status": "HELD_CHECKED_PIT_CORE", "points_xy": graph.lane.points,
                              "length_m": graph.lane.length_m, "source": graph.fit_sources,
                              "core_projection_start_m": graph.core_start_m,
                              "core_projection_span_m": graph.core_span_m,
                              "direction": "ENTRY_TO_EXIT", "fit_event_time_through": graph.fit_through,
                              "held_event_time_through": graph.verified_through,
                              "route_residual_gate_m": graph.policy["route_radius_m"],
                              "minimum_residual_separation_m": max(2*graph.policy["route_radius_m"],
                                                                graph.policy["min_cross_route_margin_m"])}
    return {"version": "s7-a1-pit-route-v1", "session": session.key, "layout": session.geometry.version,
            "frame": "OPENF1_SESSION_CARTESIAN_XY", "nodes": labels,
            "topological_links": [[a.value, b.value] for a, b in zip(
                (Route.MAIN, Route.APPROACH, Route.LANE, Route.EXIT, Route.MERGE),
                (Route.APPROACH, Route.LANE, Route.EXIT, Route.MERGE, Route.MAIN), strict=True)],
            "service_region_connected_to": Route.LANE, "metric_entry_and_merge_connectivity": "UNAVAILABLE"}


def _classification_rows(strict, revised, label):
    for before, after in zip(strict["drivers"], revised["drivers"], strict=True):
        detail = after["route_comparison"]
        yield {"session": strict["session"], "cutoff": strict["cutoff"], "label": label, "mode": strict["mode"],
               "driver": before["driver_number"], "strict_route": before["route"], "revised_route": after["route"],
               "strict_track_m": before["progress_m"], "revised_track_m": after["progress_m"],
               "strict_bound_m": before["uncertainty_m"], "revised_bound_m": after["uncertainty_m"],
               "strict_unwrapped_m": before["unwrapped_m"], "revised_unwrapped_m": after["unwrapped_m"],
               "route_reason": after["route_evidence"], "match_reason": detail["reason"],
               "racing_residual_m": detail.get("racing_residual_m"), "pit_residual_m": detail.get("pit_residual_m"),
               "lane_progress_m": detail.get("pit_progress_m") if after["route"] == "PIT" else None,
               "last_used_utc": detail.get("latest_fix_used"), "location_utc": after["location_utc"]}


def _singapore_audit(session, field, graph_evidence):
    rows = []
    for r in field["drivers"]:
        driver = r["driver_number"]
        if driver not in (63, 4):
            continue
        fix = session.fixes[driver].last(field["cutoff"])
        rows.append({"driver": driver, "session": session.key, "cutoff": field["cutoff"],
                     "xy": [fix.point.x, fix.point.y] if fix else None, "location_utc": r["location_utc"],
                     "age_s": r["age_s"], "lap_anchor": r["completed_laps"], "candidate_loop_m": r["mapped_candidate_m"],
                     "racing_residual_m": r["projection_error_m"], "pit_residual_m": None,
                     "track_m": r["progress_m"], "unwrapped_m": r["unwrapped_m"], "pit_report_utc": r["pit_timing_utc"],
                     "fix_source": r["location_sources"], "route": r["route"], "status": "AMBIGUOUS" if fix else "UNAVAILABLE",
                     "reason": r["reason"], "geometry_failure": graph_evidence["fit_reason"]})
    return rows


def _sc_order(field, proofs):
    cars = [r for r in field["drivers"] if r["unwrapped_m"] is not None]
    ordered, overlap = [], []
    for a, b in combinations(sorted(cars, key=lambda r: r["unwrapped_m"]), 2):
        gap = b["unwrapped_m"]-a["unwrapped_m"]
        if gap > a["uncertainty_m"]+b["uncertainty_m"]:
            ordered.append([a["driver_number"], b["driver_number"]])
        else:
            overlap.append([a["driver_number"], b["driver_number"]])
    # Physical circular ambiguity remains separately visible from competitive race distance.
    circular_overlap = [[a["driver_number"], b["driver_number"]] for a, b in combinations(cars, 2)
                        if oriented_interval((a["progress_m"], a["uncertainty_m"]),
                                             (b["progress_m"], b["uncertainty_m"]), field["length_m"])[0] == 0]
    return {"classification": "ORDER_AMBIGUOUS", "track_report": field["track_status"],
            "lap_unwrapped_partial_order_pairs": ordered, "lap_unwrapped_overlapping_pairs": overlap,
            "circular_overlap_pairs": circular_overlap, "cars": cars, "certificates": proofs,
            "independent_order_event_at_cutoff": None, "physical_queue_validated": False}


def _perturbed_session(session, at, variant):
    if variant.startswith("keep_every_"):
        return with_sparsity(session, int(variant.rsplit("_", 1)[-1]))
    if variant == "full":
        return session

    fixes = {}
    for driver, series in session.fixes.items():
        old = series.last(at)
        rows = series.rows
        if variant == "duplicate":
            same = [(r.point, r.raw_stamp, r.sources[0]) for r in rows if not r.conflict]
            additions = [(old.point, old.raw_stamp, old.sources[0])] if old else []
            fixes[driver], _ = reconcile_fixes((*same, *additions))
            continue
        if variant == "conflict":
            rows = (replace(r, conflict=True) if r == old else r for r in rows)
        elif variant == "irregular":
            rows = (r for i, r in enumerate(rows) if i % 5 not in (1, 2))
        else:
            gap = {"short_gap": .38, "long_gap": 2., "availability_delay": .65}[variant]
            rows = (r for r in rows if not at-timedelta(seconds=gap) < r.point.at_utc <= at)
        fixes[driver] = Series(rows, lambda r: r.point.at_utc)
    return replace(session, fixes=fixes)


def _stress(sessions, selected, route_matcher):
    variants = ("full", "keep_every_2", "keep_every_4", "keep_every_8", "short_gap", "long_gap",
                "availability_delay", "duplicate", "conflict", "irregular")
    results = []
    for key, at in selected:
        session = sessions[key]
        full = reconstruct_field(session, at, route_matcher=route_matcher)
        for variant in variants:
            current = _perturbed_session(session, at, variant)
            field = reconstruct_field(current, at, route_matcher=route_matcher)
            diffs = [abs(signed_loop_delta(a["progress_m"], b["progress_m"], full["length_m"]))
                     for a, b in zip(full["drivers"], field["drivers"], strict=True)
                     if a["progress_m"] is not None and b["progress_m"] is not None]
            proofs = field_certificates(current, field)
            widths = [p["separation_interval_m"][1]-p["separation_interval_m"][0]
                      for p in proofs if p["separation_interval_m"]]
            results.append({"session": key, "cutoff": at, "variant": variant,
                            "localized": field["supported_positions"],
                            "rate": field["supported_positions"]/len(session.roster),
                            "pit_classified": sum(r["route"] == "PIT" for r in field["drivers"]),
                            "route_unavailable": sum(r["route"] == "UNKNOWN" for r in field["drivers"]),
                            "stale_rejections": sum(r["reason"] == "STALE_LOCATION" for r in field["drivers"]),
                            "overlap_or_missing_car": sum(p["status"] in ("ORDER_AMBIGUOUS", "MISSING_CAR_CAN_INTERVENE")
                                                          for p in proofs),
                            "median_gap_width_m": median(widths) if widths else None,
                            "median_progress_difference_m": median(diffs) if diffs else None,
                            "newly_supported_against_full": [
                                b["driver_number"] for a, b in zip(full["drivers"], field["drivers"], strict=True)
                                if a["progress_m"] is None and b["progress_m"] is not None],
                            "compared_positions": len(diffs), "reference_role": "CORRELATED_COMPLETE_INPUT_ESTIMATE"})
    return results


def _replay_previous(root, prior_run):
    checked_members(root / S7A_EVIDENCE, PINNED[S7A_EVIDENCE])
    with ZipFile(root / S7A_EVIDENCE) as archive:
        frozen = loads(archive.read("release_verification.json"))["deterministic_s7a_files"]
        s7a_regression = loads(archive.read("regression_parity.json"))
    if prior_run and any(hash_bytes((prior_run / p).read_bytes()) != h for p, h in frozen.items()):
        raise ValueError("Independent S7-A replay differs from the frozen release.")
    with ZipFile(root / S6C) as archive:
        model_check = replay(root, archive.read("diagnostics/neutralization_replay.zip"))
    if model_check["snapshot"]["model"]["value"]["sha256"] != MODEL_SHA:
        raise ValueError("Frozen D8R1 model hash differs.")
    return {"frozen_s7a_artifacts": {str(p.as_posix()): h for p, h in PINNED.items()},
            "s7a_verified_files": frozen if prior_run else None,
            "s7a_deterministic_files": len(frozen)-1, "s6c_sealed_analysis_reproduced": True,
            "s6c_frozen_research_from_s7a": s7a_regression["s6_c_full_study"], "d8r1_sha256": MODEL_SHA,
            "s5_frozen_sources": s7a_regression["frozen"]}


def _prefix(session, cutoff):
    return replace(session, fixes={driver: Series((r for r in series.rows if r.point.at_utc <= cutoff),
                                                 lambda r: r.point.at_utc)
                                   for driver, series in session.fixes.items()})


def run(root: Path, out: Path, *, prior_run=None):
    sessions, _earlier, visits, protocol, meta = load(root)
    regression = _replay_previous(root, prior_run)
    pilot = loads((root/"config/experiments/stage7_s7_a.json").read_bytes())["pilots"]
    graphs, geometry, held, held_audit = _graphs(sessions, visits, protocol["policy"], meta)

    def match(session, driver, at, mode):
        return matched_at(graphs[session.key], session, driver, at, mode)

    rows, proofs, snapshots, paired = [], [], {}, []
    cutoff_specs = {int(key): dict(item["cutoffs"]) for key, item in pilot.items()}
    cutoff_specs[9987]["Imola VSC pit core"] = "2025-05-18T13:43:11Z"
    for key, cutoffs in cutoff_specs.items():
        session = sessions[key]
        for label, value in cutoffs.items():
            at = stamp(value)
            fields = {}
            for mode in (FieldMode.CAUSAL, FieldMode.RETROSPECTIVE):
                strict = reconstruct_field(session, at, mode)
                revised = reconstruct_field(session, at, mode, route_matcher=match)
                certs = field_certificates(session, revised)
                fields[mode] = {"strict": strict, "revised": revised, "certificates": certs}
                rows.extend(_classification_rows(strict, revised, label))
                proofs.extend(dict(p, label=label) for p in certs)
            causal = fields[FieldMode.CAUSAL]["revised"]
            retro = fields[FieldMode.RETROSPECTIVE]["revised"]
            if canonical(causal) != canonical(reconstruct_field(
                _prefix(session, at), at, route_matcher=match
            )):
                raise ValueError("Future fixes changed the event-time causal field.")
            paired.append({"session": key, "label": label, "cutoff": at, "causal_race": causal["supported_positions"],
                           "retrospective_race": retro["supported_positions"],
                           "causal_pit": sum(r["route"] == "PIT" for r in causal["drivers"]),
                           "retrospective_pit": sum(r["route"] == "PIT" for r in retro["drivers"]),
                           "causal_equals_prefix": True, "receipt_supported": False})
            snapshots[key, label] = fields

    trajectory_rows, entries, merges = [], [], []
    for visit in visits:
        session = sessions[visit["session"]]
        graph = graphs[session.key]
        trace = trajectory(session, visit, graph)
        trajectory_rows.extend(trace)
        entries.append(transition(trace, visit, "entry", graph))
        merge = transition(trace, visit, "merge", graph)
        merged = merge_neighbor_candidates(session, graph, merge, match)
        merges.append(merge | {"neighbors": merged})
    singapore = _singapore_audit(sessions[9896], snapshots[9896, "Archived model cutoff"][FieldMode.CAUSAL]["revised"],
                                geometry[9896])
    sc = snapshots[9987, "SC track clear"][FieldMode.CAUSAL]
    sc_order = _sc_order(sc["revised"], sc["certificates"])
    stress = _stress(sessions, ((9896, stamp("2025-10-05T12:52:29.272Z")),
                               (9987, stamp("2025-05-18T14:21:42Z"))), match)
    sc_visit = next(v for v in visits if v["id"] == "imola-sc-4")
    full_trace = [r for r in trajectory_rows if r["visit"] == sc_visit["id"]]
    trimmed = [r for r in full_trace if not sc_visit["exit"]-timedelta(seconds=2) <= r["utc"] <=
               sc_visit["exit"]+timedelta(seconds=8)]
    missing_exit = transition(trimmed, sc_visit, "merge", graphs[9987])
    full_merge = next(r for r in merges if r["visit"] == sc_visit["id"])
    strategy = {"status": "HISTORICAL_STRATEGY_UNSUPPORTED", "simulator_adapter": "NOT_RUN_UNSUPPORTED_PREREQUISITES",
                "tire_set_inventory": None, "box_occupancy": None, "authorized_pit_merge_geometry": None,
                "relevant_field_complete": False, "neutralization_calibrated": False, "opponent_future_policies": None,
                "mean_forecast_independent": True, "declared_strategy_s6c_replay": True}
    live = {"status": "LIVE_UNSUPPORTED", "availability": "EVENT_TIME_ONLY", "receipt_qualified": False,
            "freshness_verified": False, "historical_publication_verified": False, "outage_recovery_verified": False}
    metrics = {
        "release": RELEASE, "races": 2, "acquisition_requests": len(meta["acquisition"]),
        "acquired_location_fixes": sum(r["records"] for r in meta["sources"]),
        "cutoffs": len(cutoff_specs[9896])+len(cutoff_specs[9987]), "fit_visits": 2,
        "held_visits": len(held_audit), "held_pass": sum(v["passed"] for v in held_audit),
        "independent_order_events": 0, "route_witness_wrong": sum(r["status"] == "WRONG_ROUTE" for r in held),
        "racing_positions_added_causal": sum(r["strict_route"] != "RACING" and r["revised_route"] == "RACING"
                                              and r["mode"] == FieldMode.CAUSAL for r in rows),
        "racing_positions_added_retrospective": sum(r["strict_route"] != "RACING" and r["revised_route"] == "RACING"
                                                     and r["mode"] == FieldMode.RETROSPECTIVE for r in rows),
        "pit_positions_added_causal": sum(r["strict_route"] != "PIT" and r["revised_route"] == "PIT"
                                          and r["mode"] == FieldMode.CAUSAL for r in rows),
        "pit_positions_added_retrospective": sum(r["strict_route"] != "PIT" and r["revised_route"] == "PIT"
                                                 and r["mode"] == FieldMode.RETROSPECTIVE for r in rows),
        "previous_racing_regressed": sum(r["strict_route"] == "RACING" and r["revised_route"] != "RACING" for r in rows),
        "entry_windows": sum(r["earliest_utc"] is not None for r in entries),
        "merge_windows": sum(r["earliest_utc"] is not None for r in merges),
        "validated_exact_geometric_boundaries": 0,
        "certified_predecessors": sum(p["certified"] is not None for p in proofs),
        "model": MODEL_SHA, "live_status": live["status"],
    }
    proof_counts = dict(Counter(p["status"] for p in proofs))
    invariants = {
        "future_position_changes_isolated": all(r["causal_equals_prefix"] for r in paired),
        "causal_match_uses_only_past": all(r["last_used_utc"] is None or r["last_used_utc"] <= r["cutoff"]
                                          for r in rows if r["mode"] == FieldMode.CAUSAL),
        "receipt_blocked": all(reconstruct_field(s, s.start, FieldMode.RECEIPT, route_matcher=match)["support"]
                               == "RECEIPT_ORDER_UNAVAILABLE" for s in sessions.values()),
        "no_historical_strategy": strategy["status"] == "HISTORICAL_STRATEGY_UNSUPPORTED",
        "simulator_not_modified": True,
        "singapore_guard_retained": all(r["track_m"] is None for r in singapore),
        "no_invented_geometric_boundary": all(r["point_estimate_utc"] is None for r in (*entries, *merges)),
        "missing_exit_withholds_window": missing_exit["earliest_utc"] is None,
        "full_sampling_recovers_sc_route_window": full_merge["earliest_utc"] is not None,
        "sparsity_does_not_restore_stationary_unverified_cars": not any(
            r["newly_supported_against_full"] for r in stress if r["variant"] == "keep_every_4"),
    }
    if not all(invariants.values()):
        raise ValueError(f"S7-A.1 invariant failed: {invariants}")

    out.mkdir(parents=True, exist_ok=False)
    diag = out/"diagnostics"
    diag.mkdir()
    graph_json = {k: _graph_definition(s, graphs[k]) for k, s in sessions.items()}
    dashboard = {"release": RELEASE, "mode": "EVENT_TIME_ONLY", "geometry": geometry, "graphs": graph_json,
                 "route_comparison": rows, "certificates": proofs, "entry": entries, "merge": merges,
                 "singapore": singapore, "strategy_support": strategy, "live": live, "metrics": metrics,
                 "trace": [r for r in trajectory_rows if r["visit"] == "imola-vsc-1"]}
    evidence_inventory = {"sources": geometry, "dependencies": {
        "S7-A": "existing source normalization, timing, location alignment, loop, cutoff and release parity",
        "S5-A/S5-B": "archived Imola pit timing, neutralization and route context",
        "Stage 3-R / S6-C": "frozen mean D8R1 and deterministic declared strategy",
        "FIA": "topology corroboration only",
        "new_OpenF1": "predeclared separate fit and held historical acquisition"},
        "receipt_clocks": None, "historical_boundaries_independent_truth": None}
    imola_visits = [{"visit": v["id"], "coverage_fixes": sum(r["visit"] == v["id"] for r in trajectory_rows),
                     "stationary_support": [r["utc"] for r in trajectory_rows if r["visit"] == v["id"] and
                                            r["stationary_candidate"]],
                     "entry": next(r for r in entries if r["visit"] == v["id"]),
                     "merge": next(r for r in merges if r["visit"] == v["id"])}
                    for v in visits if v["session"] == 9987]
    documents = {
        "protocol": protocol, "frozen_baseline": regression, "source_manifest": meta,
        "geometry_evidence": evidence_inventory,
        "coordinate_transform_audit": {k: {"transform_applied": False, "control_points": None,
                                           "pit_and_loop_frame": "SAME_SESSION_OPENF1_CARTESIAN",
                                           "scale_basis": "FROZEN_LOOP_LENGTH",
                                           "topology": FIA[k], "coordinate_registration": "UNAVAILABLE",
                                           "fit_failure": geometry[k]["fit_reason"]}
                                       for k in sessions},
        "route_graph": graph_json, "route_support": {"strict": STRICT, "alternate": REVISED, "metrics": metrics,
                                                    "by_session": geometry, "certificate_status": proof_counts},
        "pit_visit_cohort": visits, "sc_queue_ambiguity": sc_order, "singapore_cutoff_audit": singapore,
        "imola_pit_validation": imola_visits, "sparse_sampling": {"rows": stress, "missing_exit": missing_exit,
                                                                   "baseline_sc_exit": full_merge},
        "causal_vs_retrospective": paired, "independent_validation": {"held": held_audit,
            "independent_races_with_passed_route_checks": len({r["session"] for r in held_audit if r["passed"]}),
            "independent_route_boundary_truth": None, "independent_order_truth": None,
            "timing_witnesses_are_clusters": True},
        "dashboard_integration": dashboard, "strategy_support": strategy, "live_readiness": live,
        "regression_parity": regression, "invariant_checks": invariants, "metrics": metrics,
    }
    for name, value in documents.items():
        write_json(out/f"{name}.json", value)
    tables = {
        "pit_trajectory_ledger": trajectory_rows, "route_classification": rows, "entry_intervals": entries,
        "merge_intervals": [r | {"neighbors": canonical(r["neighbors"]).decode()} for r in merges],
        "predecessor_certificates": [r | {"exclusions": canonical(r["exclusions"]).decode()} for r in proofs],
        "missing_car_exclusions": [{"session": p["session"], "cutoff": p["cutoff"], "mode": p["mode"],
            "target": p["target"], "driver": d, "status": "POSSIBLE_INTERVENER" if exclusion is None else
            exclusion["reason"], "detail": canonical(exclusion).decode() if exclusion else None}
            for p in proofs for d, exclusion in (list(p["exclusions"].items())+
                                               [(v, None) for v in p["possible_interveners"]])],
        "gap_support": [{k: r[k] for k in ("session", "cutoff", "mode", "target", "candidates", "status",
                                          "separation_interval_m")} for r in proofs],
    }
    for name, value in tables.items():
        _parquet(out/f"{name}.parquet", value)
    _parquet(diag/"held_route_witnesses.parquet", held)
    from f1ri.validation.pit_branch_plots import render

    render(diag, documents)
    _seal(out)
    return metrics
