"""Static scientific diagnostics for Stage 5.1."""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
from matplotlib import pyplot as plt

_NAVY = "#17324D"
_TEAL = "#087E83"
_ORANGE = "#C1632B"
_VIOLET = "#7358A4"
_COHORTS = {
    "development": ("Singapore development", _TEAL),
    "stage3_test_reuse": ("Stage 3 test reuse", _NAVY),
    "new_to_pace": ("New to Pace V2", _ORANGE),
}


def _style(fig, axes, title: str) -> None:
    fig.patch.set_facecolor("#F5F7FA")
    fig.suptitle(title, fontsize=18, color=_NAVY, fontweight="bold", x=0.035, ha="left")
    for ax in axes.flat:
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", alpha=0.2)
        ax.set_axisbelow(True)


def plot_pace_integration(report: dict, frame, data: dict, fixture: Path, output: Path) -> None:
    plots = output / "plots"
    plots.mkdir(exist_ok=True)
    field = data["field"]
    case = json.loads(fixture.read_text(encoding="utf-8"))["cases"][0]
    horizon = np.arange(1, len(case["expected_laps_s"]) + 1)
    intervals = np.asarray(case["cumulative_intervals_s"])
    means = np.asarray(case["expected_laps_s"])
    fig, axes = plt.subplots(2, 3, figsize=(15, 8), layout="constrained")
    _style(fig, axes, "Stage 5.1 | Pace parity and field mechanics")
    ax = axes[0, 0]
    ax.fill_between(horizon, intervals[:, 0], intervals[:, 3], color=_TEAL, alpha=0.17,
                    label="Frozen 90% interval")
    ax.plot(horizon, means.cumsum(), color=_NAVY, label="Standalone D8")
    integrated = np.asarray(report["parity"][0]["cumulative_mean_s"])
    ax.plot(horizon[::2], integrated[::2], linestyle="none", marker="+", color=_ORANGE,
            label="Verified S1 mean")
    ax.set(title="Frozen Singapore reference", xlabel="Laps ahead", ylabel="Cumulative time (s)")
    ax.legend(frameon=False, fontsize=8)

    driver = 63 if 63 in field.batches else next(iter(field.batches))
    residuals = field.batches[driver].lap_samples_s
    horizon = np.arange(1, residuals.shape[1] + 1)
    ax = axes[0, 1]
    ax.plot(horizon, residuals[:12].T, color=_TEAL, alpha=0.2)
    ax.plot(horizon, residuals.mean(axis=0), color=_NAVY, lw=2)
    ax.set(title=f"Joint Pace V2 samples · car {driver}", xlabel="Laps ahead", ylabel="Lap time (s)")
    ax = axes[0, 2]
    cov = np.corrcoef(residuals.T)
    lags = np.arange(1, min(8, residuals.shape[1]))
    ax.bar(lags, [np.diag(cov, k).mean() for k in lags], color=_VIOLET)
    ax.set(title="Sampled trajectory correlation", xlabel="Lag (laps)", ylabel="Mean correlation",
           ylim=(0, 1))

    ax = axes[1, 0]
    cutoff_us = round((field.snapshot.as_of_utc - field.snapshot.race_start_utc).total_seconds() * 1e6)
    for driver in field.batches:
        laps = [row for row in data["sample"].laps if row.driver_number == driver]
        if laps:
            ax.plot([(lap.crossing_us - cutoff_us) / 1e6 for lap in laps],
                    [lap.lap_number for lap in laps], alpha=0.50, lw=0.9)
    ax.set(title="Field progress · sampled run", xlabel="Seconds since cutoff", ylabel="Completed lap")
    ax = axes[1, 1]
    support = {}
    for batch in field.batches.values():
        status = batch.forecast.support_status
        support[status] = support.get(status, 0) + 1
    if field.unsupported:
        support["UNSUPPORTED"] = len(field.unsupported)
    ax.barh(list(support), list(support.values()), color=_TEAL)
    ax.set(title="Pace support at diagnostic cutoff", xlabel="Drivers")
    ax = axes[1, 2]
    following = report["field_mechanics"]["synthetic_no_pass"]
    if following:
        ax.plot([r["minimum_gap_s"] for r in following],
                [r["interaction_delay_s"] for r in following], "o-", color=_ORANGE)
    ax.set(title="Following delay · synchronized pair", xlabel="Minimum gap (s)",
           ylabel="Added time (s)")
    fig.savefig(plots / "stage5_s1_parity_and_mechanics.png", dpi=145)
    plt.close(fig)

    fig, axes = plt.subplots(2, 2, figsize=(12, 8), layout="constrained")
    _style(fig, axes, "Stage 5.1 | Historical clean-air forecast diagnostics")
    for cohort, (label, color) in _COHORTS.items():
        values = report["metrics"].get(cohort)
        if not values:
            continue
        horizons = [int(h) for h in values]
        cumulative = [v["cumulative"] for v in values.values()]
        endpoints = [v["endpoint"] for v in values.values()]
        axes[0, 0].plot(horizons, [v["race_weighted_mae_s"] for v in endpoints],
                        "o-", color=color, label=label)
        axes[0, 1].plot(horizons, [v["cum_race_weighted_mae_s"] for v in cumulative],
                        "o-", color=color)
        axes[1, 0].plot(horizons, [v["cum_coverage_90"] for v in cumulative], "o-", color=color)
        axes[1, 0].plot(horizons, [v["cum_coverage_50"] for v in cumulative], "o--", color=color,
                        alpha=0.6)
        rows = frame[(frame.cohort == cohort) & frame.target_is_clean_green]
        jitter = np.linspace(-0.12, 0.12, len(rows))
        axes[1, 1].scatter(rows.requested_horizon + jitter,
                           rows.observed_target_lap_time_s - rows.expected_endpoint_lap_time_s,
                           s=9, alpha=0.28, color=color)
    axes[0, 0].legend(frameon=False, fontsize=8)
    axes[0, 0].set(title="Endpoint lap error", ylabel="Race-weighted MAE (s)")
    axes[0, 1].set(title="Complete-window cumulative error", ylabel="Race-weighted MAE (s)")
    axes[1, 0].axhline(0.9, color="#94A3B8", lw=1)
    axes[1, 0].axhline(0.5, color="#94A3B8", lw=1, ls="--")
    axes[1, 0].set(title="Cumulative 90% and 50% coverage", ylabel="Observed coverage", ylim=(0, 1.04))
    axes[1, 1].axhline(0, color="#94A3B8", lw=1)
    axes[1, 1].set(title="Endpoint residuals", ylabel="Observed minus predicted (s)")
    for ax in axes.flat:
        ax.set_xlabel("Full future laps")
        if not frame.empty:
            ax.set_xticks(sorted(frame.requested_horizon.unique()))
    fig.savefig(plots / "stage5_s1_historical_validation.png", dpi=145)
    plt.close(fig)
