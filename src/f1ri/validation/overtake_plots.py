"""Render S4-A diagnostics from committed synthetic and cached replay records."""
from collections import Counter
from itertools import accumulate

import matplotlib
import numpy as np

matplotlib.use("Agg")
from matplotlib import pyplot as plt


def plot_overtakes(runs, historical, metrics, path):
    fig, grid = plt.subplots(3, 4, figsize=(20, 12), layout="constrained")
    axes = list(grid.flat)
    titles = ("Two-car progress", "Passing checkpoint crossings", "Queue predecessor changes",
              "Clean and effective traversal", "Committed following delay", "Physical and timing order",
              "Pass scheduler trace", "S2 route and rejoin events", "Attempt outcomes",
              "Invariant checks", "Frozen regression parity", "Archived and scripted timing")
    for axis, letter, title in zip(axes, "ABCDEFGHIJKL", titles, strict=True):
        axis.set_title(f"{letter}  {title}", loc="left")
        axis.grid(axis="y", alpha=.2)
    pair = runs["authorized_pair"]
    for driver in (1, 2):
        rows = [r for r in pair.traversals if r.driver_number == driver]
        x = [r.crossing_us/1e6 for r in rows]
        y = [r.checkpoint*300 for r in rows]
        axes[0].plot(x, y, "o-", label=f"Car {driver}")
        axes[1].scatter(x, [r.checkpoint for r in rows], label=f"Car {driver}")
    axes[0].axvline(pair.results[0].committed_us/1e6, color="#444", ls="--")
    axes[0].set(ylabel="Unwrapped checkpoint progress (m)", xlabel="Scenario time (s)")
    axes[1].set(ylabel="Checkpoint", xlabel="Scenario time (s)")
    queue = runs["queue"].results[0]
    after = dict(queue.neighbors_after)
    before = {d: queue.physical_before[(i+1) % len(queue.physical_before)] for i, d in enumerate(queue.physical_before)}
    for d in sorted(after):
        axes[2].plot([0, 1], [before[d], after[d]], "o-", label=f"Car {d}")
    axes[2].set(xticks=[0, 1], xticklabels=["Before", "After"], yticks=[1, 2, 3], ylabel="Car ahead")
    prior = [r for r in runs["prior_delay"].traversals if r.driver_number == 2]
    axes[3].plot([r.checkpoint for r in prior], [r.clean_us/1e6 for r in prior], "o-", label="Clean")
    axes[3].plot([r.checkpoint for r in prior], [r.effective_us/1e6 for r in prior], "s--", label="Committed")
    axes[3].set(xlabel="Checkpoint", ylabel="Traversal seconds")
    for label in ("prior_delay", "no_pass"):
        rows = [r for r in runs[label].traversals if r.driver_number == 2]
        axes[4].step([r.checkpoint for r in rows], list(accumulate(r.traffic_delay_us/1e6 for r in rows)),
                     where="post", label=label.replace("_", " "))
    axes[4].set(xlabel="Checkpoint", ylabel="Cumulative seconds")
    for i, label in enumerate(("authorized_pair", "lapping", "unlapping")):
        r = runs[label].results[0]
        axes[5].text(.02, .86-i*.22, f"{label.replace('_', ' ').title()}\n"
                     f"Pair {r.pair_at_boundary_before or '—'} → {r.pair_at_boundary_after or '—'}"
                     f"  |  timing {r.classification_before} → {r.classification_after}",
                     transform=axes[5].transAxes, fontsize=8)
    pit = runs["dynamic_rejoin"]
    entry = next(f for f in pit.run.trace if f.event.kind.name == "PIT_ROUTE_ENTRY")
    rejoin = next(f for f in pit.run.trace if f.event.kind.name == "TRACK_REJOIN")
    axes[5].text(.02, .20, f"S2 pit cycle\nTiming {entry.state.timing_order} → {rejoin.state.timing_order}",
                 transform=axes[5].transAxes, fontsize=8)
    axes[5].set_axis_off()
    phases = pair.interactions
    axes[6].scatter([r["at_us"]/1e6 for r in phases], range(len(phases)), color="#315f79")
    axes[6].set(yticks=range(len(phases)), yticklabels=[
        "INVALIDATE / RESCHEDULE" if "invalidated_generations" in r else r["phase"] for r in phases
    ], xlabel="Scenario time (s)")
    axes[6].tick_params(axis="y", labelsize=8)
    routes = [f for f in pit.run.trace if f.event.kind.name in (
        "PIT_ROUTE_ENTRY", "PIT_LANE_ENTRY", "PIT_SERVICE_START", "PIT_SERVICE_END", "PIT_LANE_EXIT", "TRACK_REJOIN")]
    axes[7].scatter([f.event.at_us/1e6 for f in routes], range(len(routes)), color="#af613a")
    axes[7].set(yticks=range(len(routes)), yticklabels=[
        "PIT_ROUTE_ENTRY (4 runs alone)" if f.event.kind.name == "PIT_ROUTE_ENTRY" else
        "TRACK_REJOIN (63 follows 4)" if f.event.kind.name == "TRACK_REJOIN" else
        f.event.kind.name for f in routes
    ], xlabel="Race clock (s)")
    axes[7].tick_params(axis="y", labelsize=8)
    outcomes = Counter(r.reason or r.status.value for run in runs.values() for r in run.results)
    axes[8].barh(list(outcomes), list(outcomes.values()), color="#315f79")
    axes[8].tick_params(axis="y", labelsize=7)
    checks = list(next(iter(metrics["invariants"].values())))
    axes[9].barh(checks, [sum(v[c] for v in metrics["invariants"].values()) for c in checks], color="#36735e")
    axes[9].tick_params(axis="y", labelsize=8)
    axes[9].set_xlabel("Scenarios passing check")
    for i, (label, passed) in enumerate(metrics["parity"].items()):
        axes[10].text(.03, .86-i*.14, f"{label.replace('_',' ')}  {'pass' if passed else 'fail'}",
                      transform=axes[10].transAxes, fontsize=9)
    axes[10].set_axis_off()
    for i, row in enumerate(historical):
        replay = row["replay"]
        start = replay.get("archived_reversal_bracket_s")
        commit = replay.get("simulated_boundary_commit_s")
        if start is not None:
            axes[11].plot([0, start], [i, i], "o-", color="#315f79")
        if commit is not None:
            axes[11].scatter(commit, i, marker="D", color="#af613a")
        if start is None or commit is None:
            axes[11].text(.02, i, replay["status"], fontsize=7, va="bottom")
    axes[11].set(yticks=np.arange(len(historical)), yticklabels=[
        f"{r['session_key']}  {r['attacker']}→{r['target']}" for r in historical], xlabel="Seconds from archived cutoff")
    for i in (0, 1, 2, 3, 4):
        axes[i].legend(frameon=False, fontsize=8)
    fig.savefig(path, dpi=130)
    plt.close(fig)
