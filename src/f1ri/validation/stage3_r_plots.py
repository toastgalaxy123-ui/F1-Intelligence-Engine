"""Descriptive plots from the Stage 3-R study outputs."""
import re

import numpy as np
from matplotlib import pyplot as plt

from f1ri.validation.stage3_r_validation import D8, MODELS

BLUE = "#236582"
ORANGE = "#c78132"


def _text(ax, value):
    ax.text(.04, .88, value, transform=ax.transAxes, va="top", fontsize=8)
    ax.set_xticks([])
    ax.set_yticks([])


def plot_reconstruction(out, sessions, population, predictions, scores, adaptation, replacement, strategy, paired,
                        manifest, gates):
    fig, layout = plt.subplots(6, 4, figsize=(24, 23), layout="constrained")
    a = layout.flatten()
    titles = (
        "Source and warehouse coverage", "Cohort role audit", "Clean-air eligibility", "Tyre compound and age",
        "Model hierarchy", "Observed versus forecast lap", "Residuals by tyre age", "Residuals by race progress",
        "Residuals by compound", "Circuit-level held-out error", "Cumulative MAE", "Online adaptation",
        "Observed versus predicted stint", "Within-stint sample dependence", "Cumulative coverage", "Fresh and used tyres",
        "Paired strategy paths", "Latent and effective pace", "Following-constrained strategy",
        "Regulation transfer", "Model provenance", "Frozen references", "Promotion gates", "Readiness",
    )
    for i, ax in enumerate(a):
        ax.set_title(f"{i+1:02d}  {titles[i]}", loc="left", fontweight="bold")
        ax.grid(alpha=.18)
    ready = [s for s in sessions if s["status"] == "READY"]
    names = [str(s["session_key"]) for s in ready]
    counts = [s["quality"]["laps"] for s in ready]
    clean = [s["quality"]["clean_green_pace_laps"] for s in ready]
    a[0].plot(names, counts, ".", color=BLUE, label="all laps")
    a[0].plot(names, clean, ".", color=ORANGE, label="clean")
    a[0].tick_params(axis="x", rotation=65, labelsize=6)
    a[0].legend(fontsize=8)
    roles = ["train", "val", "test", "transfer_2026"]
    a[1].barh(roles, [sum(s["role"] == role for s in sessions) for role in roles], color="#b3c3cb", label="frozen")
    a[1].barh(roles, [sum(s["role"] == role for s in ready) for role in roles], color=BLUE, label="ready")
    a[1].legend(fontsize=8)
    exclusions = population.exclusion_reason.fillna("ELIGIBLE").value_counts().head(7)
    a[2].barh(exclusions.index[::-1], exclusions.values[::-1], color=BLUE)
    a[2].tick_params(axis="y", labelsize=7)
    pop = population[population.is_clean_green_pace_lap]
    tyres = ("HARD", "MEDIUM", "SOFT")
    for compound in tyres:
        subset = pop[pop.compound_relative.eq(compound)]
        a[3].hist(subset.tyre_age_at_lap_start, bins=18, histtype="step", label=compound)
    a[3].legend(fontsize=8)
    a[3].set_xlabel("Age (laps)")
    order = list(MODELS)
    a[4].barh([name.split("_")[0] for name in order],
              [scores[name]["all_test"]["endpoint"][1]["race_weighted_mae_s"] for name in order], color=BLUE)
    a[4].set_xlabel("Held-out race-weighted MAE (s)")
    all_d8 = predictions[predictions.model_id.eq(D8)]
    one = all_d8[all_d8.requested_horizon.eq(1) & all_d8.target_is_clean_green]
    a[5].scatter(one.observed_target_lap_time_s, one.expected_endpoint_lap_time_s, s=5, alpha=.4, color=BLUE)
    a[5].axline((85, 85), slope=1, color="#777777", lw=.8)
    a[5].set(xlabel="Observed (s)", ylabel="Forecast (s)")
    for i, feature in ((6, "tyre_age_at_lap_start"), (7, "race_progress")):
        a[i].scatter(one[feature], one.endpoint_residual_s, s=5, alpha=.35, color=BLUE)
        a[i].set(xlabel=feature.replace("_", " "), ylabel="Observed minus forecast (s)")
    a[8].boxplot([one.loc[one.compound_relative.eq(c), "endpoint_residual_s"] for c in tyres])
    a[8].set(xticks=[1, 2, 3], xticklabels=tyres, ylabel="Residual (s)")
    circ = one.groupby("circuit_short_name").endpoint_abs_error_s.mean()
    a[9].barh(circ.index, circ.values, color=BLUE)
    a[9].set_xlabel("Endpoint MAE (s)")
    for model in (MODELS[0], MODELS[1], D8):
        data = scores[model]["all_test"]["cumulative"]
        a[10].plot(list(data), [r["cum_race_weighted_mae_s"] for r in data.values()],
                   "o-", label=model.split("_")[0])
    a[10].legend(fontsize=8)
    a[10].set(xlabel="Horizon (laps)", ylabel="Race-weighted MAE (s)")
    regimes = sorted({r["regime"] for r in adaptation})
    for i, model in enumerate((D8, "D8R1_without_stint_adaptation")):
        rows = {r["regime"]: r["endpoint"]["race_weighted_mae_s"] for r in adaptation if r["model"] == model}
        a[11].bar([j+.33*i for j in range(len(regimes))],
                  [rows[k] if rows.get(k) is not None else np.nan for k in regimes], .31,
                  label="adapted" if i == 0 else "without state adaptation")
    a[11].set(xticks=np.arange(len(regimes))+.165, xticklabels=[r.split("_", 1)[0] for r in regimes], ylabel="MAE (s)")
    a[11].legend(fontsize=7)
    ten = all_d8[all_d8.requested_horizon.eq(10) & all_d8.trajectory_is_complete_clean]
    a[12].scatter(ten.observed_cumulative_time_s, ten.expected_total_time_s, s=6, alpha=.44, color=BLUE)
    a[12].axline((850, 850), slope=1, color="#777777", lw=.8)
    a[12].set(xlabel="Observed ten-lap stint time (s)", ylabel="Forecast (s)")
    rows = replacement.get("rows", [])
    cov = [r for r in rows if r.get("draw_lap1_lap2_correlation") is not None]
    a[13].plot([r["request"]["horizon_laps"] for r in cov],
               [r["draw_lap1_lap2_correlation"] for r in cov], "o-")
    a[13].set(xlabel="Draw horizon (laps)", ylabel="Lap 1–2 sample correlation", ylim=(0, 1))
    for level in (50, 90):
        data = scores[D8]["all_test"]["cumulative"]
        a[14].plot(list(data), [r[f"cum_coverage_{level}"] for r in data.values()], "o-", label=f"{level}%")
        a[14].axhline(level/100, color="#777777", lw=.7)
    a[14].legend(fontsize=7)
    a[14].set(xlabel="Horizon (laps)", ylabel="Empirical coverage", ylim=(0, 1))
    for r in rows:
        if r.get("hypothetical_delay_laps") == 0:
            laps = r["forecast"]["lap_forecasts"]
            a[15].plot([t["target_lap_number"] for t in laps], [t["expected_lap_time_s"] for t in laps],
                       label=r["fitted_set"])
    if a[15].lines:
        a[15].legend(fontsize=7)
    a[15].set(xlabel="Race lap", ylabel="Clean lap forecast (s)")
    draws = paired.get("strategy_draws", {})
    for label in ("medium", "hard", "two-stop", "identical-medium"):
        track = [p for p in draws.get(label, {}).get("trajectory", ()) if p["driver"] == 63]
        if track:
            start_us = track[0]["clock_us"]
            a[16].plot([(p["clock_us"]-start_us)/1e6 for p in track], [p["checkpoint"] for p in track], label=label)
    a[16].set(xlabel="Elapsed time (s)", ylabel="Checkpoint")
    blocked = strategy.get("two-car", {}).get("result", {}).get("results", {})
    for label in ("medium", "hard", "used-soft"):
        data = [r for r in blocked.get(label, {}).get("checkpoint_constraints", ())
                if r["driver"] == 63 and r["latent_us"] is not None]
        if data:
            a[17].plot([r["checkpoint"] for r in data], [r["effective_us"]/1e6 for r in data], label=label)
            a[17].plot([r["checkpoint"] for r in data], [r["latent_us"]/1e6 for r in data], "--",
                       color=a[17].lines[-1].get_color())
    for ix in (16, 17):
        if a[ix].lines:
            a[ix].legend(fontsize=7)
        else:
            _text(a[ix], "Strategy trajectory unavailable in this run.")
    a[17].set(xlabel="Checkpoint", ylabel="Traversal (s)")
    a[18].barh(list(blocked), [sum(r["following_only_us"] or 0 for r in v.get("checkpoint_constraints", [])
                                  if r["driver"] == 63)/1e6 for v in blocked.values()], color=BLUE)
    a[18].set_xlabel("Headway beyond other bounds (s)")
    _text(a[19], "2026 forward transfer not evaluated.\nFrozen transfer roles preserved.")
    _text(a[20], f"{manifest['model_version']}\nTrain through {manifest['trained_through_utc']}\n"
          f"Calibrate through {manifest['calibrated_through_utc']}\nFitted digest {manifest['model_sha256'][:16]}…")
    _text(a[21], "Original fitted pickle unavailable.\nHistorical roles and estimator retained.\n"
          "Frozen S5 metrics and Stage 3 source hashes verified.")
    flags = {k.split("_")[0]: v for k, v in gates.items() if k.startswith("G")}
    a[22].barh(list(flags), [1]*len(flags), color=[BLUE if ok else ORANGE for ok in flags.values()])
    a[22].set_xticks([])
    for i, ok in enumerate(flags.values()):
        a[22].text(.05, i, "Passed" if ok else "Gated", color="white", va="center")
    _text(a[23], f"{manifest['release']}\n\n{len(ready)} reconstructed race weekends\n"
          "Physical strategy runs use a declared field.")
    folder = out/"diagnostics"
    folder.mkdir(exist_ok=True)
    fig.savefig(folder/"reconstruction.png", dpi=112)
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    inverse = fig.dpi_scale_trans.inverted()
    bounds = [ax.get_tightbbox(renderer).transformed(inverse).expanded(1.05, 1.10) for ax in a]
    fig.set_layout_engine("none")
    for i, (ax, title, box) in enumerate(zip(a, titles, bounds, strict=True), 1):
        for panel in a:
            panel.set_visible(panel is ax)
        name = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
        fig.savefig(folder/f"{i:02d}_{name}.png", dpi=144, bbox_inches=box)
    plt.close(fig)
