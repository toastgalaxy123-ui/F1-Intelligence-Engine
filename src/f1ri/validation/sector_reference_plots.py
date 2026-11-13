"""C3 reference and readiness diagnostics."""
from collections import defaultdict

import matplotlib
import numpy as np

matplotlib.use("Agg")
from matplotlib import pyplot as plt


def plot_sector_reference(predictions, metrics, path):
    fig, grid = plt.subplots(4, 4, figsize=(19, 15), layout="constrained")
    ax = list(grid.flat)
    titles = ("Clean sectors and stints", "Support by earlier clean sectors", "Common held-out MAE",
              "Error by independent stint", "Online adaptation", "Predicted and observed", "Residual by tire age",
              "Residual by race progress", "Error by training evidence", "Cold and warm starts",
              "Interval coverage", "Error and planning effect", "H1 target conversion",
              "Following segments and groups", "Frozen benchmark", "Statistical readiness")
    for a, letter, title in zip(ax, "ABCDEFGHIJKLMNOP", titles, strict=True):
        a.set_title(f"{letter}  {title}", loc="left")
        a.grid(axis="y", alpha=.2)
    names = ("R0", "R1", "R2", "R3", "R4-A", "R4-B", "R4-B-TIER-B")
    heldout = [r for r in predictions if r["evaluation"] == "B"]
    for i, (race, result) in enumerate(metrics["races"].items()):
        color = ("#315f79", "#af613a")[i]
        label = str(race)
        local = [r for r in heldout if r["session_key"] == race and r["model"] == "R4-B"]
        scores = result["reference"]["B"]
        x = np.arange(3) + (i-.5)*.35
        for sector, pos in zip(result["evidence"].values(), x, strict=True):
            bottom = 0
            for tier, tier_color in zip(("A", "B", "C"), ("#36735e", "#86b69d", "#cbd0d5"), strict=True):
                n = sector["tiers"].get(tier, 0)
                ax[0].bar(pos, n, width=.32, bottom=bottom, color=tier_color, alpha=1 if i == 0 else .65)
                bottom += n
            ax[0].text(pos, bottom+10, str(sector["clean_by_tier"]["A"]["stints"]), fontsize=7, ha="center")
        for model in names:
            hist = scores[model]["history"]
            if model in ("R0", "R1", "R2", "R3", "R4-B"):
                ax[1].plot(range(4), [v["n"]/v["eligible"] if v["eligible"] else np.nan for v in hist.values()],
                           marker="o", ms=3, alpha=.75, label=f"{label} {model}")
        common = [scores[m]["common"]["mae_s"] for m in names[:-1]]
        ax[2].bar(np.arange(6)+(i-.5)*.35, [v if v is not None else np.nan for v in common], width=.35,
                  color=color, label=f"{label} n={scores['R4-B']['common']['n']}")
        by_stint = defaultdict(list)
        for row in local:
            by_stint[row["driver_number"], row["stint_number"]].append(abs(row["signed_residual_s"]))
        ax[3].scatter([i]*len(by_stint), [np.mean(e) for e in by_stint.values()], alpha=.55, color=color)
        for m, ls in (("R4-A", ":"), ("R4-B", "-")):
            ax[4].plot(range(4), [v["mae_s"] if v["mae_s"] is not None else np.nan
                                  for v in scores[m]["history"].values()], "o", linestyle=ls, color=color, label=f"{label} {m}")
        if local:
            ax[5].scatter([r["predicted_s"] for r in local], [r["observed_s"] for r in local], s=10, alpha=.55, color=color)
        for a, field in ((ax[6], "tyre_age"), (ax[7], "race_progress")):
            a.scatter([r[field] for r in local], [r["signed_residual_s"] for r in local], color=color, s=10, alpha=.55)
        for a, values in (
            (ax[8], [scores[m]["mae_s"] for m in ("R4-B", "R4-B-TIER-B")]),
            (ax[9], [scores["R4-B"]["history"][j]["mae_s"] for j in ("0", "3")]),
            (ax[10], [scores["R4-B"][f"coverage_{j}"] for j in (50, 90)]),
            (ax[11], [scores["R4-B"][j] for j in ("median_absolute_error_s", "absolute_error_p90_s", "median_half_width_90_s")]),
            (ax[12], [result[j] for j in ("frozen_h1_following", "exploratory_following_predictions",
                                         "following_with_three_controls", "eligible_h1_proxies")]),
            (ax[13], [result[j] for j in ("frozen_h1_following", "frozen_h1_groups", "eligible_h1_groups")]),
        ):
            a.bar(np.arange(len(values))+(i-.5)*.35, [v if v is not None else np.nan for v in values],
                  width=.35, color=color, label=label)
        ax[15].text(.03, .8-i*.36, f"{label}  {'validated' if result['development_gate'] else 'gated'}\n"
                    f"{result['r4_b_uncertainty_gate'].replace('_', ' ').title()}", color=color, transform=ax[15].transAxes)
    for i, labels in {
        0: ("S1", "S2", "S3"), 2: names[:-1], 3: ("Austria", "Hungary"),
        8: ("Tier A", "A + weighted B"), 9: ("Cold", "3+ prior"), 10: ("50%", "90%"),
        11: ("Median AE", "P90 AE", "90% half-width"), 12: ("Physical", "R4 forecast", "3 controls", "Proxy"),
        13: ("Segments", "Physical groups", "Eligible groups"),
    }.items():
        ax[i].set_xticks(range(len(labels)), labels, rotation=15)
    for i in (1, 4):
        ax[i].set_xticks(range(4), ("0", "1", "2", "3+"))
    for i in (6, 7):
        ax[i].axhline(0, color="grey", lw=.7)
    lo, hi = ax[5].get_xlim()
    ax[5].plot([lo, hi], [lo, hi], "--", color="grey", lw=.7)
    ax[11].axhline(.30, ls="--", lw=.8, color="#333333")
    if all(result["reference"]["B"]["R4-B"]["interval_n"] == 0 for result in metrics["races"].values()):
        ax[10].set_ylim(0, 1)
        ax[10].text(.5, .5, "Insufficient independent calibration stints", transform=ax[10].transAxes,
                    ha="center", fontsize=9)
    ax[14].text(.03, .8, f"H0 Austria proxy +{metrics['h0_austria_proxy_s']:.6f} s\n"
                "C2 ledgers and D8 verified\nZero new telemetry requests", transform=ax[14].transAxes, linespacing=1.6)
    for i in (14, 15):
        ax[i].set_axis_off()
    for i in (1, 2, 4, 8, 9, 10, 11, 12, 13):
        ax[i].legend(fontsize=6, frameon=False, ncols=2)
    fig.savefig(path, dpi=135)
    plt.close(fig)
