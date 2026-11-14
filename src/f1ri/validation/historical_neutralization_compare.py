"""Compare frozen and supported starting fields on explicit timing populations."""
import itertools
from collections import defaultdict
from dataclasses import replace
from datetime import timedelta
from math import ceil, sqrt
from statistics import mean, median

from f1ri.simulator.neutralization import simulate_neutralization
from f1ri.simulator.proximity import PositionQuality, physical_neighborhoods
from f1ri.validation.frozen_neutralization_inputs import _command, _physical, _state
from f1ri.validation.historical_neutralization_sources import clock
from f1ri.validation.neutralization_study import _checks


def error_stats(errors):
    if not errors:
        return {"n": 0, "mae_s": None, "median_abs_s": None, "bias_s": None, "rmse_s": None, "p90_abs_s": None}
    absolute = sorted(map(abs, errors))
    return {"n": len(errors), "mae_s": mean(absolute), "median_abs_s": median(absolute), "bias_s": mean(errors),
            "rmse_s": sqrt(mean(x*x for x in errors)), "p90_abs_s": absolute[ceil(.9*len(absolute))-1]}


def compare_lines(observed, predictions):
    targets = {(r["driver"], r["completed_laps"]): clock(r["utc"]) for r in observed}
    if len(targets) != len(observed):
        raise ValueError("Historical lap target is ambiguous")
    full, common = {}, {}
    support = targets.keys() & set.intersection(*(set(x) for x in predictions.values()))
    rows = []
    for variant, simulated in predictions.items():
        full[variant] = error_stats([(simulated[k]-targets[k]).total_seconds() for k in sorted(targets.keys() & simulated.keys())])
        common[variant] = error_stats([(simulated[k]-targets[k]).total_seconds() for k in sorted(support)])
        for (d, lap), target in sorted(targets.items()):
            predicted = simulated.get((d, lap))
            rows.append({"variant": variant, "driver": d, "lap": lap, "observed_utc": target,
                         "simulated_utc": predicted, "common": (d, lap) in support,
                         "error_s": (predicted-target).total_seconds() if predicted else None})
    return rows, {"full": full, "common": common, "targets": len(targets)}


def relative_errors(observed, simulated):
    laps = []
    gaps = []
    by_lap = defaultdict(list)
    common = observed.keys() & simulated.keys()
    for driver, lap in sorted(common):
        marker = (driver, lap)
        if (prior := (driver, lap-1)) in common:
            actual = (observed[marker]-observed[prior]).total_seconds()
            modeled = (simulated[marker]-simulated[prior]).total_seconds()
            laps.append({"driver": driver, "lap": lap, "observed_s": actual, "simulated_s": modeled,
                         "error_s": modeled-actual})
        by_lap[lap].append(driver)
    for lap, roster in sorted(by_lap.items()):
        order = sorted(roster, key=lambda d: (observed[d, lap], d))
        for left, right in itertools.pairwise(order):
            if all((d, lap-1) in common for d in (left, right)):
                obs = (observed[right, lap]-observed[left, lap]-(observed[right, lap-1]-observed[left, lap-1]))
                sim = (simulated[right, lap]-simulated[left, lap]-(simulated[right, lap-1]-simulated[left, lap-1]))
                gaps.append({"prior_in_observed_crossing_order": left, "driver": right, "lap": lap,
                             "observed_change_s": obs.total_seconds(), "simulated_change_s": sim.total_seconds(),
                             "error_s": (sim-obs).total_seconds()})
    return laps, gaps


def _prediction(run):
    epoch = run.execution.initial.race_start_utc
    return {(f.event.driver_number, f.event.checkpoint//3): epoch+timedelta(microseconds=f.event.at_us)
            for f in run.execution.run.trace if f.event.kind.name == "LAP_COMPLETED"}


def replay_declared(spec, ep, profile):
    start = _state(spec["initial"])
    end = ep["end_utc"] - start.race_start_utc + timedelta(seconds=200)
    return simulate_neutralization(
        start, _physical(spec["physical"]), {int(d): v for d, v in spec["declared_pace_s"].items()},
        tuple(map(_command, spec["commands"])), laps=18, end_us=round(end.total_seconds()*1e6),
        profile=profile, individual_restart=True,
    )


def replay_localized(spec, support, geometry, ep, profile, shift=0, *, exact_sensitivity=False):
    start = _state(spec["initial"])
    if not all(r["tier_event_clock"] == "H0" for r in support) or len(support) != len(start.cars):
        return None, "INCOMPLETE_PHYSICAL_FIELD"
    prefix = {r["driver"]: r for r in support}
    cutoff = support[0]["cutoff_utc"]
    if cutoff == start.race_start_utc and any(c.completed_laps != prefix[c.driver_number]["completed_laps"]
                                             for c in start.cars):
        return None, "START_LAP_MISMATCH"
    observed = _physical(spec["physical"])
    length = geometry["length_m"]
    if any(not 0 <= r["estimated_progress_m"] + shift*r["position_uncertainty_m"] < length for r in support):
        return None, "PERTURBATION_CROSSES_LAP_LINE"
    physical = replace(observed, as_of_utc=cutoff, layout_version=geometry["version"], length_m=length, cars=tuple(
        replace(c, progress_m=r["estimated_progress_m"] + shift*r["position_uncertainty_m"],
                completed_laps=r["completed_laps"],
                uncertainty_m=r["position_uncertainty_m"], observed_at_utc=r["latest_observed_utc"],
                quality=PositionQuality(r["projection_quality"]))
        for c in observed.cars for r in (prefix[c.driver_number],)))
    neighborhoods = physical_neighborhoods(physical)
    ambiguous = sorted(d for d, row in neighborhoods.items() if row.ahead is None)
    if ambiguous:
        return None, "UNRESOLVED_PREDECESSOR: " + ", ".join(map(str, ambiguous))
    if exact_sensitivity:
        # The current solver only accepts exact scenario inputs. These points
        # sample the documented bounds and are never returned as observed state.
        physical = replace(physical, cars=tuple(
            replace(c, uncertainty_m=0., quality=PositionQuality.EXACT_SCENARIO)
            for c in physical.cars))
    positions = {c.driver_number: c for c in physical.cars}
    start_clock = round((cutoff-start.race_start_utc).total_seconds()*1e6)
    start = replace(start, race_clock_us=start_clock, cars=tuple(
        replace(c, completed_checkpoints=p.completed_laps*3 + int(p.progress_m//(length/3)))
        for c in start.cars for p in (positions[c.driver_number],)))
    end = ep["end_utc"] - start.race_start_utc + timedelta(seconds=200)
    try:
        run = simulate_neutralization(start, physical, {int(d): v for d, v in spec["declared_pace_s"].items()},
                                      tuple(map(_command, spec["commands"])), laps=18, end_us=round(end.total_seconds()*1e6),
                                      profile=profile, individual_restart=True)
    except ValueError as exc:
        return None, f"ENGINE_REJECTED_FIELD: {exc}"
    if run.execution.unsupported:
        reasons = ", ".join(sorted(set(run.execution.unsupported.values())))
        return None, "ENGINE_REJECTED_FIELD: " + reasons
    failed = [k for k, ok in _checks(run).items() if not ok]
    if failed:
        return None, "ENGINE_REJECTED_FIELD_INVARIANTS: " + ", ".join(failed)
    return run, None


def run_prediction_comparison(episodes, archive, released, scenarios, support, geometry, profile, protocol):
    comparisons, statistics, relative, sensitivity, variants = [], {}, [], [], []
    live_results = {}
    for ep in episodes:
        name = ep["episode"]
        saved = released[name]
        observed_lines = archive[name]["observed_lines"]
        predictions = {
            label: {(x["driver"], x["completed_laps"]): clock(x["simulated_utc"])
                    for x in saved[key]["compared_lines"]}
            for label, key in (("A", "frozen"), ("B", "stabilized"))
        }
        for label in ("A", "B"):
            variants.append({"episode": name, "variant": label, "status": "VERIFIED_FROZEN_PREDICTIONS", "tier": "H4"})
        preferred = [r for r in support if r["episode"] == name and r["cutoff"] == "initial" and
                     r["source_set"] == "combined"]
        variants.append({"episode": name, "variant": "C", "tier": "H1", "status": "NO_CUTOFF_VISIBLE_SECTOR_BOUNDS"
                         if preferred else "OUTSIDE_PHYSICAL_RECONSTRUCTION_SCOPE"})
        if preferred:
            run, cause = replay_localized(scenarios[name], preferred, geometry, ep, profile)
            variants.append({"episode": name, "variant": "D", "tier": "H0", "status": cause or "EXECUTED",
                             "cutoff": preferred[0]["cutoff_utc"], "available_driver_fixes": sum(
                                 r["tier_event_clock"] == "H0" for r in preferred)})
            if run:
                predictions["D"] = _prediction(run)
                live_results[name] = run
            else:
                deployment = [r for r in support if r["episode"] == name and r["cutoff"] == "deployment"
                              and r["source_set"] == "combined"]
                alternative, failure = replay_localized(scenarios[name], deployment, geometry, ep, profile)
                variants.append({"episode": name, "variant": "D_DEPLOYMENT", "tier": "H0",
                                 "status": failure or "EXECUTED_DIFFERENT_EPOCH",
                                 "cutoff": deployment[0]["cutoff_utc"], "available_driver_fixes": sum(
                                     r["tier_event_clock"] == "H0" for r in deployment)})
                if alternative:
                    predictions["D_DEPLOYMENT"] = _prediction(alternative)
        else:
            variants.append({"episode": name, "variant": "D", "tier": "H0", "status": "OUTSIDE_PHYSICAL_SCOPE"})
        rows, stats = compare_lines(observed_lines, predictions)
        _, frozen_pair = compare_lines(observed_lines, {k: predictions[k] for k in ("A", "B")})
        stats["frozen_pair_common"] = frozen_pair["common"]
        comparisons.extend({"episode": name, **r} for r in rows)
        targets = {(r["driver"], r["completed_laps"]): clock(r["utc"]) for r in observed_lines}
        common = targets.keys() & set.intersection(*(set(x) for x in predictions.values()))
        matched = {k: targets[k] for k in common}
        for variant, simulated in predictions.items():
            laps, gaps = relative_errors(matched, simulated)
            relative.extend({"episode": name, "variant": variant, "measure": kind, **r}
                            for kind, items in (("CONSECUTIVE_LAP_DELTA", laps), ("SAME_PAIR_LINE_GAP_CHANGE", gaps))
                            for r in items)
            stats.setdefault("relative_common", {})[variant] = {
                "lap_duration": error_stats([x["error_s"] for x in laps]),
                "same_pair_gap_change": error_stats([x["error_s"] for x in gaps]),
            }
        statistics[name] = stats
        if preferred:
            deployed = [r for r in support if r["episode"] == name and r["cutoff"] == "deployment"
                        and r["source_set"] == "combined"]
            for shift in protocol["position_uncertainty_sensitivity_multipliers"]:
                test, reason = replay_localized(scenarios[name], deployed, geometry, ep, profile, shift=shift,
                                                exact_sensitivity=True)
                described = {"episode": name, "input_changed": "INITIAL_LONGITUDINAL_POSITION_ONLY",
                             "cutoff": "DEPLOYMENT", "interpretation": "DECLARED_SCENARIO_FROM_OBSERVED_BOUNDS",
                             "shift_multiples_of_s3_heuristic_bound": shift, "status": reason or "EXECUTED",
                             "fixed_target_count": len(common)}
                if test:
                    sample = _prediction(test)
                    kept = sorted(common & sample.keys())
                    described["retained_targets"] = len(kept)
                    if len(kept) == len(common):
                        described["fixed_population_error"] = error_stats([(sample[k]-targets[k]).total_seconds()
                                                                            for k in kept])
                sensitivity.append(described)
    return comparisons, statistics, relative, sensitivity, variants, live_results
