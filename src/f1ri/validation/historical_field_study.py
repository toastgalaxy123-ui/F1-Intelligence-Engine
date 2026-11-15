"""Execute, compare and package the bounded historical field reconstruction."""

from collections import Counter
from datetime import timedelta
from itertools import pairwise
from json import loads
from pathlib import Path
from statistics import median
from zipfile import ZipFile

import duckdb
import pandas as pd

from f1ri.decision_board import canonical, replay
from f1ri.features.historical_field import FieldMode, reconstruct_field, with_sparsity
from f1ri.features.track_geometry import forward_passage_brackets, map_fixes, signed_loop_delta
from f1ri.validation.historical_field_sources import (
    FROZEN,
    INPUT_ARCHIVE,
    S5A,
    S5B,
    S6C,
    checked_members,
    hash_bytes,
    load_pilots,
    stamp,
)

PUBLISHED = "artifacts/reports/stage7_s7_a_dashboard.json"
RELEASE = "PARTIAL_FIELD_SUPPORTED"


def write_json(path, value):
    path.write_bytes(canonical(value)+b"\n")


def _crossings(session, cutoffs):
    """Match telemetry-only line passages against withheld timing anchors."""
    geometry = session.geometry
    seen = set()
    for driver, fixes in sorted(session.fixes.items()):
        if driver not in session.laps:
            continue
        for label, cutoff in cutoffs.items():
            if "negative" in label.lower():
                continue
            points = fixes.window(cutoff-timedelta(seconds=14), cutoff+timedelta(seconds=14))
            trace = map_fixes(geometry, (row.point for row in points if not row.conflict))
            for crossing, left, right in forward_passage_brackets(trace, 0., geometry.length_m):
                crossing_key = driver, left.fix.at_utc, right.fix.at_utc
                if crossing_key in seen:
                    continue
                seen.add(crossing_key)
                if driver in session.pit and session.pit[driver].window(
                    crossing-timedelta(seconds=45), crossing+timedelta(seconds=45)
                ):
                    continue
                anchors = session.laps[driver].window(crossing-timedelta(seconds=5), crossing+timedelta(seconds=5))
                nearest = min(anchors, key=lambda r: abs(r.at-crossing), default=None)
                yield {"session": session.key, "cutoff_label": label, "driver": driver,
                       "projected_crossing_utc": crossing, "timing_anchor_utc": nearest.at if nearest else None,
                       "elapsed_error_s": (crossing-nearest.at).total_seconds() if nearest else None,
                       "matched_within_1_25_s": bool(nearest and abs((crossing-nearest.at).total_seconds()) <= 1.25),
                       "left_fix_utc": left.fix.at_utc, "right_fix_utc": right.fix.at_utc,
                       "reference_role": "INDEPENDENT_CHECK_TIMING_ANCHOR_WITHHELD_FROM_TELEMETRY_PROJECTION"}


def _pits(session, meta):
    """Retain timing reports and observed positions before and after the unresolved branch."""
    entries = [r for r in meta["imola_pit"] if r["kind"] == "PIT_ENTRY"]
    exits = [r for r in meta["imola_pit"] if r["kind"] == "PIT_EXIT"]
    entry_rows, exit_rows = [], []
    detailed = []
    for row in sorted(entries, key=lambda r: (r["utc"], r["driver"])):
        begin = stamp(row["utc"])
        if not session.start <= begin <= session.end:
            continue
        driver = row["driver"]
        ends = [stamp(r["utc"]) for r in exits if r["driver"] == driver and 0 <
                (stamp(r["utc"])-begin).total_seconds() < 90]
        end = min(ends, default=None)
        main = session.geometry
        fixset = session.fixes.get(driver)
        points = fixset.window(begin-timedelta(seconds=18), (end or begin)+timedelta(seconds=18)) if fixset else ()
        xy = []
        for point in points:
            if point.conflict:
                continue
            distances, errors = main.candidates(point.point.x, point.point.y)
            idx = errors.argmin()
            local = float(distances[idx])
            xy.append({"session": session.key, "driver": driver, "timing_entry_utc": begin, "utc": point.point.at_utc,
                       "elapsed_s": (point.point.at_utc-begin).total_seconds(), "x": point.point.x, "y": point.point.y,
                       "projection_m": local, "projection_error_m": float(errors[idx]),
                       "corridor": "PIT_BRANCH_AMBIGUOUS" if min(local, main.length_m-local) <= 650 else
                       "OUTSIDE_PIT_CORRIDOR", "source": ";".join(point.sources)})
        last = max((r["utc"] for r in xy if r["utc"] <= begin and r["projection_error_m"] <= 25
                    and r["corridor"] == "OUTSIDE_PIT_CORRIDOR"), default=None)
        first = min((r["utc"] for r in xy if end and r["utc"] >= end and r["projection_error_m"] <= 25
                     and r["corridor"] == "OUTSIDE_PIT_CORRIDOR"), default=None)
        inside = [r for r in xy if end and begin <= r["utc"] <= end]
        still = [(a["utc"], b["utc"]) for a, b in pairwise(inside)
                 if 0 < (b["utc"]-a["utc"]).total_seconds() <= .9 and
                 abs(a["x"]-b["x"]) <= 6 and abs(a["y"]-b["y"]) <= 6]
        runs = []
        for left, right in still:
            if runs and runs[-1][1] == left:
                runs[-1] = (runs[-1][0], right)
            else:
                runs.append((left, right))
        candidate = max(runs, key=lambda p: p[1]-p[0], default=None)
        stationary = candidate if candidate and (candidate[1]-candidate[0]).total_seconds() >= 1 else None
        post = (reconstruct_field(session, first, drivers=(driver,))["drivers"][0]
                if first and first <= session.end else {})
        entry_rows.append({"session": session.key, "driver": driver, "lane_entry_report_utc": begin,
                           "earlier_loop_compatible_fix_utc": last, "physical_entry_utc": None,
                           "stationary_candidate_start_utc": stationary[0] if stationary else None,
                           "stationary_candidate_end_utc": stationary[1] if stationary else None,
                           "status": "PIT_ROUTE_PARTIAL" if xy else "LANE_TIMING_ONLY",
                           "source": S5A.name})
        exit_rows.append({"session": session.key, "driver": driver, "lane_entry_report_utc": begin,
                          "lane_exit_report_utc": end, "later_loop_compatible_fix_utc": first,
                          "physical_merge_utc": None, "physical_neighbor_at_merge": None,
                          "later_subset_neighbor": (post.get("subset_ahead") or {}).get("driver_number"),
                          "status": "POST_LANE_LOOP_FIX_ONLY" if first else "PHYSICAL_MERGE_UNAVAILABLE",
                          "source": S5A.name})
        if driver == 1 and begin == stamp("2025-05-18T13:42:58.463+00:00"):
            detailed = xy
    return entry_rows, exit_rows, detailed


def _interval_checks(sessions, results):
    for key, rows in results.items():
        causal, retro = rows[FieldMode.CAUSAL], rows[FieldMode.RETROSPECTIVE]
        length = sessions[key[0]].geometry.length_m
        for first, second in zip(causal["drivers"], retro["drivers"], strict=True):
            a, b = first["progress_m"], second["progress_m"]
            yield {"session": key[0], "cutoff_label": key[1], "driver": first["driver_number"],
                   "causal_m": a, "retrospective_m": b,
                   "divergence_m": abs(signed_loop_delta(a, b, length)) if a is not None and b is not None else None,
                   "causal_age_s": first["age_s"], "retrospective_bracket_s": second["bracket_s"],
                   "reference_role": "CORRELATED_REFERENCE_SAME_TELEMETRY"}


def _parquet(path, rows):
    """Use a canonical row order and a single DuckDB writer."""
    with duckdb.connect(config={"threads": 1}) as db:
        table = pd.DataFrame(rows)
        db.register("evidence", table)
        db.table("evidence").write_parquet(str(path), compression="zstd")


def _seal(out):
    files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    (out / "SHA256SUMS").write_bytes("".join(
        f"{hash_bytes(p.read_bytes())}  {p.relative_to(out).as_posix()}\n" for p in files
    ).encode())


def run_study(root: Path, out: Path, *, frozen_run: Path | None = None):
    sessions, meta = load_pilots(root)
    for archive in (INPUT_ARCHIVE, S5A, S5B, S6C):
        checked_members(root / archive, FROZEN[archive])
    with ZipFile(root / S6C) as archive:
        s6_regression = loads(archive.read("regression_parity.json"))
        sample = replay(root, archive.read("diagnostics/neutralization_replay.zip"))
    model_expected = "f98e8444a5982dd482a29444ce2c06848a39829cc248cb6cfceb760b59eae16a"
    if sample["snapshot"]["model"]["value"]["sha256"] != model_expected:
        raise ValueError("Frozen D8R1 provenance differs.")
    stable = s6_regression["exact_s6_c_reproduction"]
    if frozen_run and any(hash_bytes((frozen_run / p).read_bytes()) != h for p, h in stable.items()):
        raise ValueError("Frozen S6-C study output parity differs.")
    cutoffs = {key: {label: stamp(at) for label, at in meta["protocol"]["pilots"][str(key)]["cutoffs"].items()}
               for key in sessions}
    results = {(key, label): {mode: reconstruct_field(session, at, mode) for mode in FieldMode}
               for key, session in sessions.items() for label, at in cutoffs[key].items()}
    positions = [row | {"session": key, "cutoff_label": label, "cutoff": record["cutoff"], "mode": mode}
                 for (key, label), variants in results.items() for mode, record in variants.items()
                 for row in record["drivers"]]
    checks = [r for key, session in sessions.items() for r in _crossings(session, cutoffs[key])]
    paired = list(_interval_checks(sessions, results))
    entries, exits, pit_trace = _pits(sessions[9987], meta)
    cadence = [{"session": key, "driver": driver, "step_s": dt, "gap": dt > 1.} for key, s in sessions.items()
               for driver, series in sorted(s.fixes.items()) for a, b in pairwise(series.times)
               if (dt := (b-a).total_seconds()) > 0]
    sgp_exact = results[9896, "Archived model cutoff"][FieldMode.CAUSAL]
    negatives = results[9896, "Sparse negative control"]
    sparse = {f"{key}:{label}": reconstruct_field(with_sparsity(sessions[key], 8), record[FieldMode.CAUSAL]["cutoff"])[
        "supported_positions"] for (key, label), record in results.items()}
    causal = [r for r in positions if r["mode"] == FieldMode.CAUSAL]
    invariant = {
        "event_time_prefix": all(all(r[key] is None or r[key] <= r["cutoff"] for key in (
            "maximum_fix_used_utc", "lap_anchor_utc", "classification_utc", "pit_timing_utc")) for r in causal),
        "negative_control_no_positions": negatives[FieldMode.CAUSAL]["supported_positions"] == 0,
        "receipt_blocked": all(not r[FieldMode.RECEIPT]["drivers"] for r in results.values()),
        "no_strategy_or_tyre_invention": all(not snap["inventory"] and
            snap["strategy_support"] == "HISTORICAL_STRATEGY_UNSUPPORTED" for rows in results.values() for snap in rows.values()),
        "field_neighbor_requires_complete_roster": all(r["predecessor"] is None or r["ahead_status"] ==
                                                       "PREDECESSOR_SUPPORTED" for r in positions),
        "no_exact_merge_claims": all(row["physical_merge_utc"] is None for row in exits),
        "frozen_s6c_replay": True,
    }
    if not all(invariant.values()):
        raise ValueError(f"S7-A invariant failed: {invariant}")
    out.mkdir(parents=True, exist_ok=False)
    (out / "diagnostics").mkdir()

    def write(name, obj):
        write_json(out / f"{name}.json", obj)

    coverage = []
    for (key, label), variants in results.items():
        for mode in (FieldMode.CAUSAL, FieldMode.RETROSPECTIVE):
            row = variants[mode]
            coverage.append({"session": key, "cutoff_label": label, "mode": mode, "positions": row["supported_positions"],
                             "lap_unwrapped": sum(r["unwrapped_m"] is not None for r in row["drivers"]),
                             "mapped_subset_neighbors": sum(r["subset_ahead"] is not None for r in row["drivers"]),
                             "physical_predecessors": row["supported_predecessors"], "roster": len(row["field_roster"]),
                             "support": row["support"], "status": row["track_status"],
                             "excluded": dict(Counter(r["reason"] for r in row["drivers"] if r["reason"]))})
    availability = {key: {endpoint: sum(r["records"] for r in meta["sources"] if
                         r["session"] == key and r["endpoint"] == endpoint)
                         for endpoint in ("sessions","drivers","laps","position","location","intervals","pit",
                                          "stints","race_control","weather","car_data","session_result")}
                    for key in sessions}
    # These S5 archive subsets are inventoried separately from the raw endpoint snapshots.
    availability[9987]["race_control_archive"] = len(sessions[9987].control.rows)
    availability[9987]["pit_lane_timing_archive"] = len(meta["imola_pit"])
    geometry = {key: {"version": s.geometry.version, "length_m": s.geometry.length_m,
                      "available_at_event_utc": s.geometry.available_at_utc, "source": s.layout_source,
                      "line_origin": "TIMING_ANCHORED_TELEMETRY_REFERENCE",
                      "closure_gap_m": s.geometry.reference_lap_end_gap_m,
                      "coordinates": "OPENF1_ARBITRARY_CARTESIAN_XY",
                      "direction": "CHRONOLOGICAL_REFERENCE_LAP",
                      "pit_entry_geometry": None, "pit_exit_geometry": None, "physical_merge_geometry": None,
                      "guard_m_each_side_of_line": 650} for key, s in sessions.items()}
    independent = {}
    for key in sessions:
        rows = [r for r in checks if r["session"] == key]
        errors = [abs(r["elapsed_error_s"]) for r in rows if r["elapsed_error_s"] is not None]
        independent[key] = {"race": sessions[key].race, "projected_passages": len(rows), "matched": len(errors),
                            "median_abs_line_error_s": median(errors) if errors else None,
                            "mismatch_or_missing": sum(not r["matched_within_1_25_s"] for r in rows),
                            "independent_distance_truth": None, "independent_predecessor_truth": None,
                            "independent_merge_truth": None}
    gates = {"release": "LIVE_UNSUPPORTED", "historical_source_coverage": "BOUNDED_PILOTS_VERIFIED",
             "event_time_causal": "PARTIAL_FIELD_SUPPORTED", "retrospective": "PARTIAL_FIELD_SUPPORTED",
             "publication_time": "UNKNOWN", "receipt_order": "UNKNOWN", "freshness": "UNVERIFIED",
             "revisions": "BOUNDED_OVERLAP_AUDITED", "outage_recovery": "UNVERIFIED",
             "remaining": ["Receipt and publication clocks", "Freshness, revision and outage replay",
                           "Verified pit branch geometry and occupancy", "Independent order and gap reference"]}
    pair_coverage = {key: {"aligned_driver_cutoffs": sum(r["session"] == key and r["divergence_m"] is not None for r in paired),
                           "median_divergence_m": median(vals) if (vals := [r["divergence_m"] for r in paired
                                                          if r["session"] == key and r["divergence_m"] is not None]) else None}
                     for key in sessions}
    source_summary = {"sources": meta["sources"], "requests": meta["acquisition"], "hashes": meta["frozen"],
                      "duplicates": meta["duplicate_audit"]}
    dashboard = {"version": meta["protocol"]["version"], "release": RELEASE, "live_readiness": gates,
                 "geometry": geometry, "sources": source_summary, "coverage": coverage,
                 "snapshots": {str(key): {label: results[key, label] for label in cutoffs[key]} for key in sessions},
                 "pit_entry": entries, "pit_exit": exits, "pit_trace": pit_trace,
                 "neutralization": sessions[9987].release_events}
    write("protocol", meta["protocol"])
    write("source_manifest", source_summary)
    write("session_coverage", coverage)
    write("geometry_manifest", geometry)
    write("clock_conventions", {"normalized": "UTC", "source_original_timestamp_retained": True,
                                "location": "Source ISO UTC; commonly millisecond precision, accuracy unknown",
                                "lap": "Approximate lap-start anchor, timing stream",
                                "race_control": "Reported chronology, effective and receipt times unverified",
                                "availability": "EVENT_TIME_ONLY", "retrieval_is_receipt": False})
    write("source_availability", availability)
    write("neutralization_field_validation", {"episodes": sessions[9987].release_events,
        "samples": [r for r in coverage if r["session"] == 9987], "independent_episode_count": 2,
        "physical_queue_validated": False, "restart_dynamics_calibrated": False})
    write("retrospective_vs_causal", {"by_session": pair_coverage, "rows": paired})
    write("independent_checks", {"by_session": independent, "lap_line_rows": checks,
          "construction": ["location", "lap counts from timing", "sampled loop"],
          "correlated": ["retrospective vs causal location", "archived timing intervals and classification"],
          "unavailable_truth": ["Exact track distance", "Complete local order", "Entry and merge coordinates"]})
    write("negative_controls", {"sparse_cutoff": negatives, "one_in_eight_fix_retention_positions": sparse})
    write("cutoff_snapshot_support", {"release": RELEASE, "coverage": coverage})
    write("dashboard_integration", dashboard)
    write("live_readiness", gates)
    write("regression_parity", {"frozen": meta["frozen"], "s6_c_replay": "BYTEWISE_CANONICAL_RESULT",
                               "s6_c_full_study": stable if frozen_run else None, "model_sha256": model_expected})
    write("invariant_checks", invariant)
    _parquet(out / "position_quality.parquet", [{k: r.get(k) for k in (
        "session", "cutoff_label", "mode", "driver_number", "age_s", "reason", "quality", "stationary_trace")}
        for r in positions])
    _parquet(out / "track_projection.parquet", [{k: r.get(k) for k in (
        "session", "cutoff_label", "mode", "driver_number", "progress_m", "mapped_candidate_m", "projection_error_m",
        "uncertainty_m", "location_utc", "location_original", "maximum_fix_used_utc", "bracket_s", "speed_mps")}
        for r in positions])
    _parquet(out / "lap_progress.parquet", [{k: r.get(k) for k in (
        "session", "cutoff_label", "mode", "driver_number", "completed_laps", "lap_anchor_utc", "lap_aligned",
        "progress_m", "unwrapped_m")} for r in positions])
    _parquet(out / "field_snapshots.parquet", [{k: v for k, v in r.items() if k not in ("subset_ahead", "subset_behind")}
                                              for r in positions])
    _parquet(out / "predecessor_support.parquet", [{
        "session": r["session"], "cutoff_label": r["cutoff_label"], "mode": r["mode"], "driver": r["driver_number"],
        "predecessor": r["predecessor"], "successor": r["successor"], "status": r["ahead_status"],
        "subset_ahead": (r["subset_ahead"] or {}).get("driver_number"),
        "subset_behind": (r["subset_behind"] or {}).get("driver_number"),
    } for r in positions])
    _parquet(out / "gap_validation.parquet", [{
        "session": r["session"], "cutoff_label": r["cutoff_label"], "mode": r["mode"], "driver": r["driver_number"],
        "observation_age_s": r["age_s"], "semantics": r["gap_semantics"], "scope": r["ahead_status"],
        "gap_m": (r["subset_ahead"] or {}).get("gap_m"), "lower_m": (r["subset_ahead"] or {}).get("lower_m"),
        "upper_m": (r["subset_ahead"] or {}).get("upper_m"), "truth_error_m": None,
    } for r in positions])
    _parquet(out / "pit_entry_reconstruction.parquet", entries)
    _parquet(out / "pit_rejoin_reconstruction.parquet", exits)
    _parquet(out / "diagnostics" / "pit_location_trace.parquet", pit_trace)
    _parquet(out / "diagnostics" / "location_cadence.parquet", cadence)
    metrics = {"release": RELEASE, "races": 2, "independent_episodes": 2, "cutoffs": len(results),
               "singapore_exact_cutoff": {"positions": sgp_exact["supported_positions"], "roster": 20,
                                          "unique_predecessors": sgp_exact["supported_predecessors"]},
               "pit_lane_timing_visits": len(entries), "physical_merges_validated": 0,
               "line_checks": independent, "receipt_qualified": False, "invariants": invariant}
    write("metrics", metrics)
    from f1ri.validation.historical_field_plots import render

    render(out / "diagnostics", sessions, coverage, positions, cadence, checks, paired, pit_trace, sparse, metrics)
    _seal(out)
    return metrics
