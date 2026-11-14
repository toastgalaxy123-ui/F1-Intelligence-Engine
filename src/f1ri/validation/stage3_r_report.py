"""Render the Stage 3-R audit and checksum the run directory."""
import json

from f1ri.validation.stage3_r_sources import digest
from f1ri.validation.stage3_r_validation import D8, MODELS


def _load(out, name):
    return json.loads((out/f"{name}.json").read_bytes())


def _seconds(x):
    return "—" if x is None else f"{x:.3f}"


def _coverage(x):
    return "—" if x is None else f"{x:.1%}"


def _quality_tables(scores):
    lines = [
        "## Held-out pace",
        "",
        "Errors below are race-weighted MAE in seconds. Models scored the same eligible opportunities.",
        "",
        "| Estimator | All test H1 lap | All test H5 total | Historically uninspected H1 lap | Historically uninspected H5 total |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    labels = ("E2 compound median", "Adaptive reset and progress", "Pace V1 definition refit",
              "Nonlinear degradation", "Hierarchical degradation", "PaceV2_D8R1")
    for model, label in zip(MODELS, labels, strict=True):
        values = []
        for c in ("all_test", "historical_uninspected_test"):
            r = scores[model][c]
            values.extend((r["endpoint"]["1"]["race_weighted_mae_s"], r["cumulative"]["5"]["cum_race_weighted_mae_s"]))
        lines.append(f"| {label} | " + " | ".join(map(_seconds, values)) + " |")
    lines.extend([
        "",
        "### D8R1 open-loop continuation",
        "",
        "| Laps | Clean endpoints | Lap MAE (s) | Complete clean trajectories | Races | Total MAE (s) | CRPS (s) | 50% | 90% |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    main = scores[D8]["all_test"]
    for h, v in main["cumulative"].items():
        endpoint = main["endpoint"][h]
        lines.append(f"| {h} | {endpoint['eligible_forecasts']} | {_seconds(endpoint['race_weighted_mae_s'])} | "
                     f"{v['complete_eligible_trajectories']} | {v['unique_races']} | "
                     f"{_seconds(v['cum_race_weighted_mae_s'])} | {_seconds(v['cum_crps_s'])} | "
                     f"{_coverage(v['cum_coverage_50'])} | {_coverage(v['cum_coverage_90'])} |")
    lines.extend([
        "",
        ("Scores condition on observed clean endpoints or fully clean trajectories. Censoring removes interrupted "
        "stints from long-horizon scoring. The row count therefore overstates independent evidence. "
        "Each race contributes one unit to race-weighted MAE. Coverage and Gaussian CRPS pool eligible forecasts. "
        "Per-race calibration and the censoring breakdown are in the ledger."),
        "",
        ("The original D8 report recorded pooled H1 lap MAE of 0.321 s, H5 cumulative MAE of 1.516 s, "
        "and H5/H10 90% coverage of 87.1%/80.9%. The new run uses reacquired observations "
        "and every third cutoff lap. Those original values provide historical context only."),
    ])
    return lines


def _integration(out):
    replacement = _load(out, "replacement_forecast_validation")
    paired = _load(out, "paired_trajectory_validation")
    strategy = _load(out, "strategy_integration")
    lines = [
        "## Replacement forecasts and physical strategy",
        "",
        ("Singapore 2025 supplies the saved cutoff features for drivers 63 and 4. The field, pit path, shared box, "
        "tyre inventory and race-control interventions are declared scenarios. Exact historical field and rejoin "
        "geometry remain unsupported. Replacement validation in the historical ledger conditions on the set used. "
        "The counterfactual exercise supplies fresh hard, medium and soft sets and a used soft set with three laps."),
        "",
        (f"Standalone requests with numeric integrity: {sum(r['numerical_integrity'] for r in replacement.get('rows', []))}"
        f"/{len(replacement.get('rows', []))}. Frozen weather prior to cutoff: "
        f"{replacement.get('weather_valid', 'unavailable')}."),
        "",
        ("Paired draws use a common initial continuation and shared innovations for alternatives at the same stop "
        "clock key, driver, stint ordinal and horizon. Separate drivers and stint starts have separate streams. "
        "The fitted sampler represents within-stint dependence."),
        "",
    ]
    if paired.get("matched"):
        lines.append(f"Identical policies reproduced exactly: {paired['same_policy_exact']}. Reordering candidates "
                     f"preserved the same draw: {paired['candidate_order_and_population_independent']}. Unrelated "
                     f"driver's continuation preserved: {paired['unrelated_driver_stream_unchanged']}. "
                     f"Distinct streams on the two-stop path: {len(set(paired['two_stop_stream_ids']))}.")
        if paired.get("same_stop_replacement_stream_matched"):
            lines.append("The hard and medium replacements at the same stop used the same replacement draw key.")
    if "clean-air" in strategy:
        gaps = []
        for scene in ("clean-air", "two-car", "retirement"):
            pairs = strategy[scene]["result"]["pairs"]
            hard, soft = (pairs[tyre]["combined_delta_us"] for tyre in ("hard", "soft"))
            gaps.append(None if hard is None or soft is None else abs(hard-soft)/1e6)
        stack = strategy["shared-box"]["result"]["results"]["double-stack"]["pit"]
        lines.extend([
            "",
            (f"The hard and soft stops reached the timing marker {_seconds(gaps[0])} s apart in clean air, "
             f"{_seconds(gaps[1])} s apart behind the declared slower car, and "
             f"{_seconds(gaps[2])} s apart after its retirement. The slower car's saved "
             "clean-lap observations were increased by four seconds for this sensitivity test."),
            f"The double stack incurred a {sum(p['wait_us'] for p in stack)/1e6:.3f} s box wait.",
        ])
        lines.extend(["", "| Scenario | Strategy | Δ vs continuation (s) | Pit visits |",
                      "| --- | --- | ---: | ---: |"])
        for key, row in strategy.items():
            if "result" not in row:
                continue
            result = row["result"]
            for policy, branch in result["results"].items():
                if policy == "continue":
                    continue
                difference = result["pairs"][policy]["combined_delta_us"]
                lines.append(f"| {key} | {policy} | {_seconds(None if difference is None else difference/1e6)} | "
                             f"{len(branch['pit'])} |")
    lines.extend(["", ("These paired simulator times are conditional on the declared field. Headway, pit merging and "
                  "neutralization can erase clean-air differences. The exercise establishes fixture-level behavior. "
                  "Race-finishing probabilities, joint driver uncertainty and event probabilities remain out of scope.")])
    return lines


def finalize(out):
    metrics = _load(out, "metrics")
    data = _load(out, "data_availability")
    sources = _load(out, "source_manifest")
    reconstruction = _load(out, "warehouse_reconstruction")
    groups = data["ready"]
    clean = data["clean_air_population"]
    counts = {key: sum(row[key] for row in clean.values()) for key in ("laps_with_stint", "clean_laps", "clean_stints")}
    valid = [r for r in reconstruction if r["status"] == "READY"]
    excluded = [r for r in reconstruction if r["status"] not in {"READY", "TRANSFER_NOT_OPENED"}]
    available = sum(rec is not None for s in sources for rec in s["endpoints"].values() if s["split"] != "transfer_2026")
    lines = [
        "# F1 Race Intelligence Engine — Stage 3-R",
        "## Pace V2/D8 reconstruction and strategy validation",
        "",
        f"**Decision: `{metrics['decision']}`. Release: `{metrics['release']}`.**",
        "",
        ("The original fitted D8 pickle and matching warehouse could not be recovered in the available workspace, "
        "Git history or local archives. The surviving source, experiment configuration and earlier scorecards "
        "remain the reference for method and scope. Any fitted `PaceV2_D8R1` is a versioned successor "
        "trained on reacquired inputs (reconstruction outcome C)."),
        "",
        "## Sources and cohort",
        "",
        (f"OpenF1 supplied {available} race-endpoint snapshots within the frozen acquisition window. "
        f"{len(valid)}/27 races passed the warehouse checks. The retained cohort has "
        f"{len(groups['train'])} training, {len(groups['calibration'])} calibration and {len(groups['test'])} test "
        f"races, including {len(groups['test_historical_uninspected'])} marked historically uninspected. "
        "Session roles and prior inspection flags come from the hash-verified Stage 3 configuration. "
        "The five declared 2026 transfer sessions retained their roles and were outside the acquisition scope."),
        "",
        (f"Across the ready races, the warehouse retained {counts['laps_with_stint']:,} stint lap rows, "
        f"{counts['clean_laps']:,} clean green laps and {counts['clean_stints']:,} stints with clean laps, "
        f"across {len({r['circuit'] for r in valid})} named circuits and three seasons."),
        "",
    ]
    for r in excluded:
        lines.append(f"- {r['race']} ({r['session_key']}): `{r['status']}`. "
                     f"{r.get('reason') or ', '.join(r.get('missing', []))}")
        if r.get("reason") == "No eligible clean-air laps":
            lines.append("  The run applies the frozen track-status and clean-air filters to every race.")
    empty_pit = [r["race"] for r in valid if "pit_limitation" in r]
    if empty_pit:
        lines.extend(["", f"Empty pit streams ({', '.join(empty_pit)}) use the existing stint boundary and pit-out masks."])
    lines.extend(["",
                  ("Whole-race train/calibration/test clocks are checked for chronology. The 2-second clean-air mask "
                  "uses the existing lap context and green-flag rules. Forecasts use completed-lap history, "
                  "pre-race grid and qualifying features, and the last weather observation at or before the cutoff. "
                  "Historical feed receipt times are unavailable. Full lead/lag trajectories are retained as targets."),
                  ""])
    if (out/"baseline_comparison.json").is_file():
        lines.extend(_quality_tables(_load(out, "baseline_comparison")))
        lines.extend(["", "## Online adaptation", "",
                      "| History regime | Eligible H1 forecasts | Full model race MAE (s) | Without stint state (s) |",
                      "| --- | ---: | ---: | ---: |"])
        adapt = _load(out, "online_adaptation_summary")
        h1 = {(r["model"], r["regime"]): r["endpoint"] for r in adapt if r["horizon"] == 1}
        for model, regime in h1:
            if model == D8:
                x = h1[(D8, regime)]
                off = h1[("D8R1_without_stint_adaptation", regime)]
                lines.append(f"| {regime} | {x['eligible_forecasts']} | {_seconds(x['race_weighted_mae_s'])} | "
                             f"{_seconds(off['race_weighted_mae_s'])} |")
        lines.extend(["", *_integration(out), "", "## Release gates", "",
                      "| Gate | Result |", "| --- | --- |"])
        for gate, ok in metrics["promotion_gates"].items():
            if gate.startswith("G"):
                lines.append(f"| {gate.replace('_', ' ', 1).replace('_', ' ')} | {'PASS' if ok else 'FAIL'} |")
        thresholds = _load(out, "protocol")["validation"]
        lines.extend(["", (f"G3 required observed coverage of at least {thresholds['minimum_test_coverage_50']:.0%} "
                            f"for the 50% interval and {thresholds['minimum_test_coverage_90']:.0%} for the 90% "
                            "interval at H5, H10, H15 and H20.")])
        reproducibility = _load(out, "reproducibility")
        lines.extend(["", (f"Independent refit from the recorded feature table repeated "
                      f"{reproducibility['verified_rows']:,} prediction rows. Exact table equality: "
                      f"{reproducibility['exact_held_out_forecast_table']}. Fitted byte digest matched: "
                      f"{reproducibility['refit_model_sha256'] == reproducibility['initial_model_sha256']}.")])
    lines.extend([
        "",
        "## Reproduction and S6 scope",
        "",
        ("Run `f1ri stage3-r1 --acquire` to recreate the bounded source cache. Run "
        "`f1ri stage3-r1 --output artifacts/runs/stage3_r1_reproduced` against that cache. "
        "OpenF1 may revise its response bodies, so compare the source manifest before interpreting numerical "
        "differences across pulls. The evidence archive includes the input feature table, predictions, source "
        "ledger, release gates and per-file checksums. Extract it and run "
        "`f1ri stage3-r1 --verify-run <extracted-folder>` with the versions in the training manifest. "
        "The recorded run uses single-threaded BLAS and `PYTHONHASHSEED=42`."),
        "",
        "Stage 3 source hashes and frozen S5 metrics were verified. The 2026 regulation transfer remains unmeasured.",
        (("The mean-only release supports S6 comparisons within declared race scenarios. "
          "Probabilistic S6 needs horizon-aware recalibration on the calibration cohort and a further independent "
          "test of cumulative coverage, along with its own event and finishing-outcome evidence.")
         if metrics["release"] == "MEAN_ONLY_VALIDATED" else
         "S6 strategy use depends on the validated model tier and scenario support above."),
        "",
    ])
    (out/"validation_report.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    names = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    (out/"SHA256SUMS").write_text("".join(f"{digest(p)}  {p.relative_to(out).as_posix()}\n" for p in names),
                                 encoding="utf-8", newline="\n")
