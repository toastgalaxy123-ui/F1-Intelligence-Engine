"""S4-B.1 figures from archived evidence and measured support."""
from collections import Counter

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt
from matplotlib.ticker import MaxNLocator

from f1ri.validation.overtake_evidence import utc


def plot_stabilization(output, dossiers, metrics, gaps, summaries, failures):
    fig, axes = plt.subplots(1, len(dossiers), figsize=(19, 4), layout="constrained", squeeze=False)
    for ax, row in zip(axes[0], dossiers, strict=True):
        points = row["gap_trajectory"]
        start = utc(row["event_time_lower_utc"])
        time = [(utc(p["at_utc"]) - start).total_seconds() for p in points]
        gap = [p["gap_m"] for p in points]
        error = [p["uncertainty_m"] for p in points]
        ax.plot(time, gap, ".-", color="#176d81")
        ax.fill_between(time, [x-y for x, y in zip(gap, error, strict=True)],
                        [x+y for x, y in zip(gap, error, strict=True)], color="#176d81", alpha=.18)
        ax.axhline(0, color="#333", lw=.7)
        ax.set(title=f"{row['circuit']} {row['attacker']} → {row['target']}\n{row['decision'].replace('_', ' ')}",
               xlabel="Seconds from last confirmed behind", ylabel="Signed gap (m)")
    fig.savefig(output / "competitive_timelines.png", dpi=125)
    plt.close(fig)

    fig, grid = plt.subplots(4, 4, figsize=(22, 15), layout="constrained")
    axes = list(grid.flat)
    names = (
        "Competitive brackets", "Timing and lap chart checks", "Tier reassessment", "First localization failures",
        "Censoring by race and gap band", "Support before localization loss", "Fixed and event-driven follow-up",
        "Competitive and lapped cutoffs", "Overlapping window evidence", "Positive cutoffs and events",
        "Target gap coverage", "Cached support conversion", "Bracket width by final tier",
        "Encounter outcome sensitivity", "Frozen and reassessed outcomes", "Development race readiness",
    )
    for i, (ax, name) in enumerate(zip(axes, names, strict=True)):
        ax.set_title(f"{chr(65+i)}  {name}", loc="left", fontweight="bold")
        ax.grid(alpha=.15)

    def bar(i, pairs):
        pairs = list(pairs)
        axes[i].barh([str(k).replace("_", " ").lower() for k, _ in pairs],
                     [v for _, v in pairs], color="#176d81")
        axes[i].tick_params(axis="y", labelsize=8)
        axes[i].xaxis.set_major_locator(MaxNLocator(5))

    labels = [f"{r['session']} {r['attacker']}→{r['target']}" for r in dossiers]
    for i, r in enumerate(dossiers):
        axes[0].plot([0, r["event_time_bracket_s"]], [i, i], "o-", color="#176d81")
    axes[0].set(yticks=range(len(labels)), yticklabels=labels, xlabel="Bracket seconds")
    for i, r in enumerate(dossiers):
        for j, supported in enumerate((r["official_rank_flip"], r["same_lap_timing_flip"])):
            axes[1].scatter(j, i, color="#176d81" if supported else "#aa6532", marker="o" if supported else "x")
    axes[1].set(xticks=[0, 1], xticklabels=["Lap chart", "Shared markers"], yticks=range(len(labels)), yticklabels=labels)
    bar(2, metrics["tier_counts"].items())
    bar(3, sorted(metrics["censoring_causes"].items(), key=lambda r: r[1]))
    strata = summaries["outcome_strata"]
    gap_labels = {"40": "≤40 m", "80": "40–80 m", "120": "80–120 m", ">120": ">120 m"}
    bar(4, ((gap_labels.get(key.split("/")[1], key.split("/")[1]), v.get("CENSORED_LOCALIZATION", 0)/sum(v.values()))
            for key, v in strata.items() if key.startswith(("gap/", "race/"))))
    axes[4].set_xlabel("Localization-censored fraction")
    axes[5].hist([r["usable_followup_s"] for r in failures], bins=20, color="#176d81")
    axes[5].set_xlabel("Verified follow-up (s)")
    horizons = metrics["frozen_horizons"]
    seconds = [5, 10, 20, 30]
    axes[6].plot(seconds, [horizons[str(s)]["outcomes"].get("NO_PASS_OBSERVED", 0) for s in seconds],
                 ".-", label="Frozen non-pass count")
    axes[6].plot(seconds, [sum(n for hist in metrics["risk_histogram_s"].values()
                               for t, n in hist.items() if t >= s) for s in seconds],
                 ".-", label="Risk prefix ≥ horizon")
    axes[6].legend(fontsize=8)
    axes[6].set_xlabel("Horizon (s)")
    groups = metrics["groups"]
    bar(7, [("same lap", groups["competitive_cutoffs"]),
            ("different lap", groups["all_cutoffs"]-groups["competitive_cutoffs"])])
    bar(8, zip(labels, (len(r["supporting_observations"]) for r in dossiers), strict=True))
    bar(9, [("positive cutoffs", groups["positive_cutoffs_10s"]), ("distinct events", groups["positive_events_10s"])])
    for row in gaps:
        axes[10].barh(str(row["session"]), row["duration_s"], color="#bfcbd1")
        axes[10].barh(str(row["session"]), row["point_in_time_reconstruction_s"], color="#176d81")
    axes[10].set_xlabel("Gap seconds · blue is reconstructed")
    recovery = metrics["comparison"]["evaluation_b"]
    bar(11, [("alternate tick supported", metrics["localized_censored_alternate_supported"]),
             ("whole outcome recovered", recovery["recovered_cutoffs"])])
    axes[11].set_xlim(left=0, right=max(1, metrics["localized_censored_alternate_supported"]))
    for i, r in enumerate(dossiers):
        axes[12].scatter(r["event_time_bracket_s"], i,
                         color="#aa6532" if r["decision"] != "TIER_B_SUPPORTED" else "#176d81")
    axes[12].set(yticks=range(len(labels)), yticklabels=labels, xlabel="Bracket seconds")
    bounds = metrics["bounds"]
    axes[13].plot([bounds["lower_fraction"], bounds["upper_fraction"]], [0, 0], "o-", color="#176d81")
    axes[13].set(xlim=(-.02, 1.02), yticks=[], xlabel="Extreme encounter fraction bounds")
    comp = metrics["comparison"]["evaluation_a"]
    labels15 = ["PASS_OBSERVED", "AMBIGUOUS_OUTCOME", "NO_PASS_OBSERVED", "CENSORED_LOCALIZATION"]
    axes[14].scatter([comp["frozen_10s"].get(k, 0) for k in labels15], range(4), label="Frozen")
    axes[14].scatter([comp["reassessed_10s"].get(k, 0) for k in labels15], range(4), marker="x", label="Reassessed")
    axes[14].set(xscale="symlog", yticks=range(4), yticklabels=[k.replace("_", " ").lower() for k in labels15])
    axes[14].legend(fontsize=8)
    counts = Counter(r["session"] for r in dossiers if r["role"] == "development"
                     and r["decision"] == "TIER_B_SUPPORTED")
    bar(15, ((f"{key} Tier B", counts[key]) for key in metrics["readiness"]["development_races"]))
    axes[15].set_xlabel("Supported events · Tier A = 0")
    fig.savefig(output / "diagnostics.png", dpi=125)
    plt.close(fig)
