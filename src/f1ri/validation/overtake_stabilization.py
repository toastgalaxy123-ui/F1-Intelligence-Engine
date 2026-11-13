"""S4-B.1 frozen-cohort evidence audit and alternate-window follow-up."""
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import timedelta
from hashlib import file_digest
from statistics import median

import yaml

from f1ri.features.overtake_opportunities import outcome
from f1ri.simulator.pace_adapter import load_pace_bundle
from f1ri.validation.overtake_evidence import (
    audit_event,
    continuous_components,
    first_loss,
    sample_support,
    union_duration,
    utc,
)
from f1ri.validation.overtake_gap_audit import acquisition_assessment, gap_census
from f1ri.validation.overtake_historical_study import _load_json, _retirements, _source_windows
from f1ri.validation.overtake_history import RaceHistory
from f1ri.validation.overtake_readiness import _ndjson
from f1ri.validation.proximity_pilot import _json
from f1ri.validation.traffic_encounter_audit import window_rows
from f1ri.validation.traffic_history_study import _sha


def _hash(path):
    with path.open("rb") as raw:
        return file_digest(raw, "sha256").hexdigest()


def _verify(root, stage3, frozen, replay, config):
    reports = root / "artifacts/reports"
    names = {"s4_b": "stage5_s4_b_historical_overtakes", "s4_a": "stage5_s4_a_overtaking_mechanics",
             "c4": "stage5_s3_c4_clean_reference_validation"}
    hashes = {k: _sha(reports / f"{v}_metrics.json") for k, v in names.items()}
    hashes["d8"] = load_pace_bundle(root, stage3).artifact_sha256
    expected = dict(line.split("  ", 1)[::-1] for line in (frozen / "SHA256SUMS").read_text().splitlines())
    generated = {p.name: _hash(p) for p in replay.iterdir() if p.is_file()}
    if hashes != config["frozen"] or generated.get("metrics.json") != hashes["s4_b"]:
        raise ValueError("Frozen baseline hashes differ")
    if any(expected.get(name) != h or _hash(frozen / name) != h for name, h in generated.items()):
        raise ValueError("S4-B replay differs from the frozen generated ledgers")
    if set(generated) != set(expected) - {"validation_report.md"}:
        raise ValueError("S4-B replay is incomplete")
    return hashes, generated


def _rows(path):
    import json

    with path.open(encoding="utf-8") as source:
        for line in source:
            yield json.loads(line)


def risk_record(row, fixed, failure=None):
    at = utc(row["cutoff_utc"])
    terminal = utc(fixed["observed_until_utc"])
    code = fixed["outcome"]
    lo, hi = terminal, terminal
    if code == "PASS_OBSERVED":
        lo, hi = map(utc, (fixed["event_time_lower_utc"], fixed["event_time_upper_utc"]))
    elif failure:
        lo, hi = failure["last_supported_utc"], failure["first_unsupported_utc"]
    elif code == "AMBIGUOUS_OUTCOME" and fixed["event_time_lower_utc"]:
        lo = max(at, utc(fixed["event_time_lower_utc"]))
        hi = terminal
    elif code != "NO_PASS_OBSERVED":
        # The archived stop may be the first unsupported tick. Without a
        # resolved previous tick, avoid crediting that interval as exposure.
        lo = at if fixed["reason"] in ("ARCHIVE_DISAGREES_AT_CUTOFF", "CHECKPOINT_UNAVAILABLE") else terminal
    return {
        "opportunity_id": row["opportunity_id"], "encounter_id": row["encounter_id"],
        "session": row["session"], "pair": [row["attacker"], row["target"]],
        "source": row["source"], "window": row["window"], "entry_utc": at,
        "planned_end_utc": fixed["planned_end_utc"], "termination": code,
        "termination_lower_utc": lo, "termination_upper_utc": hi,
        "at_risk_s": (lo-at).total_seconds(), "pass_event_id": fixed["pass_event_id"],
        "pass_bracket": [fixed["event_time_lower_utc"], fixed["event_time_upper_utc"]]
        if fixed["pass_event_id"] else None,
        "lap_relation": row["lap_relation"], "first_eligible_state": {
            "gap_m": row["gap_m"], "bounds_m": [row["gap_lower_m"], row["gap_upper_m"]],
            "field_coverage": row["field_coverage"], "latest_observation_utc": row["latest_feature_observation_utc"],
        },
    }


def _failure(row, fixed, context, history, alternatives):
    frames, neighbors, times, period, length = context
    at = utc(row["cutoff_utc"])
    end = utc(fixed["planned_end_utc"])
    stop = utc(fixed["observed_until_utc"])
    pair = row["attacker"], row["target"]
    i = bisect_right(times, stop)
    if fixed["reason"] == "SAMPLING_GAP":
        lower, upper = stop, stop + timedelta(seconds=period)
        flags, details = {"grid": "SAMPLING_GAP"}, {}
    else:
        lower, upper = (times[i-2] if i > 1 else at), stop
        lower = max(at, lower)
        flags, details = sample_support(stop, pair, frames, neighbors, length)
        if not flags:
            flags = {"pair": "UNRESOLVED_NEIGHBOR"}
    first_lo, first_hi, first_flags, _ = first_loss(at, lower, pair, frames, neighbors, times, period, length)
    late = next((tick for tick in times[i:bisect_right(times, end)]
                 if tick <= end and not sample_support(tick, pair, frames, neighbors, length)[0]), None)
    alternative = [name for name, alt in alternatives.items() if upper in alt[0]
                   and not sample_support(upper, pair, alt[0], alt[1], alt[4])[0]]
    return {
        "opportunity_id": row["opportunity_id"], "encounter_id": row["encounter_id"],
        "session": row["session"], "source": row["source"], "window": row["window"],
        "attacker": pair[0], "target": pair[1], "cutoff_utc": at, "planned_end_utc": end,
        "gap_m": row["gap_m"], "gap_bounds_m": [row["gap_lower_m"], row["gap_upper_m"]],
        "closing_mps": row["closing_mps"], "lap_relation": row["lap_relation"], "arms": row["arms"],
        "sector": row["track_segment"]["sector"], "initial_route": [r["route"] for r in row["positions"]],
        "initial_neighbor_support": row["field_coverage"],
        "last_supported_utc": lower, "first_unsupported_utc": upper,
        "strict_prefix_last_supported_utc": first_lo if first_flags else lower,
        "strict_prefix_first_unsupported_utc": first_hi if first_flags else upper,
        "last_supported_at_cutoff_only": lower == at and stop == at,
        "usable_followup_s": ((first_lo if first_flags else lower)-at).total_seconds(),
        "time_to_horizon_end_s": (end-upper).total_seconds(),
        "frozen_reason": fixed["reason"], "failure_categories": flags, "first_loss_evidence": details,
        "next_supported_utc": late, "disqualifying_events": history.interruptions(pair, at, end),
        "alternate_cached_support": sorted(alternative),
    }


def _cutoff_neighbors(folder, session, window, max_gap):
    table = "neighborhoods" if session == 9896 else "physical_neighbors"
    result = defaultdict(list)
    for row in window_rows(folder, table, [window], ["POINT_IN_TIME"]):
        if row["gap_upper_m"] is not None and row["gap_upper_m"] <= max_gap:
            result[row["as_of_utc"]].append(row)
    return result


def _coverage_failures(folder, window):
    if not (folder / "coverage_driver_snapshots.parquet").exists():
        return {}
    return {(r["as_of_utc"], r["driver_number"]): r["failure"] for r in
            window_rows(folder, "coverage_driver_snapshots", [window], ["RETROSPECTIVE"]) if r["failure"]}


def _groups(frozen, outcomes, risks, failures, config, roles):
    competitive = [r for r in frozen if r["lap_relation"] == "SAME_LAP"]
    by_id = {r["opportunity_id"]: r for r in frozen}
    risks_by_race, contact, spans, stints = defaultdict(list), defaultdict(set), defaultdict(list), set()
    confirmed = defaultdict(set)
    competitive_ids = {r["opportunity_id"] for r in competitive}
    for r in competitive:
        key = r["session"], *sorted((r["attacker"], r["target"]))
        contact[key].add((utc(r["cutoff_utc"]), r["period_s"]))
        tires = r["tyre"]
        if all(t and t["stint"] is not None for t in tires):
            stints.add((r["session"], r["attacker"], r["target"], *(t["stint"] for t in tires)))
    for risk in risks:
        if risk["opportunity_id"] not in competitive_ids:
            continue
        key = risk["session"], *sorted(risk["pair"])
        spans[key].append((risk["entry_utc"], risk["termination_lower_utc"]))
        risks_by_race[risk["session"]].append(risk)
        if risk["termination"] == "PASS_OBSERVED":
            confirmed[risk["session"]].add(risk["pass_event_id"])
    by_race = {}
    for key, rows in sorted(risks_by_race.items()):
        fixed = [outcomes[r["opportunity_id"]] for r in rows]
        controls = {r["encounter_id"] for r, f in zip(rows, fixed, strict=True) if f["outcome"] == "NO_PASS_OBSERVED"}
        censored = {r["encounter_id"] for r, f in zip(rows, fixed, strict=True)
                    if f["outcome"].startswith("CENSORED") or f["outcome"] in ("AMBIGUOUS_OUTCOME", "HORIZON_INCOMPLETE")}
        by_race[key] = {
            "role": roles[key], "cutoffs": len(rows), "encounters": len({r["encounter_id"] for r in rows}),
            "observed_non_pass_encounters_10s": len(controls), "unresolved_encounters_10s": len(censored),
            "encounters_both": len(controls & censored), "risk_pass_events": len(confirmed[key]),
            "at_risk_pair_union_s": sum(union_duration(v) for p, v in spans.items() if p[0] == key),
            "core_predictor_cutoffs": sum(by_id[r["opportunity_id"]]["predictors"] for r in rows),
        }
    return {
        "all_cutoffs": len(frozen), "all_frozen_encounters": len({r["encounter_id"] for r in frozen}),
        "race_pairs": len({(r["session"], *sorted((r["attacker"], r["target"]))) for r in frozen}),
        "competitive_cutoffs": len(competitive),
        "competitive_frozen_encounters": len({r["encounter_id"] for r in competitive}),
        "competitive_directed_stint_groups": len(stints),
        "competitive_stint_linked_cutoffs": sum(all(t and t["stint"] is not None for t in r["tyre"])
                                               for r in competitive),
        "competitive_contiguous_sample_components": sum(
            continuous_components(sorted(t), config["contact_max_step_multiplier"]) for t in contact.values()),
        "competitive_pair_groups": len(contact), "race_count": len(by_race), "by_race": by_race,
        "thresholds": {
            m: {"cutoffs": len(rows), "outcomes_10s": dict(Counter(outcomes[r["opportunity_id"]]["outcome"] for r in rows)),
                "encounters": len({r["encounter_id"] for r in rows})}
            for m in config["opportunity_thresholds_m"]
            for rows in [[r for r in competitive if m in r["eligible_gap_thresholds_m"]]]
        },
        "positive_cutoffs_10s": sum(r["outcome"] == "PASS_OBSERVED" for r in outcomes.values()),
        "positive_events_10s": len({r["pass_event_id"] for r in outcomes.values() if r["outcome"] == "PASS_OBSERVED"}),
        "unique_localization_censored_encounters": len({r["encounter_id"] for r in failures}),
    }


def _descriptive(rows, fixed, failures):
    strata = defaultdict(Counter)
    values = defaultdict(lambda: defaultdict(list))
    reasons, detailed, sides = Counter(), Counter(), Counter()
    for r in rows:
        label = fixed[r["opportunity_id"]]["outcome"]
        for field in ("gap_m", "gap_uncertainty_m", "closing_mps", "lap", "relative_pace_s",
                      "field_close_relationships"):
            if r[field] is not None:
                values[label][field].append(r[field])
        for arm in r["arms"] or ["THRESHOLD_ONLY"]:
            strata[f"arm/{arm}"][label] += 1
        for key, value in (
            ("race", r["session"]), ("window", f"{r['session']}/{r['source']}/{r['window']}"),
            ("sector", f"{r['session']}/{r['track_segment']['sector']}"),
            ("pair", f"{r['session']}/{r['attacker']}-{r['target']}"), ("gap", r["gap_band"]),
            ("lap_relation", r["lap_relation"]), ("field_support", r["field_coverage"]),
            ("closing", r["closing_band"]), ("quality", r["quality"]),
            ("pace_supported", r["relative_pace_s"] is not None),
        ):
            strata[f"{key}/{value}"][label] += 1
    for row in failures:
        reasons.update(set(row["failure_categories"].values()))
        detailed.update(set(row["frozen_coverage_detail"].values()))
        sides.update(row["failure_categories"].keys())
    profile = {}
    for label, fields in sorted(values.items()):
        profile[label] = {}
        for field, data in sorted(fields.items()):
            data.sort()
            profile[label][field] = {"n": len(data), "p25": data[len(data)//4], "median": median(data),
                                     "p75": data[3*len(data)//4]}
    return {"censoring_causes": dict(sorted(reasons.items())),
            "archived_interpolation_detail": dict(sorted(detailed.items())),
            "failure_by_role": dict(sorted(sides.items())),
            "outcome_feature_profiles": profile,
            "outcome_strata": {k: dict(v) for k, v in sorted(strata.items())}}


def _summarize_risk(risks):
    buckets = defaultdict(Counter)
    for risk in risks:
        buckets[risk["termination"]][int(risk["at_risk_s"])] += 1
    return {label: dict(sorted(counts.items())) for label, counts in sorted(buckets.items())}


def _population(rows, fixed, roles):
    groups = defaultdict(list)
    for r in rows:
        if r["lap_relation"] == "SAME_LAP" and roles[r["session"]] == "development":
            groups[r["session"], r["encounter_id"]].append(fixed[r["opportunity_id"]]["outcome"])
    # Count unresolved encounter groups once. Extreme bounds are arithmetic
    # sensitivities on these groups, not estimators or fitted probabilities.
    states = Counter("positive" if "PASS_OBSERVED" in v else
                     "all_observed_non_pass" if set(v) == {"NO_PASS_OBSERVED"} else "unresolved"
                     for v in groups.values())
    n = len(groups)
    by_race = {}
    for race in sorted({r for r, _ in groups}):
        n_race = sum(r == race for r, _ in groups)
        n_pos = sum(r == race and "PASS_OBSERVED" in v for (r, _), v in groups.items())
        by_race[race] = {"encounter_groups": n_race, "positive_groups": n_pos,
                         "apparent_fraction": n_pos / n_race}
    fractions = [r["apparent_fraction"] for r in by_race.values()]
    return {"unit": "development race and frozen same-lap encounter, any positive 10s cutoff",
            "states": dict(states), "lower_fraction": states["positive"]/n if n else None,
            "upper_fraction": (states["positive"] + states["unresolved"])/n if n else None,
            "resolved_only_fraction": states["positive"]/(states["positive"] + states["all_observed_non_pass"])
            if states["positive"] + states["all_observed_non_pass"] else None,
            "leave_one_race_out_observed_fraction_range": [min(fractions), max(fractions)] if len(fractions) == 2
            else None,
            "by_race": by_race}


def run_stabilization(root, warehouse, stage3, frozen, replay, output, plan):
    config = yaml.safe_load(plan.read_bytes())
    if output.exists():
        raise FileExistsError(f"Choose a new S4-B.1 output directory: {output}")
    if config["api_request_budget"] != 0:
        raise ValueError("S4-B.1 only uses cached data")
    hashes, generated = _verify(root, stage3, frozen, replay, config)
    inputs = _load_json(frozen / "input_manifest.json")
    if _hash(warehouse) != inputs["warehouse_sha256"]:
        raise ValueError("The warehouse differs from the frozen S4-B input")
    sources = _load_json(frozen / "source_availability.json")
    entries = {(r["session"], r["source"]): r for r in sources}
    roles = {r["session"]: r["role"] for r in sources}
    retirements = _retirements(root)
    histories = {key: RaceHistory(warehouse, key, retirements.get(key, {})) for key in roles}
    events = _load_json(frozen / "supported_pass_events.json")
    dossiers = []
    for e in events:
        if e["event_type"] == "SAME_LAP_RACING_PASS":
            entry = entries[e["session"], e["source"]]
            geometry = _load_json(root / entry["folder"] / "geometry.json")
            dossiers.append(audit_event(e, config, entry | geometry, histories[e["session"]], root))
    by_event = {r["pass_event_id"]: r for r in dossiers}
    event_pairs = defaultdict(list)
    for r in _load_json(frozen / "physical_reversals.json"):
        row = r | {k: utc(r[k]) for k in ("event_time_lower_utc", "event_time_upper_utc")}
        if r["pass_event_id"] in by_event and by_event[r["pass_event_id"]]["decision"] in (
            "TIER_C_AMBIGUOUS", "EXCLUDED"
        ):
            row["event_type"] = "UNCERTAIN_PHYSICAL_REVERSAL"
        event_pairs[r["session"], r["attacker"], r["target"]].append(row)
    fixed, maximum, reassessed_horizons = {}, {}, defaultdict(Counter)
    reassessed_event_ids = {k for k, r in by_event.items() if r["decision"] in ("TIER_C_AMBIGUOUS", "EXCLUDED")}
    for r in _rows(frozen / "outcomes.ndjson"):
        revised = ("AMBIGUOUS_OUTCOME" if r["outcome"] == "PASS_OBSERVED"
                   and r["pass_event_id"] in reassessed_event_ids else r["outcome"])
        reassessed_horizons[r["horizon"]][revised] += 1
        if r["horizon"] == config["audit_horizon"]:
            fixed[r["opportunity_id"]] = r
        if r["horizon"] == str(config["risk_maximum_s"]):
            maximum[r["opportunity_id"]] = r
    input_rows = defaultdict(list)
    tyres = _load_json(frozen / "retrospective_tyre_context.json")
    for r in _rows(frozen / "opportunities.ndjson"):
        input_rows[r["session"], r["source"], r["window"]].append(r)
    failures, risks, population, recovered, comparisons = [], [], [], [], []
    for key in sorted(roles):
        history = histories[key]
        contexts = {}
        for (session, source), entry in entries.items():
            if session != key:
                continue
            for f, digest in entry["files"].items():
                if _hash(root / entry["folder"] / f) != digest:
                    raise ValueError(f"Source changed for {source}/{session}/{f}")
            for window, geometry, layers, period in _source_windows(
                root / entry["folder"], session, entry, outcome_only=True
            ):
                frames, neighbors = layers["RETROSPECTIVE"]
                contexts[source, window] = frames, neighbors, sorted(frames), period, geometry["length_m"]
        for (source, window), context in sorted(contexts.items()):
            frames, neighbors, times, period, length = context
            other_contexts = {f"{s}/{w}": c for (s, w), c in contexts.items() if (s, w) != (source, window)
                              and times[0] < c[2][-1] and c[2][0] < times[-1]}
            folder = root / entries[key, source]["folder"]
            cutoff_neighbors = _cutoff_neighbors(folder, key, window, max(config["opportunity_thresholds_m"]))
            coverage_failures = _coverage_failures(folder, window)
            for r in input_rows[key, source, window]:
                oid = r["opportunity_id"]
                at = utc(r["cutoff_utc"])
                pair = r["attacker"], r["target"]
                f10, fmax = fixed[oid], maximum[oid]
                end = utc(f10["planned_end_utc"])
                candidate_events = event_pairs[key, *pair]
                interruptions = history.interruptions(pair, at, end)
                censored = f10["outcome"] == "CENSORED_LOCALIZATION"
                record = None
                if censored:
                    record = _failure(r, f10, context, history, other_contexts)
                    record["frozen_coverage_detail"] = {
                        role: coverage_failures[record["first_unsupported_utc"], driver]
                        for role, driver in zip(("attacker", "target"), pair, strict=True)
                        if (record["first_unsupported_utc"], driver) in coverage_failures
                    }
                    failures.append(record)
                    # Alternative windows must independently cover the whole
                    # forecast interval on their native grid and meet S4-B rules.
                    for name in record["alternate_cached_support"]:
                        alt = other_contexts[name]
                        if at not in alt[0] or alt[2][-1] < end:
                            continue
                        result = outcome(r | {"cutoff_utc": at}, end, *alt[:3], candidate_events,
                                         interruptions, alt[3], alt[4])
                        if result["outcome"] in ("PASS_OBSERVED", "NO_PASS_OBSERVED"):
                            recovered.append({"opportunity_id": oid, "encounter_id": r["encounter_id"],
                                              "session": key, "alternate_source": name, **result})
                            break
                if f10["pass_event_id"] in reassessed_event_ids and f10["outcome"] == "PASS_OBSERVED":
                    comparisons.append({"opportunity_id": oid, "old": "PASS_OBSERVED", "new": "AMBIGUOUS_OUTCOME",
                                        "pass_event_id": f10["pass_event_id"]})
                event_id = fmax["pass_event_id"]
                if fmax["outcome"] == "PASS_OBSERVED" and event_id in reassessed_event_ids:
                    fmax = fmax | {"outcome": "AMBIGUOUS_OUTCOME"}
                stop = utc(fmax["observed_until_utc"])
                if fmax["pass_event_id"] and fmax["event_time_lower_utc"]:
                    stop = min(stop, max(at, utc(fmax["event_time_lower_utc"])))
                lower, upper, flags, _ = first_loss(at, stop, pair, frames, neighbors, times, period, length)
                loss = {"last_supported_utc": lower, "first_unsupported_utc": upper} if flags else None
                if fmax["outcome"] == "CENSORED_LOCALIZATION" and loss is None:
                    detail = _failure(r, fmax, context, history, {})
                    loss = {"last_supported_utc": detail["strict_prefix_last_supported_utc"],
                            "first_unsupported_utc": detail["strict_prefix_first_unsupported_utc"]}
                risk = risk_record(r, fmax, loss)
                if flags and (upper < stop or risk["termination"] == "PASS_OBSERVED") and flags.get("grid") not in (
                    "BETWEEN_ARCHIVE_TICKS", "OBSERVATION_WINDOW_TERMINATION"
                ):
                    risk.update(termination="EARLIER_UNSUPPORTED_SAMPLE", pass_event_id=None, pass_bracket=None,
                                termination_lower_utc=lower, termination_upper_utc=upper,
                                at_risk_s=(lower-at).total_seconds())
                risk["frozen_30s_termination"] = maximum[oid]["outcome"]
                risk["earliest_support_failure"] = flags
                risks.append(risk)
                a, b = r["recent_clean_laps"]
                pace = (sum(a["lap_times_s"])/len(a["lap_times_s"]) -
                        sum(b["lap_times_s"])/len(b["lap_times_s"])) if a and b else None
                upper = r["gap_upper_m"]
                # Count resolved close-following relationships in the field at
                # the cutoff. This is a field-density proxy.
                near_count = len(cutoff_neighbors.get(at, ()))
                population.append({k: r[k] for k in (
                    "opportunity_id", "encounter_id", "session", "source", "window", "cutoff_utc", "attacker", "target",
                    "lap_relation", "arms", "gap_m", "eligible_gap_thresholds_m", "field_coverage", "closing_mps",
                    "track_segment"
                )} | {"period_s": period, "relative_pace_s": pace, "field_close_relationships": near_count,
                      "quality": r["positions"][0]["quality"], "lap": r["positions"][0]["completed_laps"] + 1,
                      "gap_uncertainty_m": (upper-r["gap_lower_m"])/2, "tyre": tyres[oid],
                      "gap_band": next((str(m) for m in config["opportunity_thresholds_m"] if upper <= m), ">120"),
                      "closing_band": ("unknown" if r["closing_mps"] is None else
                                       "closing" if r["closing_mps"] > 0 else "nonclosing"),
                      "predictors": pace is not None and r["closing_mps"] is not None
                      and r["gap_approx_s"] is not None and r["weather"] is not None})
    if len(population) != len(fixed) or len(failures) != sum(f["outcome"] == "CENSORED_LOCALIZATION" for f in fixed.values()):
        raise ValueError("S4-B.1 must account for the complete frozen cutoff population")
    stabilized = fixed.copy()
    for row in comparisons:
        oid = row["opportunity_id"]
        stabilized[oid] = stabilized[oid] | {"outcome": row["new"]}
    gaps = [gap_census(root, stage3, g, histories[g["session"]], sources, config["opportunity_thresholds_m"])
            for g in config["gaps"]]
    summaries = _descriptive(population, fixed, failures)
    groups = _groups(population, stabilized, risks, failures, config, roles)
    risk_bins = _summarize_risk(risks)
    frozen_metrics = _load_json(frozen / "metrics.json")
    independent_races = sorted(k for k, role in roles.items() if role == config["competitive_role"])
    confirmed = [r for r in dossiers if r["role"] == "development" and r["decision"] == "TIER_A_CONFIRMED"]
    readiness = {
        "decision": "PATH_C", "development_races": independent_races, "tier_a_development": len(confirmed),
        "tier_b_development": sum(r["role"] == "development" and r["decision"] == "TIER_B_SUPPORTED" for r in dossiers),
        "exhaustive_independent_reference": False, "global_precision": None, "global_recall": None,
        "tier_a_structural_maximum_current_sources": sum(r["decision"] == "TIER_A_CONFIRMED" for r in dossiers),
        "optimistic_independent_event_count_relative_se": 1 / max(1, sum(
            r["role"] == "development" and r["decision"] == "TIER_B_SUPPORTED" for r in dossiers))**.5,
        "max_development_races_left_for_fit_with_one_held_out": len(independent_races) - 1,
        "development_passing_drivers": sorted({r["attacker"] for r in dossiers
                                               if r["role"] == "development"
                                               and r["decision"] in ("TIER_A_CONFIRMED", "TIER_B_SUPPORTED")}),
        "development_opponent_pairs": [[r["session"], r["attacker"], r["target"]] for r in dossiers
                                      if r["role"] == "development"
                                      and r["decision"] in ("TIER_A_CONFIRMED", "TIER_B_SUPPORTED")],
        "calibration_in_the_large": "UNSUPPORTED", "horizon_brier": "UNSUPPORTED",
        "interval_censored_score": "UNSUPPORTED",
    }
    comparison = {
        "evaluation_a": {"eligible_cutoffs": len(population), "encounters": groups["all_frozen_encounters"],
                         "brackets_changed": 0, "cutoff_changes": comparisons,
                         "reassessed_horizon_counts": {k: dict(v) for k, v in sorted(reassessed_horizons.items())},
                         "frozen_10s": dict(Counter(r["outcome"] for r in fixed.values())),
                         "reassessed_10s": dict(Counter(r["outcome"] for r in stabilized.values()))},
        "evaluation_b": {"recovered_cutoffs": len(recovered),
                         "recovered_encounters": len({r["encounter_id"] for r in recovered}),
                         "new_canonical_events": 0,
                         "recovered_outcomes": dict(Counter(r["outcome"] for r in recovered)),
                         "new_openf1_requests": 0},
    }
    metrics = {
        "version": config["version"], "frozen": hashes, "reproduced_generated_files": len(generated),
        "frozen_horizons": frozen_metrics["outcomes_by_horizon"], "tier_counts": dict(Counter(r["decision"] for r in dossiers)),
        "censoring_causes": summaries["censoring_causes"], "localized_censored": len(failures),
        "localized_censored_reappeared_10s": sum(r["next_supported_utc"] is not None for r in failures),
        "localized_censored_alternate_supported": sum(bool(r["alternate_cached_support"]) for r in failures),
        "median_localization_followup_s": median(r["usable_followup_s"] for r in failures),
        "at_risk_positive_duration_cutoffs": sum(r["at_risk_s"] > 0 for r in risks),
        "at_risk_30s_support_cutoffs": sum(r["at_risk_s"] >= 30 for r in risks),
        "groups": groups, "risk_histogram_s": risk_bins, "bounds": _population(population, stabilized, roles),
        "comparison": comparison, "readiness": readiness, "new_openf1_requests": 0,
    }
    lineage = {
        "physical_projection": ["OpenF1 location", "FIA circuit length", "reference lap mapped sector locations"],
        "gap_trajectory_and_neighbor": "Both derive from the same projected location samples",
        "openf1_timing_and_position": "Separate endpoint from location. Published timing lineage may be shared "
                                      "with the official lap chart",
        "fia_lap_chart": "Independent of physical projection. Completed-lap rank only",
        "official_pit_summary": "Route corroboration. Pit clock definitions differ from the warehouse pit record",
        "actual_live_receipt_times": None,
        "independent_video_review": False,
    }
    output.mkdir(parents=True)
    (output / "config.yaml").write_bytes(plan.read_bytes())
    for name, data in {
        "metrics": metrics, "source_manifest": inputs | {"s4_b_replay_sha256": generated, "sources": sources},
        "evidence_lineage": lineage, "competitive_pass_dossiers": dossiers,
        "tier_reassessment": [{k: r[k] for k in ("pass_event_id", "tier", "decision", "contradictions_or_limits")}
                              for r in dossiers],
        "descriptive_censoring": summaries, "encounter_independence": groups, "gap_census": gaps,
        "acquisition_assessment": acquisition_assessment(gaps, config), "recovered_evidence": recovered,
        "frozen_population_comparison": comparison, "model_feasibility": readiness,
    }.items():
        _json(output / f"{name}.json", data)
    _ndjson(output / "localization_censoring.ndjson", failures)
    _ndjson(output / "event_driven_risk.ndjson", risks)
    _ndjson(output / "same_lap_population.ndjson", ({k: v for k, v in r.items() if k != "tyre"}
            for r in population if r["lap_relation"] == "SAME_LAP"))
    from f1ri.validation.overtake_stabilization_plots import plot_stabilization

    plot_stabilization(output, dossiers, metrics, gaps, summaries, failures)
    return output / "metrics.json"
