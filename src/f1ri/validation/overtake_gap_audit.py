"""Inventory only the approved gap windows and estimate timing-screen coverage."""
from collections import Counter, defaultdict
from itertools import pairwise
from statistics import median

from f1ri.validation.overtake_evidence import union_duration, utc
from f1ri.validation.overtake_historical_study import _load_json
from f1ri.validation.traffic_history_study import _sha


def gap_census(root, stage3, gap, history, sources, thresholds):
    session = gap["session"]
    start, end = map(utc, (gap["start"], gap["end"]))
    payloads, seen = [], set()
    for base in dict.fromkeys((root, stage3)):
        folder = base / f"data/telemetry/proximity/{session}"
        for path in sorted(folder.glob("*/*/*.meta.json")):
            raw = _load_json(path)
            q = raw["query"]
            a, b = map(utc, (q["date>"], q["date<"]))
            if a >= end or b <= start:
                continue
            payload = path.with_name(path.name.replace(".meta.json", ".json"))
            if raw["sha256"] in seen:
                continue
            seen.add(raw["sha256"])
            if _sha(payload) != raw["sha256"]:
                raise ValueError(f"Gap cache hash differs for {payload}")
            records = _load_json(payload)
            clocks = [utc(row["date"]) for row in records if start <= utc(row["date"]) <= end]
            payloads.append({"endpoint": payload.stem, "file": payload.relative_to(base).as_posix(),
                             "query": q, "sha256": raw["sha256"], "retrieved_at_utc": raw["retrieved_at_utc"],
                             "gap_sample_count": len(clocks),
                             "first_sample_utc": min(clocks, default=None), "last_sample_utc": max(clocks, default=None)})
    reconstructed = []
    for r in sources:
        if r["session"] != session:
            continue
        for span in r["window_spans"]:
            if span["mode"] == "POINT_IN_TIME":
                a, b = map(utc, (span["first_utc"], span["last_utc"]))
                if a < end and b > start:
                    reconstructed.append((max(start, a), min(end, b)))
    # Relative timing at a common completed-lap line is a screening proxy.
    # It identifies candidate pairs and cannot establish on-track neighbors.
    by_lap = defaultdict(list)
    for driver, (times, laps) in history.laps.items():
        for row, at in zip(laps, times, strict=True):
            if start <= at < end:
                by_lap[int(row.lap_number)].append((at, driver))
    proxy, green_samples = [], 0
    for lap, rows in sorted(by_lap.items()):
        for (before, target), (at, driver) in pairwise(sorted(rows)):
            if not history.green_at(at):
                continue
            green_samples += 1
            delta = (at - before).total_seconds()
            if 0 < delta <= 2 and not history.interruptions((driver, target), before, at, pit_receipt_cutoff=at):
                proxy.append({"at_utc": at, "lap": lap, "attacker": driver, "timing_car_ahead": target,
                              "line_gap_s": delta})
    pairs = {(r["attacker"], r["timing_car_ahead"]) for r in proxy}
    timing_counts = {
        "lap_line": sum(map(len, by_lap.values())),
        "intervals": sum(start <= at < end for times, _ in history.intervals.values() for at in times),
        "position": sum(start <= at < end for times, _ in history.positions.values() for at in times),
        "race_control": sum(start <= r.date < end for r in history.control),
    }
    pits = [{"driver": d, "estimated_start_utc": lo, "estimated_end_utc": hi, "record_utc": stamp}
            for d, rows in sorted(history.pits.items()) for lo, hi, stamp in rows if start <= hi and end >= lo]
    states = [{"from": max(start, r.start_utc), "to": min(end, r.end_utc),
               "status": r.status, "confidence": r.confidence}
              for r in history.track if start < r.end_utc and end > r.start_utc]
    return gap | {
        "duration_s": (end - start).total_seconds(), "payloads": payloads,
        "telemetry_endpoints": dict(Counter(p["endpoint"] for p in payloads)),
        "point_in_time_reconstruction_s": union_duration(reconstructed),
        "supported_physical_opportunities": 0 if not reconstructed else None,
        "timing_counts": timing_counts, "green_line_pairs_screened": green_samples,
        "timing_proxy_rows": proxy, "distinct_timing_proxy_pairs": len(pairs),
        "median_timing_proxy_gap_s": median(r["line_gap_s"] for r in proxy) if proxy else None,
        "physical_thresholds_m": thresholds,
        "verified_new_independent_encounters": 0, "verified_new_pass_events": 0,
        "pit_intervals": pits, "track_intervals": states,
    }


def acquisition_assessment(gaps, config):
    """Record the no-acquisition decision and the exact untested candidate scope."""
    if config["api_request_budget"] != 0:
        raise ValueError("S4-B.1 is frozen at a zero-request budget")
    return {
        "executed": False, "request_budget": 0, "requests_made": 0,
        "candidate_windows": [{k: row[k] for k in ("session", "start", "end", "duration_s")} for row in gaps],
        "candidate_endpoints": ["location", "car_data"],
        "purpose": "Recover same-lap physical support where a timing-only screen suggests follow-up",
        "stop_condition": "Stop before acquisition. Neither gap supplies a new race, and timing-only proximity "
                          "does not establish independent competitive-event yield.",
        "deduplication": "Reuse existing session, driver, endpoint and timestamp coverage before planning queries",
        "provenance": "Cache query, payload SHA-256 and retrieval timestamp are recorded in the census",
    }
