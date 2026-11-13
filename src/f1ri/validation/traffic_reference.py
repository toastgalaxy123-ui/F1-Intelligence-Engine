"""Compute auditable Pace V2 references and evidence gates for Traffic V1."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from pathlib import Path
from statistics import mean, median

from f1ri.features.race_state import build_field_snapshot
from f1ri.models.contracts import SUPPORT_SUPPORTED
from f1ri.simulator.pace_adapter import PaceBundle, UnsupportedPaceForecast, forecast_pace


def clean_pace_references(
    warehouse: Path, scheduled_laps: int, features: list[dict], bundle: PaceBundle, config: dict,
    *, audit_history: bool = False,
) -> list[dict]:
    rows = []
    for feature in features:
        key, cutoff = feature["session_key"], feature["as_of_utc"]
        base = {"opportunity_id": feature["opportunity_id"], "as_of_utc": cutoff,
                "session_key": key, "split": feature["split"],
                "pace_artifact_sha256": bundle.artifact_sha256}
        if (key in bundle.training_sessions + bundle.calibration_sessions
                or cutoff <= max(bundle.trained_through_utc, bundle.calibrated_through_utc)):
            rows.append(base | {"pace_status": "PACE_REFERENCE_NOT_OUT_OF_SAMPLE"})
            continue
        history = {} if audit_history else None
        field = build_field_snapshot(warehouse, key, cutoff.to_pydatetime(), scheduled_laps, 1,
                                     clean_history_audit=history)
        subject = next(driver for driver in field.drivers if driver.driver_number == feature["driver_number"])
        request = subject.request
        if request is None or request.planned_start_lap != feature["lap_number"]:
            rows.append(base | {"pace_status": subject.support_reason or "PACE_LAP_ALIGNMENT_UNSUPPORTED"})
            continue
        if audit_history:
            evidence = history.get(subject.driver_number, [])
            base |= {"stint_number": request.stint_number,
                     "prior_clean_lap_numbers": [row["lap_number"] for row in evidence],
                     "prior_clean_available_by_utc": [row["available_by_utc"] for row in evidence]}
        if request.clean_laps_observed_current_stint < config["minimum_prior_clean_stint_laps"]:
            rows.append(base | {"pace_status": "INSUFFICIENT_PRIOR_CLEAN_STINT_LAPS"})
            continue
        try:
            batch = forecast_pace(bundle, replace(request, n_trajectory_samples=config["trajectory_samples"]))
        except UnsupportedPaceForecast:
            rows.append(base | {"pace_status": "PACE_V2_UNSUPPORTED"})
            continue
        forecast = batch.forecast.lap_forecasts[0]
        if forecast.support_status != SUPPORT_SUPPORTED:
            rows.append(base | {"pace_status": forecast.support_status})
            continue
        rows.append(base | {
            "pace_status": SUPPORT_SUPPORTED, "expected_clean_s": forecast.expected_lap_time_s,
            "clean_std_s": forecast.std_lap_time_s, "compound": request.compound_relative,
            "tyre_age": request.tyre_age_at_start, "prior_clean_stint_laps": request.clean_laps_observed_current_stint,
            "circuit_seen_in_training": batch.circuit_seen_in_training,
            "race_progress": request.planned_start_lap / scheduled_laps,
        })
    return rows


def control_pool(row, controls, config):
    """Count each ordered eligibility filter before matching residuals."""
    candidates = [control for control in controls if control["split"] == "development"]
    counts = {"supported_clean_controls": len(controls), "development": len(candidates)}
    filters = (
        ("distinct_lap", lambda c: c["opportunity_id"] != row["opportunity_id"]),
        ("completed_before", lambda c: c["completed_utc"] < row["as_of_utc"]),
        ("evidence_before", lambda c: c.get("outcome_available_by_utc", c["completed_utc"]) < row["as_of_utc"]),
        ("compound", lambda c: c["compound"] == row["compound"]),
        ("tire_age", lambda c: abs(c["tyre_age"] - row["tyre_age"]) <= config["control_max_tyre_age_delta"]),
        ("race_progress", lambda c: abs(c["race_progress"] - row["race_progress"])
         <= config["control_max_progress_delta"]),
    )
    for name, eligible in filters:
        candidates = [control for control in candidates if eligible(control)]
        counts[name] = len(candidates)
    return candidates, counts


def residual_research(references: list[dict], outcomes: list[dict], config: dict, *,
                      audit_controls: bool = False) -> list[dict]:
    """Join elapsed outcomes after forecasting and match only completed development controls."""
    by_id = {row["opportunity_id"]: row for row in outcomes}
    if len(by_id) != len(outcomes) or len({row["opportunity_id"] for row in references}) != len(references):
        raise ValueError("Traffic residuals require unique session, driver and lap keys")
    rows = []
    for ref in references:
        if ref["pace_status"] != SUPPORT_SUPPORTED:
            continue
        outcome = by_id[ref["opportunity_id"]]
        if outcome["completed_utc"] <= ref["as_of_utc"]:
            raise ValueError("Reference must precede its elapsed-time outcome")
        rows.append(outcome | ref | {"signed_residual_s": outcome["observed_lap_s"] - ref["expected_clean_s"]})
    controls = [row for row in rows if row["outcome_group"] == "CLEAN_CONTROL"]
    for row in rows:
        matched, attrition = control_pool(row, controls, config)
        if audit_controls:
            row["control_pool_attrition"] = attrition
        bias = (median(control["signed_residual_s"] for control in matched)
                if len(matched) >= config["minimum_matched_controls"] else None)
        row["matched_control_count"] = len(matched)
        if "outcome_available_by_utc" in row:
            row["matched_control_ids"] = [control["opportunity_id"] for control in matched]
        row["clean_control_bias_s"] = bias
        row["signed_proxy_s"] = row["signed_residual_s"] - bias if bias is not None else None
    return rows


def residual_summary(rows: list[dict]) -> dict:
    summary = {}
    for kind in ("FOLLOWING", "CLEAN_CONTROL", "INTERMEDIATE"):
        group = [row for row in rows if row["outcome_group"] == kind]
        residuals = [row["signed_residual_s"] for row in group]
        summary[kind] = {"laps": len(group),
                         "t0_mae_s": mean(map(abs, residuals)) if group else None,
                         "t0_bias_s": mean(residuals) if group else None,
                         "bias_adjusted_proxies": sum(row["signed_proxy_s"] is not None for row in group)}
    return summary


INDEPENDENCE_RULE = "driver-leader-stint-v1"


def encounter_units(rows):
    following = [row for row in rows if row.get("outcome_group", row.get("screen")) == "FOLLOWING"]
    groups = {(row["session_key"], row["driver_number"], row["cutoff_opponent"], row["stint_number"])
              for row in following if row.get("cutoff_episode_id") and row.get("cutoff_opponent") is not None
              and row.get("stint_number") is not None}
    return {"unique_laps": len({row["opportunity_id"] for row in following}),
            "cutoff_encounters": len({row["cutoff_episode_id"] for row in following if row.get("cutoff_episode_id")}),
            "fragments": len({episode for row in following for episode in row.get("episode_ids", ())}),
            "driver_leader_stints": len(groups), "independent_encounters": len(groups)}


def traffic_readiness(races: list[dict], research_rows: list[dict], evaluation: dict, *,
                      independence_rule: str | None = None) -> dict:
    counts = Counter((row["session_key"], row["outcome_group"]) for row in research_rows
                     if row["signed_proxy_s"] is not None)
    train_episodes = {}
    for row in research_rows:
        if row["split"] == "development" and row["outcome_group"] == "FOLLOWING" and row["signed_proxy_s"] is not None:
            train_episodes.setdefault(row["session_key"], set()).update(row["episode_ids"])
    independent = None
    if independence_rule is not None:
        if independence_rule != INDEPENDENCE_RULE:
            raise ValueError("Unknown traffic independence rule")
        independent = {race["session_key"]: encounter_units([
            row for row in research_rows if row["session_key"] == race["session_key"]
            and row["signed_proxy_s"] is not None
        ]) for race in races}
    eligible_train = [
        race["session_key"] for race in races if race["split"] == "development"
        and race["timing_marker_gate"]
        and counts[race["session_key"], "CLEAN_CONTROL"] >= evaluation["minimum_control_laps_per_train_race"]
        and counts[race["session_key"], "FOLLOWING"] >= evaluation["minimum_following_laps_per_train_race"]
        and (independent[race["session_key"]]["independent_encounters"] if independent is not None
             else len(train_episodes.get(race["session_key"], ()))) >= evaluation["minimum_following_laps_per_train_race"]
    ]
    validation = [race["session_key"] for race in races if race["split"] == "validation"
                  and race["timing_marker_gate"]
                  and counts[race["session_key"], "CLEAN_CONTROL"] > 0
                  and counts[race["session_key"], "FOLLOWING"] > 0]
    blockers = []
    if len(eligible_train) < evaluation["minimum_train_races"]:
        blockers.append("INSUFFICIENT_DEVELOPMENT_RACES_WITH_MATCHED_CONTROL_AND_FOLLOWING_TARGETS")
    if len(validation) < evaluation["minimum_validation_races"]:
        blockers.append("INSUFFICIENT_CHRONOLOGICAL_VALIDATION_RACES")
    result = {"eligible_development_races": eligible_train, "eligible_validation_races": validation,
            "reserved_races_evaluated": 0, "blockers": blockers,
            "empirical_fit_status": "GATED" if blockers else "BASELINE_COMPARISON_READY",
            "comparison_status": "PENDING_COMMON_T0_T1_T2_T3_EVALUATION",
            "operational_baseline": "T1_DETERMINISTIC_NO_PASS"}
    if independent is not None:
        result |= {"independence_rule": independence_rule, "qualifying_encounters_by_race": independent,
                   "minimum_reserved_test_races": evaluation["minimum_reserved_test_races"]}
    return result
