"""Recover chronologically valid controls for the frozen Austria following lap."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pandas as pd

from f1ri.features.track_geometry import TrackGeometry
from f1ri.validation.traffic_acquisition import ReplaySpan, TrafficWindow
from f1ri.validation.traffic_coverage_pilot import (
    ExtendedWindow,
    acquire_extension,
    uncovered_requests,
)
from f1ri.validation.traffic_exposures import (
    LapEligibilityAudit,
    build_traffic_exposures,
    lap_opportunities,
)
from f1ri.validation.traffic_history_audit import parquet_rows
from f1ri.validation.traffic_history_plan import _load_cached_slices, _rank
from f1ri.validation.traffic_history_study import _sha
from f1ri.validation.traffic_localization import localize_window
from f1ri.validation.traffic_reference import clean_pace_references, residual_research
from f1ri.validation.traffic_target_audit import audited_outcomes

TARGET = "9955/6/47"


def target_control_candidates(warehouse, bundle, inputs, cohort, target, config, frozen_ids):
    """Use the existing chronological censoring clock and D8 prefix for each plausible control."""
    start, end = "lap_start_utc", "lap_end_estimate_utc"
    reference = config["reference"]
    key = target["session_key"]
    ledger, features, synthetic = [], [], []
    audit = LapEligibilityAudit()
    own = inputs["laps"].set_index(["driver_number", "lap_number"])
    for row in inputs["laps"].itertuples(index=False):
        identity = f"{key}/{row.driver_number}/{row.lap_number}"
        next_lap = (row.driver_number, row.lap_number + 1)
        line = own.loc[next_lap, start] if next_lap in own.index else pd.NaT
        elapsed = row.observed_lap_time_s
        entry = {
            "opportunity_id": identity, "session_key": key, "split": "development",
            "driver_number": int(row.driver_number), "lap_number": int(row.lap_number),
            "as_of_utc": getattr(row, start), "completed_utc": getattr(row, end),
            "next_line_utc": line if pd.notna(line) else None, "compound": row.compound,
            "tyre_age": None if pd.isna(row.tyre_age_at_lap_start) else int(row.tyre_age_at_lap_start),
            "race_progress": row.lap_number / cohort["scheduled_laps"],
            "stage1_clean": bool(row.is_clean_green_pace_lap), "stage1_exclusion": row.exclusion_reason,
            "start_gap_s": None if pd.isna(row.track_gap_ahead_at_start_s) else row.track_gap_ahead_at_start_s,
        }
        if pd.isna(entry["completed_utc"]):
            entry["completed_utc"] = None
        reason = (
            "SELF" if identity == TARGET else
            "COMPOUND" if entry["compound"] != target["compound"] else
            "TIRE_AGE" if entry["tyre_age"] is None or abs(entry["tyre_age"] - target["tyre_age"])
            > reference["control_max_tyre_age_delta"] else
            "RACE_PROGRESS" if abs(entry["race_progress"] - target["race_progress"])
            > reference["control_max_progress_delta"] else
            "NOT_COMPLETED_BEFORE_TARGET" if entry["completed_utc"] is None
            or entry["completed_utc"] >= target["as_of_utc"] else
            "TIMING_BOUNDARY_UNSUPPORTED" if pd.isna(line) or pd.isna(elapsed) or elapsed <= 0
            or abs((line - entry["completed_utc"]).total_seconds()) > 2 else
            "GREEN_DRY_OUTCOME_UNSUPPORTED" if row.track_status != "GREEN" or not row.confirmed_green_lap or row.rainfall else
            "PIT_OR_STINT_TRANSITION" if row.is_pit_lap or row.stint_pit_mismatch else
            "ARCHIVE_PASS_INDICATION_CENSORED" if row.had_in_lap_on_track_pass else None
        )
        entry["reason"] = reason
        ledger.append(entry)
        if reason is None:
            audit.outcomes.append(entry | {"screen": "CLEAN_CONTROL"})
            synthetic.append(entry)
    clocks = {row["opportunity_id"]: row["outcome_available_by_utc"]
              for row in audited_outcomes(synthetic, audit, inputs)}
    for row in synthetic:
        identity = row["opportunity_id"]
        row["outcome_available_by_utc"] = clocks[identity]
        if identity in frozen_ids:
            row["reason"] = "FROZEN_MATCH"
        elif clocks[identity] >= target["as_of_utc"]:
            row["reason"] = "EVIDENCE_NOT_BEFORE_TARGET"
        else:
            features.append(row)
    forecasts = clean_pace_references(warehouse, cohort["scheduled_laps"], features, bundle, reference, audit_history=True)
    refs = {row["opportunity_id"]: row for row in forecasts}
    for row in features:
        forecast = refs[row["opportunity_id"]]
        row["pace_status"] = forecast["pace_status"]
        if (forecast["pace_status"] == "SUPPORTED" and
                (forecast["compound"], forecast["tyre_age"]) != (row["compound"], row["tyre_age"])):
            raise ValueError(f"Archived tire context disagrees with D8 prefix for {row['opportunity_id']}")
        row["reason"] = "PHYSICAL_EVALUATION_REQUIRED" if forecast["pace_status"] == "SUPPORTED" else forecast["pace_status"]
        row["expected_clean_s"] = forecast.get("expected_clean_s")
        row["prior_clean_laps"] = forecast.get("prior_clean_lap_numbers")
    return ledger, refs


def recovery_windows(ledger, cached, target, plan, geometry_available, track, *, include_missing=False):
    settings = plan["recovery"]
    eligible = [row for row in ledger if row["reason"] == "PHYSICAL_EVALUATION_REQUIRED"]
    pad = timedelta(seconds=settings["pad_seconds"])
    green = [(r.start_utc, r.end_utc) for r in track.itertuples() if r.status == "GREEN" and r.confidence == "HIGH"]
    rank = lambda row: (not row["stage1_clean"], -(row["start_gap_s"] or 0),
                        _rank(row, plan["seed"], target["session_key"], "recovery"))
    result, seconds, requests = [], 0., 0
    for row in sorted(eligible, key=rank):
        start, end = row["as_of_utc"].floor("s") - pad, max(row["completed_utc"], row["next_line_utc"]).ceil("s") + pad
        if start < geometry_available or end >= target["as_of_utc"] or not any(a <= start < end <= b for a, b in green):
            row["reason"] = "NO_FEASIBLE_PRE_TARGET_WINDOW"
            continue
        span = ReplaySpan(target["session_key"], start, end)
        reused = tuple(w for w in cached if w.start_utc < end and w.end_utc > start)
        missing = uncovered_requests(span, reused, timedelta(seconds=178), timedelta(seconds=1))
        row["missing_spans"] = [w.bounds() for w in missing]
        if bool(missing) != include_missing:
            continue
        if include_missing and (not row["stage1_clean"] or (row["start_gap_s"] or 0) < settings["minimum_coarse_gap_s"]):
            row["reason"] = "UNCACHED_WITHOUT_STRONG_CLEAN_PRIOR"
            continue
        cost = sum((w.end_utc - w.start_utc).total_seconds() for w in missing)
        if (include_missing and (len(result) >= settings["maximum_windows"]
                or seconds + cost > settings["maximum_new_field_seconds"]
                or requests + 2 * len(missing) > settings["maximum_endpoint_requests"]
                or any(start < w.replay.telemetry.end_utc and end > w.replay.telemetry.start_utc for w in result))):
            continue
        result.append(ExtendedWindow(TrafficWindow("recovery", span, row["driver_number"], row["lap_number"]),
                                     row["opportunity_id"], reused, missing))
        seconds += cost
        requests += 2 * len(missing)
    return result


def evaluate_controls(root, windows, ledger, inputs, geometry_info, config, off_track, client=None):
    geometry = TrackGeometry(geometry_info["version"], geometry_info["length_m"],
                             pd.Timestamp(geometry_info["available_at_utc"]),
                             tuple(map(tuple, geometry_info["points_xy"])), geometry_info["reference_end_gap_m"])
    rows = {row["opportunity_id"]: row for row in ledger}
    drivers = tuple(int(n) for n in inputs["drivers"].driver_number)
    period = config["sampling"]["sample_period_seconds"]
    admitted, manifest = [], []
    for item in windows:
        frames, source = acquire_extension(root, item, config["sampling"]["endpoints"], drivers, client)
        manifest.append(source)
        _, neighbors, _, _ = localize_window(
            geometry, item.replay, frames["location"], frames["car_data"], inputs,
            geometry_info["sector_markers_m"], config["reconstruction"], period,
            geometry_info["reference"]["driver"], set(), off_track, marker_laps_per_window=0,
        )
        _, segments = build_traffic_exposures(neighbors, config["exposure"], period)
        audit = LapEligibilityAudit()
        _, outcomes, _ = lap_opportunities(neighbors, segments, inputs["laps"], config, "development", audit=audit)
        outcome = next((r for r in audit.outcomes if r["opportunity_id"] == item.anchor), {})
        row = rows[item.anchor]
        row.update(reason=outcome.get("screen", "CUTOFF_NOT_IN_LEDGER"),
                   physical_coverage=outcome.get("physical_coverage"), cutoff_exposure=outcome.get("cutoff_exposure"))
        if row["reason"] == "CLEAN_CONTROL":
            screened = audited_outcomes(outcomes, audit, inputs)
            admitted.append(next(r for r in screened if r["opportunity_id"] == item.anchor))
    return admitted, manifest


def frozen_control_pool(run, history, baseline, target, config):
    # Supported residual rows retain all fields required by the unchanged estimator.
    from f1ri.validation.traffic_history_study import _deduplicate, _frozen_rows

    frozen = [_frozen_rows(folder) for folder in (
        baseline / "9928", baseline / "coverage_pilot/9928", baseline / "9912", baseline / "9987",
    )]
    diagnostics = json.loads((baseline / "coverage_diagnostics.json").read_text(encoding="utf8"))
    spans = [(pd.Timestamp(r["telemetry"]["start_utc"]), pd.Timestamp(r["telemetry"]["end_utc"]))
             for r in diagnostics["development_pilot"]["sources"]["windows"]]
    keep = {r["opportunity_id"] for r in frozen[0][0]
            if not any(start <= r["as_of_utc"] < end for start, end in spans)}
    frozen[0] = tuple([r for r in rows if r["opportunity_id"] in keep] for rows in frozen[0])
    historical, current = [], []
    history_cutoffs, current_cutoffs = set(), set()
    for key in (9955, 9928):
        historical.append(_frozen_rows(history / str(key)))
        current.append(_frozen_rows(run / str(key)))
        for folder, ids in ((history, history_cutoffs), (run, current_cutoffs)):
            ids.update(r["opportunity_id"] for r in parquet_rows(folder / str(key) / "lap_cutoff_cohort.parquet"))
    prior = [tuple([r for r in rows if r["opportunity_id"] not in excluded] for rows in pair)
             for pairs, excluded in ((frozen, history_cutoffs | current_cutoffs), (historical, current_cutoffs))
             for pair in pairs]
    refs, outcomes = _deduplicate(prior + current)
    recovered = next(r for r in residual_research(refs, outcomes, config["reference"], audit_controls=True)
                     if r["opportunity_id"] == TARGET)
    for field in ("control_pool_attrition", "matched_control_ids", "signed_proxy_s", "signed_residual_s"):
        if recovered[field] != target[field]:
            raise ValueError(f"Frozen control matching differs for {field}")
    return refs, outcomes


def recovery_sources(root, run, history):
    races = []
    for folder in (run, history):
        for key in (9955, 9928):
            path = folder / str(key) / "acquisition.json"
            races.append(json.loads(path.read_text(encoding="utf8")) | {"session_key": key})
    cached = _load_cached_slices(root, races, 9955)
    return cached, {str(Path(s["cache_path"]).as_posix()): _sha(root / s["cache_path"])
                    for r in races if r["session_key"] == 9955 for w in r["windows"] for s in w["sources"]}
