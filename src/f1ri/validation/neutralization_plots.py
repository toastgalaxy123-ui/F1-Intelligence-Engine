"""Measured synthetic and historical S5-A diagnostics."""
from collections import Counter
from itertools import pairwise

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt


def plot_study(output, runs, metrics, invariant):
    fig, grid = plt.subplots(4, 4, figsize=(22, 15), layout="constrained")
    axes = list(grid.flat)
    titles = (
        "Race-control timeline", "VSC traversal time", "Mid-traversal VSC deployment", "VSC physical gaps",
        "SC field compression", "SC train and virtual boundary", "SC predecessor chain", "SC withdrawal and restart",
        "Passing restrictions", "Elapsed pit-route time", "Strategic pit loss", "RED clock accounting",
        "RED suspension and restart", "Finish and classification", "Imola archived control clocks", "Invariants and parity",
    )
    for i, (ax, label) in enumerate(zip(axes, titles, strict=True)):
        ax.set_title(f"{chr(65+i)}  {label}", loc="left", fontweight="bold")
        ax.grid(alpha=.18)
    vsc, sc, red = (runs[n].progress for n in ("vsc", "sc_train", "red_restart"))

    def values(rows, key):
        return [r[key]/1e6 for r in rows]

    for y, rows in enumerate((vsc, sc, red)):
        for a, b in pairwise(rows):
            color = {"GREEN": "#53885b", "SC": "#a97129", "VSC": "#246c85", "RED": "#a83d41"}[a["track_state"]]
            axes[0].plot([a["at_us"]/1e6, b["at_us"]/1e6], [y, y], lw=5, color=color)
    axes[0].set(yticks=range(3), yticklabels=["VSC script", "SC script", "RED script"], xlabel="Race clock (s)")
    trav = [t for t in runs["vsc"].execution.traversals if t.driver_number == 1]
    axes[1].plot([t.checkpoint for t in trav], [t.clean_us/1e6 for t in trav], ".-", label="Latent clean")
    axes[1].plot([t.checkpoint for t in trav], [t.effective_us/1e6 for t in trav], ".-", label="Committed elapsed")
    axes[1].legend(fontsize=8)
    axes[1].set(xlabel="Checkpoint", ylabel="Traversal (s)")

    def series(ax, rows, key, drivers):
        for d in drivers:
            own = [r for r in rows if d in r[key]]
            ax.plot(values(own, "at_us"), [r[key][d] for r in own], ".-", label=f"Car {d}")

    series(axes[2], vsc, "positions_m", (1, 2))
    axes[2].axvline(5, ls="--", color="#444")
    axes[2].set(xlabel="Race clock (s)", ylabel="Unwrapped progress (m)")
    series(axes[3], vsc, "gaps_m", (2,))
    series(axes[4], sc, "gaps_m", (2, 3))
    for ax in axes[3:5]:
        ax.set(xlabel="Race clock (s)", ylabel="Forward gap (m)")
        ax.legend(fontsize=8)
    active = [r for r in sc if r["virtual_sc_m"] is not None]
    series(axes[5], active, "positions_m", (1, 2, 3))
    axes[5].plot(values(active, "at_us"), [r["virtual_sc_m"] for r in active], "--", color="#555", label="Virtual boundary")
    axes[5].legend(fontsize=8)
    axes[5].set(xlabel="Race clock (s)", ylabel="Unwrapped progress (m)")
    train = next(r for r in sc if r["kind"] == "TRAIN_FORMED")
    axes[6].axis("off")
    for i, d in enumerate(sorted(train["neighbors"])):
        axes[6].text(.03, .8-i*.18, f"Car {d} → {train['neighbors'][d]}   {train['gaps_m'][d]:.1f} m",
                     transform=axes[6].transAxes)
    series(axes[7], [r for r in sc if r["at_us"] >= 44e6], "positions_m", (1, 2, 3))
    for r in runs["sc_train"].controls:
        if r["event"]["command"].name in ("SC_WITHDRAWN", "GREEN") and r["outcome"] == "COMMITTED":
            axes[7].axvline(r["clock_us"]/1e6, ls="--", color="#444")
    axes[7].set(xlabel="Race clock (s)", ylabel="Unwrapped progress (m)")
    attempts = Counter(r.status for name, run in runs.items() if name in ("cancel_pass", "completed_pass")
                       for r in run.execution.results)
    axes[8].bar(list(attempts), list(attempts.values()), color="#246c85")
    pit = [r for r in metrics["pit_comparisons"] if r["scenario"] != "shared_box" and r["status"] == "RESOLVED"]
    for key, label in (("lane_motion_us", "Lane"), ("service_us", "Service"), ("route_transition_us", "Transition")):
        previous = ["lane_motion_us", "service_us", "route_transition_us"]
        axes[9].bar([r["scenario"] for r in pit], [r[key]/1e6 for r in pit],
                    bottom=[sum(r[k] for k in previous[:previous.index(key)])/1e6 for r in pit], label=label)
    axes[9].legend(fontsize=8)
    axes[9].set_ylabel("Seconds")
    axes[10].bar([r["scenario"] for r in pit], [r["strategic_loss_us"]/1e6 for r in pit], color="#246c85")
    axes[10].set_ylabel("Seconds versus stay out")
    for key, label in (("wall_elapsed_us", "Wall"), ("active_elapsed_us", "Active"), ("suspended_us", "Suspended")):
        axes[11].plot(values(red, "at_us"), values(red, key), ".-", label=label)
    axes[11].legend(fontsize=8)
    axes[11].set(xlabel="Race clock (s)", ylabel="Seconds")
    series(axes[12], red, "positions_m", (1, 2))
    axes[12].axvspan(5, 35, color="#a83d41", alpha=.13)
    axes[12].set(xlabel="Race clock (s)", ylabel="Unwrapped progress (m)")
    for i, label in enumerate(("finish_sc", "finish_restart", "finish_red", "finish_vsc")):
        final = runs[label].execution.run.final_state
        axes[13].text(.03, .83-i*.18, f"{label.removeprefix('finish_')}  {final.timing_order}  "
                      f"laps {[c.completed_laps for c in final.cars]}", transform=axes[13].transAxes)
    axes[13].axis("off")
    for i, (name, block) in enumerate((k, v) for k, v in metrics["historical"].items()
                                      if "_sc_" in k or "_vsc_" in k):
        for r in block["command_timing"]:
            axes[14].plot([r["requested_us"]/1e6, r["effective_us"]/1e6], [i, i], "o-", color="#246c85")
    axes[14].set(yticks=[0, 1], yticklabels=["VSC", "SC"], xlabel="Window clock (s)")
    categories = list(next(iter(invariant.values())))
    axes[15].barh([k.replace("_", " ") for k in categories],
                  [sum(r.get(k, False) for r in invariant.values()) for k in categories], color="#53885b")
    axes[15].tick_params(axis="y", labelsize=8)
    axes[15].set_xlabel("Passing scenarios")
    fig.savefig(output / "diagnostics.png", dpi=120)
    plt.close(fig)
