"""Cutoff-only census and locked acquisition for clean-to-traffic transitions."""
from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import duckdb
import pandas as pd

from f1ri.ingestion.telemetry import TelemetryWindow
from f1ri.models.contracts import SUPPORT_SUPPORTED
from f1ri.simulator.pace_adapter import UnsupportedPaceForecast, forecast_pace, load_pace_bundle
from f1ri.validation.proximity_pilot import _json, _parquet
from f1ri.validation.traffic_acquisition import ReplaySpan, TrafficWindow, race_inputs
from f1ri.validation.traffic_continuation import load_traffic_protocol
from f1ri.validation.traffic_coverage_pilot import ExtendedWindow, uncovered_requests
from f1ri.validation.traffic_history_audit import physical_history_check, visible_history
from f1ri.validation.traffic_history_plan import _load_cached_slices, _rank, cutoff_candidates
from f1ri.validation.traffic_history_study import _frozen_rows, _sha
from f1ri.validation.traffic_reference import control_pool

KEYS = (9955, 9928)
APPROACH = {"PHYSICAL_APPROACH", "COARSE_INTERVAL_APPROACH"}


def _cached_neighbors(folder):
    with duckdb.connect() as con:
        con.execute("SET TimeZone = 'UTC'")
        frame = con.execute("""
            WITH source AS (SELECT * FROM read_parquet(?)), r AS (
                SELECT * FROM source WHERE mode = 'RETROSPECTIVE'
            )
            SELECT p.*, coalesce(own.route = 'RACING' AND other.route = 'RACING', false) AS route_mask_ok
            FROM source p LEFT JOIN r own ON own."window" = p."window"
                AND own.as_of_utc = p.as_of_utc AND own.driver_number = p.driver_number
            LEFT JOIN r other ON other."window" = p."window"
                AND other.as_of_utc = p.as_of_utc AND other.driver_number = p.ahead
            WHERE p.mode = 'POINT_IN_TIME' ORDER BY p.as_of_utc
        """, [str(folder / "physical_neighbors.parquet")]).df()
    return {int(driver): (tuple(group.as_of_utc), group.to_dict("records"))
            for driver, group in frame.groupby("driver_number")}


def _interval_history(warehouse, key):
    with duckdb.connect(str(warehouse), read_only=True) as con:
        con.execute("SET TimeZone = 'UTC'")
        frame = con.execute("""
            SELECT driver_number, date, min(try_cast(interval_raw AS double)) AS gap
            FROM silver.intervals WHERE session_key = ? AND date IS NOT NULL
            GROUP BY driver_number, date
            HAVING count(DISTINCT interval_raw) = 1 AND min(try_cast(interval_raw AS double)) > 0
            ORDER BY date
        """, [key]).df()
    return {int(driver): (tuple(group.date), tuple(group.gap)) for driver, group in frame.groupby("driver_number")}


def approach_signal(current, earlier, upper_m, previous_upper_m, current_gap, previous_gap):
    """Prioritize physical evidence, using a timing interval only as a coarse seed."""
    if current is not None:
        if (earlier is not None and earlier["ahead"] == current["ahead"]
                and previous_upper_m > 120 and previous_upper_m > upper_m + 10 and upper_m <= 225):
            return "PHYSICAL_APPROACH"
        if current["gap_upper_m"] <= 120:
            return "ALREADY_PHYSICALLY_CLOSE"
    if (current_gap is not None and previous_gap is not None
            and .25 <= current_gap <= 2.2 and previous_gap >= 2.0 and previous_gap - current_gap >= .4):
        return "COARSE_INTERVAL_APPROACH"
    return "NO_USABLE_APPROACH"


def prospective(row):
    return row["d8_status"] == SUPPORT_SUPPORTED and row["approach"] in APPROACH


def _visible_physical(rows, clocks, cutoff, maximum_age=1):
    i = bisect_right(clocks, cutoff) - 1
    if i < 0 or (cutoff - clocks[i]).total_seconds() > maximum_age:
        return None
    row = rows[i]
    if row["coverage"] != "COMPLETE" or pd.isna(row["gap_upper_m"]):
        return None
    return row


def _prior_interval(clocks, values, cutoff):
    i = bisect_right(clocks, cutoff - timedelta(seconds=12)) - 1
    if i < 0 or (cutoff - clocks[i]).total_seconds() > 30:
        return None, None
    return clocks[i], values[i]


def _pace_prediction(bundle, request, samples):
    try:
        forecast = forecast_pace(bundle, replace(request, n_trajectory_samples=samples)).forecast.lap_forecasts[0]
    except UnsupportedPaceForecast:
        return "PACE_V2_UNSUPPORTED", None
    return forecast.support_status, forecast.expected_lap_time_s


def _annotate_relative_pace(rows):
    supported = {}
    for row in rows:
        if row["d8_status"] == SUPPORT_SUPPORTED:
            supported.setdefault(row["driver_number"], []).append(row)
        leader = row["physical_neighbor"]
        prior = next((candidate for candidate in reversed(supported.get(leader, ()))
                      if candidate["cutoff_utc"] <= row["cutoff_utc"]), None)
        if (prior is None or row["expected_clean_s"] is None
                or (row["cutoff_utc"] - prior["cutoff_utc"]).total_seconds() > 90
                or row["physical_relationship"] != "SAME_LAP"):
            row["relative_clean_pace_s"] = None
            row["leader_pace_cutoff_utc"] = None
        else:
            row["relative_clean_pace_s"] = prior["expected_clean_s"] - row["expected_clean_s"]
            row["leader_pace_cutoff_utc"] = prior["cutoff_utc"]


def census_race(warehouse, cohort, config, population, folder, bundle, controls):
    key = cohort["session_key"]
    inputs = race_inputs(warehouse, key)
    geometry = json.loads((folder / "geometry.json").read_text(encoding="utf8"))
    available = pd.Timestamp(geometry["available_at_utc"])
    # Include ineligible states in the census. Prefix/geometry rules are applied
    # below, separately from the actual request reconstruction.
    grid = cutoff_candidates(warehouse, key, inputs["track"], population, available, 0, include_ineligible=True)
    physical = _cached_neighbors(folder)
    intervals = _interval_history(warehouse, key)
    reference = config["reference"]

    def inspect(row):
        driver, lap, at = row["driver_number"], row["lap_number"], row["cutoff_utc"]
        identifier = f"{key}/{driver}/{lap}"
        clocks, prior_values = intervals.get(driver, ((), ()))
        prior_clock, prior_gap = _prior_interval(clocks, prior_values, at)
        clocks, observed = physical.get(driver, ((), ()))
        current = _visible_physical(observed, clocks, at)
        earlier = _visible_physical(observed, clocks, at - timedelta(seconds=3))
        approach = approach_signal(current, earlier, current["gap_upper_m"] if current else None,
                                   earlier["gap_upper_m"] if earlier else None, row["selection_gap_s"], prior_gap)
        request, history, reason = (visible_history(warehouse, cohort, driver, lap, at.to_pydatetime(), audit=True)
                                    if row["state_eligible"] else (None, [], "STATE_OR_GEOMETRY_UNSUPPORTED"))
        clean = request.clean_laps_observed_current_stint if request else None
        d8, predicted, counts, control_ids = None, None, None, None
        history_status = reason
        if request is not None:
            history_status = ("INSUFFICIENT_COMPLETED_HISTORY" if len(history) < 3 else
                              "INSUFFICIENT_CLEAN_HISTORY" if clean < 3 else "QUALIFIED")
            if clean >= 3:
                d8, predicted = _pace_prediction(bundle, request, reference["trajectory_samples"])
                if d8 == SUPPORT_SUPPORTED:
                    matching, counts = control_pool({
                        "opportunity_id": identifier, "as_of_utc": at, "compound": request.compound_relative,
                        "tyre_age": request.tyre_age_at_start, "race_progress": lap / cohort["scheduled_laps"],
                    }, controls, reference)
                    control_ids = [control["opportunity_id"] for control in matching]
        for prior in history:
            first = bisect_left(clocks, prior["started_utc"] - timedelta(seconds=.5))
            last = bisect_right(clocks, min(at, prior["completed_utc"] + timedelta(seconds=.5)))
            evidence = observed[first:last]
            comparison = physical_history_check(evidence, prior["started_utc"], prior["completed_utc"], config)
            state = comparison["physical_history"]
            prior.update(comparison | {"agreement": (
                "INSUFFICIENT_EVIDENCE" if state == "INSUFFICIENT_EVIDENCE" else
                "AGREEMENT" if (state == "CLEAN") == prior["clean_green"] else "DISAGREEMENT"
            )})
        return row | {
            "session_key": key, "opportunity_id": identifier, "history_status": history_status,
            "prior_completed": len(history), "prior_clean": clean, "prior_laps": history,
            "stint_number": request.stint_number if request else None,
            "compound": request.compound_relative if request else None,
            "tire_age": request.tyre_age_at_start if request else None,
            "d8_status": d8, "expected_clean_s": predicted, "control_pool": counts,
            "matched_control_ids": control_ids, "approach": approach,
            "earlier_interval_utc": prior_clock, "earlier_interval_s": prior_gap,
            "physical_neighbor": int(current["ahead"]) if current else None,
            "physical_as_of_utc": current["as_of_utc"] if current else None,
            "physical_gap_m": current["gap_m"] if current else None,
            "physical_gap_lower_m": current["gap_lower_m"] if current else None,
            "physical_gap_upper_m": current["gap_upper_m"] if current else None,
            "physical_relationship": current["encounter"] if current else None,
            "physical_closing_mps": (
                (earlier["gap_m"] - current["gap_m"]) / (current["as_of_utc"] - earlier["as_of_utc"]).total_seconds()
                if current and earlier and current["ahead"] == earlier["ahead"] else None
            ),
        }

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(inspect, grid))
    _annotate_relative_pace(rows)
    return rows


def run_transition_census(root: Path, warehouse: Path, stage3: Path, history: Path, output: Path):
    config, protocol_hash = load_traffic_protocol(root / "config/experiments/stage5_s3_c_protocol.yaml")
    frozen = root / "artifacts/reports/stage5_s3_c_history_metrics.json"
    if _sha(history / "history_study_metrics.json") != _sha(frozen):
        raise ValueError("Transition census requires the frozen history metrics")
    bundle = load_pace_bundle(root, stage3)
    cohorts = {row["session_key"]: row for row in config["cohorts"] if row.get("session_key") in KEYS}
    settings = {"first_lap": 8, "last_lap": 46, "early_phase_last_lap": 24, "maximum_interval_age_s": 8}
    controls = []
    for key in KEYS:
        refs, outcomes = _frozen_rows(history / str(key))
        by_id = {row["opportunity_id"]: row for row in refs}
        controls.extend(row | by_id[row["opportunity_id"]] for row in outcomes
                        if row["outcome_group"] == "CLEAN_CONTROL"
                        and by_id[row["opportunity_id"]]["pace_status"] == SUPPORT_SUPPORTED)
    output.mkdir(parents=True, exist_ok=True)
    summary = {}
    for key in KEYS:
        rows = census_race(warehouse, cohorts[key], config, settings, history / str(key), bundle, controls)
        _json(output / f"candidates_{key}.json", rows)
        flat = [{k: v for k, v in row.items() if k != "prior_laps"} for row in rows]
        _parquet(output / f"candidates_{key}.parquet", flat)
        qualified = [row for row in rows if prospective(row)]
        summary[key] = {"candidates": len(rows), "history": dict(Counter(row["history_status"] for row in rows)),
                        "d8": dict(Counter(row["d8_status"] or "UNATTEMPTED" for row in rows)),
                        "qualified_approach": len(qualified), "approach": dict(Counter(row["approach"] for row in rows)),
                        "qualified_by_phase_stint": dict(Counter(
                            f"{row['phase']}/{row['stint_number']}" for row in rows
                            if row["history_status"] == "QUALIFIED"
                        )),
                        "with_five_controls": sum(len(row["matched_control_ids"]) >= 5 for row in qualified),
                        "driver_stints": len({(row["driver_number"], row["stint_number"]) for row in qualified}),
                        "known_leader_stints": len({(row["driver_number"], row["physical_neighbor"], row["stint_number"])
                                                   for row in qualified if row["physical_neighbor"] is not None}),
                        "ledger_sha256": _sha(output / f"candidates_{key}.json")}
    result = {"version": "s3-c-transition-census-v1", "races": summary, "protocol_sha256": protocol_hash,
              "d8_sha256": bundle.artifact_sha256, "history_sha256": _sha(frozen), "warehouse_sha256": _sha(warehouse)}
    _json(output / "census.json", result)
    return result


def lock_transition_windows(root, warehouse, history, census, plan, output):
    census_summary = json.loads((census / "census.json").read_text(encoding="utf8"))
    if (census_summary["warehouse_sha256"] != _sha(warehouse)
            or census_summary["history_sha256"] != _sha(history / "history_study_metrics.json")):
        raise ValueError("Transition selection inputs differ from the census")
    frozen = json.loads((history / "history_study_metrics.json").read_text(encoding="utf8"))
    if (plan["frozen_protocol_sha256"] != census_summary["protocol_sha256"]
            or [row["session_key"] for row in plan["cohorts"]] != list(KEYS)):
        raise ValueError("Transition plan differs from the frozen development protocol")
    baseline_archive = json.loads((root / "artifacts/reports/stage5_s3_c_acquisition_manifest.json").read_text())
    extension = json.loads((root / "artifacts/reports/stage5_s3_c_coverage_diagnostics.json").read_text())
    archives = [row["sources"] for row in frozen["races"]] + baseline_archive["races"] + [
        extension["development_pilot"]["sources"],
    ]
    width = timedelta(seconds=plan["acquisition"]["window_seconds"])
    prefix = timedelta(seconds=plan["acquisition"]["before_cutoff_seconds"])
    pad = timedelta(seconds=plan["acquisition"]["boundary_overlap_seconds"])
    acquisition = plan["acquisition"]
    if (acquisition["stop"] != "execute_locked_windows_once"
            or not timedelta(0) < prefix < width or not timedelta(0) <= pad < timedelta(seconds=90)
            or any(quota <= 0 for quota in acquisition["arms"].values())):
        raise ValueError("Invalid transition budget or stop condition")
    result = {"census_sha256": _sha(census / "census.json"), "races": {}}
    for key in KEYS:
        source = census / f"candidates_{key}.json"
        if _sha(source) != census_summary["races"][str(key)]["ledger_sha256"]:
            raise ValueError("Candidate ledger hash mismatch")
        rows = json.loads(source.read_text(encoding="utf8"))
        inputs = race_inputs(warehouse, key)
        green = [(row.start_utc, row.end_utc) for row in inputs["track"].itertuples()
                 if row.status == "GREEN" and row.confidence == "HIGH"]
        available = pd.Timestamp(next(row["geometry_available_utc"] for row in frozen["races"]
                                      if row["session_key"] == key))
        cached = _load_cached_slices(root, archives, key)
        transitions = [row for row in rows if prospective(row)]
        controls = [row for row in rows if row["d8_status"] == SUPPORT_SUPPORTED
                    and row["selection_gap_s"] is not None and row["selection_gap_s"] >= 3]

        def control_utility(row, targets=transitions):
            return sum(
                len(target["matched_control_ids"]) < 5 and target["compound"] == row["compound"]
                and abs(target["lap_number"] - row["lap_number"]) <= 14
                and abs(target["tire_age"] - row["tire_age"]) <= 5
                and pd.Timestamp(target["cutoff_utc"]) > pd.Timestamp(row["cutoff_utc"]) + width / 2
                for target in targets
            )

        ordered = {
            "transition": sorted(transitions, key=lambda row: (
                row["approach"] != "PHYSICAL_APPROACH", -len(row["matched_control_ids"]),
                (row.get("relative_clean_pace_s") or 0) <= 0,
                _rank(row, plan["seed"], key, "transition"),
            )),
            "earlier_control": sorted(controls, key=lambda row: (
                -control_utility(row), row["cutoff_utc"], _rank(row, plan["seed"], key, "earlier_control"),
            )),
            "random_green": sorted((row for row in rows if row["state_eligible"]),
                                   key=lambda row: _rank(row, plan["seed"], key, "random_green")),
        }
        windows, seen = [], set()
        used_requests, used_seconds = 0, 0.
        for arm, quota in plan["acquisition"]["arms"].items():
            accepted = 0
            diversity = Counter()
            pool = list(ordered[arm])
            while pool and accepted < quota:
                if arm == "transition" and accepted:
                    i = min(range(len(pool)), key=lambda j: (
                        diversity["phase", pool[j]["phase"]] + diversity["compound", pool[j]["compound"]], j,
                    ))
                else:
                    i = 0
                row = pool.pop(i)
                start = pd.Timestamp(row["cutoff_utc"]).floor("s") - prefix
                end = start + width
                group = (row["driver_number"], row["stint_number"])
                if start < available or not any(a <= start < end <= b for a, b in green):
                    continue
                if arm == "transition" and group in seen:
                    continue
                if any(start < pd.Timestamp(w["end_utc"]) and end > pd.Timestamp(w["start_utc"]) for w in windows):
                    continue
                reuse = tuple(item for item in cached if start < item.end_utc and end > item.start_utc)
                span = ReplaySpan(key, start, end)
                request = uncovered_requests(span, reuse, timedelta(seconds=180) - 2 * pad, pad)
                seconds = sum((w.end_utc - w.start_utc).total_seconds() for w in request)
                requests = len(request) * 2
                if (used_requests + requests > plan["acquisition"]["maximum_field_requests_per_race"]
                        or used_seconds + seconds > plan["acquisition"]["maximum_new_field_seconds_per_race"]):
                    continue
                used_requests += requests
                used_seconds += seconds
                anchor = {name: value for name, value in row.items() if name != "prior_laps"}
                windows.append({"arm": arm, "start_utc": start.isoformat(), "end_utc": end.isoformat(),
                                "anchor": anchor, "reused": [w.bounds() for w in reuse],
                                "requested": [w.bounds() for w in request]})
                if arm == "transition":
                    seen.add(group)
                    diversity["phase", row["phase"]] += 1
                    diversity["compound", row["compound"]] += 1
                accepted += 1
                if accepted == quota:
                    break
        result["races"][str(key)] = {"windows": windows, "field_requests": used_requests,
                                     "field_request_seconds": used_seconds,
                                     "blocks_by_arm": dict(Counter(w["arm"] for w in windows))}
    _json(output, result)
    return result


def locked_extensions(key, locked, plan):
    windows = []
    for row in locked["races"][str(key)]["windows"]:
        anchor = row["anchor"]
        span = ReplaySpan(key, pd.Timestamp(row["start_utc"]), pd.Timestamp(row["end_utc"]))
        replay = TrafficWindow(row["arm"], span, anchor["driver_number"], anchor["lap_number"])
        reused, requested = (tuple(TelemetryWindow(key, None, pd.Timestamp(a), pd.Timestamp(b)) for a, b in row[name])
                             for name in ("reused", "requested"))
        windows.append(ExtendedWindow(replay, anchor["opportunity_id"], reused, requested))
    return windows, {"planned_arm_counts": plan["acquisition"]["arms"], "locked": locked["races"][str(key)]}


def anchor_conversion(results, locked, output, reference):
    from f1ri.validation.traffic_history_audit import parquet_rows

    conversions = []
    for race in results:
        key = race["session_key"]
        folder = output / str(key)
        cutoffs = {r["opportunity_id"]: r for r in parquet_rows(folder / "lap_cutoff_cohort.parquet")}
        outcomes = {r["opportunity_id"]: r for r in parquet_rows(folder / "lap_eligibility_ledger.parquet")
                    if r["screen"] != "LEFT_TRUNCATED_WINDOW"}
        residual_path = folder / "traffic_residuals.parquet"
        residuals = ({r["opportunity_id"]: r for r in parquet_rows(residual_path)}
                     if residual_path.is_file() else {})
        for window in locked["races"][str(key)]["windows"]:
            seed = window["anchor"]
            identity = seed["opportunity_id"]
            cutoff, outcome = cutoffs.get(identity, {}), outcomes.get(identity, {})
            residual = residuals.get(identity, {})
            screen = outcome.get("screen", "CUTOFF_NOT_IN_LEDGER")
            matched = outcome.get("matched_control_count")
            initialized = cutoff.get("cutoff_exposure") not in (None, "UNSUPPORTED")
            leader = cutoff.get("opponent_number")
            stages = {
                "selected": True, "history_qualified": seed["history_status"] == "QUALIFIED",
                "d8_eligible_at_selection": seed["d8_status"] == SUPPORT_SUPPORTED,
                "physical_initialization": initialized,
                "full_target_lap": bool(outcome.get("full_lap")),
                "physical_support": initialized and (
                    (outcome.get("physical_coverage") or 0) >= reference["label_minimum_coverage"]
                ),
                "cutoff_encounter": cutoff.get("cutoff_exposure") == "CLOSE" and leader is not None,
                "continued_following": bool(outcome.get("cutoff_encounter_continued")),
                "event_screen_passed": screen in {"FOLLOWING", "CLEAN_CONTROL", "INTERMEDIATE",
                                                  "PHYSICAL_SUPPORT_BELOW_MINIMUM"},
                "five_earlier_controls": matched is not None and matched >= reference["minimum_matched_controls"],
                "final_target": screen == "FOLLOWING" and outcome.get("signed_proxy_s") is not None,
            }
            failure = (None if stages["final_target"] else
                       screen if screen not in {"FOLLOWING", "CLEAN_CONTROL", "INTERMEDIATE"} else
                       "NO_CONTINUING_CUTOFF_ENCOUNTER" if screen != "FOLLOWING" else
                       outcome.get("pace_status") if outcome.get("pace_status") != SUPPORT_SUPPORTED else
                       "INSUFFICIENT_EARLIER_CONTROLS")
            conversions.append({"session_key": key, "arm": window["arm"], "opportunity_id": identity,
                                "selected_as_of_utc": seed["cutoff_utc"], "selection_approach": seed["approach"],
                                "selected_physical_neighbor": seed["physical_neighbor"],
                                "realized_cutoff_neighbor": leader,
                                "selected_neighbor_agrees": (None if seed["physical_neighbor"] is None or leader is None
                                                             else seed["physical_neighbor"] == leader),
                                "screen": screen, "first_decisive_outcome": failure,
                                "d8_status": outcome.get("pace_status"), "matched_control_count": matched,
                                "physical_coverage": outcome.get("physical_coverage"),
                                "cutoff_episode_id": outcome.get("cutoff_episode_id"),
                                "later_or_different_episode": outcome.get("later_or_different_episode"),
                                "archive_pass_indication": outcome.get("archive_pass_indication"),
                                "pit_or_stint_transition": outcome.get("pit_or_stint_transition"),
                                "matched_control_ids": outcome.get("matched_control_ids"),
                                "expected_clean_s": residual.get("expected_clean_s"),
                                "observed_lap_s": residual.get("observed_lap_s"),
                                "signed_residual_s": outcome.get("signed_residual_s"),
                                "signed_proxy_s": outcome.get("signed_proxy_s"),
                                "stages": stages})
    _json(output / "anchor_conversion.json", conversions)
    return conversions
