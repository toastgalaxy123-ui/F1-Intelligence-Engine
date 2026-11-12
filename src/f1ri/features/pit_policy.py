"""Prespecified retrospective eligibility; independent of model prediction errors.

Flags are compatible: a red-flag visit can also be an unresolved long stay.
Duration triggers review, never a claim of a repair or penalty cause. Raw
measurements are retained, and screened-unverified is never called confirmed.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PitEligibilityPolicy:
    version: str = "stage4.1-ordinary-v1"
    long_stay_threshold_s: float = 60.0
    allow_screened_unverified: bool = True
    exclude_possible_double_stack: bool = True

    def __post_init__(self):
        if not np.isfinite(self.long_stay_threshold_s) or self.long_stay_threshold_s <= 0:
            raise ValueError("Long-stay review threshold must be positive and finite")

    def to_dict(self) -> dict:
        return asdict(self)


def _json_object(value, default):
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return default
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _finite(value):
    try:
        value = float(value)
        return value if np.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def apply_pit_eligibility(
    events: pd.DataFrame, policy: PitEligibilityPolicy | None = None,
) -> pd.DataFrame:
    policy = policy or PitEligibilityPolicy()
    if events.empty:
        return events.copy()
    output = []
    for original in events.to_dict("records"):
        row = original.copy()
        flags, exclusions = set(), set()
        timing_flags = _json_object(row.get("quality_flags_json"), [])
        lane, stationary = _finite(row.get("lane_duration_s")), _finite(row.get("stop_duration_s"))
        transition = _json_object(row.get("state_overlap_seconds_json"), {})
        lane_states = _json_object(row.get("lane_state_overlap_seconds_json"), {})
        red = float(transition.get("RED", 0) or 0) > 0 or float(lane_states.get("RED", 0) or 0) > 0
        red |= row.get("transition_track_status") == "RED" or row.get("lane_track_status") == "RED"
        if red:
            flags.add("RED_FLAG_OVERLAP")
            exclusions.add("RED_FLAG_OVERLAP")
        if lane is None or lane <= 0:
            exclusions.add("INVALID_LANE_DURATION")
        if lane is not None and lane > policy.long_stay_threshold_s:
            flags.update(("PIT_LANE_LONG_HOLD", "UNRESOLVED_LONG_STAY"))
            exclusions.add("UNRESOLVED_LONG_STAY")
        if stationary is not None and (stationary < 0 or lane is None or stationary > lane):
            exclusions.add("INVALID_STATIONARY_DURATION")
        if timing_flags or row.get("timing_quality") != "B":
            flags.add("AMBIGUOUS_TIMING")
            exclusions.add("AMBIGUOUS_TIMING")
        if row.get("transition_track_status") in {None, "UNKNOWN", "UNCLASSIFIED"}:
            exclusions.add("UNKNOWN_TRACK_STATE")
        kind = str(row.get("pit_event_type", "UNCLASSIFIED"))
        raw_evidence = row.get("classification_evidence")
        evidence = str(raw_evidence).strip() if raw_evidence is not None and pd.notna(raw_evidence) else ""
        confirmed = kind == "ROUTINE_CONFIRMED" or row.get("routine_status") == "ROUTINE_CONFIRMED"
        if confirmed and evidence and evidence != "VERIFIED_STINT_TRANSITION":
            flags.add("ROUTINE_CONFIRMED")
            row["routine_status"] = "ROUTINE_CONFIRMED"
            row["pit_event_type"] = "ROUTINE_TIRE_CHANGE"
        elif kind in {"ROUTINE_TIRE_CHANGE", "ROUTINE_UNVERIFIED"} or row.get("routine_status") == "ROUTINE_UNVERIFIED":
            flags.add("ROUTINE_UNVERIFIED")
            row["routine_status"] = "ROUTINE_UNVERIFIED"
            if not policy.allow_screened_unverified:
                exclusions.add("ROUTINE_NOT_INDEPENDENTLY_CONFIRMED")
        else:
            row["routine_status"] = "NOT_ROUTINE"
            exclusions.add("NON_ROUTINE_OR_UNCLASSIFIED")
        if kind in {"FRONT_WING_REPLACEMENT", "MECHANICAL_REPAIR", "REPAIR_OR_GARAGE"}:
            flags.add("REPAIR_OR_GARAGE")
        elif kind in {"PENALTY_SERVICE", "STOP_GO_PENALTY", "DRIVE_THROUGH"}:
            flags.add("PENALTY_SERVICE")
        elif kind not in {"ROUTINE_TIRE_CHANGE", "ROUTINE_UNVERIFIED", "ROUTINE_CONFIRMED", "RED_FLAG_OVERLAP", "RED_FLAG_EVENT"}:
            flags.add("UNKNOWN_EXCEPTION")
        if red:
            row["pit_event_type"] = "RED_FLAG_OVERLAP"
        elif "UNRESOLVED_LONG_STAY" in flags and kind in {"ROUTINE_TIRE_CHANGE", "ROUTINE_UNVERIFIED"}:
            row["pit_event_type"] = "UNKNOWN_EXCEPTION"
            flags.add("UNKNOWN_EXCEPTION")
        if row.get("possible_double_stack") is True:
            flags.add("POSSIBLE_DOUBLE_STACK")
            if policy.exclude_possible_double_stack:
                exclusions.add("UNRESOLVED_DOUBLE_STACK")
        eligible = not exclusions and row["routine_status"] in {"ROUTINE_CONFIRMED", "ROUTINE_UNVERIFIED"}
        net_exclusions = set(exclusions)
        if row.get("blue_flag_overlap") is True:
            flags.add("ON_TRACK_TRAFFIC_CONTAMINATION")
            net_exclusions.add("ON_TRACK_TRAFFIC_CONTAMINATION")
        if row.get("multiple_pit_transitions") is True:
            flags.add("MULTIPLE_PIT_TRANSITIONS")
            net_exclusions.add("MULTIPLE_PIT_TRANSITIONS")
        if row.get("transition_track_status") != "GREEN":
            net_exclusions.add("NON_GREEN_REFERENCE_REQUIRED")
        row.update(
            classification_flags_json=json.dumps(sorted(flags)),
            eligibility_exclusion_reasons_json=json.dumps(sorted(exclusions)),
            net_label_exclusion_reasons_json=json.dumps(sorted(net_exclusions)),
            classification_policy_version=policy.version,
            long_stay_review_threshold_s=policy.long_stay_threshold_s,
            eligible_direct_lane=eligible,
            eligible_direct_stationary=eligible and stationary is not None and stationary > 0,
            eligible_primary_label=eligible and not net_exclusions,
            eligibility_grade="CONFIRMED" if eligible and confirmed else (
                "SCREENED_UNVERIFIED" if eligible else "EXCLUDED"),
        )
        output.append(row)
    return pd.DataFrame(output, index=events.index)
