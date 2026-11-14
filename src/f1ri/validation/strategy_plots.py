"""Render the paired strategy study from committed event and pace ledgers."""
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import itertools

from matplotlib import pyplot as plt


def _bars(ax, runs):
    pairs = [(k, v["combined_delta_us"]/1e6) for k, v in runs["pairs"].items()
             if v["combined_delta_us"] is not None]
    if pairs:
        ax.barh([k for k, _ in pairs], [v for _, v in pairs], color="#316c85")
    ax.axvline(0, color="#777777", lw=.7)
    ax.set_xlabel("Δ to continue at common marker (s)")


def _trajectory(run, driver):
    return [r for r in run["trajectory"] if r["driver"] == driver]


def plot_strategy_study(output, families, runs, sensitivity, metrics):
    fig, grid = plt.subplots(4, 4, figsize=(22, 16), layout="constrained")
    ax = grid.flatten()
    titles = ("Common initial field", "Tyre changes", "Clean and effective pace", "Matched elapsed-time difference",
              "Field progress and pit route", "Pit itinerary", "GREEN paired outcomes", "VSC paired outcomes",
              "SC paired outcomes", "Shared service box", "Tyre age and declared pace", "Competitive order",
              "SC and route sensitivity", "Tyre sensitivity", "Support and randomness", "Conditional first strategy")
    for i, (a, title) in enumerate(zip(ax, titles, strict=True)):
        a.set_title(f"{chr(65+i)}  {title}", loc="left", fontweight="bold")
        a.grid(alpha=.18)
    green = runs["green"]["results"]
    d = 63
    colors = {"HARD": "#888888", "MEDIUM": "#cd951f", "SOFT": "#a04049"}
    for y, p in enumerate(families["green"].physical.cars):
        ax[0].scatter(p.progress_m, y, s=50, color="#316c85")
    ax[0].set(yticks=[0, 1], yticklabels=[f"Car {p.driver_number}" for p in families["green"].physical.cars],
              xlabel="Starting lap progress (m)")
    for y, (name, run) in enumerate(green.items()):
        rows = _trajectory(run, d)
        start = rows[0]["clock_us"]
        changes = [rows[0], *(r for r in rows if r["event"] == "PIT_SERVICE_END" and r["event_driver"] == d), rows[-1]]
        for a, b in itertools.pairwise(changes):
            ax[1].plot([(a["clock_us"]-start)/1e6, (b["clock_us"]-start)/1e6], [y, y],
                       lw=5, color=colors[a["tyres"]["compound"]])
    ax[1].set(yticks=range(len(green)), yticklabels=list(green), xlabel="Window time (s)")
    focus = runs["degradation"]["results"]
    for name in ("continue", "extend-one", "pit-now-medium"):
        laps = defaultdict(list)
        for r in focus[name]["traversals"]:
            if r["driver_number"] == d:
                laps[(r["checkpoint"]-1)//3+1].append(r)
        keys = [l for l, data in laps.items() if len(data) == 3]
        ax[2].plot(keys, [sum(r["effective_us"] for r in laps[l])/1e6 for l in keys], "o-", label=name)
        ax[2].plot(keys, [sum(r["clean_us"] for r in laps[l])/1e6 for l in keys], "--", color=ax[2].lines[-1].get_color())
    ax[2].set(xlabel="Lap · dashed clean, solid effective", ylabel="Elapsed (s)")
    ax[2].legend(fontsize=7)

    def stamps(run):
        return {r["checkpoint"]: r["clock_us"] for r in _trajectory(run, d)
                if r["event_driver"] == d and r["event"] in ("CHECKPOINT_CROSSING", "LAP_COMPLETED", "ON_TRACK_PASS")
                and r["route"] == "ON_TRACK"}

    reference = stamps(green["continue"])
    for name in ("extend-one", "pit-now-medium", "two-stop"):
        candidate = stamps(green[name])
        keys = sorted(reference.keys() & candidate.keys())
        ax[3].plot(keys, [(candidate[k]-reference[k])/1e6 for k in keys], label=name)
    ax[3].set(xlabel="Common racing checkpoint", ylabel="Δ clock (s)")
    ax[3].legend(fontsize=7)
    green_pit = green["pit-now-medium"]
    for driver in (63, 4):
        rows = _trajectory(green_pit, driver)
        ax[4].plot([r["clock_us"]/1e6 for r in rows], [r["progress_m"] for r in rows], label=f"Car {driver}")
        ax[11].step([r["clock_us"]/1e6 for r in rows], [r["classification"] for r in rows], where="post",
                    label=f"Car {driver}")
    ax[4].set(xlabel="Race clock (s)", ylabel="Unwrapped racing progress (m)")
    ax[4].legend(fontsize=8)
    ax[11].set(xlabel="Race clock (s)", ylabel="Competitive rank", yticks=[1, 2], ylim=(2.2, .8))
    ax[11].legend(fontsize=8)
    pit = green_pit["pit"][0]
    stages = [("Route entry", pit["entry_us"]), ("Box arrival", pit["arrival_us"]),
              ("Service end", pit["service_end_us"]), ("Lane exit", pit["exit_us"]), ("Merge", pit["merge_us"])]
    ax[5].plot([t/1e6 for _, t in stages], range(len(stages)), "o-")
    ax[5].set(yticks=range(len(stages)), yticklabels=[n for n, _ in stages], xlabel="Race clock (s)")
    for i, key in ((6, "green"), (7, "vsc"), (8, "sc")):
        _bars(ax[i], runs[key])
    for y, pit in enumerate(runs["shared-box"]["results"]["stack"]["pit"]):
        ax[9].plot([pit["arrival_us"]/1e6, pit["service_start_us"]/1e6], [y, y], lw=4, color="#cd951f")
        ax[9].plot([pit["service_start_us"]/1e6, pit["service_end_us"]/1e6], [y, y], lw=6, color="#316c85")
    ax[9].set(yticks=[0, 1], yticklabels=["Car 63", "Car 4"], xlabel="Race clock (s) · gold is waiting")
    curves = [c for c in families["degradation"].pace.curves if c.driver == d]
    for curve in curves:
        times = curve.lap_seconds[:6]
        ax[10].plot(range(curve.initial_age, curve.initial_age+len(times)), times, "o-", label=curve.set_id)
    ax[10].set(xlabel="Tyre age (laps)", ylabel="Declared clean lap (s)")
    ax[10].legend(fontsize=7)
    for i, candidate in ((12, "react-sc"), (13, "pit-now-medium")):
        cases = [(x["case"], x["delta_us"]/1e6) for x in sensitivity
                 if x["candidate"] == candidate and x["delta_us"] is not None]
        ax[i].barh([k for k, _ in cases], [val for _, val in cases], color="#316c85")
        ax[i].axvline(0, color="#777777", lw=.7)
        ax[i].set_xlabel("Δ to continue (s)")
    for i in (14, 15):
        ax[i].axis("off")
    ax[14].text(.05, .87, "Declared scenarios · deterministic\nSaved D8 draw · identical branches\n"
                "Live D8 strategy forecasts · unavailable\nS5-B physical calibration · partial",
                va="top", transform=ax[14].transAxes)
    summary = []
    for key, result in runs.items():
        if result["ranking"]:
            best = result["ranking"][0][1]
            tied = [name for name, delta in result["ranking"] if delta == best]
            summary.append(f"{key}: {tied[0]}" + (f" (+{len(tied)-1} tied)" if len(tied) > 1 else ""))
    ax[15].text(.05, .93, "Minimum time to supported markers\n\n"+"\n".join(summary)+
                f"\n\n{len(metrics['invariant_failures'])} recorded invariant failures",
                va="top", fontsize=9, transform=ax[15].transAxes)
    (output/"diagnostics").mkdir()
    fig.savefig(output/"diagnostics/diagnostics.png", dpi=120)
    plt.close(fig)
