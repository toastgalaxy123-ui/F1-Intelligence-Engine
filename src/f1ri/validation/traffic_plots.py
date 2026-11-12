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


def plot_coverage_diagnostics(result: dict, path: Path) -> None:
    races = result["races"]
    if pilot := result.get("development_pilot"):
        races = [*races, pilot | {"name": f"{pilot['name']} extended"}]
    names = [race["name"] for race in races]
    y = np.arange(len(races))
    fig, axes = plt.subplots(1, 3, figsize=(13.2, 4.8), layout="constrained")
    fig.suptitle("Stage 5.3-C  |  Field coverage and following-target yield", fontsize=15)
    for axis in axes:
        axis.set_yticks(y, names)
        axis.invert_yaxis()
        axis.set_axisbelow(True)
        axis.grid(axis="x", alpha=.22)
    for mode, label, offset, color in (
        ("POINT_IN_TIME", "Point in time", -.22, "#27734c"),
        ("POINT_IN_TIME_NO_SPEED", "Without speed fallback", 0., "#a97b35"),
        ("RETROSPECTIVE_MATCHED_MASK", "Retrospective, same mask", .22, "#416984"),
    ):
        rows = [race["coverage"]["modes"][mode] for race in races]
        rates = [100 * row["complete"] / row["snapshots"] for row in rows]
        axes[0].barh(y + offset, rates, height=.20, color=color, label=label)
    axes[0].axvline(100 * result["coverage_target"], color="#374355", linestyle="--", lw=1)
    axes[0].set(xlim=(0, 100), title="Complete field", xlabel="Share of snapshots (%)")
    axes[0].legend(frameon=False, fontsize=8, loc="upper center", bbox_to_anchor=(.5, -.20))
    current = [race["coverage"]["modes"]["POINT_IN_TIME"] for race in races]
    left = np.zeros(len(races))
    for key, label, color in (("one_missing", "One missing", "#b85d61"),
                              ("multiple_missing", "Two or more missing", "#6f8096")):
        rates = [100 * row[key] / row["snapshots"] for row in current]
        axes[1].barh(y, rates, left=left, height=.52, color=color, label=label)
        left += rates
    axes[1].set(xlim=(0, 100), title="Point-in-time blockers", xlabel="Share of snapshots (%)")
    axes[1].legend(frameon=False, fontsize=8, loc="upper center", bbox_to_anchor=(.5, -.20))
    for key, label, offset, color in (
        ("following", "Physical screen", -.22, "#b85d61"),
        ("supported_following", "Supported D8", 0., "#27734c"),
        ("following_with_proxy", "Earlier-control proxy", .22, "#416984"),
    ):
        values = [race["targets"][key]["unique_laps"] for race in races]
        axes[2].barh(y + offset, values, height=.2, color=color, label=label)
        for row, value in enumerate(values):
            axes[2].annotate(str(value), (value, row + offset), xytext=(3, 0),
                             textcoords="offset points", va="center", fontsize=8, color=color)
    axes[2].set_title("Following laps by gate")
    axes[2].set_xlabel("Unique laps")
    axes[2].legend(frameon=False, fontsize=8, loc="upper center", bbox_to_anchor=(.5, -.20))
    fig.savefig(path, dpi=150)
    plt.close(fig)
