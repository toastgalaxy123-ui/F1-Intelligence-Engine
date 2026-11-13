"""Preregistered cutoff-only selection for the two development races."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

import duckdb
import pandas as pd

from f1ri.ingestion.telemetry import TelemetryWindow
from f1ri.validation.traffic_acquisition import ReplaySpan, TrafficWindow
from f1ri.validation.traffic_coverage_pilot import ExtendedWindow, uncovered_requests
from f1ri.validation.traffic_history_audit import visible_history


def cutoff_candidates(warehouse: Path, session_key: int, track: pd.DataFrame, population: dict,
                      geometry_available, prefix_s: int) -> list[dict]:
    """Use only recorded lap starts and intervals available at each cutoff."""
    with duckdb.connect(str(warehouse), read_only=True) as con:
        con.execute("SET TimeZone = 'UTC'")
        cutoffs = con.execute("""
            WITH lap_starts AS (
                SELECT driver_number, lap_number, min(date_start) AS cutoff_utc
                FROM silver.laps WHERE session_key = ? AND lap_number BETWEEN ? AND ?
                GROUP BY driver_number, lap_number HAVING count(DISTINCT date_start) = 1
            )
            SELECT l.*, i.date AS interval_utc, i.interval_raw, try_cast(i.interval_raw AS double) AS interval_s
            FROM lap_starts l ASOF LEFT JOIN (
                SELECT driver_number, date, any_value(interval_raw) AS interval_raw
                FROM silver.intervals WHERE session_key = ? AND date IS NOT NULL
                GROUP BY driver_number, date HAVING count(DISTINCT interval_raw) = 1
            ) i ON l.driver_number = i.driver_number AND l.cutoff_utc >= i.date
            ORDER BY l.cutoff_utc, l.driver_number, l.lap_number
        """, [session_key, population["first_lap"], population["last_lap"], session_key]).df()
    green = [(row.start_utc, row.end_utc) for row in track.itertuples()
             if row.status == "GREEN" and row.confidence == "HIGH"]
    result = []
    for row in cutoffs.itertuples():
        at = row.cutoff_utc
        start = at.floor("s") - timedelta(seconds=prefix_s)
        if start < geometry_available or not any(a <= at < b for a, b in green):
            continue
        age = (at - row.interval_utc).total_seconds() if pd.notna(row.interval_utc) else None
        gap = row.interval_s if age is not None and 0 <= age <= population["maximum_interval_age_s"] else None
        result.append({
            "driver_number": int(row.driver_number), "lap_number": int(row.lap_number), "cutoff_utc": at,
            "block_start_utc": start,
            "phase": "early" if row.lap_number <= population["early_phase_last_lap"] else "middle",
            "interval_source_utc": row.interval_utc if pd.notna(row.interval_utc) else None,
            "interval_age_s": age, "interval_source_raw": row.interval_raw,
            "selection_gap_s": float(gap) if pd.notna(gap) else None,
        })
    return result


def _rank(row, seed, key, arm):
    return sha256(f"{seed}/{key}/{arm}/{row['driver_number']}/{row['lap_number']}".encode()).hexdigest()


def _matches(value, predicate):
    return value is not None and predicate(value)


def qualified_close(warehouse, cohort, candidates, settings, seed, minimum_clean):
    close = [row for row in candidates if _matches(
        row["selection_gap_s"], lambda gap: 0 < gap <= settings["close_interval_max_s"],
    )]
    audit = []
    for phase in ("early", "middle"):
        pool = sorted((row for row in close if row["phase"] == phase),
                      key=lambda row: _rank(row, seed, cohort["session_key"], "history_close"))
        for row in pool[:settings["close_history_checks_per_phase"]]:
            request, prior, reason = visible_history(
                warehouse, cohort, row["driver_number"], row["lap_number"], row["cutoff_utc"].to_pydatetime(),
                audit=True,
            )
            clean = request.clean_laps_observed_current_stint if request is not None else None
            passes = clean is not None and clean >= minimum_clean
            audit.append(row | {
                "stint_number": request.stint_number if request else (prior[-1]["stint_number"] if prior else None),
                "compound": request.compound_relative if request else (prior[-1]["compound"] if prior else None),
                "completed_same_stint": len(prior), "clean_same_stint": clean,
                "qualified": passes, "history_reason": (
                    "QUALIFIED" if passes else reason or "INSUFFICIENT_PRIOR_CLEAN_STINT_LAPS"
                ),
            })
    return close, audit


def _profiles(rows):
    by_stint = {(row["driver_number"], row.get("stint_number")) for row in rows if row.get("stint_number") is not None}
    return {
        "cutoffs": len(rows), "drivers": sorted({row["driver_number"] for row in rows}),
        "driver_stints": sorted(by_stint),
        "by_compound": dict(Counter(row.get("compound") or "UNKNOWN" for row in rows)),
        "by_phase": dict(Counter(row["phase"] for row in rows)),
    }


def _load_cached_slices(root: Path, archives: list[dict], session_key: int):
    candidates = {}
    for archive in archives:
        if archive["session_key"] != session_key:
            continue
        for window in archive["windows"]:
            for row in window["sources"]:
                cache = root / row["cache_path"]
                if not cache.is_file() or sha256(cache.read_bytes()).hexdigest() != row["sha256"]:
                    raise ValueError(f"Frozen telemetry failed cache verification: {cache}")
                key = (pd.Timestamp(row["start_utc"]), pd.Timestamp(row["end_utc"]))
                candidates.setdefault(key, set()).add(row["endpoint"])
    return tuple(TelemetryWindow(session_key, None, start, end) for (start, end), endpoints in candidates.items()
                 if endpoints == {"location", "car_data"})


def choose_blocks(key, candidates, history, plan, cached, track):
    config, settings = plan["acquisition"], plan["cutoff_population"]
    width = timedelta(seconds=config["window_seconds"])
    pad = timedelta(seconds=config["boundary_overlap_seconds"])
    chunk = timedelta(seconds=config["maximum_chunk_seconds"]) - 2 * pad
    green = [(row.start_utc, row.end_utc) for row in track.itertuples()
             if row.status == "GREEN" and row.confidence == "HIGH"]
    order = {
        "early_control": [row for row in candidates if row["phase"] == "early" and _matches(
            row["selection_gap_s"], lambda gap: gap >= settings["clean_interval_min_s"],
        )],
        "history_close": sorted((row for row in history if row["qualified"]),
                                key=lambda row: _rank(row, plan["seed"], key, "history_close")),
        "random_green": sorted(candidates, key=lambda row: _rank(row, plan["seed"], key, "random_green")),
    }
    selected, evidence = [], []
    for arm, limit in config["arms"].items():
        added = 0
        for row in order[arm]:
            start = row["block_start_utc"]
            span = ReplaySpan(key, start, start + width)
            if not any(a <= start < span.end_utc <= b for a, b in green):
                continue
            if any(start < item.replay.telemetry.end_utc and span.end_utc > item.replay.telemetry.start_utc
                   for item in selected):
                continue
            reuse = tuple(item for item in cached if start < item.end_utc and span.end_utc > item.start_utc)
            requests = uncovered_requests(span, reuse, chunk, pad)
            replay = TrafficWindow(arm, span, row["driver_number"], row["lap_number"])
            selected.append(ExtendedWindow(replay, f"{key}/{row['driver_number']}/{row['lap_number']}",
                                           reuse, requests))
            evidence.append(row | {"arm": arm, "window": replay.name, "block_end_utc": span.end_utc,
                                   "source_slices_reused": len(reuse), "source_slices_to_request": len(requests)})
            added += 1
            if added == limit:
                break
    requested = [chunk for window in selected for chunk in window.requested]
    seconds = sum((w.end_utc - w.start_utc).total_seconds() for w in requested)
    requests = 2 * len(requested)
    if seconds > config["maximum_new_field_seconds_per_race"] or requests > config["maximum_field_requests_per_race"]:
        raise ValueError(f"Development plan exceeds the frozen acquisition budget for {key}")
    return selected, evidence, {"field_request_seconds": seconds, "field_requests": requests}


def plan_development_race(root, warehouse, cohort, inputs, available, plan, protocol, archives):
    key = cohort["session_key"]
    if cohort["split"] != "development":
        raise ValueError("Only development cohorts can acquire history-study data")
    population = cutoff_candidates(warehouse, key, inputs["track"], plan["cutoff_population"],
                                   available, plan["acquisition"]["before_cutoff_seconds"])
    close, history = qualified_close(warehouse, cohort, population, plan["cutoff_population"], plan["seed"],
                                     protocol["reference"]["minimum_prior_clean_stint_laps"])
    cached = _load_cached_slices(root, archives, key)
    windows, anchors, budget = choose_blocks(key, population, history, plan, cached, inputs["track"])
    accepted = [row for row in history if row["qualified"]]
    rejected = [row for row in history if not row["qualified"]]
    before, after, lost = map(_profiles, (history, accepted, rejected))
    summary = {
        "session_key": key, "eligible_cutoffs": _profiles(population), "fresh_close_signal": _profiles(close),
        "history_checked": before, "history_qualified": after, "history_rejected": lost,
        "history_reasons": dict(Counter(row["history_reason"] for row in history)),
        "drivers_lost_after_history": sorted(set(before["drivers"]) - set(after["drivers"])),
        "driver_stints_lost_after_history": sorted(set(before["driver_stints"]) - set(after["driver_stints"])),
        "anchors": anchors, "budget": budget, "planned_arm_counts": plan["acquisition"]["arms"],
    }
    return windows, summary, history
