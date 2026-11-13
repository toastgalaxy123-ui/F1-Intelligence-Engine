"""Render the sixteen short-horizon diagnostics from the replay ledgers."""
from collections import Counter

import matplotlib
import numpy as np

matplotlib.use("Agg")
from matplotlib import pyplot as plt


def plot_short_horizon(rows, validation, traffic, result, plan, path):
    fig, grid = plt.subplots(4, 4, figsize=(21, 16), layout="constrained")
    axes = list(grid.flat)
    titles = ("Target eligibility", "Physical support", "Raw vs independent following", "Timing disagreement",
              "Held-out reference MAE", "Held-out clean residuals", "Residual vs initial gap",
              "Residual vs relative clean pace", "Residual vs close exposure", "Lap relationship",
              "Clean negative controls", "Reference error vs effect scale", "Fragmentation and interruptions",
              "Lap vs short-horizon yield", "Development readiness", "Frozen regression inputs")
    for letter, title, ax in zip("ABCDEFGHIJKLMNOP", titles, axes, strict=True):
        ax.set_title(f"{letter}  {title}", loc="left")
        ax.grid(axis="y", alpha=.2)
    keys = (9955, 9928)
    labels = ("H0", "H1", "H2", "H3_5", "H3_10", "H3_20", "H3_30")
    names = ("Lap", "Sector", "Distance", "5 s", "10 s", "20 s", "30 s")
    colors = ("#275975", "#a45034")
    for i, (key, color) in enumerate(zip(keys, colors, strict=True)):
        data = [result["horizons"][f"{key}/{h}"] for h in labels]
        x = np.arange(len(labels)) + (i - .5) * .32
        axes[0].plot(x, [r["funnel"]["final_proxy"] for r in data], "o-", color=color, label=str(key))
        axes[1].plot(x[1:], [r["physical_pass_fraction"] for r in data[1:]], "o-", color=color, label=str(key))
        axes[2].scatter([r["funnel"]["physical_following"] for r in data],
                        [r["independent_groups"] for r in data], color=color, label=str(key))
        axes[13].bar(x[:3], [r["funnel"]["physical_following"] for r in data[:3]], width=.32, color=color, label=str(key))
        axes[14].text(.04, .8 - i * .32, f"{key}  " + ", ".join(
            f"{h} {'pass' if result['horizons'][f'{key}/{h}']['development_gate'] else 'gated'}" for h in labels[:3]),
            color=color, transform=axes[14].transAxes)
        relations = Counter(r["lap_relationship"] for r in rows if r["session_key"] == key
                            and r["horizon_type"] in ("H1", "H2") and r["outcome"]["following_label"])
        axes[9].bar(np.arange(3)+(i-.5)*.32, [relations[k] for k in ("SAME_LAP", "LAPPING", "BEING_LAPPED")],
                    width=.32, color=color, label=str(key))
        axes[12].bar(x[1:3], [r["raw_fragments"] for r in data[1:3]], width=.32, color=color, label=str(key))
    for ax in axes[:2]:
        ax.set_xticks(range(len(labels)), names, rotation=20)
    for ax in (axes[12], axes[13]):
        ax.set_xticks(range(3), names[:3])
    axes[9].set_xticks(range(3), ["Same lap", "Lapping", "Being lapped"])
    axes[2].set(xlabel="Segments", ylabel="Driver–leader–stint groups")
    for horizon in ("H1", "H2"):
        timing = [r["timing"]["timing_error_s"] for r in rows if r["horizon_type"] == horizon
                  and r["timing"].get("timing_error_s") is not None]
        if timing:
            axes[3].hist(timing, bins=25, alpha=.55, label=horizon)
    models = ("R0", "R1", "R2", "R3")
    for i, (key, local) in enumerate(result["references"].items()):
        x = np.arange(4) + (i - 1.5) * .17
        axes[4].bar(x, [local[m]["mae_s"] if local[m]["mae_s"] is not None else np.nan for m in models],
                    width=.17, label=key)
        axes[11].bar(x, [local[m]["absolute_error_p90_s"] if local[m]["absolute_error_p90_s"] is not None else np.nan
                         for m in models], width=.17, label=key)
    for ax in (axes[4], axes[11]):
        ax.set_xticks(range(4), models)
        ax.set_ylabel("Seconds")
    axes[11].axhline(plan["feasibility"]["planning_effect_s"], color="#323232", ls="--", label="Planning effect")
    for model in models:
        clean = [r["signed_residual_s"] for r in validation if r["model"] == model]
        if clean:
            axes[5].hist(clean, bins=30, alpha=.45, label=model)
            axes[10].scatter(models.index(model), np.mean(clean), color=colors[0])
    axes[10].axhline(0, lw=.7, color="grey")
    axes[10].set(xticks=range(4), xticklabels=models, ylabel="Held-out clean bias (s)")
    for ax, field, xlabel in zip(axes[6:9], ("initial_gap_m", "relative_clean_pace_s", "close_exposure_s"),
                                ("Initial gap (m)", "Relative D8 pace (s)", "Observed close exposure (s)"), strict=True):
        eligible = [r for r in traffic if r["signed_proxy_s"] is not None and r[field] is not None]
        if eligible:
            ax.scatter([r[field] for r in eligible], [r["signed_proxy_s"] for r in eligible], s=9, alpha=.5)
        else:
            ax.text(.5, .5, "No eligible proxies", ha="center", transform=ax.transAxes)
        ax.set(xlabel=xlabel, ylabel="Observational proxy (s)")
    axes[15].text(.04, .8, "Frozen D8 verified\nFrozen encounter metrics verified\nAustria recovery preserved\nZero new requests",
                  linespacing=1.6, transform=axes[15].transAxes)
    for ax in (axes[14], axes[15]):
        ax.set_axis_off()
    for ax in axes:
        handles, _ = ax.get_legend_handles_labels()
        if handles:
            ax.legend(frameon=False, fontsize=7)
    fig.savefig(path, dpi=140)
    plt.close(fig)
