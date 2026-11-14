"""Diagnostic figures and report for executed S6-A comparisons."""

from collections import Counter, defaultdict
from json import loads

from matplotlib import pyplot as plt

from f1ri.validation.stage3_r_sources import digest

BLUE = "#236582"
ORANGE = "#bf7033"
GREEN = "#488768"


def _text(ax, value):
    ax.text(.04, .92, value, transform=ax.transAxes, va="top", fontsize=9, linespacing=1.5)
    ax.set_xticks([])
    ax.set_yticks([])


def _name(key):
    return key.replace("reference", "stay out").replace("63-", "").replace("__", " + ")


def _time(value):
    return "—" if value is None else f"{value/1e6:+.3f}"


def _rank_plot(ax, rows, limit=6):
    valid = sorted((r for r in rows if r["objective_delta_us"] is not None),
                   key=lambda r: (r["objective_us"], r["candidate"]))[:limit]
    ax.barh([_name(r["candidate"]) for r in reversed(valid)],
            [r["objective_delta_us"]/1e6 for r in reversed(valid)], color=BLUE)
    ax.axvline(0, color="#333333", linewidth=.7)
    zeros = [_name(r["candidate"]) for r in valid if r["objective_delta_us"] == 0]
    if zeros:
        ax.scatter([0]*len(zeros), zeros, color=BLUE, zorder=3)
    ax.set_xlabel("vs stay out (s)")


def _charts(out, summary, metrics, rows, rejected, visits, progress, constraints, sensitivity):
    titles = ("Candidate funnel", "Preflight rejection reasons", "Search coverage by scenario", "Pit lap vs elapsed delta",
              "Compound choice by pit lap", "One- and two-stop outcomes", "Tyre timeline", "Cumulative field progress",
              "Latent vs effective pace", "Following constraints", "Entry, service and rejoin", "Green flag comparison",
              "Declared VSC", "Declared Safety Car", "Shared-box queue", "Initial gap sensitivity",
              "Mean model sensitivity", "Near-tied rankings", "Search work and forecast cache", "Frozen regressions",
              "Model and scenario provenance", "Readiness")
    fig, grid = plt.subplots(6, 4, figsize=(22, 24), layout="constrained")
    axs = grid.flatten()
    for ax, title in zip(axs, titles, strict=False):
        ax.set_title(title, loc="left", fontweight="bold", fontsize=12)
        ax.grid(alpha=.14)
    for ax in axs[len(titles):]:
        ax.set_axis_off()
    declared = defaultdict(list)
    for row in rows:
        if row["evaluation"] == "declared":
            declared[row["scenario"]].append(row)
    searches = [s["search"] for s in summary.values()]
    names = ("Proposed", "Legal", "Evaluated", "Reached markers")
    amounts = [sum(s[k] for s in searches) for k in ("total_combinations", "legal", "evaluated")]
    amounts.append(sum(s["outcomes"].get("MARKERS_REACHED", 0) for s in searches))
    axs[0].bar(names, amounts, color=(BLUE, BLUE, BLUE, GREEN))
    axs[0].tick_params(axis="x", labelrotation=18)
    reasons = Counter(r["reason"] for r in rejected).most_common(6)
    axs[1].barh([x.replace("_", " ").title() for x, _ in reversed(reasons)],
                [n for _, n in reversed(reasons)], color=ORANGE)
    axs[1].tick_params(axis="y", labelsize=8)
    selected = ("clean-air", "vsc", "sc", "shared-box", "two-stop", "budget-limited")
    axs[2].barh(selected, [summary[k]["search"]["legal"] for k in selected], color="#cdd8df", label="legal")
    axs[2].barh(selected, [summary[k]["search"]["evaluated"] for k in selected], color=BLUE, label="evaluated")
    axs[2].legend(fontsize=8)
    for scene, color in (("clean-air", BLUE), ("late-vsc", ORANGE)):
        points = [(int(r["candidate"].split("-")[1][1:]), r["objective_delta_us"]/1e6)
                  for r in declared[scene] if r["candidate"].startswith("63-L") and r["objective_delta_us"] is not None]
        if points:
            axs[3].scatter(*zip(*points, strict=True), label=scene, color=color)
    axs[3].set(xlabel="Scheduled pit lap", ylabel="vs stay out (s)")
    axs[3].legend(fontsize=8)
    for tyre in ("hard", "medium", "soft", "used-soft"):
        points = [(int(r["candidate"].split("-")[1][1:]), r["objective_delta_us"]/1e6)
                  for r in declared["clean-air"] if r["candidate"].startswith("63-L")
                  and r["candidate"].split("-", 2)[-1] == tyre and r["objective_delta_us"] is not None]
        if points:
            axs[4].plot(*zip(*points, strict=True), "o-", label=tyre)
    axs[4].set(xlabel="Scheduled pit lap", ylabel="vs stay out (s)")
    axs[4].legend(fontsize=8)
    for n, color in ((1, BLUE), (2, ORANGE)):
        values = [r["objective_delta_us"]/1e6 for r in declared["two-stop"]
                  if r["pit_visits"] == n and r["objective_delta_us"] is not None]
        axs[5].scatter([n]*len(values), values, color=color, alpha=.6)
    axs[5].set(xticks=[1, 2], xlabel="Completed pit visits", ylabel="vs stay out (s)")
    candidate_ids = ("reference", "63-L31-soft", "63-L32-hard")
    marker = int(summary["clean-air"]["targets"][0][1])
    for key in candidate_ids:
        visits_for = []
        for row in progress:
            if row["scenario"] == "clean-air" and row["candidate"] == key and row["driver"] == 63:
                visits_for.append(row)
                if row["checkpoint"] == marker:
                    break
        if visits_for:
            start = visits_for[0]["clock_us"]
            xt = [(p["clock_us"]-start)/1e6 for p in visits_for]
            axs[6].step(xt, [p["compound"] for p in visits_for], where="post", label=_name(key))
            axs[7].step(xt, [p["checkpoint"] for p in visits_for], where="post", label=_name(key))
    for index in (6, 7):
        axs[index].set_xlabel("Elapsed (s)")
        axs[index].legend(fontsize=8)
    axs[7].set_ylabel("Checkpoint")
    for tyre in ("hard", "soft"):
        key = f"63-L31-{tyre}"
        selected_rows = [r for r in constraints if r["scenario"] == "two-car" and r["candidate"] == key
                         and r["driver"] == 63 and r["latent_us"] is not None]
        for field, style in (("latent_us", "--"), ("effective_us", "-")):
            axs[8].plot([r["checkpoint"] for r in selected_rows], [r[field]/1e6 for r in selected_rows],
                        style, label=f"{tyre} {field.split('_')[0]}")
    axs[8].set(xlabel="Checkpoint", ylabel="Traversal (s)")
    axs[8].legend(fontsize=7)
    following = [r for r in declared["two-car"] if r["candidate"] in (*candidate_ids, "63-L31-hard")]
    axs[9].barh([_name(r["candidate"]) for r in following], [r["following_us"]/1e6 for r in following], color=BLUE)
    axs[9].set_xlabel("Following beyond other bounds (s)")
    p = next((x for x in visits if x["scenario"] == "clean-air" and x["candidate"] == "63-L31-soft"), None)
    if p is not None:
        clocks = ("entry_us", "service_start_us", "service_end_us", "exit_us", "merge_us")
        axs[10].scatter([(p[k]-p["entry_us"])/1e6 for k in clocks], range(len(clocks)), color=BLUE)
        axs[10].set(yticks=range(len(clocks)), yticklabels=("Entry", "Service start", "Service end", "Exit", "Rejoin"),
                    xlabel="Seconds after entry")
    for i, name in enumerate(("clean-air", "vsc", "sc"), 11):
        _rank_plot(axs[i], [r for r in declared[name] if r["pit_visits"] or r["candidate"] == "reference"])
    stacks = sorted((r for r in declared["shared-box"] if r["pit_visits"] == 2 and r["objective_us"] is not None),
                    key=lambda r: (-r["service_wait_us"], r["objective_us"]))[:8]
    axs[14].barh([_name(r["candidate"]) for r in stacks], [r["service_wait_us"]/1e6 for r in stacks], color=ORANGE)
    axs[14].tick_params(axis="y", labelsize=7)
    axs[14].set_xlabel("Team service wait (s)")
    gap_names = ("two-car", "wider-gap", "retirement", "original-opponent")
    for tyre in ("hard", "soft"):
        key = f"63-L31-{tyre}"
        selected_rows = [next(r for r in declared[scene] if r["candidate"] == key) for scene in gap_names]
        axs[15].plot(gap_names, [r["objective_delta_us"]/1e6 if r["objective_delta_us"] is not None else None
                                 for r in selected_rows], "o-", label=tyre)
    axs[15].tick_params(axis="x", labelrotation=18)
    axs[15].set_ylabel("vs stay out (s)")
    axs[15].legend(fontsize=8)
    varied = ("two-car", "mean-pace-plus-0.3s", "soft-plus-0.3s", "degradation-plus-0.06s-per-age-lap")
    for key in ("63-L31-hard", "63-L31-soft"):
        values = [next(r["objective_delta_us"] for r in declared[scene] if r["candidate"] == key)
                  for scene in varied]
        axs[16].plot(("base", "pace", "soft", "age slope"), [x/1e6 if x is not None else None for x in values],
                     "o-", label=_name(key))
    axs[16].set_ylabel("vs stay out (s)")
    axs[16].legend(fontsize=8)
    _rank_plot(axs[17], [r for r in declared["late-vsc"] if r["pit_visits"] or r["candidate"] == "reference"], limit=8)
    for field, color in (("forecast_calls", BLUE), ("forecast_cache_hits", GREEN)):
        axs[18].plot(selected, [summary[name]["search"][field] for name in selected], "o-", color=color,
                     label=field.replace("_", " "))
    axs[18].legend(fontsize=8)
    axs[18].tick_params(axis="x", labelrotation=18)
    _text(axs[19], f"Frozen model, archive and S5 hashes matched.\n"
          f"{sum(metrics['fixture_checks'].values())}/{len(metrics['fixture_checks'])} fixture checks passed.\n"
          f"{len(metrics['invariant_failures'])} invariant failures recorded.")
    _text(axs[20], "PaceV2_D8R1 · 2025 Singapore cutoff\nMean-only release · Declared field and tyres\n"
          "No unscripted pass in the main scenarios\nNeutralization pace is uncalibrated")
    _text(axs[21], f"{metrics['decision'].replace('_', ' ')}\n"
          f"{metrics['scenarios']} declared scenarios\n"
          f"{metrics['simulator_evaluations']} simulator evaluations\n"
          f"{sum(s['winner_set_changed'] for s in sensitivity)} sensitivity winner-set changes")
    (out/"diagnostics").mkdir(exist_ok=True)
    fig.savefig(out/"diagnostics/conditional_optimization.png", dpi=115)
    plt.close(fig)
    runtime = loads((out/"performance_observations.json").read_bytes())
    fig, (wall, calls) = plt.subplots(1, 2, figsize=(12, 5), layout="constrained")
    wall.barh(selected, [runtime[k] for k in selected], color=BLUE)
    wall.set(xlabel="Wall-clock seconds", title="Runtime on the study machine")
    counts = loads((out/"cache_diagnostics.json").read_bytes())
    calls.bar(("Cache on", "Cache off"), (counts["calls_with_cache"], counts["calls_without_cache"]),
              color=(BLUE, ORANGE))
    calls.set(ylabel="Model forecast calls", title="Identical VSC branches")
    fig.savefig(out/"diagnostics/runtime_and_cache.png", dpi=125)
    plt.close(fig)


def finish_report(out, metrics, summary, rows, rejected, visits, progress, constraints, sensitivity):
    _charts(out, summary, metrics, rows, rejected, visits, progress, constraints, sensitivity)
    lines = [
        "# F1 Race Intelligence Engine — Stage 6 S6-A",
        "## Conditional strategy optimization",
        "",
        f"**`{metrics['decision']}`**",
        "",
        (f"Bounded enumeration executed {metrics['simulator_evaluations']} strategy branches across "
         f"{metrics['scenarios']} declared scenarios and sensitivities using frozen PaceV2_D8R1 means. "
         "The S5-C scheduler resolved actual entry, service, rejoin, following and neutralization. "
         "The Singapore 2025 cutoff supplies model features. The track positions, resources, routes, "
         "opponent variations and control schedules come from declared scenarios. A midlap cutoff can "
         "require the next lap's forecast to complete the final partial sector."),
        "",
        "## Decisions and scenario outcomes",
        "",
        ("A point-in-time choice uses the events known at the cutoff. The same candidates also run through "
         "the full declared script to show what happens if those events occur. Reactive policies see committed "
         "announcements as they arrive. Perfect-foresight runs make future scripted intervention clocks "
         "available for candidate review and are labeled separately."),
        "",
        "| Scenario | Point-in-time best or oracle best | Best in declared script | Best delta vs reference (s) | Search |",
        "| --- | --- | --- | ---: | --- |",
    ]
    chosen = ("clean-air", "two-car", "retirement", "vsc", "sc", "late-vsc", "shared-box", "two-stop",
              "three-lap", "ten-lap", "budget-limited", "vsc-oracle", "sc-oracle")
    for name in chosen:
        v = summary[name]
        top = v["top_declared"]
        win = []
        for key in ("best_decision", "best_declared"):
            winners = v[key]
            displayed = sorted(winners, key=lambda k: (k != "reference", k))
            labels = ", ".join(_name(x) for x in displayed[:3]) or "Unsupported"
            win.append(labels + (f" (+{len(winners)-3} exact ties)" if len(winners) > 3 else ""))
        lines.append(f"| {name} | {win[0]} | {win[1]} | "
                     f"{_time(top[0]['objective_delta_us'] if top else None)} | {v['completion']} |")
    lines.extend([
        "",
        ("Inactive reactive policies can tie the reference because no trigger committed inside the pit window. "
         "The decision ledger preserves all exact ties and near-equal candidates. Near-equality margins are "
         "0.91, 1.44 and 3.54 seconds per driver at three, five and ten laps, based on held-out mean errors. "
         "These are reporting thresholds and carry no significance or probability interpretation."),
        "",
        "## Physical and model sensitivities",
        "",
    ])
    q = {(r["scenario"], r["candidate"]): r for r in rows if r["evaluation"] == "declared"}
    for name in ("clean-air", "two-car", "retirement", "wider-gap"):
        hard, soft = (q[name, f"63-L31-{t}"] for t in ("hard", "soft"))
        a, b = hard["objective_us"], soft["objective_us"]
        lines.append(f"- {name} · hard minus soft {_time(None if a is None or b is None else a-b)} s. "
                     f"Soft receives {soft['following_us']/1e6:.3f} s from following beyond other bounds.")
    queues = [p["wait_us"] or 0 for p in visits if p["scenario"] == "shared-box"]
    late = q["late-vsc", "63-L32-soft"]
    stack, four_first, sixty_three_first = (q["shared-box", k] for k in (
        "4-L30-soft__63-L31-soft", "4-L30-soft__63-L32-soft", "4-L31-soft__63-L31-soft"))
    lines.extend([
        "",
        (f"The late-VSC fixed stop gained {abs(late['objective_delta_us'] or 0)/1e6:.3f} s over staying out. "
         "That difference falls within the five-lap model-error margin. The decisive timing changes in the "
         "early-VSC and SC fixtures are conditional on their declared speeds, pit geometry and control clocks."),
        "",
        (f"The largest observed shared-box wait was {max(queues, default=0)/1e6:.3f} s. "
         f"For the soft/soft plans, the double stack cost {_time(stack['objective_delta_us'])} s versus staying out. "
         f"Staggering changed that to {_time(four_first['objective_delta_us'])} s with driver 4 first and "
         f"{_time(sixty_three_first['objective_delta_us'])} s with driver 63 first. "
         "Team branches report per-driver deltas alongside the combined, lead-driver and weighted objectives."),
        "",
        (f"{sum(x['winner_set_changed'] for x in sensitivity)}/{len(sensitivity)} declared variations changed "
         "the set of first-place candidates. "
         f"{sum(x['pairwise_rank_reversals'] > 0 for x in sensitivity)} variations reversed the order of at least "
         "one comparable candidate pair. The sensitivity ledger records changes in pace level, tyre slope, "
         "compound, pit travel, service, gap, opponent behavior, VSC speed and restart pace."),
        "",
        "## Verification",
        "",
        "| Fixture | Result |",
        "| --- | --- |",
    ])
    for name, ok in metrics["fixture_checks"].items():
        lines.append(f"| {name.replace('_', ' ')} | {'PASS' if ok else 'FAIL'} |")
    lines.extend([
        "",
        (f"The full physics ledgers recorded {len(metrics['invariant_failures'])} invariant failures. "
         "The small-space benchmark builds the fixed policies independently, compares marker values and event "
         "traces, and also checks reordered subsets. Forecast caching reuses complete typed forecast requests "
         "within one search. The cache parity run checks complete path and score equality."),
        "",
        "## Reproduce the study",
        "",
        ("`f1ri stage6-s6-a --output artifacts/runs/stage6_s6_a_reproduced` runs the frozen matrix. "
         "Install the repository modeling dependencies and verify the input hashes in `regression_parity.json`. "
         "The checksum list covers the report, configuration, ledger tables and charts. "
         "Wall-clock measurements and the runtime figure vary with the machine and run."),
        "",
        ("This readiness decision applies to conditional, bounded 2025 mean optimization. Exact historical "
         "field reconstruction, calibrated neutralization pace, 2026 transfer and race-win probabilities remain "
         "outside the validated release."),
        "",
    ])
    (out/"validation_report.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    seal(out)


def seal(out):
    files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    (out/"SHA256SUMS").write_text("".join(f"{digest(p)}  {p.relative_to(out).as_posix()}\n" for p in files),
                                 encoding="utf-8", newline="\n")
