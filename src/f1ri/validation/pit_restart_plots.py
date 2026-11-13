"""S5-A.1 diagnostics from committed event and support ledgers."""
from collections import Counter

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt


def plot_study(output, metrics, itinerary, restart, comparison, parity):
    folder = output / "diagnostics"
    folder.mkdir()
    fig, grid = plt.subplots(4, 4, figsize=(22, 15), layout="constrained")
    axes = list(grid.flat)
    titles = (
        "Reference and actual entries", "Entry retiming", "Retimed VSC itinerary", "Reversed shared-box arrival",
        "Pit exit and merge", "Pit elapsed and strategic loss", "Imola global and driver release",
        "Pit restart permission timeline", "Drivers awaiting the restart line", "Imola archived and applied clear",
        "Matched Imola line errors", "Imola physical snapshot support", "Entry schedule generations",
        "Scenario invariant checks", "Frozen and no-op parity", "S5-B readiness",
    )
    for i, (ax, title) in enumerate(zip(axes, titles, strict=True)):
        ax.set_title(f"{chr(65+i)}  {title}", loc="left", fontweight="bold")
        ax.grid(alpha=.16)
    pit = metrics["pit_accounting"]
    labels = [r["scenario"].replace("dynamic_", "").replace("p2_", "").replace("p3_", "")
              .replace("p4_", "").replace("p5_", "").replace("p14_", "") for r in pit]
    for i, row in enumerate(pit):
        axes[0].plot([row["reference_entry_us"]/1e6, row["entry_us"]/1e6], [i, i], "o-", color="#276b80")
        axes[4].plot([row["service_end_us"]/1e6, row["merge_us"]/1e6], [i, i], ".-", color="#276b80")
    for ax in (axes[0], axes[4]):
        ax.set(yticks=range(len(labels)), yticklabels=labels, xlabel="Race clock (s)")
    axes[1].barh(labels, [r["entry_shift_us"]/1e6 for r in pit], color="#276b80")
    axes[1].set_xlabel("Entry shift (s)")
    for i, scenario in enumerate(("dynamic_green", "p2_vsc")):
        rows = [r for r in itinerary if r["scenario"] == scenario and r["status"] in (
            "PIT_ENTRY_COMMITTED", "SERVICE_ACTIVE", "SERVICE_COMPLETE", "REJOINED")]
        axes[2].plot([r["at_us"]/1e6 for r in rows], [i]*len(rows), "o-", color="#276b80")
    axes[2].set(yticks=[0, 1], yticklabels=["GREEN", "VSC"], xlabel="Entry · box · service end · merge (s)")
    for i, r in enumerate(r for r in itinerary if r["scenario"] == "p12_box_reverse" and r["status"] == "SERVICE_QUEUE"):
        axes[3].plot([r["at_us"]/1e6, r["service_start_us"]/1e6], [i, i], "o-", color="#b06c32")
        axes[3].plot([r["service_start_us"]/1e6, r["service_end_us"]/1e6], [i, i], "o-", color="#276b80")
        axes[3].text(r["service_end_us"]/1e6, i+.06, f"Car {r['driver']}")
    axes[3].set(yticks=[], xlabel="Queue and occupancy (s)")
    axes[5].plot(labels, [r["elapsed_pit_us"]/1e6 for r in pit], "o-", label="Pit elapsed")
    axes[5].plot(labels, [r["strategic_loss_us"]/1e6 if r["strategic_loss_us"] is not None else None for r in pit],
                 "o-", label="Strategic loss")
    axes[5].legend(fontsize=8)
    axes[5].tick_params(axis="x", rotation=24)
    axes[5].set_ylabel("Seconds")
    clear = metrics["imola_green"]["effective_us"]/1e6
    imola = sorted((r for r in restart if r["scenario"] == "imola_sc_4" and r["status"] == "CROSSED_RESTART_LINE"),
                   key=lambda r: r["at_us"])
    axes[6].scatter([r["at_us"]/1e6-clear for r in imola], range(len(imola)), s=12, color="#276b80")
    axes[6].axvline(0, color="#b06c32")
    axes[6].set(xlabel="Seconds from global GREEN", ylabel="Driver release order")
    for y, r in enumerate(r for r in restart if r["scenario"] == "r8_pit_exit" and r["driver"] == 63):
        axes[7].scatter(r["at_us"]/1e6, y, color="#276b80")
        axes[7].text(r["at_us"]/1e6+2, y, r["status"].replace("_", " ").lower(), fontsize=8)
    axes[7].set(yticks=[], xlabel="Race clock (s)")
    axes[7].margins(x=.35)
    clocks = [r["at_us"]/1e6-clear for r in imola]
    axes[8].step([0., *clocks], [len(clocks), *reversed(range(len(clocks)))], where="post")
    axes[8].set(xlabel="Seconds from global GREEN", ylabel="Drivers still awaiting line")
    axes[9].barh(["S5-A", "S5-A.1"], [comparison["imola_sc_4"]["frozen"]["command_timing"][-1]["difference_us"]/1e6,
                                       metrics["imola_green"]["difference_us"]/1e6], color="#276b80")
    axes[9].set_xlabel("Global release delay (s)")
    before, after = (metrics["imola_sc"][f"matched_median_abs_{k}_s"] for k in ("before", "after"))
    axes[10].bar(["S5-A", "S5-A.1"], [before, after], color="#276b80")
    axes[10].set_ylabel("Median absolute error (s)")
    cover = metrics["snapshot_coverage"]
    keys = ["deployment", "announcement", "global_clear"]
    axes[11].bar(keys, [cover[k]["active"] for k in keys], color="#bac9cf", label="Active")
    axes[11].bar(keys, [cover[k]["supported"] for k in keys], color="#276b80", label="Localized")
    axes[11].tick_params(axis="x", rotation=20)
    axes[11].legend(fontsize=8)
    counts = metrics["pit_prediction_counts"]
    axes[12].barh(["VSC", "SC", "RED"], [counts.get(k, 0) for k in ("p2_vsc", "p4_sc", "p5_red")], color="#276b80")
    axes[12].set_xlabel("Scheduled entry generations")
    checked = Counter(metrics["scenario_invariants"].values())
    axes[13].bar(["Pass", "Fail"], [checked[True], checked[False]], color="#276b80")
    axes[14].axis("off")
    axes[14].text(.03, .85, f"Frozen S5-A files  {parity['s5_a_generated_files']}\n"
                  f"Frozen S4-A files  {len(parity['s4_a_reproduced_files'])}\n"
                  f"GREEN 160–217 s pit parity  {parity['green_pit_160_217']}", transform=axes[14].transAxes)
    axes[15].axis("off")
    axes[15].text(.03, .85, f"{metrics['next_stage']}\n"
                  "Historical restart snapshot  unsupported\n"
                  "Archived global clear delay  0 s", transform=axes[15].transAxes)
    fig.savefig(folder/"diagnostics.png", dpi=120)
    plt.close(fig)
