"""Scientific plots for synthetic pit mechanics and conditional post-stop pace."""
from __future__ import annotations

from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
from matplotlib import pyplot as plt

from f1ri.simulator.events import EventKind

_NAVY = "#17324D"
_TEAL = "#087E83"
_ORANGE = "#C1632B"
_PURPLE = "#7358A4"


def _style(fig, axes, title: str) -> None:
    fig.patch.set_facecolor("#F5F7FA")
    fig.suptitle(title, fontsize=17, color=_NAVY, fontweight="bold", x=0.04, ha="left")
    for ax in axes.flat:
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", alpha=0.18)
        ax.set_axisbelow(True)


def plot_pit_integration(synthetic, field, variants, pairs, scores, historical, output: Path) -> None:
    plots = output / "plots"
    plots.mkdir(exist_ok=True)
    resolved = synthetic["resolved"]
    stop, = resolved.stops
    fig, axes = plt.subplots(1, 3, figsize=(15, 5), layout="constrained")
    _style(fig, axes, "Stage 5.2 | Pit-route mechanics")
    ax = axes[0]
    for driver, name, color in ((63, "Pits", _TEAL), (4, "Stays out", _NAVY)):
        crossings = [frame.event for frame in resolved.run.trace if frame.event.driver_number == driver
                     and frame.event.kind in (EventKind.CHECKPOINT_CROSSING, EventKind.LAP_COMPLETED)]
        ax.plot([event.at_us / 1e6 for event in crossings], [event.checkpoint / 3 for event in crossings],
                "o-", ms=3.5, color=color, label=name)
    ax.axvspan(stop.route_entry_us / 1e6, stop.merge_us / 1e6, color=_TEAL, alpha=0.12,
               label="Pit route")
    ax.axvline(stop.merge_us / 1e6, lw=1, ls="--", color=_ORANGE, label="Common merge")
    ax.set(title="Two-car time and progress", xlabel="Race clock (s)", ylabel="Canonical lap progress",
           xlim=(105, 320))
    ax.legend(frameon=False, fontsize=8)
    ax = axes[1]
    for i, visit in enumerate(synthetic["double_stack"].stops):
        start, finish = visit.service_start_us / 1e6, visit.service_end_us / 1e6
        wait = visit.queue_wait_us / 1e6
        ax.barh(i, finish - start, left=start, color=_TEAL)
        if wait:
            ax.barh(i, wait, left=start - wait, color=_ORANGE, alpha=0.7)
    ax.set(yticks=(0, 1), yticklabels=("First car", "Second car"), xlim=(180, 190),
           xlabel="Race clock (s)", title="Shared box · service and wait")
    ax = axes[2]
    cases = [synthetic["resolved"].stops[0], synthetic["coarse"].stops[0],
             synthetic["double_stack"].stops[1]]
    base = [case.normal_two_lap_us / 1e6 for case in cases]
    ax.bar(["Resolved", "Coarse", "Queued"], base, color=_NAVY, label="Normal reference")
    ax.bar(["Resolved", "Coarse", "Queued"], [case.net_loss_us / 1e6 for case in cases],
           bottom=base, color=_ORANGE, label="Net loss")
    ax.set(title="Two-lap accounting", ylabel="Time (s)")
    ax.legend(frameon=False, fontsize=8)
    fig.savefig(plots / "stage5_s2_pit_mechanics.png", dpi=145)
    plt.close(fig)

    fig, axes = plt.subplots(2, 3, figsize=(15, 8), layout="constrained")
    _style(fig, axes, "Stage 5.2 | Replacement-tire pace and historical evidence")
    ax = axes[0, 0]
    driver = next(iter(variants["fresh"].replacement_batches))
    old = field.batches[driver].forecast.lap_forecasts
    ax.plot([lap.target_lap_number for lap in old], [lap.expected_lap_time_s for lap in old],
            "o-", ms=3, color=_NAVY, label="Continue old stint")
    for label, color in (("fresh", _TEAL), ("used", _ORANGE)):
        new = variants[label].replacement_batches[driver].forecast.lap_forecasts
        ax.plot([lap.target_lap_number for lap in new], [lap.expected_lap_time_s for lap in new],
                "o-", ms=3, color=color, label=f"{label.title()} replacement")
    ax.set(title="Frozen D8 normal-lap references", xlabel="Race lap", ylabel="Lap time (s)")
    ax.legend(frameon=False, fontsize=8)
    ax = axes[0, 1]
    for label, color in (("fresh", _TEAL), ("used", _ORANGE)):
        values = [row["delta_s"] for row in pairs
                  if row["scenario"] == label and row["sample_index"] >= 0]
        ax.hist(values, bins=18, color=color, alpha=0.45, label=label.title())
    ax.set(title="Paired stop versus continue", xlabel="Stop minus continue at equal distance (s)",
           ylabel="Realizations")
    ax.legend(frameon=False, fontsize=8)

    ax = axes[0, 2]
    for label, color in (("fresh", _TEAL), ("used", _ORANGE)):
        values = np.array([row["delta_s"] for row in pairs
                           if row["scenario"] == label and row["sample_index"] >= 0])
        count = np.arange(1, len(values) + 1)
        ax.plot(count, values.cumsum() / count, color=color, label=label.title())
    ax.set(title="Convergence of paired mean", xlabel="Realizations", ylabel="Mean delta (s)")
    ax.legend(frameon=False, fontsize=8)

    ax = axes[1, 0]
    if scores:
        rows = pd.DataFrame(scores)
        rows = rows[rows.target.str.startswith("ELIGIBLE")]
        for seen, group in rows.groupby("circuit_seen"):
            ax.scatter(group.tyre_age, group.actual_s - group.predicted_s, s=18, alpha=0.55,
                       color=_TEAL if seen else _ORANGE, label="Familiar" if seen else "Unseen")
        ax.axhline(0, color="#94A3B8", lw=1)
        if not rows.empty:
            ax.legend(frameon=False, fontsize=8)
    ax.set(title="Historical flying-lap residual", xlabel="Tire age (laps)",
           ylabel="Observed minus predicted (s)")
    ax = axes[1, 1]
    if scores:
        rows = pd.DataFrame(scores)
        rows = rows[rows.target.str.startswith("COMPLETE")].copy()
        rows["horizon"] = rows.target.str.rsplit("_", n=1).str[-1].astype(int)
        rows["absolute_error"] = (rows.actual_s - rows.predicted_s).abs()
        for (circuit, group), color in zip(rows.groupby("circuit"), (_ORANGE, _TEAL, _NAVY)):
            summary = group.groupby("horizon").absolute_error.agg(["mean", "size"])
            ax.plot(summary.index, summary["mean"], "o-", label=circuit, color=color)
            for horizon, row in summary.iterrows():
                ax.annotate(f"n={int(row['size'])}", (horizon, row["mean"]), xytext=(5, 5),
                            textcoords="offset points", fontsize=7, color=color)
        if not rows.empty:
            ax.legend(frameon=False, fontsize=8)
    ax.set(title="Cumulative post-stop error", xlabel="Consecutive clean laps", ylabel="MAE (s)",
           xticks=(5, 10))
    ax = axes[1, 2]
    if historical:
        frame = pd.DataFrame(historical).dropna(subset=["lane_duration_s"])
        grouped = list(frame.groupby("circuit_short_name"))
        if grouped:
            ax.boxplot([group.lane_duration_s for _, group in grouped],
                showfliers=False, patch_artist=True,
                boxprops={"facecolor": _TEAL, "alpha": 0.38},
                medianprops={"color": _PURPLE})
            ax.set_xticks(range(1, len(grouped) + 1), [name for name, _ in grouped])
    ax.set(title="Observed provider lane windows", ylabel="Lane duration (s)")
    fig.savefig(plots / "stage5_s2_pace_and_replay.png", dpi=145)
    plt.close(fig)
