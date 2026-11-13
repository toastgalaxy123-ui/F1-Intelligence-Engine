"""Plot S4-B label evidence and opportunity support from recorded run outputs."""
import matplotlib

matplotlib.use("Agg")
from collections import Counter

from matplotlib import pyplot as plt
from matplotlib.ticker import EngFormatter, MaxNLocator


def plot_history(path, metrics, events, supported, comparisons, reference):
    fig, panels = plt.subplots(4, 4, figsize=(22, 15), layout="constrained")
    axes = panels.flat
    names = (
        "Supported order reversals", "Observed event brackets", "Gap trajectories and uncertainty", "Event taxonomy",
        "Evidence tier by circuit", "Prospective opportunity funnel", "10 s by starting gap", "10 s by earlier clean pace",
        "Outcomes by horizon", "10 s censoring by cause", "Cutoffs and encounter groups", "Same-lap and lapped passes",
        "Physical and competitive order", "Supported bracket widths", "Frozen S4-A comparisons", "S4-C readiness",
    )
    for i, (ax, name) in enumerate(zip(axes, names, strict=True)):
        ax.set_title(f"{chr(65+i)}  {name}", loc="left", fontweight="bold")
        ax.grid(axis="y", alpha=.2)

    for row in supported:
        axes[0].plot([0, 1], [row["before"]["gap_m"], row["after"]["gap_m"]], ".-", alpha=.25, color="#246b84")
        trajectory = row["gap_trajectory"]
        lower = row["event_time_lower_utc"]
        x = [(r["at_utc"] - lower).total_seconds() for r in trajectory]
        y = [r["gap_m"] for r in trajectory]
        err = [r["uncertainty_m"] for r in trajectory]
        axes[2].plot(x, y, alpha=.22, color="#246b84")
        axes[2].fill_between(x, [g-e for g, e in zip(y, err, strict=True)],
                             [g+e for g, e in zip(y, err, strict=True)], color="#246b84", alpha=.03)
    axes[0].set(xticks=[0, 1], xticklabels=["Behind", "Ahead"], ylabel="Signed gap (m)")
    axes[2].set(xlabel="Seconds from lower bound", ylabel="Signed gap (m)")
    for i, row in enumerate(sorted(supported, key=lambda r: (r["session"], r["event_time_lower_utc"]))):
        axes[1].plot([0, row["event_time_bracket_s"]], [i, i], color="#246b84")
    axes[1].set(xlabel="Seconds from lower bound", ylabel="Supported event index")

    def horizontal(axis, counts):
        labels = [label.replace("_", " ").lower() for label in counts]
        axis.barh(labels, list(counts.values()), color="#246b84")
        axis.tick_params(axis="y", labelsize=8)
        axis.xaxis.set_major_locator(MaxNLocator(5))

    horizontal(axes[3], metrics["event_types"])
    bottoms = Counter()
    for tier in ("A", "B", "C", "EXCLUDED"):
        counts = {k: v.get(tier, 0) for k, v in metrics["tier_by_circuit"].items()}
        axes[4].bar(list(counts), list(counts.values()), bottom=[bottoms[k] for k in counts], label=tier)
        bottoms.update(counts)
    axes[4].legend(fontsize=8)
    axes[4].tick_params(axis="x", labelrotation=20)
    funnel = metrics["opportunity_funnel"]
    ten = metrics["outcomes_by_horizon"]["10"]["outcomes"]
    horizontal(axes[5], {k: funnel.get(k, 0) for k in ("driver_snapshots", "physically_supported", "unique_cutoffs")} | {
        "resolved_10s": sum(ten.get(k, 0) for k in ("PASS_OBSERVED", "NO_PASS_OBSERVED")),
        "unresolved_10s": sum(v for k, v in ten.items() if k not in ("PASS_OBSERVED", "NO_PASS_OBSERVED")),
    })
    axes[5].xaxis.set_major_formatter(EngFormatter())
    for ax, rates in ((axes[6], metrics["gap_rates_10s"]), (axes[7], metrics["pace_rates_10s"])):
        horizontal(ax, {f"{k} ({r['resolved_encounters']})": 100*r["pass_fraction"]
                        for k, r in rates.items() if r["pass_fraction"] is not None})
        ax.set_xlabel("Pass encounters (%) · denominator in parentheses")
    horizons = metrics["outcomes_by_horizon"]
    axes[8].plot(list(horizons), [r["outcomes"].get("NO_PASS_OBSERVED", 0) for r in horizons.values()],
                 ".-", label="No pass")
    axes[8].plot(list(horizons), [r["outcomes"].get("PASS_OBSERVED", 0) for r in horizons.values()],
                 ".-", label="Pass")
    axes[8].plot(list(horizons), [sum(v for k, v in r["outcomes"].items() if k.startswith("CENSORED"))
                                for r in horizons.values()], ".-", label="Censored")
    axes[8].legend(fontsize=8)
    horizontal(axes[9], {k.removeprefix("CENSORED_"): v for k, v in ten.items() if k.startswith("CENSORED")})
    horizontal(axes[10], {"unique_cutoffs": funnel["unique_cutoffs"], "encounters": funnel["encounters"]})
    axes[10].xaxis.set_major_formatter(EngFormatter())
    horizontal(axes[11], Counter(r["event_type"].removesuffix("_PASS") for r in supported))
    horizontal(axes[12], metrics["independent_position_agreement"])
    axes[13].hist([r["event_time_bracket_s"] for r in supported], bins=10, color="#246b84")
    axes[13].set_xlabel("Bracket width (s)")
    names = []
    for i, r in enumerate(reference):
        names.append(f"{r['session_key']} {r['attacker']}→{r['target']}")
        replay = r["replay"]
        width, commit = replay.get("archived_reversal_bracket_s"), replay.get("simulated_boundary_commit_s")
        if width is not None:
            axes[14].plot([0, width], [i, i], "o-", color="#246b84")
        if commit is not None:
            axes[14].scatter(commit, i, marker="D", color="#b8613a")
    axes[14].set(yticks=range(len(names)), yticklabels=names, xlabel="Seconds from cutoff · diamond is simulated")
    support = metrics["readiness"]["support"]
    axes[15].set_axis_off()
    axes[15].text(.01, .90, f"{metrics['readiness']['decision'].replace('_', ' ')}\n\n"
                   f"Development pass events  {support['development_supported_passes']}\n"
                   f"Tier A same-lap events  {support['development_same_lap_tier_a']}\n"
                   f"Encounters across all cohorts  {support['encounters']}\n"
                   f"Layouts  {support['layouts']}\n"
                   f"Scripted directions reproduced  {sum(r['direction_matches'] for r in comparisons)} / {len(supported)}",
                   va="top", transform=axes[15].transAxes)
    fig.savefig(path, dpi=130)
    plt.close(fig)
