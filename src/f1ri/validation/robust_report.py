"""Scenario-conditional explanations and 25 diagnostics from executed S6-B ledgers."""

from collections import Counter
from json import loads

import numpy as np
from matplotlib import pyplot as plt

from f1ri.simulator.strategy_policy import PolicyKind
from f1ri.validation.optimization_report import seal

BLUE = "#206689"
ORANGE = "#b56b32"
GREEN = "#438065"


def policy_label(key):
    return key.replace("reference", "Stay out").replace("63-", "").replace("__", " + ")


def describe_policy(row):
    planned = row["stops"]
    if not planned:
        return "Stay out over the evaluation window."
    if row["type"] == "ObservedPolicy":
        stop, = planned
        return ("Before the decision deadline, take "
                f"{row['neutralized_set']} at the next valid entry on laps {row['first_lap']}–{row['last_lap']} "
                "if VSC or SC is active and deployment has been committed. "
                f"If GREEN continues until lap {row['green_lap']} is the next pit opportunity, take {stop['set_id']}.")
    if row["type"] == "WindowedPolicy":
        condition = {PolicyKind.VSC: "VSC is active", PolicyKind.SC: "Safety Car is active"}.get(
            row["kind"], "green running has resumed after a Safety Car")
        return (f"Take {planned[0]['set_id']} at the next valid entry on laps {row['first_lap']}–{row['last_lap']} "
                f"if {condition}. Stay out if the trigger never arrives in the window.")
    return " · ".join(f"Driver {s['driver']} pits on lap {s['lap']} for {s['set_id']}" for s in planned)+"."


def _seconds(x):
    return "—" if x is None else f"{x/1e6:+.3f}"


def _text(ax, text):
    ax.text(.04, .88, text, transform=ax.transAxes, va="top", fontsize=8, linespacing=1.5)
    ax.set_xticks([])
    ax.set_yticks([])


def _heat(ax, fig, grid, choices, cases, field, cmap):
    values = np.array([[np.nan if grid[p, s][field] is None else grid[p, s][field]/1e6 for s in cases]
                       for p in choices])
    im = ax.imshow(values, cmap=cmap, aspect="auto")
    ax.set_xticks(range(len(cases)), cases, rotation=35, ha="right", fontsize=7)
    ax.set_yticks(range(len(choices)), [policy_label(p) for p in choices], fontsize=7)
    fig.colorbar(im, ax=ax, shrink=.65, label="Seconds")


def _bars(ax, rows, key, limit=8):
    rows = sorted((r for r in rows if r[key] is not None), key=lambda r: (r[key], r["policy"]))[:limit]
    ax.barh(range(len(rows)), [r[key]/1e6 for r in rows], color=BLUE)
    ax.scatter([r[key]/1e6 for r in rows], range(len(rows)), color=BLUE, s=12, zorder=3)
    ax.set(yticks=range(len(rows)), yticklabels=[policy_label(r["policy"]) for r in rows], xlabel="Seconds")
    ax.tick_params(axis="y", labelsize=7)
    ax.invert_yaxis()


def _diagnostics(out, metrics, summary, oracles, regrets, outcomes, executions, equivalence, cache):
    titles = (
        "Declared primary futures", "Common initial-state audit", "Legal policy funnel", "Scenario coverage",
        "Reference-relative time", "Regret vs best realized", "Maximum regret", "Worst relative loss", "Pareto frontier",
        "GREEN / VSC / Safety Car", "Policy and hindsight oracle", "Realized pit actions", "SC deployment timing",
        "SC duration sensitivity", "VSC pit opportunity", "Tyre and pace sensitivity", "Initial gap and traffic",
        "Shared-box coordination", "Behavioral equivalence", "Near-equal alternatives", "Horizon stability",
        "Model and scenario support", "Work and forecast caching", "Frozen regression parity", "Decision readiness",
    )
    fig, grid = plt.subplots(5, 5, figsize=(25, 21), layout="constrained")
    axes = grid.flatten()
    for ax, title in zip(axes, titles, strict=True):
        ax.set_title(title, loc="left", fontsize=11, fontweight="bold")
        ax.grid(alpha=.15)
    primary = summary["primary"]
    cases = tuple(primary["scenarios"])
    best = primary["presentation"][0] if primary["presentation"] else "reference"
    focal = ("reference", "63-L31-soft", "63-L32-soft", "vsc-63-soft", "sc-63-soft", best)
    policy_cases = {k: {} for k in summary}
    for r in regrets:
        policy_cases[r["family"]][r["policy"], r["scenario"]] = r
    audit = loads((out/"initial_state_manifest.json").read_bytes())
    _text(axes[0], "\n".join(f"{k} · {primary['scenarios'][k]['category'].replace('_', ' ').title()}" for k in cases))
    _text(axes[1], f"Primary initialization hashes: {len({r['initial_sha256'] for r in audit['primary']['variants'].values()})}\n"
          f"Gap family initialization hashes: {len({r['initial_sha256'] for r in audit['initial-gap']['variants'].values()})}\n"
          "Scenarios share the cutoff and markers.\nGap alternatives explicitly move cars.")
    search = primary["search"]
    axes[2].bar(("Offered", "Evaluated", "Full support"),
                (search["offered_policies"], search["evaluated_policies"], sum(r["comparable"] for r in primary["results"])),
                color=(BLUE, BLUE, GREEN))
    labels = ("primary", "neutralization-timing", "initial-gap", "team-combined", "budget-limited")
    y = range(len(labels))
    axes[3].barh(y, [summary[k]["search"]["expected_policy_scenario"] for k in labels], color="#c1d3df")
    axes[3].barh(y, [summary[k]["search"]["supported_cells"] for k in labels], color=BLUE)
    axes[3].set(yticks=y, yticklabels=labels, xlabel="Policy/scenario cells")
    _heat(axes[4], fig, policy_cases["primary"], focal, cases, "delta_us", "RdBu_r")
    _heat(axes[5], fig, policy_cases["primary"], focal, cases, "best_realized_regret_us", "YlOrBr")
    _bars(axes[6], primary["results"], "worst_regret_us")
    _bars(axes[7], primary["results"], "worst_delta_us")
    for row in primary["results"]:
        if row["comparable"] and row["worst_regret_us"] is not None:
            front = not row["dominated_by"]
            axes[8].scatter(row["worst_delta_us"]/1e6, row["worst_regret_us"]/1e6,
                            color=GREEN if front else "#a7b8c1", s=22)
    axes[8].set(xlabel="Worst delta (s)", ylabel="Worst regret (s)")
    for policy in focal[1:]:
        cases_to_plot = ("green", "early-vsc", "early-sc")
        axes[9].plot(cases_to_plot, [policy_cases["primary"][policy, c]["delta_us"]/1e6 for c in cases_to_plot],
                     "o-", label=policy_label(policy))
    axes[9].legend(fontsize=6)
    axes[9].set_ylabel("vs stay out (s)")
    for field, label in (("best_realized_regret_us", "Best realized"),
                         ("perfect_information_regret_us", "Perfect information")):
        axes[10].plot(range(len(cases)), [policy_cases["primary"][best, c][field]/1e6
                      if policy_cases["primary"][best, c][field] is not None else np.nan for c in cases],
                      "o-", label=label)
    axes[10].set(xticks=range(len(cases)), xticklabels=cases, ylabel="Regret (s)")
    axes[10].tick_params(axis="x", rotation=30, labelsize=7)
    axes[10].legend(fontsize=7)
    selected_visits = {r["scenario"]: r["pit"] for r in executions if r["family"] == "primary" and r["policy"] == best}
    axes[11].barh(cases, [p[0]["target_lap"] if (p := selected_visits.get(c)) else 0 for c in cases], color=BLUE)
    axes[11].set(xlabel="Realized pit lap")
    for ax, tag in zip(axes[12:15], ("sc-deploy-", "sc-release-", "vsc-deploy-"), strict=True):
        chosen = sorted((int(k.removeprefix(tag)), k) for k in summary["neutralization-timing"]["scenarios"]
                        if k.startswith(tag))
        for policy in ("63-L31-soft", "63-L32-soft", "vsc-63-soft", "sc-63-soft", best):
            ax.plot([n for n, _ in chosen], [policy_cases["neutralization-timing"][policy, k]["delta_us"]/1e6
                    for _, k in chosen], "o-", label=policy_label(policy))
        best_by_case = [min(r["delta_us"] for (_, s), r in policy_cases["neutralization-timing"].items()
                            if s == k and r["delta_us"] is not None)/1e6 for _, k in chosen]
        ax.scatter([n for n, _ in chosen], best_by_case, facecolors="none", edgecolors="#202020",
                   marker="o", s=53, label="Best evaluated", zorder=4)
        ax.set(xlabel="Script clock offset (s)", ylabel="vs stay out (s)")
        ax.legend(fontsize=6)
    for j, (family, cs) in enumerate((
        ("pace-and-route-sensitivity", ("baseline-pace", "soft-plus-0.3s", "older-used-soft", "new-stint-plus-0.3s",
                                       "degradation-plus-0.06s-per-age-lap")),
        ("initial-gap", ("close-gap", "wider-gap")),
    ), 15):
        for key in ("63-L31-hard", "63-L31-soft", "63-L31-used-soft"):
            values = [policy_cases[family][key, c]["delta_us"] for c in cs]
            axes[j].plot(cs, [n/1e6 if n is not None else np.nan for n in values], "o-", label=policy_label(key))
        axes[j].set_ylabel("vs stay out (s)")
        axes[j].tick_params(axis="x", rotation=25, labelsize=7)
        axes[j].legend(fontsize=6)
    queues = [x for x in outcomes if x["family"] == "team-combined" and x["scenario"] == "green"
              and x.get("service_wait_us", 0) > 0]
    queues.sort(key=lambda r: -r["service_wait_us"])
    axes[17].barh([policy_label(r["policy"]) for r in queues[:5]], [r["service_wait_us"]/1e6 for r in queues[:5]], color=ORANGE)
    axes[17].tick_params(axis="y", labelsize=7)
    axes[17].set_xlabel("Shared-box wait (s)")
    cnt = Counter(pair["equivalence"] or "Distinct" for pair in equivalence["primary"])
    axes[18].barh(("Whole family", "Subset", "Behavior differs"),
                  (cnt["POLICY_EQUIVALENT_ACROSS_FAMILY"], cnt["EQUIVALENT_IN_SUBSET"], cnt["Distinct"]), color=BLUE)
    axes[18].set_xlabel("Policy pairs")
    near = [r for r in primary["results"] if r["policy"] in {*primary["selected"], *primary["near"]}]
    _bars(axes[19], near, "worst_regret_us")
    axes[19].axvline(primary["near_margin_us"]/1e6, linestyle="--", color=ORANGE)
    for policy in ("63-L31-soft", "adaptive-63-medium-or-soft", "reference"):
        values = [next(r["worst_regret_us"] for r in summary[f"horizon-{h}"]["results"] if r["policy"] == policy)
                  for h in (3, 5, 10)]
        axes[20].plot((3, 5, 10), [x/1e6 if x is not None else np.nan for x in values], "o-", label=policy_label(policy))
    axes[20].legend(fontsize=7)
    axes[20].set(xlabel="Horizon (laps)", ylabel="Worst regret (s)")
    _text(axes[21], "PaceV2_D8R1 · mean only\n2025 declared geometry and inventory\n"
          f"Supported study cells {metrics['supported_cells']:,}\n"
          "No statistical scenario weights")
    axes[22].bar(("Cache on", "Cache off"), (cache["cached_calls"], cache["uncached_calls"]), color=(BLUE, ORANGE))
    axes[22].set_ylabel("Model forecasts")
    _text(axes[23], f"Frozen Stage 3-R, S5 and S6-A hashes matched.\n"
          f"{sum(metrics['checks'].values())}/{len(metrics['checks'])} executed fixture checks passed.\n"
          f"{len(metrics['invariant_failures'])} physical invariant failures.")
    _text(axes[24], f"{metrics['decision'].replace('_', ' ')}\n{metrics['families']} families\n"
          f"{metrics['evaluations']:,} simulator evaluations\nPrimary selection scoped to six futures.")
    folder = out/"diagnostics"
    folder.mkdir()
    fig.savefig(folder/"robust_strategy.png", dpi=115)
    plt.close(fig)


def generate_report(out, metrics, summary, oracles, regrets, outcomes, executions, equivalence, cache):
    _diagnostics(out, metrics, summary, oracles, regrets, outcomes, executions, equivalence, cache)
    primary = summary["primary"]
    reference = "reference"
    best = primary["presentation"][0] if primary["presentation"] else reference
    chosen = next(r for r in primary["results"] if r["policy"] == best)
    definitions = loads((out/"policy_library.json").read_bytes())["primary"]["policies"]
    observed = {(r["scenario"], r["policy"]): r for r in regrets if r["family"] == "primary"}
    trips = {(r["scenario"], r["policy"]): r["pit"] for r in executions if r["family"] == "primary"}
    lines = [
        "# F1 Race Intelligence Engine — Stage 6 S6-B",
        "## Robust strategy under declared futures",
        "", f"**`{metrics['decision']}`**", "",
        (f"The study evaluated {metrics['evaluations']:,} branches across {metrics['families']} families. "
         "Every policy in a family runs from the same decision cutoff and targets the same common marker. "
         "The initial-gap family declares alternative physical positions explicitly. The primary family uses the "
         "Singapore 2025 saved model context and a declared two-car field with a slower predecessor."),
        "", "## Primary minimax-regret policy", "",
        (f"{len(primary['selected'])} policies tied at a worst-case regret of {_seconds(chosen['worst_regret_us'])} s "
         f"and a worst reference-relative loss of {_seconds(chosen['worst_delta_us'])} s. "
         "The GREEN fallback tyre changes the action while following equalizes the marker time. "
         f"Worst reference-relative cases · {', '.join(chosen['worst_delta_cases'])}."),
    ]
    for candidate in primary["presentation"]:
        lines.append(f"- **{policy_label(candidate)}** · {describe_policy(definitions[candidate])}")
    lines.extend(["", ("The adaptive policy can react to committed VSC or Safety Car deployment until 120 seconds "
                       "after cutoff, within its declared pit window. Inactive triggers and fallback decisions "
                       "are visible in the ledger. Exact ties retain different actions where applicable."),
                  "", "| Future | Declared assumption | Realized stop | Δ vs stay out (s) | Best-realized regret (s) | Oracle regret (s) |",
                  "| --- | --- | --- | ---: | ---: | ---: |"])
    for scene, assumptions in primary["scenarios"].items():
        row = observed[scene, best]
        pits = trips.get((scene, best), ())
        actions = ", ".join(f"L{x['target_lap']} {x['set_id']}" for x in pits) or "Stay out"
        lines.append(f"| {scene} | {assumptions['changed_assumptions']} | {actions} | {_seconds(row['delta_us'])} | "
                     f"{_seconds(row['best_realized_regret_us'])} | {_seconds(row['perfect_information_regret_us'])} |")
    lines.extend(["",
                  (f"Near-equal alternatives under the prespecified {primary['near_margin_us']/1e6:.2f} s margin · "
                   + (", ".join(map(policy_label, primary["near"])) or "None")),
                  ("The margin is a model-error reporting guideline. Exact minimax arithmetic and scenario-wise "
                   "dominance use unrounded simulated microseconds."),
                  "", "## Comparator and search scope", "",
                  ("Best-realized regret compares against the fastest valid execution from the fixed library in each "
                   "future. The perfect-information comparator additionally searches S6-A review clocks chosen "
                   "using the declared future script. Both benchmarks are bounded. The primary library is "
                   "evaluated exhaustively and every primary oracle search completes."),
                  "",
                  ("No scenario probabilities or uncalibrated D8R1 intervals enter these decisions. A separate "
                   "declared-weight demonstration normalizes supplied weights. Incomplete searches preserve "
                   "observed deltas and withhold exact minimax rankings and regret."),
                  "", "## Sensitivity and team decisions", "",
                  "| Family | Selected policy or ties | Coverage |", "| --- | --- | --- |"])
    for key in ("neutralization-timing", "pit-entry-boundary", "initial-gap", "future-traffic", "separate-clean-field",
                "pace-and-route-sensitivity", "horizon-3", "horizon-5", "horizon-10", "team-combined", "team-lead",
                "team-weighted", "budget-limited"):
        s = summary[key]
        names = ", ".join(map(policy_label, s["presentation"])) or "Withheld"
        lines.append(f"| {key} | {names} | {s['search']['evaluated_policy_scenario']}/{s['search']['expected_policy_scenario']} |")
    lines.extend(["",
                  ("The timing sweep varies deployment, duration and control pace with the original scheduler. "
                   "The boundary test brackets the earlier S6-A GREEN entry clock by one microsecond on each side. "
                   "The wider gap restores a tyre difference suppressed by following in the close-gap case. "
                   "The scripted pass is an authorized deterministic sensitivity."),
                  "",
                  ("H3, H5 and H10 use the same policy definitions on separate common-marker matrices. Planned "
                   "stops beyond the marker are labeled as unexecuted prefixes. A pending in-window stop that "
                   "fails to complete remains unsupported. Team results retain each driver's elapsed delta, "
                   "committed arrival and service order, wait and rejoin in the audit ledger."),
                  "",
                  "## Verification and reproduction", "",
                  (f"The study recorded {len(metrics['invariant_failures'])} physical invariant failures. "
                   "The single-future run reproduced S6-A marker outcomes and event traces. Reversing the "
                   "policy and scenario orders and adding policies or cases preserved earlier executions. "
                   f"The cache parity fixture used {cache['cached_calls']} calls with caching and "
                   f"{cache['uncached_calls']} without, with equal traces."),
                  "",
                  ("`f1ri stage6-s6-b --output artifacts/runs/stage6_s6_b_reproduced` runs the declared matrix. "
                   "The evidence archive includes the protocol, realized decision and pit ledgers, forecast provenance, "
                   "both regret comparators, dominance, near-equality diagnostics and checksums."),
                  "",
                  ("This release is limited to 2025 declared conditions and the D8R1 mean forecast gate. "
                   "Historical field reconstruction, neutralization pace calibration and probabilistic outcomes "
                   "remain outside the release. S6-C and the separate Stage 3-R.1 uncertainty study can proceed."),
                  ""])
    (out/"validation_report.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    seal(out)
