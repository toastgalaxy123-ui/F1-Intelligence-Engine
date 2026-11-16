"""Feasibility bounds and repeatable scheduler comparisons."""

from collections import Counter
from itertools import pairwise
from math import floor
from statistics import median

from f1ri.ingestion.priority_polling import PriorityScheduler

PERIODS = {"location": .27, "position": 4., "laps": 18., "race_control": 12., "sessions": 300.}
DURATIONS = {"location": .39, "position": .38, "laps": .94, "race_control": .14, "sessions": .52}
PROCESSING = {"location": .015, "position": .020, "laps": .085, "race_control": .008, "sessions": .007}


def measured(envelopes, limits):
    grouped = {}
    starts = [e for e, _ in sorted(envelopes, key=lambda pair: pair[0].sequence)]
    for e in starts:
        group = grouped.setdefault(e.endpoint, [])
        group.append(e)
    rows = {}
    for ep, group in grouped.items():
        differences = [(b.completed_utc-a.completed_utc).total_seconds() for a, b in pairwise(group)]
        usable = [(b.available_utc-a.available_utc).total_seconds() for a, b in pairwise(group)]
        limit = limits.get(ep)
        rows[ep] = {"requests": len(group), "successful": sum(not e.error for e in group),
                    "response_intervals_s": differences, "usable_intervals_s": usable,
                    "max_usable_interval_s": max(usable, default=None), "freshness_limit_s": limit,
                    "ideal_completion_duty_cycle": sum(min(g, limit) for g in usable)/sum(usable)
                    if usable and limit else None,
                    "processing_median_s": median((e.processed_utc-e.completed_utc).total_seconds() for e in group)}
    gaps = [(b.request_ns-a.request_ns)/1e9 for a, b in pairwise(starts) if a.clock_epoch == b.clock_epoch]
    return {"endpoints": rows, "requests": len(starts), "request_spacings_s": gaps,
            "min_spacing_s": min(gaps, default=None), "request_rate_floor_respected": all(g >= 2.099 for g in gaps),
            "empirical_racing_freshness": None}


def feasibility(protocol):
    minimum = protocol["minimum_start_spacing_s"]
    limits = protocol["freshness_limits_s"]
    steady_ceiling = 60/minimum
    rates = {ep: 60/value for ep, value in limits.items()}
    return {"classification": "POLLING_PARTIALLY_FEASIBLE", "location_continuous": "POLLING_INFEASIBLE",
            "rate_ceiling_steady_requests_per_min": steady_ceiling,
            "rate_ceiling_any_60s_window": protocol["request_ceiling_per_60_s"],
            "minimum_global_spacing_s": minimum, "max_concurrent_requests": 1,
            "location_only_ideal_duty_upper_bound": limits["location"]/minimum,
            "required_requests_per_min": rates, "total_required_requests_per_min": sum(rates.values()),
            "location_only_rate_margin": steady_ceiling-rates["location"],
            "model": "Periodic updates and ideal instantaneous source values. Jitter, transport, processing and "
                     "incomplete source coverage can only invalidate a continuous guarantee.",
            "individual_minimum_feasible": {ep: minimum <= age for ep, age in limits.items()},
            "provider_streaming_not_tested": True}


def compare(protocol):
    return [_simulate(protocol, name, failed) for name in protocol["planning_policies"] for failed in (False, True)]


def _simulate(protocol, method, failures):
    plan = protocol["rehearsal"]
    horizon = protocol["comparison"]["horizon_s"]
    max_attempts = protocol["comparison"]["request_budget"]
    limits = protocol["freshness_limits_s"]
    rules = plan["scheduler"]["rules"]
    if method == "S0":
        rules = {ep: row | {"budget": max_attempts} for ep, row in rules.items()}
    scheduler = PriorityScheduler(plan["endpoints"], {"method": method, "rules": rules}, plan["interval_s"])
    chronology = []
    now = 0.
    injected_rate = False
    while now < horizon and len(chronology) < max_attempts:
        blockers = []
        if method in ("S3", "S4"):
            for ep, limit in limits.items():
                recent = next((r for r in reversed(chronology) if r["endpoint"] == ep and r["status"] == 200
                               and r["usable"] <= now), None)
                if recent is None or now-recent["source_event"] >= limit or (
                    ep in ("location", "position") and recent["drivers"] != [1, 4]
                ):
                    blockers.append(ep)
        endpoint, eligible = scheduler.choose(now, blockers)
        if eligible is None:
            break
        if endpoint is None:
            now = eligible
            continue
        scheduler.started(endpoint, now)
        status, wait = 200, None
        if failures and endpoint == "location" and 14 <= now <= 31:
            if not injected_rate:
                status, wait, injected_rate = 429, 5., True
            else:
                status, wait = 503, 2.
        processing = PROCESSING[endpoint]
        complete = now+DURATIONS[endpoint]
        usable = complete+processing
        source_event = floor(now/PERIODS[endpoint])*PERIODS[endpoint] if status == 200 else None
        drivers = [1, 4] if endpoint in ("location", "position", "laps") and status == 200 else []
        if endpoint == "location" and 32 <= now <= 40:
            drivers = [1]
        scheduler.completed(endpoint, usable, status == 200, status=status, retry_after=wait)
        chronology.append({"endpoint": endpoint, "start": now, "completed": complete, "usable": usable,
                           "source_event": source_event, "drivers": drivers, "status": status, "backoff_s": wait,
                           "processing_s": processing, "queue_delay_s": max(0., now-eligible),
                           "scheduler_reason": scheduler.last_block,
                           "revision": endpoint == "laps" and now >= 35})
        now = usable

    checks = []
    checkpoints = range(0, horizon+1, protocol["comparison"]["checkpoint_step_s"])
    for at in checkpoints:
        reports = {}
        for ep, limit in limits.items():
            available = [r for r in chronology if r["endpoint"] == ep and r["usable"] <= at and r["status"] == 200]
            last = available[-1] if available else None
            age = at-last["source_event"] if last else None
            missing = sorted({1, 4}-set(last["drivers"])) if last and ep in ("location", "position") else []
            reports[ep] = {"at": at, "fresh": age is not None and age <= limit and not missing,
                           "age_s": age, "receipt_age_s": at-last["usable"] if last else None,
                           "response_age_s": at-last["completed"] if last else None,
                           "missing_driver_count": len(missing), "event_cutoff": at}
        checks.append({"at": at, "feeds": reports, "support": all(r["fresh"] for r in reports.values())})
    stats = {}
    for ep, limit in limits.items():
        successful = [r for r in chronology if r["endpoint"] == ep and r["status"] == 200 and r["usable"] <= horizon]
        gaps = [b["usable"]-a["usable"] for a, b in pairwise(successful)]
        ages = [c["feeds"][ep]["age_s"] for c in checks if c["feeds"][ep]["age_s"] is not None]
        complete_gaps = [successful[0]["usable"], *gaps, horizon-successful[-1]["usable"]] if successful else [horizon]
        stats[ep] = {"count": len(successful), "median_refresh_s": median(gaps) if gaps else None,
                     "max_refresh_s": max(complete_gaps), "max_age_s": max(ages, default=None),
                     "deadline_s": limit, "duty_cycle": sum(c["feeds"][ep]["fresh"] for c in checks)/len(checks),
                     "ideal_duty_cycle_upper_bound": min(1., sum(min(limit, dt) for dt in gaps)/sum(gaps))
                     if gaps else None, "starvation_guard_s": rules[ep]["max_gap_s"],
                     "marginal_peak_gap_over_guard": max(complete_gaps) > rules[ep]["max_gap_s"]}
    spacings = [b["start"]-a["start"] for a, b in pairwise(chronology)]
    if any(x < plan["interval_s"]-1e-8 for x in spacings) or len(chronology) > max_attempts:
        raise ValueError("Controlled scheduler violated the frozen request ceiling.")
    starts = [r["start"] for r in chronology]
    completed = sum(r["usable"] <= horizon and r["status"] == 200 for r in chronology)
    return {"method": method, "experiment": "FAULTS" if failures else "NOMINAL", "mode": "SYNTHETIC_DELIVERY",
            "requests": len(chronology), "usable_completed": completed, "requests_per_min": len(starts)*60/horizon,
            "successful_responses_per_min": completed*60/horizon,
            "spacing_violations": 0, "request_budget": max_attempts,
            "by_endpoint": dict(Counter(r["endpoint"] for r in chronology)), "freshness": stats,
            "max_queue_delay_s": max(r["queue_delay_s"] for r in chronology),
            "withheld_checkpoints": sum(not r["support"] for r in checks), "checkpoints": checks, "schedule": chronology}
