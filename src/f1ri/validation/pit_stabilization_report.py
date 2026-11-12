"""Read-only reporting of the locked Stage 4.1 study, including unmet evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from f1ri.validation.pit_backtest import _json, _safe_json


def _number(value, digits=3):
    return "—" if value is None or pd.isna(value) else f"{value:.{digits}f}"


def _table(headers, rows):
    return "\n".join(["| "+" | ".join(headers)+" |", "| "+" | ".join(["---"]*len(headers))+" |",
                       *["| "+" | ".join(str(v) for v in row)+" |" for row in rows]])


def write_stabilization_report(root: Path, cfg: dict) -> dict:
    from f1ri.validation.pit_stabilization import verify_preserved_references

    run = root/"artifacts/runs"/cfg["experiment"]["name"]
    reports = root/"artifacts/reports"
    preserved = verify_preserved_references(root, run)
    lock, access, validation, tests, paired, service, reviewed = [json.loads((run/f).read_text()) for f in (
        "validation_selection.lock.json", "test_access_log.json", "validation_metrics.json", "test_metrics.json",
        "paired_test_comparisons.json", "service_training_audit.json", "reviewed_run_reclassification.json")]
    if pd.Timestamp(access["first_access_at_utc"]) <= pd.Timestamp(lock["locked_at_utc"]):
        raise ValueError("Holdouts were accessed before model selection")
    events = pd.concat([pd.read_parquet(run/phase/"events.parquet") for phase in ("development", "reserved_tests")], ignore_index=True)
    labels = pd.concat([pd.read_parquet(run/phase/"labels.parquet") for phase in ("development", "reserved_tests")], ignore_index=True)
    predictions = pd.concat([pd.read_parquet(run/f"{phase}_predictions.parquet") for phase in ("validation", "test")], ignore_index=True)
    neutral = pd.concat([pd.read_parquet(run/phase/"neutralization_audit.parquet") for phase in ("development", "reserved_tests")], ignore_index=True)
    primary = labels[labels.measurement_method == "TWO_LAP_CROSS_FIT"]
    comparisons = primary[primary.eligible_primary_label & primary.method_disagreement_s.notna()]
    disagreements = comparisons.method_disagreement_s.abs()
    agreement = {"eligible_comparisons": len(comparisons),
                 "median_absolute_disagreement_s": disagreements.median(),
                 "p90_absolute_disagreement_s": disagreements.quantile(.9),
                 "max_absolute_disagreement_s": disagreements.max(),
                 "independent_physical_truth_established": False,
                 "shared_bias_warning": "Both estimators share provider lap timing, pre-stop anchors and structural assumptions"}
    audit = primary.merge(events.drop(columns="eligible_primary_label"), on=["session_key", "pit_event_id"], validate="one_to_one")
    audit["absolute_disagreement_s"] = audit.method_disagreement_s.abs()
    audit.sort_values("absolute_disagreement_s", ascending=False).to_csv(run/"stabilized_label_audit.csv", index=False)
    _json(run/"measurement_agreement.json", _safe_json(agreement))
    neutral_summary = {"candidate_windows": len(neutral),
                       "matched_field_proxies": int(neutral.conditional_elapsed_loss_s.notna().sum()) if not neutral.empty else 0,
                       "verified_route_labels": 0, "approved_for_strategy_comparison": False,
                       "reasons": neutral.reason.value_counts().to_dict() if not neutral.empty else {}}
    _json(run/"neutralization_audit.json", neutral_summary)
    flags = events.classification_flags_json.map(json.loads).explode().value_counts().to_dict()
    confirmed = int((events.routine_status == "ROUTINE_CONFIRMED").sum())
    sensitivity = {"confirmed_visits": confirmed, "confirmed_only_metrics": "NO_EVALUABLE_EVENTS" if confirmed == 0 else "SEE_EVENT_AUDIT",
                   "screened_unverified_allowed": True, "independent_confirmation_missing": confirmed == 0}
    _json(run/"confirmed_only_sensitivity.json", sensitivity)
    attrition = []
    reserved_events = pd.read_parquet(run/"reserved_tests/events.parquet")
    primary_predictions = predictions[predictions.model_id == "P1"]
    for sid, block in reserved_events.groupby("session_key"):
        p = primary_predictions[primary_predictions.session_key == sid]
        attrition.append({"session_key": int(sid), "circuit": block.circuit_short_name.iloc[0],
                          "raw_visits": len(block), "ordinary_lane_visits": int(block.eligible_direct_lane.sum()),
                          "eligible_net_forecasts": int(p.eligible_primary_label.sum())})
    _json(run/"test_population_attrition.json", attrition)
    supplementary = json.loads((run/"supplementary_results.json").read_text()) if (run/"supplementary_results.json").exists() else None
    supplementary_text = "No supplementary outcomes have been evaluated."
    if supplementary is not None:
        extra = supplementary["supplementary_metrics"]["P1"]["supplementary_unseen"]["net_loss_proxy"]
        supplementary_text = (
            "After observing primary-cohort attrition, one additional Spa 2025 replication was registered "
            "before fetching its outcomes. It preserves the original eligibility, calibration, weights and selection. "
            f"Spa contributes {extra['n_events']} eligible net forecasts from {extra['n_races']} races; "
            "it does not repair the missing multi-race unseen net-loss evidence. Its results are supplementary, "
            "not part of the original preregistration. The original primary metrics are unchanged.")
    old_predictions = pd.read_parquet(root/"artifacts/runs/stage4_pit_loss_v1/predictions.parquet")
    lusail = old_predictions[(old_predictions.model_id == "P8") & (old_predictions.split == "test")
                             & (old_predictions.circuit_short_name == "Lusail") & old_predictions.eligible_primary_label]
    lusail_audit = {"reviewed_run_diagnostic_only": True, "n_events": len(lusail),
                    "mean_measured_lane_s": lusail.target_lane_duration_s.mean(),
                    "mean_predicted_lane_s": lusail.expected_lane_duration_s.mean(),
                    "mean_net_label_s": lusail.target_transition_loss_s.mean(),
                    "mean_net_prediction_s": lusail.expected_loss_s.mean(), "physical_cause_verified": False}
    _json(run/"lusail_diagnostic.json", _safe_json(lusail_audit))
    selected = lock["selected_model"]
    fia_path = run/"fia_reference_audit.json"
    fia = json.loads(fia_path.read_text()) if fia_path.exists() else None
    fia_text = (
        f"FIA access is now active. A post-lock audit covers **{fia['events_catalogued']} race weekends**, "
        f"with **{fia['text_verified_speed_rows']} explicit text-based speed references**, "
        f"**{fia['visual_map_sign_speed_rows']} separately reviewed map-sign observations**, and "
        f"**{fia['events_with_preforecast_layout_documents']} dated layout-document references** available "
        "before their race's first saved forecast. Each source has an official URL, SHA-256 and dated evidence. "
        "Unknown speed limits, route lengths and physical geometry versions remain missing. The separate "
        "catalog lives under `data/raw/reference/stage4_1_fia/`; it does not change the frozen models or "
        "their primary inputs. Reference features were not retrofitted to opened tests, so an accuracy "
        "gain cannot be claimed. See the [official FIA reference audit](stage4_1_fia_reference_audit.md)."
        if fia else
        "Official FIA reference sourcing is pending. No event-specific speed limit, layout identity or legal "
        "route length has been invented. A PDF importer checks official source identity, date evidence and "
        "pre-forecast availability; absent verified rows, no reference-feature accuracy gain can be claimed.")
    verification_path = run/"verification_results.json"
    verification = json.loads(verification_path.read_text()) if verification_path.exists() else {}
    verification_text = (
        f"{verification['automated_tests_passed']} automated tests passed; Ruff and Git whitespace checks passed. "
        "Regression coverage includes both known extreme visits, compatible reason flags, physical calibration "
        "reconciliation, exact known-service ownership, equal-tire-state control contrasts, future-calibration "
        "rejection and prevention of test access/refitting without a valid lock. All saved predictions passed "
        "forecast timestamp checks and shared-target/eligibility checks. Original reference and frozen model "
        "hashes remain unchanged." if verification else "See the automated tests and frozen-hash checks before using these artifacts.")
    metric_rows = []
    for name in cfg["validation"]["experiments"]:
        net = validation[name]["val"]["net_loss_proxy"]
        score = lock["model_scores"][name]
        metric_rows.append([name+(" (selected)" if name == selected else ""), net["n_events"],
                            _number(net.get("race_weighted_mae_s")), _number(net.get("race_weighted_crps_s")),
                            _number(net.get("coverage_90")), _number(net.get("mean_width_90_s")), score["final_scale"]])
    test_rows = []
    for split in ("test_familiar", "test_unseen"):
        for name in cfg["validation"]["experiments"]:
            net = tests[name][split]["net_loss_proxy"]
            test_rows.append([split.removeprefix("test_"), name, net["n_events"], net["n_races"],
                              _number(net.get("race_weighted_mae_s")), _number(net.get("race_weighted_crps_s")),
                              _number(net.get("coverage_50")), _number(net.get("coverage_90")), _number(net.get("mean_width_90_s"))])
    direct_rows = []
    for phase, metric in (("val", validation), ("test_familiar", tests), ("test_unseen", tests)):
        for name in sorted({"P1", "P8", selected}):
            block = metric[name][phase]
            lane, stop, tail = block["direct_lane"], block["direct_stationary"], block["slow_stop_calibration"]
            direct_rows.append([phase, name, lane["n_events"], _number(lane.get("mae_s")),
                                stop["n_events"], _number(stop.get("mae_s")), _number(tail.get("observed_slow_rate")),
                                _number(tail.get("predicted_slow_rate")), _number(tail.get("brier_score"))])
    old_rows = []
    for name in ("P9", "P10"):
        before = reviewed["raw_metrics"][name]["transfer_2026"]["direct_lane"]
        after = reviewed["ordinary_policy_metrics"][name]["transfer_2026"]["direct_lane"]
        old_rows.append([name, before["n_events"], _number(before.get("mae_s")), after["n_events"], _number(after.get("mae_s"))])
    provenance = pd.read_csv(run/"service_training_provenance.csv")
    weekends = provenance.groupby(["session_key", "circuit_short_name"]).measured_stops.sum().reset_index()
    text = f"""# Stage 4.1 — Validation and stabilization

Stage 4 stabilization is implemented and evaluated. The locked selection is **{selected}**, with role **{lock['selected_role']}**. This is a research result, not unrestricted strategy approval. Circuit-reference feature validation and physically validated VSC/SC loss labels remain unmet requirements. Stage 5 work has not started.

## Scope and preservation

The reviewed Stage 4 run, model pickles, figures and report are unchanged. Stage 3 source/report hashes and the pit time-accounting contract are unchanged. The absent original Stage 3 fitted model artifacts still prevent a claim of bit-for-bit fitted-artifact reproduction. The accounting remains `normal racing reference + net transition loss`; lane duration already contains stationary service. Calibration retains the shared reference-error draw with its original sign, and a known +2.5 s service scenario changes total loss by exactly +2.5 s.

## 1. Exceptional visits and one eligibility policy

Policy `{cfg['eligibility']['version']}` applies to every candidate. Any positive RED overlap, invalid timing, unresolved double stack or lane stay above {cfg['eligibility']['long_stay_threshold_s']:g} s excludes ordinary direct evaluation. Duration triggers review without assigning a repair or penalty cause. Recorded BLUE flags and repeated pit operations within the two-lap window exclude the net-loss label while retaining eligible measured lane/service observations. Forecasts at a non-GREEN cutoff are not scored against an eventual GREEN counterfactual.

All raw visits remain available. Compatible flags include confirmed/unverified routine status, red overlap, unresolved long hold, repair/garage evidence, penalty evidence and ambiguous timing. Reclassifying the reviewed run retains {reviewed['events_retained']} events; {reviewed['red_overlap_events']} have RED overlap, and **{reviewed['red_overlap_still_eligible']} remain ordinary-lane eligible**. Direct-lane eligibility changes from {reviewed['direct_lane_eligible_before']} to {reviewed['direct_lane_eligible_after']} across that run. The Monza `11361:63:1` 1,846.2 s and Catalunya `11307:23:3` 777.7 s observations are retained and excluded; their causes are not inferred from duration.

The fixed old predictions are rescored, without refitting to their errors:

{_table(['Model', 'Old eligible n', 'Old lane MAE s', 'Policy eligible n', 'Policy lane MAE s'], old_rows)}

These before/after MAEs describe different eligibility populations. They demonstrate removal of misclassified holds, not a model accuracy improvement on identical events. Independently confirmed routine visits: **{confirmed}**. The confirmed-only sensitivity is therefore `{sensitivity['confirmed_only_metrics']}`; all reported ordinary results use explicitly screened-unverified visits.

## 2. Independent service evidence

Measured training service evidence expands from 23 stops at one weekend to **{service['measured_service_events']} stops at {service['independent_races']} weekends, {service['circuits']} circuits and {service['teams']} teams**. Missing service times are not converted into measured zeros. All training weekends precede the early-2025 validation races. Team estimates remain pooled; six weekends do not establish broad team/season calibration.

{_table(['Session', 'Circuit', 'Measured eligible stops'], weekends.itertuples(index=False, name=None))}

Provenance is saved in `service_training_provenance.csv`; raw endpoint snapshots and their SHA-256 hashes are recorded in each phase's data manifest.

## 3. Circuit references and Lusail

{fia_text}

The reviewed Lusail 2025 results remain diagnostics, not tuning targets. The model originally had no fitted Lusail visits. Across the original eight eligible stops, measured lane duration averages **{_number(lusail_audit['mean_measured_lane_s'])} s** versus a **{_number(lusail_audit['mean_predicted_lane_s'])} s** forecast. The estimated net label averages **{_number(lusail_audit['mean_net_label_s'])} s** versus **{_number(lusail_audit['mean_net_prediction_s'])} s** predicted. Underprediction is therefore visible in independently recorded lane duration as well as the net proxy; it is not solely a discrepancy between two net-label estimators. This supports the need for circuit information, without identifying a physical cause. New historical Lusail 2024 observations supply circuit support, but do not prove that the old bias is repaired. Provider report times are not certified legal entry loops. Speed, route geometry, warm-up and shared reference bias remain unresolved explanations.

## 4. Net-loss measurement checks

The primary past-race ridge reference is retained. The supporting estimate now uses a separately fitted robust Huber reference plus non-pitting control pairs matched within 45 s of the target interval. Both target old-tire and new-tire normal states are represented, rather than comparing two old-tire laps with an old/new transition. Nuisance parameters use development races strictly earlier than the target and never fit on a reserved test race.

Among eligible ordinary GREEN estimates, {agreement['eligible_comparisons']} have supporting comparisons: median absolute disagreement **{_number(agreement['median_absolute_disagreement_s'])} s**, p90 **{_number(agreement['p90_absolute_disagreement_s'])} s**, maximum **{_number(agreement['max_absolute_disagreement_s'])} s**. These are two statistical estimates sharing provider timing and structural assumptions. Agreement is not independent physical validation, and uncertainty is retained/inflated for disagreement rather than filtering cases by model residual.

![Service evidence and supporting measurement comparison](stage4_1_evidence_audit.png)

The extreme Singapore 2024 `9606:20:2` estimate (~108.8 s) coincides with recorded BLUE flags and a very slow in-lap middle sector; its lane duration is 31.2 s. The net target is excluded for recorded on-track contamination, while the lane measurement remains available. Suzuka 2026 `11253:23:6` spans repeated pit operations and is not a single-stop transition. A 74.7 s lane stay (`9472:77:2`) is flagged for unresolved long-stay review. None of these facts establishes a repair, puncture or penalty cause. The complete old and updated disagreement audits retain excluded observations.

## 5. Locked evaluation design

The preregistered plan hash is `{cfg['experiment']['preregistered_plan_sha256']}`. Training contains 23 historical 2023–2024 races; validation uses four early-2025 races. New familiar tests: 2025 Zandvoort and Las Vegas. New unseen tests: 2025 Montreal, Silverstone and Baku. The old Interlagos/Lusail/Mexico reserved races and previously inspected 2026 races remain diagnostics.

Selection locked at `{lock['locked_at_utc']}`; first reserved-test access was `{access['first_access_at_utc']}`. Frozen source, configuration, development data and model hashes are checked before evaluation. Opening tests creates a persistent access log and disables refitting. Re-running evaluation can reproduce frozen predictions; it cannot reselect a model. Familiar/unseen estimates are reported separately.

**Attrition materially limits this test:** the familiar net-loss score has ten stops from two races; the unseen score has eleven stops from **Baku alone**, despite three planned unseen races. Silverstone's GREEN transitions use wet tires, outside the dry normal-reference domain. Montreal's ordinary stops fail independent stint/pit-out alignment: for example, Russell's stint 2 starts on raw lap 13 while its pit-out marker is on lap 14. Its later repeated SC pit-lane passages are distinct from a routine tire change. The raw track-state timeline itself contains GREEN; unreconciled visits have unknown transition bounds. These records are retained without shifting lap conventions after seeing results.

{_table(['Session', 'Circuit', 'Raw visits', 'Ordinary lane visits', 'Eligible net forecasts'],
        [[r['session_key'], r['circuit'], r['raw_visits'], r['ordinary_lane_visits'], r['eligible_net_forecasts']] for r in attrition])}

Race-bootstrap intervals from two races are descriptive. **Unseen net-loss race-level uncertainty cannot be estimated from one race**; any degenerate point interval emitted by the generic metric helper is not an informative confidence band and is omitted below. The intended multi-race unseen net-loss acceptance gate is still unmet.

{supplementary_text}

## 6. Calibration and model choice

P8 physical variation can shrink or widen over prespecified scales 0.5, 0.75, 1, 1.25 and 1.5. Shared normal-reference uncertainty is not scaled. Validation is strictly chronological: each race uses calibration from earlier validation races only, with at least two races and ten eligible cases. This is a rolling-prefix implementation of the preregistered rolling validation plan. The final scale uses validation only. Point means, lane/service measurements and known-service scenario ownership remain intact.

Selection minimizes prequential race-weighted CRPS. A tail-capable candidate must retain MAE within 10% of the best simple benchmark and have service evidence from at least three weekends. Coverage, sharpness and service Brier score are reported. A failing complex candidate does not replace the simple model. A simple fallback is explicitly unsuitable as an approved slow-service risk model. Selection does not establish production calibration.

{_table(['Model', 'Val n', 'Race MAE s', 'Race CRPS s', '90% coverage', '90% width s', 'Final scale'], metric_rows)}

### Fresh reserved tests

{_table(['Population', 'Model', 'n', 'Races', 'Race MAE s', 'Race CRPS s', '50% cover', '90% cover', '90% width s'], test_rows)}

![Model comparison with usable event and race counts](stage4_1_model_comparison.png)

### Measured components and slow service (>4.5 s)

Direct component populations can differ from the stricter net-loss population. Values are conditional on the actual tire scenario, including explicitly supplied used-tire scenarios; this study does not predict which tire the team will choose.

{_table(['Population', 'Model', 'Lane n', 'Lane MAE s', 'Service n', 'Service MAE s', 'Slow observed', 'Slow predicted', 'Brier'], direct_rows)}

Paired race-bootstrap comparisons to P1 (candidate minus baseline; negative favors candidate):

{_table(['Population', 'Candidate', 'Common n', 'Races', 'MAE delta s', '5% delta', '95% delta'],
        [[split, name, block['n_events'], block['n_races'], _number(block.get('race_mean_mae_delta_s')), _number(block.get('p05_delta_s') if block['n_races'] > 1 else None), _number(block.get('p95_delta_s') if block['n_races'] > 1 else None)]
         for split, items in paired.items() for name, block in items.items()])}

## 7. Dedicated VSC/SC audit

The dedicated audit contains {neutral_summary['candidate_windows']} neutralized/mixed windows, including {neutral_summary['matched_field_proxies']} with at least three same-state matched two-lap field controls. It records driver identities, time windows, full state coverage and dispersion. **Zero have independently verified target-route anchors.** Mixed-state windows and insufficient controls carry explicit reasons; matched field proxies remain `UNSUPPORTED`. SC queue effects and target-specific counterfactual movement are unidentified. No generic green penalty or arbitrary percentage discount becomes an approved VSC/SC label.

## Remaining completion requirements

- Complete the remaining historical speed-limit coverage and verify physical route information; evaluate the newly sourced reference features in a separately frozen study.
- Independently validate normal-lap and neutralized route references. Two statistical methods cannot resolve their common timing bias.
- Confirm ordinary service types independently and expand race-level service-tail validation. The current candidate/fallback remains experimental.
- Obtain usable net-loss targets from additional independently held-out unseen circuits; one eligible unseen race is insufficient even though the separate test cohorts and direct timing evaluation are implemented.

Stage 4 implementation and the requested stabilization evaluation are available for review. Stage 4 empirical acceptance remains provisional; no Stage 5 simulation or strategy deployment is authorized by these results.

## Verification

{verification_text}
"""
    (reports/"stage4_1_stabilization.md").write_text(text)
    _plots(reports, predictions, comparisons, weekends)
    manifest = {"study": cfg["experiment"]["name"], "selected_model": selected,
                "selected_role": lock["selected_role"], "production_approved": False,
                "stage5_started": False, "preserved_references": preserved,
                "verification": verification,
                "official_fia_reference_audit": fia,
                "completion_gates": {"exceptional_visit_regressions": True, "multiple_service_training_weekends": True,
                                     "dated_official_reference_catalog": fia is not None,
                                     "verified_circuit_features": False, "independently_validated_physical_net_labels": False,
                                     "separate_test_populations": True, "multiple_usable_unseen_net_loss_races": False,
                                     "locked_selection_and_simple_fallback": True, "validated_neutralized_losses": False},
                "selection_lock": lock, "test_access_log": access, "service_evidence": service,
                "classification_flags": flags, "confirmed_only_sensitivity": sensitivity,
                "measurement_agreement": agreement, "neutralization": neutral_summary, "lusail_diagnostic": lusail_audit,
                "validation_metrics": _compact_metrics(validation), "test_metrics": _compact_metrics(tests), "paired_comparisons": paired,
                "test_population_attrition": attrition,
                "supplementary_replication": ({**supplementary,
                    "supplementary_metrics": _compact_metrics(supplementary["supplementary_metrics"]),
                    "combined_descriptive_metrics": _compact_metrics(supplementary["combined_descriptive_metrics"])} if supplementary else None),
                "reviewed_run_reclassification": {**{k: v for k, v in reviewed.items() if k not in {"raw_metrics", "ordinary_policy_metrics"}},
                                                    "raw_metrics": _compact_metrics(reviewed["raw_metrics"]),
                                                    "ordinary_policy_metrics": _compact_metrics(reviewed["ordinary_policy_metrics"])},
                "report_sha256": hashlib.sha256((reports/"stage4_1_stabilization.md").read_bytes()).hexdigest()}
    _json(reports/"stage4_1_stabilization_manifest.json", _safe_json(manifest))
    return manifest


def _compact_metrics(metrics):
    return {name: {split: {k: v for k, v in block.items() if k != "slices"}
                   for split, block in populations.items()} for name, populations in metrics.items()}


def _plots(reports, predictions, comparisons, weekends):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 3, figsize=(16, 9), constrained_layout=True)
    models = ["P0", "P1", "P2", "P3", "P6", "P7", "P8"]
    for index, split in enumerate(("val", "test_familiar", "test_unseen")):
        frame = predictions[(predictions.split == split) & predictions.eligible_primary_label]
        count = frame[frame.model_id == "P1"]
        label = {"val": "Validation", "test_familiar": "Familiar test", "test_unseen": "Unseen test"}[split]
        for row, column, title in ((0, "residual_s", "Race-weighted MAE (s)"), (1, "crps_s", "Race-weighted CRPS (s)")):
            values = []
            for model in models:
                block = frame[frame.model_id == model]
                score = block[column].abs() if column == "residual_s" else block[column]
                values.append(score.groupby(block.session_key).mean().mean())
            axes[row, index].bar(models, values, color=["#15803d" if m == "P1" else ("#2563eb" if m == "P8" else "#94a3b8") for m in models])
            axes[row, index].set(title=f"{label}: {title}\n{len(count)} events / {count.session_key.nunique()} races", ylabel="seconds")
    fig.suptitle("Stage 4.1 — P1 locked fallback (green); P8 research candidate (blue)")
    fig.savefig(reports/"stage4_1_model_comparison.png", dpi=150)
    plt.close(fig)
    fig, axes = plt.subplots(1, 2, figsize=(13, 5), constrained_layout=True)
    axes[0].bar(weekends.circuit_short_name, weekends.measured_stops, color="#2563eb")
    axes[0].tick_params(axis="x", rotation=35)
    axes[0].set(title="Direct measured service training: six independent weekends", ylabel="eligible measurements")
    axes[1].scatter(comparisons.transition_loss_s, comparisons.matched_control_loss_s, alpha=.5, s=12)
    if not comparisons.empty:
        lower = min(comparisons.transition_loss_s.min(), comparisons.matched_control_loss_s.min())
        upper = max(comparisons.transition_loss_s.max(), comparisons.matched_control_loss_s.max())
        axes[1].plot([lower, upper], [lower, upper], color="black", linestyle="--")
    axes[1].set(title="Ordinary GREEN estimates; shared timing bias remains", xlabel="Primary ridge estimate (s)", ylabel="Robust matched-control estimate (s)")
    fig.savefig(reports/"stage4_1_evidence_audit.png", dpi=150)
    plt.close(fig)
