"""Separate cutoff cohorts, realized screens, and supported pace targets."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
from pathlib import Path

import pandas as pd

from f1ri.models.contracts import SUPPORT_SUPPORTED
from f1ri.validation.proximity_pilot import _parquet

SCREENED = {"FOLLOWING", "CLEAN_CONTROL", "INTERMEDIATE"}


def audited_outcomes(outcomes, audit, inputs):
    """Use completion, next-line and archived censoring evidence clocks for prior controls."""
    accepted = {row["opportunity_id"]: row for row in audit.outcomes if row["screen"] in SCREENED}
    laps = inputs["laps"].sort_values(["driver_number", "lap_number"])
    next_lines = laps.groupby("driver_number").lap_start_utc.shift(-1)
    rival_end = pd.concat([laps.lap_end_estimate_utc, next_lines], axis=1).max(axis=1)
    pits = [(row.date, max(0., float(row.lane_duration)) if pd.notna(row.lane_duration) else 0.)
            for row in inputs["pits"].itertuples()]
    result = []
    for outcome in outcomes:
        record = accepted[outcome["opportunity_id"]]
        start, end = record["as_of_utc"], outcome["completed_utc"]
        overlaps = laps.lap_start_utc.lt(end) & laps.lap_end_estimate_utc.gt(start) & ~laps.is_pit_lap
        other_evidence = rival_end[overlaps].dropna().tolist()
        nearby_pits = [at for at, duration in pits
                       if at + timedelta(seconds=2) >= start and at - timedelta(seconds=duration + 2) <= end]
        ready = max(end, record["next_line_utc"], *other_evidence, *nearby_pits)
        record["outcome_available_by_utc"] = ready
        result.append(outcome | {"outcome_available_by_utc": ready})
    return result


def _units(rows):
    return {
        "unique_laps": len({row["opportunity_id"] for row in rows}),
        "drivers": len({row["driver_number"] for row in rows}),
        "driver_stints": len({(row["driver_number"], row["stint_number"]) for row in rows}),
        "driver_leader_stints": len({(row["driver_number"], row["cutoff_opponent"], row["stint_number"])
                                     for row in rows if row.get("cutoff_opponent") is not None}),
        "races": len({row["session_key"] for row in rows}),
    }


def target_summary(audit, episodes, segments, references, residuals, output: Path) -> dict:
    pace_by_id = {row["opportunity_id"]: row for row in references}
    residual_by_id = {row["opportunity_id"]: row for row in residuals}
    episode_by_id = {row["episode_id"]: row for row in episodes}
    cutoff_by_id = {row["opportunity_id"]: row for row in audit.cutoffs}
    for row in audit.outcomes:
        identity = row["opportunity_id"]
        if row["screen"] == "LEFT_TRUNCATED_WINDOW":
            continue
        if row.get("cutoff_episode_id"):
            row["cutoff_episode_termination"] = episode_by_id[row["cutoff_episode_id"]]["termination"]
        pace = pace_by_id.get(identity, {})
        research = residual_by_id.get(identity, {})
        row.update({
            "pace_status": pace.get("pace_status", "BUNDLE_NOT_REQUESTED" if row["screen"] in SCREENED
                                   else "PHYSICAL_OR_OUTCOME_SCREEN_REJECTED"),
            "prior_clean_lap_numbers": pace.get("prior_clean_lap_numbers"),
            "prior_clean_available_by_utc": pace.get("prior_clean_available_by_utc"),
            "signed_residual_s": research.get("signed_residual_s"),
            "signed_proxy_s": research.get("signed_proxy_s"),
            "matched_control_count": research.get("matched_control_count"),
            "matched_control_ids": research.get("matched_control_ids"),
        })
    _parquet(output / "lap_cutoff_cohort.parquet", audit.cutoffs)
    _parquet(output / "lap_eligibility_ledger.parquet", audit.outcomes)

    def counts(rows, local_episodes, local_segments):
        screened = [row for row in rows if row["screen"] in SCREENED]
        following = [row for row in screened if row["screen"] == "FOLLOWING"]
        supported = [row for row in following if row["pace_status"] == SUPPORT_SUPPORTED]
        proxies = [row for row in supported if row["signed_proxy_s"] is not None]
        pace_supported = [row for row in screened if row["pace_status"] == SUPPORT_SUPPORTED]
        clusters = Counter((row["driver_number"], row["opponent_number"]) for row in local_episodes)
        return {
            "cutoff_cohort": sum(row["screen"] != "LEFT_TRUNCATED_WINDOW" for row in rows),
            "full_laps": sum(row["full_lap"] for row in rows),
            "screens": dict(Counter(row["screen"] for row in rows)),
            "physically_screened": _units(screened), "following": _units(following),
            "supported_following": _units(supported), "following_with_proxy": _units(proxies),
            "supported_d8_all": _units(pace_supported),
            "all_bias_adjusted_proxies": sum(row["signed_proxy_s"] is not None for row in pace_supported),
            "episodes": len(local_episodes), "segments": len(local_segments),
            "episode_terminations": dict(Counter(row["termination"] for row in local_episodes)),
            "fragment_driver_leader_pairs": len(clusters),
            "largest_same_pair_fragment_count": max(clusters.values(), default=0),
            "cutoff_encounter_continued": sum(row.get("cutoff_encounter_continued", False) for row in screened),
            "later_or_different_episode": sum(row.get("later_or_different_episode", False) for row in screened),
        }

    def sliced(column, value):
        rows = [row for row in audit.outcomes if row[column] == value]
        fragments = [row for row in episodes if row[column] == value]
        ids = {row["episode_id"] for row in fragments}
        return counts(rows, fragments, [row for row in segments if row["episode_id"] in ids])

    return counts(audit.outcomes, episodes, segments) | {
        "by_stratum": {kind: sliced("window_kind", kind) for kind in sorted({row["window_kind"] for row in audit.outcomes})},
        "by_window": {name: sliced("window", name) for name in sorted({row["window"] for row in audit.outcomes})},
        "following_evidence": [
            {name: row.get(name) for name in (
                "opportunity_id", "window", "window_kind", "stint_number", "cutoff_opponent", "cutoff_episode_id",
                "cutoff_episode_termination", "pace_status", "prior_clean_lap_numbers", "prior_clean_available_by_utc",
                "outcome_available_by_utc", "matched_control_count", "matched_control_ids", "signed_residual_s",
                "signed_proxy_s",
            )} | {"as_of_utc": cutoff_by_id[row["opportunity_id"]]["as_of_utc"]}
            for row in audit.outcomes if row["screen"] == "FOLLOWING"
        ],
    }
