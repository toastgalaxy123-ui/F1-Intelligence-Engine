"""Figures for the Singapore proximity pilot and deterministic following."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
from matplotlib import pyplot as plt

COLORS = {1: "#597286", 4: "#ba6723", 18: "#9b5b9c", 31: "#27734c", 43: "#ba6723", 63: "#274e78"}


def plot_proximity(geometry, cfg, positions, neighborhoods, following, report, output: Path) -> None:
    position_rows = pd.DataFrame(positions)
    neighbor_rows = pd.DataFrame(neighborhoods)
    focus = cfg["geometry"]["driver"]
    windows = cfg["encounters"]
    fig, axes = plt.subplots(len(windows), 2, figsize=(11, 3.85 * len(windows)),
                             layout="constrained", squeeze=False,
                             gridspec_kw={"width_ratios": [1, 1.65]})
    fig.suptitle("Singapore 2025  |  Physical proximity near the control line", fontsize=16)
    coords = np.array(geometry.points)
    distance = geometry.cumulative_m
    for row, window in enumerate(windows):
        name = window["name"]
        start = pd.Timestamp(window["start"])
        position = position_rows[(position_rows.window == name)
                                 & (position_rows["mode"] == "RETROSPECTIVE")]
        neighbor = neighbor_rows[(neighbor_rows.window == name)
                                 & (neighbor_rows.driver_number == focus)]
        retrospective = neighbor[(neighbor["mode"] == "RETROSPECTIVE") & neighbor.gap_m.notna()]
        ax = axes[row, 0]
        ax.plot(*coords.T, color="#d5dbe3", lw=2)
        ax.scatter(*coords[0], marker="s", s=30, color="#202e3b", label="Control line", zorder=4)
        relative = (position.as_of_utc - start).dt.total_seconds()
        middle = position.loc[relative.between(11, 18)]
        for driver in window["drivers"]:
            arc = middle.loc[middle.driver_number == driver, "progress_m"].dropna()
            mapped = [np.interp(arc, distance, coords[:, axis]) for axis in (0, 1)]
            color = COLORS.get(driver, "#586a77")
            ax.scatter(*mapped, edgecolors=color, facecolors="none" if driver == focus else color,
                       s=23 if driver == focus else 13, linewidth=.85, label=f"Car {driver}",
                       zorder=6 if driver == focus else 5)
        ax.set_title(window.get("label", name), loc="left")
        ax.legend(loc="lower left", frameon=False, fontsize=8)
        ax.set_aspect("equal")
        ax.set_axis_off()

        ax = axes[row, 1]
        for driver, group in retrospective.groupby("ahead"):
            color = COLORS.get(driver, "#586a77")
            for coverage, samples in group.groupby("coverage"):
                complete = coverage == "COMPLETE"
                elapsed = (samples.as_of_utc - start).dt.total_seconds()
                label = f"Car {int(driver)}" + ("" if complete else " (subset)")
                ax.scatter(elapsed, samples.gap_m, edgecolors=color,
                           facecolors=color if complete else "none", s=15, linewidth=.8, label=label)
                ax.vlines(elapsed, samples.gap_lower_m, samples.gap_upper_m,
                          color=color, alpha=.28, linewidth=1)
        causal = neighbor[(neighbor["mode"] == "POINT_IN_TIME") & neighbor.gap_m.notna()]
        ax.scatter((causal.as_of_utc - start).dt.total_seconds(), causal.gap_m, s=12,
                   marker="x", linewidth=.65, color="#222831", label="Point-in-time estimate")
        ax.set_ylabel("Russell’s forward gap (m)")
        ax.set_xlabel(f"Seconds after {start.strftime('%H:%M:%S')} UTC")
        ax.set_ylim(bottom=0)
        ax.grid(axis="y", alpha=.25)
        ax.legend(loc="upper center", bbox_to_anchor=(.5, -.15), ncol=3, frameon=False, fontsize=8)
    fig.savefig(output / "stage5_s3_singapore.png", dpi=155)
    plt.close(fig)

    data = pd.DataFrame(asdict(row) for row in following.traversals)
    fig, axes = plt.subplots(1, 2, figsize=(10, 4), layout="constrained")
    fig.suptitle("Synthetic circular following  |  No passing allowed", fontsize=15)
    for driver, group in data.groupby("driver_number"):
        elapsed = group.crossing_us.to_numpy() / 1e6
        clean = group.clean_us.to_numpy().cumsum() / 1e6
        traversal = np.arange(1, len(group) + 1)
        axes[0].plot(traversal, elapsed, "o-", color=COLORS[driver], label=f"Car {driver}")
        axes[0].plot(traversal, clean, "--", color=COLORS[driver], alpha=.5)
        axes[1].plot(traversal, group.traffic_delay_us.cumsum() / 1e6,
                     "o-", color=COLORS[driver], label=f"Car {driver}")
    axes[0].set_title("Effective (solid) and clean (dashed)")
    axes[0].set_ylabel("Crossing time (s)")
    axes[1].set_title("Cumulative committed delay")
    axes[1].set_ylabel("Delay (s)")
    for ax in axes:
        ax.set_xlabel("Traversal from initial snapshot")
        ax.grid(axis="y", alpha=.25)
        ax.legend(frameon=False)
    fig.savefig(output / "stage5_s3_following.png", dpi=155)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(10, 4), layout="constrained")
    fig.suptitle("Singapore 2025  |  Localization checks", fontsize=15)
    checks = report["independent_lap"]["checks"]
    labels = ("Start", "Sector 1", "Sector 2", "Finish")
    deltas = [row["delta_s"] if row["delta_s"] is not None else np.nan for row in checks]
    axes[0].barh(labels, deltas, color="#274e78", height=.55)
    axes[0].invert_yaxis()
    heldout = report["independent_lap"]
    axes[0].set_title(f"Car {heldout['driver']} lap {heldout['lap']} timing residuals")
    axes[0].set_xlabel("Projected passage minus archived timing (s)")
    axes[0].axvline(0, lw=1, color="#222831")
    axes[0].grid(axis="x", alpha=.25)
    y = np.arange(len(report["windows"]))
    for mode, offset, color in (("retrospective", -.15, "#27734c"),
                                ("point_in_time", .15, "#ba6723")):
        ratios = [row["snapshot_counts"][f"{mode}_complete"]
                  / row["snapshot_counts"][f"{mode}_snapshots"] * 100 for row in report["windows"]]
        label = "Retrospective" if mode == "retrospective" else "Point in time"
        axes[1].barh(y + offset, ratios, height=.26, color=color, label=label)
    window_labels = [f"Lap {row['stage1_comparison'][0]['leader_lap']}"
                     if row["stage1_comparison"] else window.get("label", row["name"])
                     for row, window in zip(report["windows"], windows, strict=True)]
    axes[1].set_yticks(y, window_labels)
    axes[1].invert_yaxis()
    axes[1].set_xlim(0, 100)
    axes[1].set_title("Complete-field coverage")
    axes[1].set_xlabel("Share of snapshots (%)")
    axes[1].legend(loc="upper center", bbox_to_anchor=(.5, -.18), ncol=2, frameon=False)
    axes[1].grid(axis="x", alpha=.25)
    fig.savefig(output / "stage5_s3_localization.png", dpi=155)
    plt.close(fig)
