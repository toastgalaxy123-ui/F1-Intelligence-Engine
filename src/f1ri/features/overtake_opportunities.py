"""Cutoff-only opportunity selection and interval-censored historical outcomes."""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from hashlib import sha256

from f1ri.features.overtake_events import PASS_TYPES, racing_position, relative_gap, track_segment


def seeded_score(seed, *key):
    digest = sha256("|".join(map(str, (seed, *key))).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def sample_opportunities(session, window, positions, neighbors, geometry, history, config, funnel):
    """Read only the point-in-time layer, including each pair's prior gap."""
    previous = {}
    thresholds = config["gap_thresholds_m"]
    for at, frame in sorted(positions.items()):
        green = history.green_at(at)
        for d, near in sorted(neighbors.get(at, {}).items()):
            funnel["driver_snapshots"] += 1
            other = near.get("ahead")
            if not green or other is None or near.get("encounter") in (None, "LAP_RELATION_UNKNOWN"):
                continue
            other = int(other)
            a, b = frame.get(d, {}), frame.get(other, {})
            if not racing_position(a) or not racing_position(b) or not history.active_at((d, other), at):
                continue
            stamps = [r.get("observation_utc") or r.get("observed_at_utc") for r in (a, b)]
            stamps += [r.get("fallback_speed_utc") for r in (a, b) if r.get("fallback_speed_utc")]
            if any(stamp is None or stamp > at for stamp in stamps):
                funnel["unsafe_or_missing_source_clock"] += 1
                continue
            lower, upper = near["gap_lower_m"], near["gap_upper_m"]
            if lower is None or upper is None or lower <= 0:
                continue
            funnel["physically_supported"] += 1
            pair = d, other
            prior = previous.get(pair)
            previous[pair] = (at, lower, upper, near["gap_m"])
            elapsed = (at - prior[0]).total_seconds() if prior else None
            closing = (prior[3] - near["gap_m"]) / elapsed if elapsed and elapsed <= 2 else None
            closing_supported = closing is not None and prior[1] > upper
            arms = []
            if near["encounter"] == "SAME_LAP":
                if upper <= thresholds[0]:
                    arms.append("A_CLOSE_FOLLOWING")
                elif upper <= thresholds[-1] and closing_supported:
                    arms.append("B_APPROACHING")
            elif upper <= thresholds[-1]:
                arms.append("D_LAPPED")
            if upper <= config["random_reference_max_m"] and seeded_score(
                config["seed"], session, d, other, at.isoformat()
            ) < config["random_reference_fraction"]:
                arms.append("C_RANDOM_GREEN")
            # Keep the prespecified sensitivity population even if no named arm selects it.
            eligible = [m for m in thresholds if upper <= m]
            if not (arms or eligible):
                continue
            funnel["selected_cutoffs"] += 1
            yield {
                "opportunity_id": f"{session}/{d}-{other}/{at.isoformat()}",
                "session": session, "window": window, "attacker": d, "target": other,
                "cutoff_utc": at, "arms": arms, "eligible_gap_thresholds_m": eligible,
                "layout_version": a["layout_version"], "track_state": "GREEN",
                "lap_relation": near["encounter"], "field_coverage": near["coverage"],
                "gap_m": near["gap_m"], "gap_lower_m": lower, "gap_upper_m": upper,
                "gap_approx_s": near["gap_m"] / a["speed_mps"] if (a.get("speed_mps") or 0) > 0 else None,
                "closing_mps": closing, "closing_supported": closing_supported,
                "prior_gap_utc": prior[0] if closing is not None else None,
                "track_segment": track_segment(a["progress_m"], geometry),
                "positions": [a, b], "latest_feature_observation_utc": max(stamps),
                "availability_mode": "ARCHIVE_OBSERVATION_CLOCK", "receipt_utc": None,
                **history.cutoff_context(d, other, at, (a["completed_laps"], b["completed_laps"])),
            }


def group_encounters(opportunities, link_s):
    pairs = defaultdict(list)
    for row in opportunities:
        key = row["session"], *sorted((row["attacker"], row["target"]))
        pairs[key].append(row)
    encounters = []
    for (session, a, b), rows in sorted(pairs.items()):
        last = None
        for row in sorted(rows, key=lambda r: (r["cutoff_utc"], r["opportunity_id"])):
            at = row["cutoff_utc"]
            if last is None or (at - last).total_seconds() > link_s:
                group = {"encounter_id": f"{session}/{a}-{b}/{at.isoformat()}", "session": session,
                         "pair": [a, b], "first_cutoff_utc": at, "opportunities": 0}
                encounters.append(group)
            group["opportunities"] += 1
            group["last_cutoff_utc"] = last = at
            row["encounter_id"] = group["encounter_id"]
    return encounters


def outcome(opportunity, planned, frames, neighbors, times, events, interruptions, period, length):
    """Use only the future outcome layer and keep interval boundary overlap ambiguous."""
    at = opportunity["cutoff_utc"]
    d, target = opportunity["attacker"], opportunity["target"]
    result = {"opportunity_id": opportunity["opportunity_id"], "planned_end_utc": planned,
              "pass_event_id": None, "event_time_lower_utc": None, "event_time_upper_utc": None}
    if planned is None:
        return result | {"outcome": "HORIZON_INCOMPLETE", "reason": "CHECKPOINT_UNAVAILABLE",
                         "observed_until_utc": at, "observed_duration_s": 0., "fully_observed": False}

    def finish(label, when, reason=None, event=None):
        return result | {
            "outcome": label, "reason": reason, "observed_until_utc": when,
            "observed_duration_s": max(0., (when - at).total_seconds()),
            "fully_observed": label in ("PASS_OBSERVED", "NO_PASS_OBSERVED"),
            **({key: event[key] for key in ("pass_event_id", "event_time_lower_utc", "event_time_upper_utc")}
               if event else {}),
        }

    stop, cause = interruptions[0] if interruptions else (planned, None)
    candidates = [e for e in events if e["event_time_upper_utc"] > at and e["event_time_lower_utc"] < stop]
    event = min(candidates, key=lambda e: e["event_time_upper_utc"], default=None)
    cutoff_frame = frames.get(at, {})
    cut_a, cut_b = cutoff_frame.get(d, {}), cutoff_frame.get(target, {})
    if cause and stop <= at:
        return finish(cause, stop, cause)
    if not racing_position(cut_a) or not racing_position(cut_b):
        return finish("CENSORED_LOCALIZATION", at, "PAIR_UNSUPPORTED_AT_CUTOFF")
    cutoff_gap, cutoff_error = relative_gap(cut_a, cut_b, length)
    if cutoff_gap <= cutoff_error or neighbors.get(at, {}).get(d, {}).get("ahead") != target:
        return finish("AMBIGUOUS_OUTCOME", at, "ARCHIVE_DISAGREES_AT_CUTOFF")
    future = times[bisect_right(times, at):bisect_left(times, stop) + 1]
    last = at
    overlap = False
    for tick in future:
        if (tick - last).total_seconds() > period * 1.51:
            return finish("CENSORED_LOCALIZATION", last, "SAMPLING_GAP")
        if cause and tick >= stop:
            break
        a, b = frames[tick].get(d, {}), frames[tick].get(target, {})
        if a.get("route") == "PIT" or b.get("route") == "PIT":
            return finish("CENSORED_PIT", tick, "ARCHIVED_PIT_ROUTE")
        if not racing_position(a) or not racing_position(b):
            return finish("CENSORED_LOCALIZATION", tick, "PAIR_POSITION_UNSUPPORTED")
        gap, error = relative_gap(a, b, length)
        if event and tick > event["event_time_lower_utc"]:
            upper = event["event_time_upper_utc"]
            if tick >= upper or tick >= stop:
                if (event["event_type"] in PASS_TYPES and at <= event["event_time_lower_utc"]
                        and upper <= stop and gap < -error
                        and neighbors.get(tick, {}).get(target, {}).get("ahead") == d):
                    return finish("PASS_OBSERVED", upper, event=event)
                return finish("AMBIGUOUS_OUTCOME", min(tick, stop), "REVERSAL_OR_HORIZON_BOUNDARY", event)
            last = tick
            continue
        if abs(gap) <= error:
            overlap = True
        elif gap < 0:
            return finish("AMBIGUOUS_OUTCOME", min(tick, stop), "UNMATCHED_ORDER_REVERSAL")
        elif neighbors.get(tick, {}).get(d, {}).get("ahead") != target:
            new_ahead = neighbors.get(tick, {}).get(d, {}).get("ahead")
            reason = "OTHER_NEIGHBOR_RESOLVED" if new_ahead is not None else "NEIGHBOR_UNRESOLVED"
            label = "CENSORED_NEIGHBOR_CHANGE" if new_ahead is not None else "CENSORED_LOCALIZATION"
            return finish(label, min(tick, stop), reason)
        last = tick
        if tick >= stop:
            return finish("AMBIGUOUS_OUTCOME" if overlap else "NO_PASS_OBSERVED", stop,
                          "ORDER_OVERLAP_DURING_HORIZON" if overlap else None)
    if cause and (stop - last).total_seconds() <= period * 1.51:
        return finish(cause, stop, cause)
    return finish("HORIZON_INCOMPLETE", last, "ARCHIVE_END_BEFORE_HORIZON")
