"""Eight diagnostic panels for the S3-C encounter audit."""
from datetime import datetime

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt


def plot_encounter_study(timelines, failures, alignment, result, path):
    fig, grid = plt.subplots(2, 4, figsize=(20, 9), layout="constrained")
    gap, timeline, boundary, support, fragments, controls, yield_plot, readiness = grid.flat
    for i, row in enumerate(timelines):
        at = row["cutoff_utc"]
        label = row["opportunity_id"]
        samples = row["samples"]
        x = [(r["as_of_utc"] - at).total_seconds() for r in samples]
        line, = gap.plot(x, [r["gap_m"] for r in samples], lw=.8, label=label)
        gap.fill_between(x, [float("nan") if r["gap_lower_m"] is None else r["gap_lower_m"] for r in samples],
                         [float("nan") if r["gap_upper_m"] is None else r["gap_upper_m"] for r in samples],
                         color=line.get_color(), alpha=.12)
        signal = (datetime.fromisoformat(row["first_approach_utc"]) - at).total_seconds()
        timeline.scatter(signal, i, marker="^", color=line.get_color())
        if close := row["first_supported_close"]:
            elapsed = (close["as_of_utc"] - at).total_seconds()
            timeline.plot([signal, elapsed], [i, i], "o-", color=line.get_color())
        if row["next_line_utc"] is not None:
            timeline.scatter((row["next_line_utc"] - at).total_seconds(), i, marker="|", color="black")
    gap.axhline(120, color="black", lw=.7, ls="--")
    gap.set(ylim=(0, 350), xlabel="Seconds from cutoff", ylabel="Forward gap (m)", title="Transition-anchor gaps")
    gap.legend(frameon=False, fontsize=8)
    timeline.axvline(0, color="grey", lw=.8)
    timeline.set(yticks=list(range(len(timelines))), yticklabels=[r["opportunity_id"] for r in timelines],
                 xlabel="Seconds from cutoff", title="Approach △, close ●, next line |")
    for i, row in enumerate(failures):
        points = [r for r in row["samples"] if row["cutoff_utc"] <= r["as_of_utc"] < row["target_end_utc"]]
        support.scatter([(r["as_of_utc"] - row["cutoff_utc"]).total_seconds() for r in points],
                        [i] * len(points), c=["#427c65" if r["target_supported"] else "#b85448" for r in points], s=8)
    support.set(yticks=list(range(len(failures))), yticklabels=[r["opportunity_id"] for r in failures],
                xlabel="Seconds from cutoff", title="Driver-specific support")
    for key, color in ((9955, "#35617b"), (9928, "#b66c43")):
        rows = [r for r in alignment if r["session_key"] == key and r["lead_to_boundary_s"] is not None]
        boundary.scatter([r["lead_to_boundary_s"] for r in rows], [r["duration_s"] for r in rows],
                         s=8, alpha=.3, label=str(key), color=color)
    boundary.plot([0, 85], [0, 85], ls="--", color="grey", lw=.7)
    boundary.set(xlabel="Seconds until next lap boundary", ylabel="Fragment duration (s)", title="Lap-boundary alignment")
    boundary.legend(frameon=False)
    causes = sorted({reason for race in result["episode_alignment"].values() for reason in race["termination"]})
    for i, (key, row) in enumerate(result["episode_alignment"].items()):
        fragments.barh([j + i * .35 for j in range(len(causes))],
                       [row["termination"].get(reason, 0) for reason in causes], height=.35, label=str(key))
    fragments.set(yticks=range(len(causes)), yticklabels=[r.replace("_", " ").title() for r in causes],
                  title="Fragment termination reasons")
    fragments.legend(frameon=False)
    before, after = (result[name]["control_pool_attrition"] for name in ("frozen_target", "recovery_target"))
    controls.plot(range(len(before)), list(before.values()), "o-", label="Frozen")
    controls.plot(range(len(after)), list(after.values()), "s--", label="Recovery")
    controls.set(xticks=range(len(before)), xticklabels=[r.replace("_", " ") for r in before],
                 title="Austria car 6 control attrition")
    controls.tick_params(axis="x", rotation=55)
    controls.legend(frameon=False)
    for i, (key, row) in enumerate(result["target_bounds"].items()):
        values = [row["A"]["laps"], row["B"]["laps_upper_bound"], row["C"]["laps_upper_bound"]]
        yield_plot.bar([x + .35*i for x in range(3)], values, width=.35, label=str(key))
        ready = "Eligible" if key in result["readiness"]["eligible_development_races"] else "Gated"
        readiness.text(.05, .78-.26*i, f"{key}  {ready}", fontsize=14)
    yield_plot.set(xticks=[.175, 1.175, 2.175], xticklabels=["Observed", "Physical bound", "Control bound"],
                   ylabel="Following laps", title="One-factor target yield")
    yield_plot.legend(frameon=False)
    readiness.set_title("Development readiness")
    readiness.set_axis_off()
    fig.savefig(path, dpi=135)
    plt.close(fig)
