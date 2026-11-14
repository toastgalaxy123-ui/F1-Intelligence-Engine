"""Render the S5-B evidence and simulation diagnostics."""
from collections import Counter, defaultdict

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt


def _missing(ax, label):
    ax.text(.03, .78, label, transform=ax.transAxes, wrap=True, va="top")
    ax.set_xticks([])
    ax.set_yticks([])


def plot_history(output, metrics, census, episodes, support, timing, queue, restart, comparison,
                 movement, sensitivity, route, released, declared):
    fig, grid = plt.subplots(4, 4, figsize=(22, 16), layout="constrained")
    ax = grid.flatten()
    titles = (
        "Cached archive coverage", "Imola SC location eligibility", "Imola cutoff support", "Imola race control",
        "Observed VSC lap durations", "Observed sector timing", "VSC observed versus simulated",
        "SC line-time spread", "SC train evidence", "First recorded lines after SC clear",
        "Imola SC line timing", "Absolute and relative error", "Lane time during neutralization",
        "Initial position sensitivity", "Frozen regression checks", "S5-C readiness",
    )
    for i, (panel, name) in enumerate(zip(ax, titles, strict=True)):
        panel.set_title(f"{chr(65+i)}  {name}", loc="left", fontweight="bold")
        panel.grid(alpha=.18)
    colors = {"X": "#a63d40", "H0": "#2c7c71", "H1": "#32619a", "H2": "#eaa640", "H3": "#a095a8"}
    sc_name, vsc_name = "imola_sc_4", "imola_vsc_1"
    sources = Counter((r["session"], r["endpoint"]) for r in census if r["source"] in
                      ("BOUNDED_S5_B", "APPROVED_EXISTING_CACHE"))
    ax[0].barh([f"{s} {endpoint}" for s, endpoint in sources], list(sources.values()), color="#32619a")
    ax[0].set_xlabel("Snapshots")
    selected = [r for r in support if r["episode"] == sc_name and r["source_set"] == "combined"]
    drivers = sorted({r["driver"] for r in selected})
    labels = ["initial", "deployment", "announcement", "global_clear"]
    reasons = dict.fromkeys(r["first_exclusion"] for r in selected if r["cutoff"] in labels and r["first_exclusion"])
    for i, reason in enumerate(reasons, 1):
        reasons[reason] = i
    for row in selected:
        if row["cutoff"] in labels:
            failure = row["first_exclusion"]
            ax[1].scatter(labels.index(row["cutoff"]), drivers.index(row["driver"]),
                          color=colors["X" if failure else "H0"], s=30, marker="x" if failure else "s")
            if failure:
                ax[1].annotate(str(reasons[failure]), (labels.index(row["cutoff"]), drivers.index(row["driver"])),
                               xytext=(5, -3), textcoords="offset points", fontsize=6)
    ax[1].set(yticks=range(len(drivers)), yticklabels=drivers,
              xticks=range(len(labels)), xticklabels=["Initial", "Deploy", "Announce", "Clear"],
              xlim=(-.4, len(labels)-.35))
    if reasons:
        ax[1].set_xlabel(" · ".join(f"{i} {r.replace('_', ' ').lower()}" for r, i in reasons.items()), fontsize=7)
    stack = [r for r in metrics["localization_counts"] if r["episode"] == sc_name and
             r["source_set"] == "combined" and r["cutoff"] in labels]
    bottom = [0]*len(stack)
    for tier in ("H0", "H1", "H2", "H3"):
        values = [r[tier] for r in stack]
        ax[2].bar([r["cutoff"].replace("global_", "") for r in stack], values, bottom=bottom, label=tier, color=colors[tier])
        bottom = [a+b for a, b in zip(bottom, values)]
    ax[2].legend(fontsize=8)
    for y, ep in enumerate(e for e in episodes if e["scope"] == "TRACK"):
        ax[3].barh(y, ep["duration_s"], color="#32619a", alpha=.6)
        if ep["announcement_utc"]:
            ax[3].scatter((ep["announcement_utc"]-ep["start_utc"]).total_seconds(), y, color="#eaa640")
        ax[3].scatter(ep["duration_s"], y, color="#2c7c71")
    ax[3].set(yticks=[0, 1], yticklabels=["VSC", "SC"], xlabel="Seconds from archived deployment")
    phases = ["PRE_STATUS_UNRESOLVED_OR_LOCAL", "ENTIRELY_NEUTRALIZED", "POST_GREEN"]
    distribution = [[r["duration_s"] for r in timing if r["episode"] == vsc_name and
                     r["segment"] == "LAP" and r["population"] == p] for p in phases]
    if any(distribution):
        ax[4].boxplot(distribution)
        ax[4].set(xticks=[1, 2, 3], xticklabels=["Pre, status mixed", "Full VSC", "Post GREEN"], ylabel="Elapsed (s)")
    else:
        _missing(ax[4], "VSC lap snapshot unavailable.")
    for kind, color in (("ENTIRELY_NEUTRALIZED", "#32619a"), ("PARTIALLY_NEUTRALIZED", "#eaa640")):
        for i, segment in enumerate(("S1", "S2", "S3")):
            rows = [r["duration_s"] for r in timing if r["segment"] == segment and r["population"] == kind]
            ax[5].scatter([i]*len(rows), rows, s=8, alpha=.3, color=color,
                          label=kind.split("_")[0].title() if i == 0 else None)
    ax[5].set(xticks=[0, 1, 2], xticklabels=["S1", "S2", "S3"], ylabel="Elapsed (s)",
              xlabel="Imola VSC and SC observations")
    ax[5].legend(fontsize=8)
    observed = [p for p in movement["vsc_matched_lap_durations"] if p["variant"] == "B"]
    ax[6].scatter([p["observed_s"] for p in observed], [p["simulated_s"] for p in observed], s=12)
    ax[6].axline((80, 80), slope=1, color="#888888", lw=.7)
    ax[6].set(xlabel="Observed elapsed (s)", ylabel="Simulated elapsed (s)")
    ax[7].plot([r["lap"] for r in queue], [r["full_observed_line_spread_s"] for r in queue], "o-", label="Archived")
    ax[7].plot([r["lap"] for r in queue], [r["matched_observed_line_spread_s"] for r in queue], "o-", label="Matched subset")
    ax[7].plot([r["lap"] for r in queue], [r["matched_simulated_line_spread_s"] for r in queue], "o-", label="Replay B")
    ax[7].set(xlabel="Completed lap", ylabel="Crossing spread (s)")
    ax[7].legend(fontsize=8)
    frames = [r for r in declared.progress if r["gaps_m"]]
    ax[8].plot([r["at_us"]/1e6 for r in frames], [max(r["gaps_m"].values()) for r in frames], lw=1)
    ax[8].set(xlabel="Replay clock (s)", ylabel="Simulated max gap (m)")
    ax[8].text(.04, .92, "Observed formation time unavailable", transform=ax[8].transAxes)
    next_lines = sorted(r["next_line_elapsed_s"] for r in restart if r["episode"] == sc_name)
    ax[9].scatter(next_lines, range(len(next_lines)), s=13, label="Recorded")
    permitted = sorted((r["simulated_driver_permission_utc"]-r["global_clear_utc"]).total_seconds()
                       for r in restart if r["episode"] == sc_name and r["simulated_driver_permission_utc"])
    ax[9].scatter(permitted, range(len(permitted)), s=13, label="Replay B permission")
    ax[9].axvline(0, label="Track clear", color="#2c7c71")
    ax[9].set(xlabel="Seconds after track clear", ylabel="Driver order")
    ax[9].legend(fontsize=8)
    values = defaultdict(list)
    for r in comparison:
        if r["episode"] == sc_name and r["common"]:
            values[r["variant"]].append(r)
    origin = next(e["start_utc"] for e in episodes if e["episode"] == sc_name)
    for label, rows in values.items():
        ax[10].scatter([(r["observed_utc"]-origin).total_seconds() for r in rows],
                       [(r["simulated_utc"]-origin).total_seconds() for r in rows], s=9, alpha=.55, label=label)
    ax[10].axline((0, 0), slope=1, color="#888888", lw=.7)
    ax[10].set(xlabel="Observed clock (s)", ylabel="Replay clock (s)")
    ax[10].legend(fontsize=8)
    stats = metrics["comparison"][sc_name]
    labels = list(stats["common"])
    x = list(range(len(labels)))
    ax[11].bar([n-.16 for n in x], [stats["common"][v]["median_abs_s"] for v in labels], .32, label="Line")
    ax[11].bar([n+.16 for n in x], [stats["relative_common"][v]["lap_duration"]["median_abs_s"] for v in labels],
               .32, label="Lap duration")
    ax[11].set(xticks=x, xticklabels=labels, ylabel="Median abs. error (s)")
    ax[11].legend(fontsize=8)
    for i, name in enumerate((vsc_name, sc_name)):
        data = [r["recorded_lane_elapsed_s"] for r in route if r["episode"] == name and r["recorded_lane_elapsed_s"]]
        ax[12].scatter(data, [i]*len(data), s=18, color="#32619a")
    ax[12].set(yticks=[0, 1], yticklabels=["VSC", "SC"], xlabel="Archived lane elapsed (s)")
    ran = [r for r in sensitivity if r.get("fixed_population_error")]
    if ran:
        ax[13].bar([str(r["shift_multiples_of_s3_heuristic_bound"]) for r in ran],
                   [r["fixed_population_error"]["median_abs_s"] for r in ran])
        ax[13].set(xlabel="Imola SC · signed multiple of position bound", ylabel="Common line median abs. error (s)")
    else:
        _missing(ax[13], "No complete, admissible initialization for a physical sensitivity replay.")
    _missing(ax[14], f"{sum(metrics['invariants'].values())} study invariants passed\n"
             f"Frozen SC delay  {released[sc_name]['frozen']['command_timing'][-1]['difference_us']/1e6:.3f} s\n"
             f"Revised SC delay  {released[sc_name]['stabilized']['command_timing'][-1]['difference_us']/1e6:.3f} s")
    decision = "Path B · Partial historical support" if metrics["s5_c"].startswith("PATH_B") else "Path C · Insufficient evidence"
    _missing(ax[15], f"{decision}\n"
             f"{metrics['independent_global_episodes']} global episodes in {metrics['independent_global_races']} race\n"
             "Strategy outcomes remain deterministic scenarios")
    (output/"diagnostics").mkdir()
    fig.savefig(output/"diagnostics/diagnostics.png", dpi=120)
    plt.close(fig)
