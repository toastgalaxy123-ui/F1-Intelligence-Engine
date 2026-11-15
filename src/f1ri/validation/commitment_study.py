"""S6-B.1 reproduction, bounded boundary search and recommendation support."""
from dataclasses import asdict, replace
from hashlib import sha256
from itertools import pairwise
from json import loads
from pathlib import Path
from zipfile import ZipFile

import yaml

from f1ri.simulator.events import EventKind
from f1ri.simulator.race_control import ControlCommand as C
from f1ri.simulator.strategy import fingerprint
from f1ri.simulator.strategy_commitment import (
    ANCHORS,
    CONTRACT,
    FIXED,
    FOCAL,
    ORIGINAL,
    REPLAN,
    REVALIDATE,
    alternatives,
)
from f1ri.simulator.strategy_robust import PHYSICAL, ScenarioFamily, evaluate_family
from f1ri.validation.neutralization_scenarios import controls as scripted_controls
from f1ri.validation.optimization_scenarios import fixture_request
from f1ri.validation.proximity_pilot import _json
from f1ri.validation.robust_scenarios import _study, control_script, neutralization, variant
from f1ri.validation.robust_study import _write_table, verified_robust_model
from f1ri.validation.stage3_r_release import REPORT
from f1ri.validation.stage3_r_sources import digest
from f1ri.validation.strategy_study import _invariants

PROTOCOL = "config/experiments/stage6_s6_b1.yaml"
COMPARATORS = {"original": "s6-b-v1-library-37", "expanded": "s6-b1-v1-library-42"}
FROZEN = {
    "artifacts/reports/stage6_s6_b_metrics.json": "c1d724eb9a00f86601e0bd8b0ba7e4a9490bf6e06ac0de9cb917545a29b4c4b8",
    "artifacts/reports/stage6_s6_b_ledgers.zip": "8a6dc557e8a9948267985a417dea74df0da905a7cb1ce33e31cb4fbf94788d8b",
}


def verified_baseline(root):
    if any(digest(root/p) != h for p, h in FROZEN.items()):
        raise ValueError("Frozen S6-B inputs differ.")
    with ZipFile(root/next(p for p in FROZEN if p.endswith(".zip"))) as z:
        checked = {p: h for h, p in (line.split("  ", 1) for line in z.read("SHA256SUMS").decode().splitlines())}
        if len(checked) != 29 or set(z.namelist()) != set(checked)|{"SHA256SUMS"} or any(
            sha256(z.read(p)).hexdigest() != h for p, h in checked.items()
        ):
            raise ValueError("Frozen S6-B archive member checksums differ.")
        rankings = loads(z.read("robust_rankings.json"))
    bundle, hashes = verified_robust_model(root)
    return bundle, rankings, hashes|FROZEN, checked


def _checkpoint_clocks(run, marker):
    return {r["checkpoint"]: r["clock_us"] for r in run["trajectory"] if r["driver"] == r["event_driver"] == 63
            and r["event"] in {"CHECKPOINT_CROSSING", "LAP_COMPLETED", "ON_TRACK_PASS"} and r["checkpoint"] <= marker}


def reproduce(families, bundle, rankings):
    original = {}
    for family in families[:2]:
        r = original[family.family_id] = evaluate_family(family, bundle)
        if r["analysis"]["rows"] != rankings[family.family_id]["results"]:
            raise ValueError(f"Frozen S6-B ranking differs for {family.family_id}.")
    stress = original["neutralization-timing"]
    case = stress["cases"]["sc-deploy-58"]
    pivot = stress["oracles"]["sc-deploy-58"]
    policies = tuple(dict.fromkeys((*ANCHORS, *pivot["winners"])))
    runs = case["paired"]["results"]
    clocks = _checkpoint_clocks(runs[FIXED], 106)
    adaptive = _checkpoint_clocks(runs[ORIGINAL], 106)
    compared = [{"checkpoint": cp, "fixed_us": clocks[cp], "adaptive_us": adaptive[cp],
                 "adaptive_minus_fixed_us": adaptive[cp]-clocks[cp]} for cp in sorted(clocks.keys() & adaptive.keys())]
    decisions = {p: {k: case["rows"][p][k] for k in ("objective_us", "forecast_support", "markers")}
                 | {"pit": runs[p]["pit"], "decision": runs[p]["decisions"], "control": runs[p]["race_control"],
                    "pit_events": runs[p]["pit_events"], "first_observation_us": next(
                        r["clock_us"] for r in runs[p]["trajectory"]
                        if r["event"] in {k.name for k in EventKind if k <= EventKind.STRATEGY_REVIEW})}
                 for p in policies}
    regret = stress["analysis"]["regret"][ORIGINAL]["sc-deploy-58"]
    fixed = next(r["worst_regret_us"] for r in stress["analysis"]["rows"] if r["policy"] == FIXED)
    if regret != 46_754_185 or fixed != 13_518_744 or compared[-1]["adaptive_minus_fixed_us"] != regret:
        raise ValueError("Published failure or marker difference did not reproduce.")
    return original, {"regret_us": regret, "fixed_worst_regret_us": fixed, "marker_gap": compared,
                      "outcomes": decisions, "oracle": pivot, "classification": "EXPECTED_COMMITMENT_BEHAVIOR"}


def offset_case(base, flag, deployment, *, duration=None, key=None):
    duration = duration or (92 if flag == "SC" else 75)
    label = key or f"{flag.lower()}-{deployment}"
    return variant(base, label, f"{flag} deployment +{deployment} us and duration {duration} s",
                   controls=control_script(base, state=flag, deployment=deployment/1e6,
                                           release=deployment/1e6+duration))


def _signature(result, case):
    entry = result["cases"][case]["rows"][ORIGINAL]
    visits = entry["visited_pits"]
    times = result["cases"][case]["paired"]["results"][ORIGINAL]["pit"]
    return (tuple(result["selected"]), entry["status"],
            visits[0]["target_lap"] if visits else None, str(times[0]["track_state_at_entry"]) if times else None)


def refine(base, bundle, protocol):
    """Locate observed transitions using bounded midpoint probes. No monotonicity hypothesis."""
    matrices, ledger, transitions = {}, {}, []
    extra = alternatives(base)
    for flag in ("SC", "VSC"):
        points, probe_budget = {}, protocol["refinement"]["maximum_midpoints_per_flag"]

        def observe(at, points=points, flag=flag):
            if at not in points:
                case = offset_case(base, flag, at)
                r = evaluate_family(ScenarioFamily(f"probe-{case.scenario_id}", base, (case,)), bundle,
                                    extra_policies=extra)
                points[at] = _signature(r, case.scenario_id)
            return points[at]

        seeds = protocol["deployment_seeds_us"]
        for val in seeds:
            observe(val)
        intervals = [[a, b] for a, b in pairwise(seeds) if points[a] != points[b]][
            :protocol["refinement"]["maximum_coarse_brackets_per_flag"]]
        original = [pair.copy() for pair in intervals]
        for step in range(probe_budget if intervals else 0):
            pair = intervals[step%len(intervals)]
            lo, hi = pair
            if hi-lo <= 1:
                continue
            mid = (lo+hi)//2
            pair[0 if observe(mid) == points[lo] else 1] = mid
        for source, (lo, hi) in zip(original, intervals, strict=True):
            transitions.append({"flag": flag, "initial_interval_us": source, "observed_interval_us": [lo, hi],
                                "width_us": hi-lo, "left": points[lo], "right": points[hi]})
        matrices[flag] = ScenarioFamily(f"C-{flag.lower()}-boundary", base,
                                         tuple(offset_case(base, flag, p) for p in sorted(points)))
        ledger[flag] = {"maximum_midpoints": probe_budget, "actual_midpoints": len(points)-len(seeds),
                        "points": {p: points[p] for p in sorted(points)},
                        "note": "Adjacent probe classifications differ. Other transitions may lie between probes."}
    return tuple(matrices.values()), ledger, transitions


def _new_families(root, base, prior, protocol):
    # Reuse the previously declared physical alternatives without pooling unlike initial states.
    by = {f.family_id: f for f in prior}
    yield replace(by["primary"], family_id="A-primary")
    yield replace(by["neutralization-timing"], family_id="B-stress")
    yield replace(by["initial-gap"], family_id="D-gap")
    yield replace(by["future-traffic"], family_id="D-opponent")
    yield replace(by["team-combined"], family_id="D-shared-box")
    for name in ("clean-air", "wider-gap", "original-opponent"):
        req = fixture_request(root, name)
        yield ScenarioFamily(f"D-{name}", req, neutralization(req))
    wider = fixture_request(root, "wider-gap")
    yield ScenarioFamily("E-wider-gap-stress", wider, (
        variant(wider, "green", "GREEN with wider declared gap"),
        offset_case(wider, "SC", 58_000_000), offset_case(wider, "VSC", 89_000_000)))
    duration = protocol["duration"]
    rows = []
    for flag in ("SC", "VSC"):
        low = flag.lower()
        dep = duration[f"{low}_late_deployment_seconds"]*1_000_000
        rows.extend(offset_case(base, flag, dep, duration=d, key=f"{low}-duration-{d}")
                    for d in duration[f"{low}_durations_seconds"])
    rows.append(variant(base, "sc-restart-faster", "SC +58 s, restart pace +10%",
                        category=PHYSICAL, controls=offset_case(base, "SC", 58_000_000).request.scenario.controls,
                        profile=replace(base.scenario.profile, restart_speed_mps=base.scenario.profile.restart_speed_mps*1.1)))
    yield ScenarioFamily("C-duration", base, tuple(rows))
    for horizon in (3, 5, 10):
        req = fixture_request(root, "two-car", horizon=horizon, budget=128)
        long = fixture_request(root, "two-car", horizon=10, budget=128)
        yield ScenarioFamily(f"H{horizon}", req, (variant(req, "green", "GREEN"),
                                                    offset_case(req, "SC", 58_000_000)),
                             library_reference=long)


def boundary_controls(base, bundle):
    """Pin before/equal/after GREEN controls to a separately executed unresolved entry."""
    clock = base.scenario.initial.race_clock_us
    scripts = {
        "VSC": [(C.VSC, 5), (C.VSC_ENDING, 10)],
        "SC": [(C.SC, 5), (C.SC_IN, 15), (C.SC_WITHDRAWN, 20)],
    }
    meta, cases = {}, []
    for kind, prefix in scripts.items():
        script = scripted_controls(base.scenario.initial, [(cmd, clock/1e6+offset) for cmd, offset in
                                                           (*prefix, (C.GREEN, 135))])
        hold = variant(base, kind.lower()+"-hold", "Declared prefix with delayed GREEN", controls=script)
        observed = evaluate_family(ScenarioFamily("prefix-entry", base, (hold,), include_oracle=False), bundle)
        entry = observed["cases"][hold.scenario_id]["rows"][ORIGINAL]["visited_pits"][0]["entry_us"]
        meta[kind] = {"prefix_entry_us": entry, "held_green_us": script[-1].at_us}
        cases.append(hold)
        for label, shift in (("before", -1), ("equal", 0), ("after", 1)):
            cases.append(variant(base, f"{kind.lower()}-release-{label}", f"GREEN at prefix entry {shift:+d} us",
                                 controls=(*script[:-1], replace(script[-1], at_us=entry+shift))))
    for label, shift in (("before", -1), ("equal", 0), ("after", 1)):
        cases.append(offset_case(base, "SC", 29_967_958+shift, key=f"sc-deployment-{label}"))
    return ScenarioFamily("C-exact-event-order", base, tuple(cases)), meta


def _summary(family, result):
    scenario = {v.scenario_id: v for v in family.scenarios}
    return {"selected": result["selected"], "near": result["near"], "status": result["status"],
            "search": result["search"], "horizon": family.initial.scenario.horizon_laps,
            "executable_comparator_policies": sorted(result["policies"]),
            "executable_comparator_hash": fingerprint(result["policies"]),
            "rankings": result["analysis"]["rows"], "oracles": result["oracles"],
            "outcomes": {s: {p: {"delta_us": result["analysis"]["deltas"][p][s],
                                "regret_us": result["analysis"]["regret"][p][s],
                                "oracle_regret_us": result["analysis"]["perfect_information_regret"][p][s],
                                **{k: r[k] for k in ("objective_us", "following_us", "effective_racing_us", "latent_racing_us",
                                                   "neutralization_us", "restart_us", "pit_route_us", "status",
                                                   "marker_note", "forecast_support", "visited_pits", "committed_actions")
                                   if k in r}}
                             for p, r in c["rows"].items() if p in FOCAL}
                         for s, c in result["cases"].items()},
            "cases": {k: {"category": v.category, "description": v.changed_assumptions}
                      for k, v in scenario.items()}}


def recommendation_support(summaries, transitions):
    """Qualify the result with comparisons and evidence gates, without arbitrary time cutoffs."""
    primary, stress = (summaries[k] for k in ("A-primary", "B-stress"))
    alternatives_by_family = {k: v["selected"] for k, v in summaries.items()}
    physical = primary["outcomes"]["green"]
    hard, soft = (physical[p] for p in (FIXED, ORIGINAL))
    # Require same time and different actual actions with materialized headway attribution.
    dominated = (hard["following_us"] > 0 or soft["following_us"] > 0) and (
        hard["visited_pits"] != soft["visited_pits"])
    statuses = ["ROBUST_WITHIN_DECLARED_FAMILY"] if primary["selected"] else []
    if set(primary["selected"]) != set(stress["selected"]):
        statuses.append("FAMILY_SENSITIVE")
    if transitions:
        statuses.append("BOUNDARY_SENSITIVE")
    if any(s["near"] for s in summaries.values()):
        statuses.append("NEAR_EQUAL_ALTERNATIVES")
    if dominated and all(r["delta_us"] == 0 for r in (hard, soft)):
        statuses.append("CONSTRAINT_DOMINATED_IDENTIFIABILITY")
    if any(v["status"] == "ROBUST_SEARCH_INCOMPLETE" for v in summaries.values()):
        statuses.append("SEARCH_INCOMPLETE")
    rejected = [r for v in summaries.values() for r in v["rankings"] if r["first_failure"]]
    for label, tokens in (("MODEL_SUPPORT_LIMITED", ("FORECAST", "MODEL", "REQUEST_UNSUPPORTED")),
                          ("PHYSICAL_SUPPORT_LIMITED", ("PHYSICAL", "MARKER", "PIT", "GEOMETRY", "STRATEGY_ACTION"))):
        if any(any(token in str(r["first_failure"]) for token in tokens) for r in rejected):
            statuses.append(label)
    unique = len(primary["selected"]) == 1 and statuses == ["ROBUST_WITHIN_DECLARED_FAMILY"]
    if not unique:
        statuses.append("NO_UNIQUE_RECOMMENDATION")
    return {"version": "s6-b1-recommendation-support-v1", "statuses": statuses, "unique_action": (
        primary["selected"][0] if unique else None), "family_choices": alternatives_by_family,
        "primary_margin_us": 1_440_000, "clock_unit": "microseconds", "scenario_probabilities_estimated": False}


def _extract(name, r, execution, commitments, controls, outcomes, checks):
    for sid, case in r["cases"].items():
        if case["paired"] is None:
            continue
        for p, run in case["paired"]["results"].items():
            context = {"family": name, "scenario": sid, "policy": p}
            summary = case["rows"][p]
            base = {k: v for k, v in summary.items() if k not in ("committed_actions", "visited_pits")}
            analysis = r["analysis"]
            outcomes.append(context | base | {"regret_us": analysis["regret"].get(p, {}).get(sid),
                                               "oracle_regret_us": analysis["perfect_information_regret"].get(p, {}).get(sid),
                                               "reference_delta_us": analysis["deltas"].get(p, {}).get(sid)})
            execution.append(context | {"trace_sha256": fingerprint(run), "decisions": run["decisions"],
                                        "visits": run["pit"], "trajectory": run.get("trajectory", []),
                                        "forecasts": run.get("forecast_requests", []),
                                        "constraints": run.get("checkpoint_constraints", [])})
            commitments.extend(context|dict(row) for row in run.get("pit_events", []))
            commitments.extend(context|{"status": row["outcome"], **row} for row in run["decisions"])
            controls.extend(context|row for row in run.get("race_control", []))
    checks.update({f"{name}/{v.scenario_id}": _invariants(v.request.scenario, r["cases"][v.scenario_id]["paired"])
                   for v in r["_family"].scenarios if r["cases"][v.scenario_id]["paired"]})


def run_study(root: Path, out: Path):
    if out.exists():
        raise FileExistsError(out)
    bundle, rankings, hashes, members = verified_baseline(root)
    protocol = yaml.safe_load((root/PROTOCOL).read_bytes())
    prior = tuple(_study(root))
    print("S6-B.1 frozen replay", flush=True)
    originals, baseline = reproduce(prior, bundle, rankings)
    base = prior[0].initial
    print("S6-B.1 bounded boundary probes", flush=True)
    boundary, probes, transitions = refine(base, bundle, protocol)
    exact, exact_clocks = boundary_controls(base, bundle)
    families = (*_new_families(root, base, prior, protocol), *boundary, exact)
    summaries, invariant, equivalence = {}, {}, {}
    execution, commitments, controls, outcomes = [], [], [], []
    for family in families:
        print(f"S6-B.1 {family.family_id}", flush=True)
        r = evaluate_family(family, bundle, extra_policies=alternatives(family.library_reference or family.initial))
        summaries[family.family_id] = _summary(family, r)
        equivalence[family.family_id] = [p for p in r["pairs"] if p["a"] in FOCAL and p["b"] in FOCAL]
        _extract(family.family_id, r|{"_family": family}, execution, commitments, controls, outcomes, invariant)
    for transition in transitions:
        low, high = transition["observed_interval_us"]
        flag = transition["flag"].lower()
        group = summaries[f"C-{flag}-boundary"]
        transition["endpoint_evidence"] = {
            str(x): group["outcomes"][f"{flag}-{x}"][ORIGINAL] for x in (low, high)}
        transition["horizon"] = group["horizon"]
        transition["complete"] = group["search"]["fixed_space_exhausted"]
    contract = recommendation_support(summaries, transitions)
    bad = [{"scenario": case, "policy": p, "invariant": k} for case, runs in invariant.items()
           for p, check in runs.items() for k, val in check.items() if not val]
    worst = summaries["B-stress"]["outcomes"]["sc-deploy-58"]
    negative = any(row[k] < 0 for row in outcomes for k in ("regret_us", "oracle_regret_us") if row[k] is not None)
    checks = {"frozen_failure": baseline["regret_us"] == 46_754_185,
              "frozen_fixed_worst": baseline["fixed_worst_regret_us"] == 13_518_744,
              "no_negative_regret": not negative,
              "cancel_before_entry": not worst[REVALIDATE]["visited_pits"] and worst[REVALIDATE]["delta_us"] == 0,
              "single_revised_fit": len(worst[REPLAN]["visited_pits"]) == 1 and
              worst[REPLAN]["visited_pits"][0]["target_lap"] == 33,
              "family_sensitive": "FAMILY_SENSITIVE" in contract["statuses"],
              "transitions_detected": bool(transitions),
              "exact_green_boundary": all(
                  not summaries[exact.family_id]["outcomes"][f"{flag}-release-before"][REVALIDATE]["visited_pits"]
                  and all(len(summaries[exact.family_id]["outcomes"][f"{flag}-release-{point}"][REVALIDATE][
                      "visited_pits"]) == 1 for point in ("equal", "after"))
                  for flag in ("sc", "vsc"))}
    metrics = {"stage": protocol["version"], "baseline_tests": 373, "families": len(summaries),
               "evaluations": sum(x["search"]["simulator_evaluations"] for x in summaries.values()),
               "boundary_probes": sum(len(r["points"]) for r in probes.values()), "original_regret_us": baseline["regret_us"],
               "fixed_worst_regret_us": baseline["fixed_worst_regret_us"], "checks": checks,
               "invariant_failures": bad, "statuses": contract["statuses"],
               "decision": ("PATH_A_TIMING_ROBUSTNESS_AND_POLICY_SEMANTICS_VALIDATED" if not bad and all(checks.values())
                            else "PATH_B_COMMITMENT_OR_SCHEDULING_REVIEW_REQUIRED")}
    out.mkdir(parents=True)
    for key, data in {
        "protocol": protocol, "frozen_baseline": baseline, "model_manifest": loads((root/REPORT).read_bytes()),
        "scenario_family_manifest": {f.family_id: asdict(f) for f in families},
        "policy_semantics": {p.strategy_id: asdict(p) for p in alternatives(base)},
        "commitment_contract": CONTRACT, "timing_grid": {k: v["cases"] for k, v in summaries.items()},
        "boundary_search": probes|{"exact_order_probe_clocks": exact_clocks}, "boundary_transitions": transitions,
        "regret_comparisons": summaries,
        "comparator_versions": COMPARATORS | {"original_verified": {k: x["search"] for k, x in originals.items()},
                                             "replacement_oracle": "Expanded executable library plus S6-A oracle-only clocks"},
        "policy_equivalence": equivalence,
        "constraint_attribution": {k: v["outcomes"] for k, v in summaries.items() if k.startswith(("D", "E", "B"))},
        "horizon_stability": {k: v for k, v in summaries.items() if k in ("H3", "H5", "H10")},
        "search_completeness": {k: s["search"] for k, s in summaries.items()}, "recommendation_support": contract,
        "invariant_checks": invariant, "regression_parity": {"frozen": hashes, "archive_member_sha256": members},
        "metrics": metrics,
    }.items():
        _json(out/f"{key}.json", data)
    for name, rows in {"policy_execution_ledger": execution, "pit_commitment_ledger": commitments,
                       "race_control_timeline": controls, "scenario_outcomes": outcomes}.items():
        _write_table(out/f"{name}.parquet", rows)
    from f1ri.validation.commitment_report import finish_report

    finish_report(out, baseline, summaries, transitions, metrics, contract)
    return out/"metrics.json"
