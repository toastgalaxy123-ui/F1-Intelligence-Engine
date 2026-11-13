"""C4 evidence, calibration and control diagnostics."""
import matplotlib
import numpy as np

matplotlib.use("Agg")
from matplotlib import pyplot as plt


def plot_c4(result, support, predictions, path):
    fig, grid = plt.subplots(4, 4, figsize=(21, 15), layout="constrained")
    ax = list(grid.flat)
    titles = ("Clean evidence by sector", "Net independent stints added", "Prior calibration stints",
              "R4-B before and after", "Common held-out MAE", "Cold and warm starts",
              "Tier B sensitivity", "Residuals by age and sector", "Empirical interval coverage",
              "Interval width by prior history", "Uncertainty and effect scale", "H1 control attrition",
              "Eligible and recoverable H1 groups", "Repeat-layout eligibility", "Frozen benchmark parity",
              "Development readiness")
    for axis, letter, title in zip(ax, "ABCDEFGHIJKLMNOP", titles, strict=True):
        axis.set_title(f"{letter}  {title}", loc="left")
        axis.grid(axis="y", alpha=.2)
    models = ("R0", "R1", "R2", "R3", "R4-A", "R4-B")
    policies = ("R4-B", "R4-B-TIER-B-0.10", "R4-B-TIER-B", "R4-B-TIER-B-0.50")
    colors = ("#315d78", "#a35735")
    for i, (key, r) in enumerate(result["races"].items()):
        color = colors[i]
        label = "Austria" if key == "9955" else "Hungary"
        offset = (i-.5)*.32
        for j, source in enumerate(r["by_sector"].values()):
            bottom = 0
            for tier, shade in zip(("A", "B", "C"), ("#36735e", "#86b69d", "#cbd0d5"), strict=True):
                n = source["tiers"].get(tier, 0)
                ax[0].bar(j+offset, n, width=.3, bottom=bottom, color=shade, alpha=1 if i == 0 else .6)
                bottom += n
        ax[1].bar(i, r["added_tier_a_stints_net"], color=color)
        rows = [row for row in support if str(row["session_key"]) == key and not row["following"]]
        ax[2].scatter([x["race_progress"] for x in rows], [x["independent_prior_stints"] for x in rows],
                      s=9, alpha=.4, color=color, label=label)
        new = r["references"]["B"]
        main = new["R4-B"]
        plots = {
            3: [r["c3_r4_b"]["mae_s"], main["mae_s"]],
            4: [new[m]["common"]["mae_s"] for m in models],
            5: [main["history"][v]["mae_s"] for v in ("0", "1", "2", "3")],
            6: [new.get(m, {}).get("bias_s") for m in policies],
            10: [main["median_absolute_error_s"], main["absolute_error_p90_s"], r["timing_disagreement_p90_s"]],
            12: [r["bounds"][v]["groups"] for v in "ABC"],
        }
        for j, values in plots.items():
            ax[j].bar(np.arange(len(values))+offset, [v if v is not None else np.nan for v in values],
                      width=.31, color=color, label=label)
        local = [x for x in predictions if str(x["session_key"]) == key and x["model"] == "R4-B" and x["evaluation"] == "B"]
        for sector in range(3):
            s = [x for x in local if x["checkpoint"] == sector and x["tyre_age"] is not None]
            ax[7].scatter([x["tyre_age"] for x in s], [x["signed_residual_s"] for x in s],
                          marker=("o", "^", "s")[sector], color=color, alpha=.5, s=16, label=f"{label} S{sector+1}")
        for j, method in enumerate(("U0", "U1", "U2")):
            values = [r["intervals"][method][f"coverage_{level}"] for level in (50, 90)]
            ax[8].scatter(np.arange(2) + offset + (j-1)*.06,
                          [v if v is not None else np.nan for v in values], color=color, s=14)
        for row in local:
            if row["half_width_90_s"] is not None:
                ax[9].scatter(row["prior_clean_sectors"], row["half_width_90_s"], color=color, s=14)
        counts = r["control_attrition"]
        stages = ["evidence_before_cutoff", "same_sector", "compound", "tyre_age", "race_progress",
                  "environment", "clean_evidence", "independent_reference", "independent_matched_stints"]
        ax[11].plot(range(len(stages)), [counts[s] for s in stages], "o-", color=color, label=label)
        ax[15].text(.02, .79-.36*i, f"{label}  {'ready' if r['development_gate'] else 'gated'}\n"
                    f"R4-B support {main['support_fraction']:.1%}, {main['clean_stints']} stints",
                    color=color, transform=ax[15].transAxes)
    for i, labels in {
        0: ("S1", "S2", "S3"), 1: ("Austria", "Hungary"), 3: ("C3", "C4"), 4: models,
        5: ("0", "1", "2", "3+"), 6: ("A only", "B .10", "B .25", "B .50"),
        8: ("50%", "90%"), 10: ("Median AE", "P90 AE", "Timing P90"),
        11: ("Earlier", "Sector", "Compound", "Age", "Progress", "Temp", "Tier A", "Residual", "Distinct"),
        12: ("Observed", "Gate passes", "Recoverable"),
    }.items():
        ax[i].set_xticks(range(len(labels)), labels, rotation=22)
    ax[2].axhline(8, ls="--", color="grey", lw=.8)
    ax[6].axhline(0, color="grey", lw=.8)
    ax[10].axhline(.30, ls="--", color="grey", lw=.8)
    ax[11].set_yscale("symlog", linthresh=5)
    if not any(r["intervals"]["U1"]["n"] for r in result["races"].values()):
        for i in (8, 9):
            ax[i].text(.5, .5, "Insufficient independent calibration", ha="center", transform=ax[i].transAxes)
    ax[13].text(.03, .8, "Austria 2024 · warehouse availability audited\n"
                "Hungary 2024 · official timing metadata only\nNo approved transfer cohort",
                linespacing=1.6, transform=ax[13].transAxes)
    ax[14].text(.03, .8, f"H0 Austria +{result['h0_austria_proxy_s']:.6f} s\n"
                "C2, C3 and D8 hashes verified\nZero new requests", linespacing=1.6, transform=ax[14].transAxes)
    for i in (13, 14, 15):
        ax[i].set_axis_off()
    for i in (2, 3, 4, 5, 6, 7, 10, 11, 12):
        ax[i].legend(frameon=False, fontsize=7, ncols=2)
    fig.savefig(path, dpi=130)
    plt.close(fig)
