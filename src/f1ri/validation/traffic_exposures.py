"""Build point-in-time exposure observations and separately screen lap outcomes."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from itertools import groupby

import pandas as pd

from f1ri.features.physical_positions import LocalizationMode

_LAP_RELATIONS = {"SAME_LAP", "LAPPING", "BEING_LAPPED"}


@dataclass(slots=True)
class LapEligibilityAudit:
    cutoffs: list[dict] = field(default_factory=list)
    outcomes: list[dict] = field(default_factory=list)


def exposure_state(row: dict, config: dict) -> str:
    if (row["track_state"] != "GREEN" or row["coverage"] != "COMPLETE"
            or row["route"] != "RACING" or row["encounter"] not in _LAP_RELATIONS):
        return "UNSUPPORTED"
    if row["gap_upper_m"] <= config["entry_upper_m"]:
        return "CLOSE"
    if row["gap_lower_m"] >= config["clean_lower_m"]:
        return "CLEAR"
    return "INTERMEDIATE"


def build_traffic_exposures(neighbors: list[dict], config: dict, period: float) -> tuple[list[dict], list[dict]]:
    """Use cutoff-local state for features and finalize episode metadata afterwards."""
    visible = [row for row in neighbors if row["mode"] == LocalizationMode.POINT_IN_TIME]
    routes = {(row["window"], row["as_of_utc"], row["driver_number"]): row["route"] for row in visible}
    episodes, segments = [], []
    key = lambda row: (row["session_key"], row["window"], row["driver_number"])
    ordered = sorted(visible, key=lambda row: (*key(row), row["as_of_utc"]))
    for _, stream in groupby(ordered, key=key):
        active, exit_candidates = [], []

        def close(reason, active=active, exit_candidates=exit_candidates):
            if not active:
                return
            first, last = active[0], active[-1]
            duration = (last["as_of_utc"] - first["as_of_utc"]).total_seconds()
            if duration >= config["minimum_duration_s"]:
                episode_id = "/".join(str(value) for value in (
                    config["definition"], first["session_key"], first["driver_number"], first["ahead"],
                    first["as_of_utc"].isoformat(),
                ))
                episodes.append({
                    "episode_id": episode_id, "definition": config["definition"], "session_key": first["session_key"],
                    "window": first["window"], "window_kind": first["window_kind"],
                    "layout_version": first["layout_version"], "driver_number": first["driver_number"],
                    "opponent_number": first["ahead"], "encounter": first["encounter"],
                    "start_utc": first["as_of_utc"], "end_utc": last["as_of_utc"],
                    "duration_s": duration, "termination": reason, "samples": len(active),
                    "initial_gap_m": first["gap_m"], "minimum_gap_m": min(row["gap_m"] for row in active),
                })
                previous = None
                for row in active:
                    segments.append({
                        "episode_id": episode_id, "session_key": row["session_key"], "window": row["window"],
                        "driver_number": row["driver_number"], "as_of_utc": row["as_of_utc"],
                        "opponent_number": row["ahead"], "encounter": row["encounter"],
                        "gap_m": row["gap_m"], "gap_lower_m": row["gap_lower_m"],
                        "gap_upper_m": row["gap_upper_m"], "speed_mps": row["speed_mps"],
                        "source_age_s": row["source_age_s"], "completed_laps": row["completed_laps"],
                        "closing_mps": (previous["gap_m"] - row["gap_m"]) / period if previous else None,
                    })
                    previous = row
            active.clear()
            exit_candidates.clear()

        for row in stream:
            state = exposure_state(row, config)
            if active:
                last = (exit_candidates or active)[-1]
                old_route = routes.get((row["window"], row["as_of_utc"], last["ahead"]))
                if row["track_state"] != "GREEN":
                    close("NON_GREEN")
                elif row["route"] == "PIT" or old_route == "PIT":
                    close("PIT_ROUTE")
                elif (row["as_of_utc"] - last["as_of_utc"]).total_seconds() != period:
                    close("MISSING_SAMPLE")
                elif state == "UNSUPPORTED":
                    close("LOCALIZATION_UNSUPPORTED")
                elif row["ahead"] != last["ahead"]:
                    close("NEIGHBOR_CHANGED")
                elif row["encounter"] != last["encounter"]:
                    close("LAP_RELATION_CHANGED")
            if active:
                if row["gap_upper_m"] > config["exit_upper_m"]:
                    exit_candidates.append(row)
                    if len(exit_candidates) == config["exit_confirmation_samples"]:
                        close("GAP_OPENED")
                else:
                    active.extend(exit_candidates)
                    exit_candidates.clear()
                    active.append(row)
            elif state == "CLOSE":
                active.append(row)
        close("WINDOW_ENDED")
    return episodes, segments


def lap_opportunities(
    neighbors: list[dict], segments: list[dict], laps: pd.DataFrame, config: dict, split: str,
    *, audit: LapEligibilityAudit | None = None,
) -> tuple[list[dict], list[dict], dict]:
    """Expose the forecast input at lap start and hold elapsed-time outcomes separately."""
    exposure, reference = config["exposure"], config["reference"]
    period = config["sampling"]["sample_period_seconds"]
    period_delta = timedelta(seconds=period)
    clocks: dict[tuple, list] = {}
    streams: dict[tuple, list] = {}
    retro = {}
    for row in neighbors:
        key = (row["window"], row["driver_number"])
        if row["mode"] == LocalizationMode.POINT_IN_TIME:
            streams.setdefault(key, []).append(row)
            clocks.setdefault(key, []).append(row["as_of_utc"])
        else:
            retro[(*key, row["as_of_utc"])] = row
    by_driver = {int(driver): frame.set_index("lap_number") for driver, frame in laps.groupby("driver_number")}
    episode_at = {(row["window"], row["driver_number"], row["as_of_utc"]): row["episode_id"] for row in segments}
    counts = Counter()
    features, outcomes = [], []
    for (window, driver), rows in streams.items():
        times = clocks[window, driver]
        own = by_driver.get(driver)
        if own is None:
            continue
        targets = own[own.lap_start_utc.between(times[0], times[-1])]
        if audit is not None:
            left = own[own.lap_start_utc.lt(times[0]) & own.lap_end_estimate_utc.gt(times[0])]
            for lap_number, lap in left.iterrows():
                key = rows[0]["session_key"]
                audit.outcomes.append({
                    "opportunity_id": f"{key}/{driver}/{lap_number}", "session_key": key,
                    "split": split, "driver_number": driver, "lap_number": int(lap_number),
                    "window": window, "window_kind": rows[0]["window_kind"], "as_of_utc": lap.lap_start_utc,
                    "screen": "LEFT_TRUNCATED_WINDOW", "full_lap": False,
                })
        for lap_number, lap in targets.iterrows():
            start = lap.lap_start_utc
            cutoff = rows[bisect_right(times, start) - 1]
            state = exposure_state(cutoff, exposure)
            opportunity = f"{cutoff['session_key']}/{driver}/{lap_number}"
            common = {"opportunity_id": opportunity, "split": split, "session_key": cutoff["session_key"],
                      "driver_number": driver, "lap_number": int(lap_number)}
            feature = common | {
                "as_of_utc": start, "physical_as_of_utc": cutoff["as_of_utc"], "window": window,
                "opponent_number": cutoff["ahead"], "encounter": cutoff["encounter"],
                "cutoff_exposure": state, "gap_m": cutoff["gap_m"],
                "gap_lower_m": cutoff["gap_lower_m"], "gap_upper_m": cutoff["gap_upper_m"],
            }
            ledger = {}
            if audit is not None:
                audit.cutoffs.append(feature | {"window_kind": cutoff["window_kind"], "horizon_laps": 1})
                ledger = common | {
                    "window": window, "window_kind": cutoff["window_kind"], "as_of_utc": start,
                    "stint_number": int(lap.stint_number) if pd.notna(lap.stint_number) else None,
                    "track_status": lap.track_status, "archive_pass_indication": lap.had_in_lap_on_track_pass,
                    "pit_or_stint_transition": bool(lap.is_pit_lap or lap.stint_pit_mismatch),
                    "cutoff_episode_id": episode_at.get((window, driver, cutoff["as_of_utc"])),
                    "cutoff_exposure": state,
                    "full_lap": False,
                }
                audit.outcomes.append(ledger)
            if pd.isna(lap.observed_lap_time_s):
                ledger["screen"] = "MISSING_OBSERVED_ELAPSED"
                continue
            elapsed = float(lap.observed_lap_time_s)
            end = start + timedelta(seconds=elapsed)
            ledger["completed_utc"] = end
            if end > times[-1] + period_delta or elapsed <= 0:
                counts["PARTIAL_WINDOW"] += 1
                ledger["screen"] = "PARTIAL_WINDOW"
                continue
            counts["FULL_LAPS_IN_WINDOW"] += 1
            ledger["full_lap"] = True
            next_lap = lap_number + 1
            if next_lap not in own.index or abs((own.loc[next_lap].lap_start_utc - end).total_seconds()) > 2:
                counts["TIMING_BOUNDARY_UNSUPPORTED"] += 1
                ledger["screen"] = "TIMING_BOUNDARY_UNSUPPORTED"
                continue
            ledger["next_line_utc"] = own.loc[next_lap].lap_start_utc
            if lap.track_status != "GREEN" or not lap.confirmed_green_lap or lap.rainfall != 0:
                counts["GREEN_DRY_OUTCOME_UNSUPPORTED"] += 1
                ledger["screen"] = "GREEN_DRY_OUTCOME_UNSUPPORTED"
                continue
            if lap.is_pit_lap or lap.stint_pit_mismatch:
                counts["PIT_OR_STINT_TRANSITION"] += 1
                ledger["screen"] = "PIT_OR_STINT_TRANSITION"
                continue
            if lap.had_in_lap_on_track_pass:
                counts["ARCHIVE_PASS_INDICATION_CENSORED"] += 1
                ledger["screen"] = "ARCHIVE_PASS_INDICATION_CENSORED"
                continue
            samples = rows[bisect_left(times, start):bisect_left(times, end)]
            usable = []
            for sample in samples:
                point = sample["as_of_utc"]
                retrospective = retro[(window, driver, point)]
                other = retro.get((window, sample["ahead"], point))
                if (exposure_state(sample, exposure) != "UNSUPPORTED"
                        and retrospective["route"] == "RACING" and other and other["route"] == "RACING"):
                    usable.append(sample)
            support = min(1., len(usable) * period / elapsed)
            ledger["physical_coverage"] = support
            if support < reference["label_minimum_coverage"] or state == "UNSUPPORTED":
                counts["PHYSICAL_SUPPORT_BELOW_MINIMUM"] += 1
                ledger["screen"] = "PHYSICAL_SUPPORT_BELOW_MINIMUM"
                continue
            clear_fraction = sum(exposure_state(sample, exposure) == "CLEAR" for sample in usable) / len(usable)
            close_fraction = sum(exposure_state(sample, exposure) == "CLOSE" for sample in usable) / len(usable)
            episode_ids = sorted({episode_at[(window, driver, sample["as_of_utc"])] for sample in usable
                                  if (window, driver, sample["as_of_utc"]) in episode_at})
            kind = "INTERMEDIATE"
            if state == "CLEAR" and clear_fraction >= reference["minimum_clean_control_fraction"]:
                kind = "CLEAN_CONTROL"
            cutoff_episode = episode_at.get((window, driver, cutoff["as_of_utc"]))
            if state == "CLOSE" and cutoff_episode in episode_ids:
                kind = "FOLLOWING"
            ledger.update({
                "screen": kind, "cutoff_episode_id": cutoff_episode,
                "cutoff_encounter_continued": cutoff_episode in episode_ids,
                "later_or_different_episode": any(identifier != cutoff_episode for identifier in episode_ids),
                "outcome_episode_ids": episode_ids, "cutoff_opponent": cutoff["ahead"],
            })
            features.append(feature)
            outcomes.append(common | {
                "completed_utc": end, "observed_lap_s": elapsed, "outcome_group": kind,
                "physical_coverage": support, "clear_fraction": clear_fraction,
                "close_fraction": close_fraction, "episode_ids": episode_ids,
            })
            counts[kind] += 1
    return features, outcomes, dict(counts)
