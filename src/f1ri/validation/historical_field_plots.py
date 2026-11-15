"""S7-A figures generated from the pilot and stress-test ledgers."""

from collections import Counter

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt

from f1ri.features.historical_field import FieldMode

BLUE, GREEN, ORANGE = "#126b98", "#208871", "#c75b27"


def render(folder, sessions, coverage, positions, cadence, checks, paired, pit_trace, sparse, metrics):
    """Emit small standalone figures with archive evidence and visible missing support."""
    causal = [r for r in coverage if r["mode"] == FieldMode.CAUSAL]
    points = [r for r in positions if r["mode"] == FieldMode.CAUSAL]
    exact = [r for r in points if r["cutoff_label"] == "Archived model cutoff"]
    matched = [r for r in points if r["subset_ahead"]]
    labels = [r["cutoff_label"] for r in causal]

    def draw(number, title, plot, subtitle=""):
        fig, ax = plt.subplots(figsize=(10, 4.9), layout="constrained")
        plot(ax)
        ax.spines[["right", "top"]].set_visible(False)
        ax.grid(axis="y", alpha=.18)
        fig.suptitle(title, x=.03, ha="left", fontsize=15, color="#142a39", weight="bold")
        if subtitle:
            ax.set_title(subtitle, loc="left", fontsize=9, color="#496475")
        fig.savefig(folder / f"{number:02d}.png", dpi=125, metadata={"Software": "F1RI S7-A"})
        plt.close(fig)

    def bars(ax, names, values, color=BLUE):
        ax.barh(names[::-1], values[::-1], color=color)

    draw(1, "Archived field coverage by pilot cutoff",
         lambda ax: bars(ax, labels, [r["positions"] for r in causal]), "Event-time causal • supported positions")
    draw(2, "Location sample cadence", lambda ax: [ax.hist(
        [r["step_s"] for r in cadence if r["session"] == key and r["step_s"] <= 1.5],
        bins=35, alpha=.6, label=session.race) for key, session in sessions.items()],
        "Per-driver gaps up to 1.5 seconds. Longer gaps are counted in the cadence ledger.")
    draw(3, "Observation completeness by driver",
         lambda ax: bars(ax, [str(d) for d in sorted({r["driver_number"] for r in points})],
                         [sum(r["progress_m"] is not None and r["driver_number"] == d for r in points)
                          for d in sorted({r["driver_number"] for r in points})]))

    def geometry(ax):
        for key, session in sessions.items():
            xs, ys = zip(*session.geometry.points, strict=True)
            center = sum(xs)/len(xs)
            ax.plot([x-center+(10000 if key == 9896 else -10000) for x in xs], ys, label=session.race)
        ax.legend()
        ax.set_aspect("equal")

    draw(4, "Sampled racing-line references", geometry, "Layout geometry from archived Cartesian fixes, shown side by side.")
    draw(5, "Pit corridor and racing-route support",
         lambda ax: bars(ax, ["Loop-compatible", "Unresolved pit corridor", "Lane timing only"],
                         [sum(r["route"] == "RACING" for r in points),
                          sum(r["reason"] == "PIT_BRANCH_UNRESOLVED_NEAR_CONTROL_LINE" for r in points),
                          sum(r["reason"] == "PIT_ROUTE_PARTIAL_LANE_TIMING" for r in points)]))
    draw(6, "Lap-unwrapped Singapore progress", lambda ax: ax.scatter(
        [r["driver_number"] for r in exact if r["unwrapped_m"] is not None],
        [r["unwrapped_m"]/1000 for r in exact if r["unwrapped_m"] is not None], c=GREEN),
        "Kilometres from the archived lap anchor and sampled layout")
    draw(7, "Telemetry-projected lap line versus timing",
         lambda ax: ax.scatter([r["timing_anchor_utc"] for r in checks if r["timing_anchor_utc"]],
                               [r["elapsed_error_s"] for r in checks if r["timing_anchor_utc"]], c=BLUE),
         "Seconds of residual; the timing anchors were withheld from these projections.")
    draw(8, "Official position and track-local position", lambda ax: ax.scatter(
        [r["classification"] for r in exact if r["progress_m"] is not None],
        [r["progress_m"] for r in exact if r["progress_m"] is not None], c=ORANGE),
        "Singapore archived cutoff • classification on x, sampled metres on y")
    draw(9, "Track-local ordering support", lambda ax: bars(
        ax, labels, [r["mapped_subset_neighbors"] for r in causal]), "Nearest ahead within the supported subset")
    draw(10, "Gap estimate and source age", lambda ax: ax.scatter(
        [r["age_s"] for r in matched], [r["subset_ahead"]["gap_m"] for r in matched], alpha=.7, c=GREEN),
        "Observation age in seconds on x, distance in metres on y")
    exclusions = Counter(r["reason"] for r in points if r["reason"]).most_common(8)
    draw(11, "Reasons for withholding a position", lambda ax: bars(
        ax, [k.replace("_", " ").title() for k, _ in exclusions], [n for _, n in exclusions], ORANGE))

    def pit(ax, after):
        rows = [r for r in pit_trace if (r["elapsed_s"] >= 20) == after]
        ax.scatter([r["elapsed_s"] for r in rows], [r["projection_error_m"] for r in rows], c=ORANGE, s=14)
        ax.set_xlabel("Seconds from lane entry report")
        ax.set_ylabel("Distance from sampled line (m)")

    draw(12, "Imola VSC pit approach and service trace", lambda ax: pit(ax, False),
         "Car 1 • archived lane timing, no verified physical entry clock")
    draw(13, "Imola VSC pit exit and return to mapped loop", lambda ax: pit(ax, True),
         "Car 1 • sampled line offset, geometric merge time withheld")

    def field(ax, label):
        rows = sorted((r for r in points if r["cutoff_label"] == label and r["progress_m"] is not None),
                      key=lambda r: r["progress_m"])
        ax.errorbar([r["progress_m"] for r in rows], [str(r["driver_number"]) for r in rows],
                    xerr=[r["uncertainty_m"] for r in rows], fmt=".", color=BLUE, capsize=2)
        ax.set_xlim(0, 5000)
        ax.set_xlabel("Metres on sampled racing loop")

    draw(14, "Imola Safety Car deployment", lambda ax: field(ax, "SC deployment"), "Supported observations by car")
    draw(15, "Imola Safety Car track clear", lambda ax: field(ax, "SC track clear"),
         "Track clear is a reported event. Queue geometry is unavailable.")
    draw(16, "Imola VSC movement coverage", lambda ax: bars(
        ax, [r["cutoff_label"] for r in causal if r["cutoff_label"].startswith("VSC")],
        [r["positions"] for r in causal if r["cutoff_label"].startswith("VSC")]))
    encounters = Counter(r["subset_ahead"]["encounter"] for r in matched)
    draw(17, "Lap relationship of mapped neighbors", lambda ax: bars(ax, list(encounters), list(encounters.values())),
         "Local neighbors can differ from competitive order and may be on different laps.")
    draw(18, "Source sparsity stress test", lambda ax: bars(ax, labels, list(sparse.values()), ORANGE),
         "Supported positions after keeping every eighth frozen fix")
    draw(19, "Retrospective versus causal progress", lambda ax: ax.hist(
        [r["divergence_m"] for r in paired if r["divergence_m"] is not None], bins=25, color=BLUE),
        "Absolute wrapped difference in metres • correlated estimates")
    draw(20, "Singapore exact cutoff support", lambda ax: bars(
        ax, ["Lap-unwrapped", "Track-local", "Roster"],
        [sum(r["unwrapped_m"] is not None for r in exact), sum(r["progress_m"] is not None for r in exact), len(exact)]))
    draw(21, "Held timing-check errors by race", lambda ax: ax.boxplot(
        [[abs(r["elapsed_error_s"]) for r in checks if r["session"] == key and r["elapsed_error_s"] is not None]
         for key in sessions], tick_labels=[s.race for s in sessions.values()]),
        "Absolute seconds. A mismatch or missing timing anchor is retained in the ledger.")
    neg = next(r for r in causal if "negative" in r["cutoff_label"].lower())
    draw(22, "Negative control with missing telemetry", lambda ax: bars(
        ax, ["Supported positions", "Roster"], [neg["positions"], neg["roster"]], ORANGE))
    draw(23, "Decision Board evidence coverage", lambda ax: bars(ax, ["Singapore cutoffs", "Imola cutoffs"],
         [sum(r["session"] == key for r in causal) for key in sessions]),
         "Race State and Evidence use the same release snapshot catalog.")
    gates = metrics["invariants"]
    draw(24, "Frozen regression and state invariants", lambda ax: bars(
        ax, [k.replace("_", " ").title() for k in gates], list(gates.values()), GREEN))
    draw(25, "Historical and operational evidence gates", lambda ax: bars(
        ax, ["Bounded source coverage", "Receipt ordering", "Freshness", "Outage recovery"], [1, 0, 0, 0]),
         "1 = established at the audited scope")
    draw(26, "Stage 7-A decision", lambda ax: bars(
        ax, ["Pilot races", "Independent SC or VSC episodes", "Validated geometric merges"],
        [metrics["races"], metrics["independent_episodes"], metrics["physical_merges_validated"]]),
         "Partial historical field support • live use remains unsupported")
