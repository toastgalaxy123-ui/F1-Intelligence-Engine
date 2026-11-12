"""Stage 5.3-C localization and lap support figure."""
from __future__ import annotations

from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
from matplotlib import pyplot as plt


def plot_traffic_research(races: list[dict], path: Path, coverage_target: float) -> None:
    names = [race["name"] for race in races]
    y = np.arange(len(races))
    fig, axes = plt.subplots(1, 3, figsize=(13, 4), layout="constrained")
    fig.suptitle("Stage 5.3-C  |  Cross-circuit traffic evidence", fontsize=15)
    for axis in axes:
        axis.set_yticks(y, names)
        axis.invert_yaxis()
        axis.grid(axis="x", alpha=.25)
        axis.set_axisbelow(True)
    errors = np.array([row["timing_markers"]["mae"] for row in races], dtype=float)
    axes[0].barh(y, errors, color="#315f81", height=.5)
    axes[0].set_title("Independent timing markers")
    axes[0].set_xlabel("Mean absolute error (s)")
    for mode, label, offset, color in (("point_in_time", "Point in time", -.14, "#27734c"),
                                       ("retrospective", "Retrospective", .14, "#ba6723")):
        coverage = [row[f"{mode}_complete"] / row[f"{mode}_snapshots"] * 100 for row in races]
        axes[1].barh(y + offset, coverage, color=color, height=.26, label=label)
    axes[1].axvline(coverage_target * 100, color="#455264", linestyle="--", linewidth=1)
    axes[1].set(xlim=(0, 100), title="Complete-field localization", xlabel="Share of snapshots (%)")
    axes[1].legend(frameon=False, fontsize=8, ncol=2, loc="upper center", bbox_to_anchor=(.5, -.2))
    offset = np.zeros(len(races), dtype=int)
    for group, label, color in (("FOLLOWING", "Following", "#9a555e"),
                                ("CLEAN_CONTROL", "Clean control", "#27734c"),
                                ("INTERMEDIATE", "Other supported", "#8ca0b0")):
        values = np.array([row["lap_opportunities"].get(group, 0) for row in races])
        axes[2].barh(y, values, left=offset, color=color, height=.5, label=label)
        offset += values
    axes[2].set_title("Laps after physical screening")
    axes[2].set_xlabel("Lap opportunities")
    axes[2].legend(frameon=False, fontsize=8, ncol=3, loc="upper center", bbox_to_anchor=(.5, -.2))
    fig.savefig(path, dpi=155)
    plt.close(fig)
