"""Summarize historical label support at event, encounter and race grains."""
import json
from collections import Counter, defaultdict
from statistics import mean

from f1ri.features.overtake_events import PASS_TYPES
from f1ri.features.overtake_opportunities import seeded_score
from f1ri.validation.proximity_pilot import _json
from f1ri.validation.traffic_history_study import _sha


def _ndjson(path, rows):
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, default=str, allow_nan=False, separators=(",", ":")) + "\n")


def _summarize_outcomes(results, by_id):
    counts = Counter(r["outcome"] for r in results)
    groups = defaultdict(set)
    for row in results:
        groups[row["outcome"]].add(by_id[row["opportunity_id"]]["encounter_id"])
    positive_ids = {r["pass_event_id"] for r in results if r["outcome"] == "PASS_OBSERVED"}
    return {"cutoffs": len({r["opportunity_id"] for r in results}), "outcomes": dict(counts),
            "encounters": len(set().union(*groups.values())), "event_count": len(positive_ids),
            "encounters_by_outcome": {k: len(v) for k, v in groups.items()}}


def _descriptive_rates(results, by_id, classify):
    bins = defaultdict(list)
    for row in results:
        if row["horizon"] == "10":
            label = classify(by_id[row["opportunity_id"]])
            if label is not None:
                bins[label].append(row)
    out = {}
    for label, rows in sorted(bins.items()):
        summary = _summarize_outcomes(rows, by_id)
        positive = {by_id[r["opportunity_id"]]["encounter_id"] for r in rows if r["outcome"] == "PASS_OBSERVED"}
        controls = {by_id[r["opportunity_id"]]["encounter_id"] for r in rows if r["outcome"] == "NO_PASS_OBSERVED"}
        out[label] = summary | {"resolved_encounters": len(positive | controls), "pass_encounters": len(positive),
                                "pass_fraction": len(positive) / len(positive | controls)
                                if positive or controls else None}
    return out


def _audit_sample(events, results, by_id, config, histories):
    strata = defaultdict(list)
    for event in events:
        strata["EVENT/" + event["event_type"]].append((event["pass_event_id"], event))
        for reason in event["exclusions"]:
            if reason == "IMPLAUSIBLE_PROJECTION_JUMP":
                strata["PROJECTION_JUMP"].append((event["pass_event_id"], event))
    for row in results:
        if row["horizon"] == "10":
            strata["OUTCOME/" + row["outcome"]].append((row["opportunity_id"], row))
    audit = {}
    for stratum, rows in sorted(strata.items()):
        sampled = sorted(rows, key=lambda pair: seeded_score(config["seed"], stratum, pair[0]))
        chosen, seen = [], set()
        for key, record in sampled:
            opportunity = by_id[key] if stratum.startswith("OUTCOME/") else None
            group = opportunity["encounter_id"] if opportunity else key
            if group in seen:
                continue
            seen.add(group)
            if opportunity:
                history = histories[opportunity["session"]]
                pair = opportunity["attacker"], opportunity["target"]
                planned = record["planned_end_utc"]
                interruptions = history.interruptions(pair, opportunity["cutoff_utc"], planned) if planned else []
                record = {"cutoff": opportunity, "outcome": record, "independent_interruptions": interruptions}
            chosen.append(record)
            if len(chosen) >= config["audit_per_stratum"]:
                break
        audit[stratum] = {"eligible_rows": len(rows), "examples": chosen}
    return audit


def _readiness(events, opportunities, results, encounters, roles):
    supported = [r for r in events if r["event_type"] in PASS_TYPES]
    development = [e for e in supported if roles[e["session"]] == "development"]
    same_lap_a = [e for e in development if e["event_type"] == "SAME_LAP_RACING_PASS" and e["tier"] == "A"]
    by_id = {r["opportunity_id"]: r for r in opportunities}
    result_set = [r for r in results if roles[by_id[r["opportunity_id"]]["session"]] == "development"]
    support = {
        "observed_races": sorted({r["session"] for r in opportunities}),
        "supported_pass_races": sorted({r["session"] for r in supported}),
        "development_supported_passes": len(development),
        "development_same_lap_tier_a": len(same_lap_a),
        "development_same_lap_tier_a_races": sorted({r["session"] for r in same_lap_a}),
        "drivers": len({d for r in opportunities for d in (r["attacker"], r["target"])}),
        "layouts": len({r["layout_version"] for r in opportunities}),
        "race_pairs": len({(g["session"], *g["pair"]) for g in encounters}),
        "encounters": len(encounters),
        "cutoffs_with_pace_context": sum(all(r["recent_clean_laps"]) for r in opportunities),
        "cutoffs_with_supported_closing": sum(r["closing_supported"] for r in opportunities),
        "cutoffs_with_complete_core_predictors": sum(
            all(r["recent_clean_laps"]) and r["closing_mps"] is not None
            and r["gap_approx_s"] is not None and r["weather"] is not None for r in opportunities
        ),
        "development_10s": _summarize_outcomes([r for r in result_set if r["horizon"] == "10"], by_id),
    }
    return {"decision": "PATH_C", "model_fit": False, "support": support,
            "reason": "High-confidence competitive events and independent development race coverage "
                      "are insufficient to support a race-held-out completion hazard study.",
            "reference_population": "NO_EXHAUSTIVE_INDEPENDENT_ANNOTATIONS",
            "precision_recall": None,
            "next_evaluation": {
                "split": "Chronological whole races with pair and encounter clusters",
                "metrics": ["calibration-in-the-large", "Brier score by horizon", "interval-censored log score"],
                "precision": "Predefine race-cluster confidence interval widths and minimum useful "
                             "calibration resolution before opening validation cohorts",
            }}


def write_results(output, config, plan, frozen, warehouse_hash, census, histories, order, replays, events,
                  supported, opportunities, results, encounters, comparisons, tyre, funnel, route_changes):
    from f1ri.validation.overtake_historical_plots import plot_history
    from f1ri.validation.overtake_historical_study import BASELINE

    by_id = {r["opportunity_id"]: r for r in opportunities}
    roles = {r["session"]: r["role"] for r in census}
    circuits = {r["session"]: r["circuit"] for r in census}
    types = Counter(r["event_type"] for r in [*events, *route_changes])
    supported_tiers = Counter(r["tier"] for r in supported)
    readiness = _readiness(events, opportunities, results, encounters, roles)
    gap_rates = _descriptive_rates(results, by_id, lambda r: (
        "≤40 m" if r["gap_upper_m"] <= 40 else "40–80 m" if r["gap_upper_m"] <= 80 else
        "80–120 m" if r["gap_upper_m"] <= 120 else ">120 m"
    ))

    def pace_band(row):
        a, b = row["recent_clean_laps"]
        if not a or not b:
            return None
        return "Attacker faster" if mean(a["lap_times_s"]) < mean(b["lap_times_s"]) else "Attacker equal or slower"

    pace_rates = _descriptive_rates(results, by_id, pace_band)
    sensitivity = {}
    for threshold in config["gap_thresholds_m"]:
        rows = [r for r in results if r["horizon"] == "10"
                and threshold in by_id[r["opportunity_id"]]["eligible_gap_thresholds_m"]]
        sensitivity[str(threshold)] = _summarize_outcomes(rows, by_id) | {
            "field_coverage": dict(sorted(Counter(
                by_id[key]["field_coverage"] for key in {r["opportunity_id"] for r in rows}
            ).items()))
        }
    horizons = {h: _summarize_outcomes([r for r in results if r["horizon"] == h], by_id)
                for h in (*map(str, config["horizons_s"]), "checkpoint")}
    arms = {arm: _summarize_outcomes(
        [r for r in results if r["horizon"] == "10" and arm in by_id[r["opportunity_id"]]["arms"]], by_id)
        for arm in ("A_CLOSE_FOLLOWING", "B_APPROACHING", "C_RANDOM_GREEN", "D_LAPPED")}
    event_ids = defaultdict(set)
    for row in results:
        if row["outcome"] == "PASS_OBSERVED":
            event_ids[row["pass_event_id"]].add(by_id[row["opportunity_id"]]["encounter_id"])
    for group in encounters:
        group["pass_event_ids"] = sorted(event for event, ids in event_ids.items() if group["encounter_id"] in ids)
    metrics = {
        "version": config["version"], "frozen_sha256": frozen, "baseline_revision": BASELINE,
        "new_openf1_requests": 0, "operational_baseline": "T1_DETERMINISTIC_NO_PASS", "traffic_v1": "RESEARCH_ONLY",
        "census_sources": len(census), "census_windows": sum(len(r["window_spans"]) // 2 for r in census),
        "event_types": dict(types), "supported_evidence_tiers": dict(supported_tiers),
        "pair_reversal_candidates": len(events), "route_neighbor_changes": len(route_changes),
        "opportunity_funnel": dict(funnel) | {"unique_cutoffs": len(opportunities), "encounters": len(encounters)},
        "tier_by_circuit": {circuit: dict(Counter(r["tier"] for r in [*events, *route_changes] if r["session"] == key))
                            for key, circuit in circuits.items()},
        "independent_position_agreement": dict(Counter(r["evidence"]["position_agreement"] for r in supported)),
        "independent_marker_agreement": dict(Counter(
            r["evidence"]["nearest_shared_marker_support"] for r in supported
            if r["event_type"] == "SAME_LAP_RACING_PASS"
        )),
        "bracket_diagnostics": [{
            "pass_event_id": r["pass_event_id"], "circuit": circuits[r["session"]],
            "pair": [r["attacker"], r["target"]], "event_type": r["event_type"],
            "width_s": r["event_time_bracket_s"], "source_grid_period_s": r["source_period_s"],
            "gap_uncertainty_m": [r[p]["gap_uncertainty_m"] for p in ("before", "after")],
            "position_quality": [r[p][d]["quality"] for p in ("before", "after") for d in ("attacker", "target")],
            "source_age_s": [r[p][d].get("source_age_s") for p in ("before", "after") for d in ("attacker", "target")],
            "sector": r["segment"]["sector"],
        } for r in supported],
        "sensitivity_10s": sensitivity, "outcomes_by_horizon": horizons, "arms_10s": arms,
        "gap_rates_10s": gap_rates, "pace_rates_10s": pace_rates,
        "s4_a_reference_brackets": sum(len(r["exchanges"]) for r in replays),
        "s4_a_reference_completed": sum(r["replay"]["status"] == "COMPLETED" for r in replays),
        "scripted_direction_matches": sum(r["direction_matches"] for r in comparisons),
        "scripted_replay_statuses": dict(Counter(r["status"] for r in comparisons)),
        "readiness": readiness,
    }
    (output / "config.yaml").write_bytes(plan.read_bytes())
    for name, data in {
        "source_availability": census, "physical_order_trace_audit": order, "s4_a_reference": replays,
        "physical_reversals": events, "supported_pass_events": supported,
        "ambiguous_and_excluded_events": [r for r in events if r["event_type"] not in PASS_TYPES],
        "route_order_changes": route_changes,
        "encounters": encounters, "independent_evidence_audit": _audit_sample(
            events + [r | {"pass_event_id": r["order_change_id"]} for r in route_changes],
            results, by_id, config, histories),
        "scripted_comparisons": comparisons, "retrospective_tyre_context": tyre,
        "statistical_readiness": readiness, "metrics": metrics,
        "input_manifest": {"baseline_revision": BASELINE, "warehouse_sha256": warehouse_hash,
                           "config_sha256": _sha(plan), "frozen_sha256": frozen,
                           "warehouse_sources": {k: h.availability for k, h in histories.items()},
                           "session_metadata": {k: h.metadata for k, h in histories.items()}},
    }.items():
        _json(output / f"{name}.json", data)
    _ndjson(output / "opportunities.ndjson", opportunities)
    _ndjson(output / "outcomes.ndjson", results)
    plot_history(output / "diagnostics.png", metrics, events, supported, comparisons, replays)
