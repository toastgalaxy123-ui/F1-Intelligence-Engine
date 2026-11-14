"""Plot committed pace constraints and the verified continuation archive."""

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt

BLUE = "#236582"
GOLD = "#ca8c32"


def _message(ax, text):
    ax.text(.02, .87, text, transform=ax.transAxes, va="top", fontsize=9)
    ax.set_xticks([])
    ax.set_yticks([])


def _delta(panel, result, names):
    pairs = [(n, result["pairs"][n]["combined_delta_us"]/1e6) for n in names
             if result["pairs"][n]["combined_delta_us"] is not None]
    panel.barh([p[0] for p in pairs], [p[1] for p in pairs], color=BLUE)
    panel.axvline(0, lw=.6, color="#666666")
    panel.set_xlabel("Time vs continue (s)")


def plot_model_strategy(output, metrics, comparisons, discrimination, sensitivity, saved, recovery):
    fig, grid = plt.subplots(5, 4, figsize=(22, 18), layout="constrained")
    axes = grid.flatten()
    titles = ("Bundle recovery", "Forecast support", "Saved forecast horizons", "Replacement trajectory gate",
              "Compound and tyre age", "Saved trajectory dependence", "Paired saved paths", "Latent vs effective",
              "Following through marker", "Tied checkpoint audit", "Blocker removal", "One vs two stops",
              "VSC and SC outcomes", "Entry and rejoin", "Sensitivity reversals", "Six-car field",
              "Frozen regressions", "Readiness")
    for i, (a, title) in enumerate(zip(axes[:18], titles, strict=True)):
        a.set_title(f"{i+1:02d}  {title}", loc="left", fontweight="bold")
        a.grid(alpha=.18)
    for ax in axes[18:]:
        ax.axis("off")
    roots = recovery["roots"]
    axes[0].barh(list(roots), [sum(v["status"] == "VERIFIED" for v in x["files"].values())
                               for x in roots.values()], color=BLUE)
    axes[0].set_xlabel("Verified bundle files")
    _message(axes[1], f"{saved['sample_count']} saved draws per driver\n"
             f"{len(saved.get('drivers', []))} continuation drivers\nOriginal fitted bundle: {recovery['status']}")
    rows = [r for r in saved["rows"] if r["driver"] == 63]
    if rows:
        horizons = [r["horizon"] for r in rows]
        axes[2].plot(horizons, [r["sd_cumulative_s"] for r in rows], "o-", color=BLUE)
        axes[2].set(xlabel="Continuation horizon (laps)", ylabel="Cumulative SD (s)")
        correlations = [saved["within_car_63_lap1_lap2_correlation"], saved["between_4_63_lap1_sample_correlation"]]
        axes[5].barh(["Within car 63", "Between cars 4 and 63"], correlations, color=BLUE)
        axes[5].set(xlim=(-1, 1), xlabel="Sample correlation")
        for index, paths in saved["sample_rows"].items():
            axes[6].plot(range(1, 8), paths[63][:7], label=f"Draw {index}")
        axes[6].legend(fontsize=8)
        axes[6].set(xlabel="Stint lap", ylabel="Clean pace (s)")
    else:
        for n in (2, 5, 6):
            _message(axes[n], "Saved continuation data unavailable.")
    _message(axes[3], "Authentic replacement forecasts gated.\n"
             "Synthetic compound cases below use\nexplicitly declared pace curves.")
    focus = comparisons["green"]["results"]
    names = ("pit-now-medium", "pit-now-hard", "pit-now-used-soft")
    for key in names:
        rs = [r for r in focus[key]["checkpoint_constraints"] if r["driver"] == 63 and r["stint"] > 1 and
              r["latent_us"] is not None and r["checkpoint"] <= 17]
        axes[4].plot([r["tyre_age"] for r in rs], [r["latent_us"]/1e6 for r in rs], ".", label=key)
        axes[7].plot([r["checkpoint"] for r in rs], [r["latent_us"]/1e6 for r in rs], "--", label=key)
        axes[7].plot([r["checkpoint"] for r in rs], [r["effective_us"]/1e6 for r in rs], "-",
                     color=axes[7].lines[-1].get_color())
    axes[4].set(xlabel="Mounted tyre age (laps)", ylabel="Clean traversal (s)")
    axes[4].legend(fontsize=7)
    axes[7].set(xlabel="Checkpoint · dashed clean, solid effective", ylabel="Traversal (s)")
    labels = ["green", "clean-air", "unblocked", "longer-horizon"]
    roster = [next(r for r in discrimination[k] if r["left"] == "pit-now-medium" and r["right"] == "pit-now-hard")
              ["drivers"][0] for k in labels]
    axes[8].barh(labels, [r["left_summary"]["following_only_us"]/1e6 for r in roster], color=BLUE)
    axes[8].set_xlabel("Binding headway time beyond other floors (s)")
    axes[9].barh(labels, [len(r["following_equal_crossing_checkpoints"]) for r in roster], color=BLUE)
    axes[9].set_xlabel("Equal crossings with different clean pace")
    values = [(r["right_marker"]["clock_us"]-r["left_marker"]["clock_us"])/1e6 for r in roster]
    axes[10].barh(labels, values, color=BLUE)
    axes[10].set_xlabel("Hard minus medium at common marker (s)")
    for i, key in enumerate(("pit-now-medium", "two-stop")):
        for p in focus[key]["pit"]:
            axes[11].plot([p["entry_us"]/1e6, p["merge_us"]/1e6], [i, i], "o-", color=BLUE)
    axes[11].set(yticks=[0, 1], yticklabels=["One stop", "Two stops"], xlabel="Pit-route clock (s)")
    pairs = [("VSC reactive", comparisons["vsc"]["pairs"]["react-vsc"]["combined_delta_us"]),
             ("SC reactive", comparisons["sc"]["pairs"]["react-sc"]["combined_delta_us"])]
    axes[12].barh([k for k, _ in pairs], [v/1e6 for _, v in pairs], color=BLUE)
    axes[12].set_xlabel("Time vs continue (s)")
    for i, key in enumerate(("green", "vsc", "sc")):
        name = "pit-now-medium" if key == "green" else f"react-{key}"
        stop = comparisons[key]["results"][name]["pit"][0]
        axes[13].plot([stop["entry_us"]/1e6, stop["merge_us"]/1e6], [i, i], "o-", color=BLUE)
    axes[13].set(yticks=[0, 1, 2], yticklabels=["GREEN", "VSC", "SC"], xlabel="Entry to merge (s)")
    vals = [r for r in sensitivity if r["case_delta_us"] is not None]
    axes[14].barh([r["case"] for r in vals], [r["case_delta_us"]/1e6 for r in vals],
                  color=[GOLD if r["rank_reversed"] else BLUE for r in vals])
    axes[14].tick_params(axis="y", labelsize=7)
    axes[14].set_xlabel("Time vs continue (s)")
    result = comparisons["multi-car"]
    for d in metrics["field_roster"]:
        path = [r for r in result["results"]["stagger"]["trajectory"] if r["driver"] == d]
        axes[15].plot([r["clock_us"]/1e6 for r in path], [r["progress_m"] for r in path], label=str(d))
    axes[15].legend(fontsize=7, ncols=2)
    axes[15].set(xlabel="Race clock (s)", ylabel="Unwrapped progress (m)")
    _message(axes[16], f"{metrics['branches']} executed engineering branches\n"
             f"{metrics['invariant_failures']} invariant failures\n"
             f"{sum(x['identical_trajectory'] for x in metrics['saved_realizations'])} identical saved-path pairs")
    _message(axes[17], f"{metrics['decision']}\n\n"
             "Deterministic scenario discrimination available.\nObserved race-outcome calibration remains open.")
    (output/"diagnostics").mkdir()
    fig.savefig(output/"diagnostics/diagnostics.png", dpi=120)
    plt.close(fig)
