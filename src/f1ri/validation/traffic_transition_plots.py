"""Diagnostic panels for the preregistered transition study."""
from __future__ import annotations

import matplotlib
import numpy as np

matplotlib.use("Agg")
from matplotlib import pyplot as plt


def plot_transition(census, result, control_counts, path):
    fig, axes = plt.subplots(2, 4, figsize=(17, 8), layout="constrained")
    axes = axes.ravel()
    races = result["races"]
    colors = ("#35617b", "#b66c43")
    for color, race in zip(colors, races, strict=True):
        key = race["session_key"]
        name = race["name"]
        counts = census["races"][str(key)]
        values = [counts["candidates"], counts["history"].get("QUALIFIED", 0),
                  counts["d8"].get("SUPPORTED", 0), counts["qualified_approach"], counts["with_five_controls"]]
        axes[0].plot(range(5), values, "o-", color=color, label=name)
        history = counts["qualified_by_phase_stint"]
        for phase, marker in (("early", "o"), ("middle", "s")):
            xs = sorted(int(label.split("/")[1]) for label in history if label.startswith(phase + "/"))
            axes[1].plot(xs, [history[f"{phase}/{x}"] for x in xs], marker=marker, color=color,
                         label=f"{name} {phase}")
        targets = race["targets"]
        metrics = [targets[k]["unique_laps"] for k in ("following", "supported_following", "following_with_proxy")]
        axes[3].plot(range(3), metrics, "o-", color=color, label=name)
        group = race["following_independence"]
        axes[5].plot(range(3), [group[k] for k in ("unique_laps", "fragments", "independent_encounters")],
                     "o-", color=color, label=name)
    axes[0].set_xticks(range(5), ["Cutoffs", "History", "D8", "Approach", "5 controls"], rotation=25)
    axes[0].set_title("Cutoff census")
    axes[1].set(xlabel="Stint number", ylabel="Qualified cutoffs", title="Clean history by phase")
    axes[3].set_xticks(range(3), ["Physical", "D8", "Proxy"])
    axes[3].set_title("Following lap yield")
    axes[5].set_xticks(range(3), ["Laps", "Fragments", "Groups"])
    axes[5].set_title("Encounter concentration")
    anchors = result["anchor_conversion"]
    arms = sorted({row["arm"] for row in anchors})
    for i, stage in enumerate(("selected", "cutoff_encounter", "continued_following", "final_target")):
        ys = [sum(row["arm"] == arm and row["stages"][stage] for row in anchors) for arm in arms]
        axes[2].bar(np.arange(len(arms)) + (i - 1.5) * .2, ys, width=.2, label=stage.replace("_", " "))
    axes[2].set_xticks(range(len(arms)), [arm.replace("_", " ") for arm in arms], rotation=20)
    axes[2].set_title("Anchor conversion")
    for race, color in zip(races, colors, strict=True):
        points = control_counts[race["session_key"]]
        axes[4].scatter([lap for lap, _ in points], [count for _, count in points],
                        s=14, alpha=.35, color=color, label=race["name"])
    axes[4].axhline(5, ls="--", color="#687782")
    axes[4].set(xlabel="Candidate cutoff lap", ylabel="Earlier controls", title="Pre-acquisition control pool")
    for i, race in enumerate(races):
        key = race["session_key"]
        status = "Eligible" if key in result["readiness"]["eligible_development_races"] else "Gated"
        axes[6].text(.04, .74 - .29 * i, f"{race['name']} · {status}", color=colors[i], fontsize=13)
    axes[6].set_title("Development readiness")
    axes[6].set_axis_off()
    axes[7].set_axis_off()
    for axis in axes[:6]:
        axis.grid(axis="y", alpha=.2)
        axis.legend(frameon=False, fontsize=8)
    fig.savefig(path, dpi=145)
    plt.close(fig)
