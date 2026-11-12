"""Dedicated neutralized reference audit, without claiming verified route geometry."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from f1ri.features.pit_events import status_overlap, utc


def measure_neutralized_pit_windows(
    events: pd.DataFrame, laps: pd.DataFrame, intervals: pd.DataFrame,
) -> pd.DataFrame:
    """Estimate a limited two-lap field proxy for fully VSC/SC transitions.

    This does not turn a whole-lap slowdown into a pit-route discount. It retains
    equal two-lap race progress, time matching, actual state coverage, control IDs
    and dispersion. Shared SC queue effects and target-specific counterfactual
    movement remain unidentified, so anchors_verified is always False.
    """
    rows = []
    for event in events.to_dict("records"):
        state = event.get("transition_track_status")
        overlaps = json.loads(event.get("state_overlap_seconds_json", "{}"))
        if state not in {"VSC", "SC", "MIXED"} and not any(s in overlaps for s in ("VSC", "SC")):
            continue
        base = {"pit_event_id": event["pit_event_id"], "session_key": event["session_key"],
                "circuit_short_name": event["circuit_short_name"], "track_status": state,
                "measurement_method": "CONSTANT_STATUS_MATCHED_TWO_LAP_FIELD_PROXY",
                "anchors_verified": False, "observation_available_utc": event["transition_end_utc"],
                "conditional_elapsed_loss_s": None, "label_std_s": None,
                "n_controls": 0, "controls_json": "[]", "support_status": "UNSUPPORTED",
                "reason": "", "relative_field_positions_modeled": False}
        if state == "MIXED":
            base["reason"] = "MIXED_TIMELINE_REQUIRES_PHASE_MATCHED_REFERENCE"
            rows.append(base)
            continue
        if not event.get("eligible_direct_lane", False):
            base["reason"] = "EVENT_EXCLUDED_BY_ORDINARY_POLICY"
            rows.append(base)
            continue
        target_times = [event.get("observed_in_lap_s"), event.get("observed_out_lap_s")]
        if any(pd.isna(t) or float(t) <= 0 for t in target_times):
            base["reason"] = "INCOMPLETE_TARGET_TWO_LAP_INTERVAL"
            rows.append(base)
            continue
        target_start, target_end = utc(event["transition_start_utc"]), utc(event["transition_end_utc"])
        controls, totals = [], []
        for driver, group in laps.groupby("driver_number"):
            if int(driver) == int(event["driver_number"]):
                continue
            group = group.sort_values("lap_number")
            offset = (pd.to_datetime(group.lap_start_utc, utc=True)-target_start).dt.total_seconds().abs()
            starts = group[offset <= 45].assign(_offset=offset[offset <= 45]).sort_values("_offset")
            for _, first in starts.iterrows():
                pair = group[group.lap_number.isin([int(first.lap_number), int(first.lap_number)+1])]
                if len(pair) != 2 or pair.is_pit_lap.fillna(True).any() or pair.observed_lap_time_s.isna().any():
                    continue
                if "is_blue_flagged_lap" in pair and pair.is_blue_flagged_lap.fillna(False).any():
                    continue
                a, b = utc(pair.iloc[0].lap_start_utc), utc(pair.iloc[1].lap_end_estimate_utc)
                if a is None or b is None or abs((b-target_end).total_seconds()) > 90:
                    continue
                observed_state, seconds = status_overlap(intervals, a, b)
                if observed_state != state:
                    continue
                total = float(pair.observed_lap_time_s.sum())
                if total <= 0:
                    continue
                controls.append({"driver_number": int(driver), "start_lap": int(first.lap_number),
                                 "start_utc": a.isoformat(), "end_utc": b.isoformat(),
                                 "elapsed_s": total, "state_overlap_seconds": json.loads(seconds)})
                totals.append(total)
                break
        base["n_controls"], base["controls_json"] = len(controls), json.dumps(controls, sort_keys=True)
        if len(totals) >= 3:
            reference = float(np.median(totals))
            base["conditional_elapsed_loss_s"] = float(event["observed_in_lap_s"]+event["observed_out_lap_s"]-reference)
            base["label_std_s"] = max(float(1.4826*np.median(np.abs(np.array(totals)-reference))), 3.0)
            base["reason"] = "FIELD_PROXY_ONLY_UNVERIFIED_TARGET_ROUTE_AND_SC_QUEUE"
        else:
            base["reason"] = "INSUFFICIENT_UNINTERRUPTED_SAME_STATE_CONTROLS"
        rows.append(base)
    return pd.DataFrame(rows)
