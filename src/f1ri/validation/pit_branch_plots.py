"""Archived pit branch and historic order diagnostics."""

from collections import Counter

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt

BLUE, GREEN, ORANGE = "#176789", "#208a72", "#be601f"


def render(folder, doc):
    dashboard = doc["dashboard_integration"]
    trace = dashboard["trace"]
    entries, merges = dashboard["entry"], dashboard["merge"]
    metrics = doc["metrics"]
    held = doc["independent_validation"]["held"]
    field = dashboard["route_comparison"]
    sg = dashboard["singapore"]
    sc = doc["sc_queue_ambiguity"]

    def draw(n, title, plot, note=""):
        fig, ax = plt.subplots(figsize=(10, 4.7), layout="constrained")
        plot(ax)
        ax.spines[["right", "top"]].set_visible(False)
        ax.grid(axis="y", alpha=.2)
        fig.suptitle(title, x=.03, ha="left", fontsize=15, color="#142a39", weight="bold")
        if note:
            ax.set_title(note, loc="left", color="#496475", fontsize=9)
        fig.savefig(folder/f"{n:02}.png", dpi=125, metadata={"Software": "F1RI S7-A.1"})
        plt.close(fig)

    def bars(ax, values, color=BLUE):
        keys, counts = zip(*values.items(), strict=True)
        ax.barh(keys[::-1], counts[::-1], color=color)

    def missing(ax, text):
        ax.set_axis_off()
        ax.text(.04, .70, text, transform=ax.transAxes, fontsize=13, va="top", wrap=True)

    def geometry(ax):
        nodes = dashboard["graphs"][9987]["nodes"]
        core = nodes["PIT_LANE"]["points_xy"]
        min_x, max_x = min(p[0] for p in core), max(p[0] for p in core)
        min_y, max_y = min(p[1] for p in core), max(p[1] for p in core)
        for route, c in (("RACING_LOOP", BLUE), ("PIT_LANE", ORANGE)):
            x, y = zip(*nodes[route]["points_xy"], strict=True)
            ax.plot(x, y, color=c, label=route)
        ax.set(xlim=(min_x-700, max_x+700), ylim=(min_y-700, max_y+700), aspect="equal")
        ax.legend()

    def trace_view(ax, start=None, end=None):
        rows = [r for r in trace if (start is None or r["seconds_from_lane_entry"] >= start)
                and (end is None or r["seconds_from_lane_entry"] <= end)]
        x = [r["seconds_from_lane_entry"] for r in rows]
        ax.plot(x, [r["racing_residual_m"] for r in rows], label="Racing residual", color=BLUE)
        ax.plot(x, [r["pit_residual_m"] for r in rows], label="Pit-core residual", color=ORANGE)
        ax.axvline(0, color="#374151", linestyle="--")
        ax.legend()
        ax.set(xlabel="Seconds from reported lane entry", ylabel="Offset (m)")

    def windows(ax, rows):
        ok = [r for r in rows if r["earliest_utc"]]
        for r in ok:
            start = (r["earliest_utc"]-r["timing_report_utc"]).total_seconds()
            ax.barh(r["visit"], r["width_s"], left=start, color=GREEN)
        ax.axvline(0, color="#374151", linestyle="--")
        ax.set_xlabel("Seconds relative to the archived timing report")

    draw(1, "Geometry evidence by session", lambda ax: bars(ax, {
        f"{key} sampled main segments": row["racing_loop_segments"] for key, row in dashboard["geometry"].items()} |
        {f"{key} held-checked pit segments": row["validated_core_segments"] for key, row in dashboard["geometry"].items()}))
    draw(2, "Coordinate-system audit", lambda ax: missing(
        ax, "Pit and racing fixes share the session source frame.\nFIA diagrams establish topology.\n"
        "Control points for a metric registration are unavailable."))
    draw(3, "Imola main loop and held-checked pit core", geometry, "Arbitrary OpenF1 Cartesian coordinates")
    draw(4, "Imola approach residuals", lambda ax: trace_view(ax, end=10))
    draw(5, "Imola exit residuals", lambda ax: trace_view(ax, 19))
    draw(6, "Imola car 1 VSC location trace", trace_view, "Reference visit used for fit.")
    draw(7, "Route labels across Imola visit traces", lambda ax: bars(ax, dict(
        Counter(r["qualified"] for r in trace))))
    draw(8, "Entry route-change windows", lambda ax: windows(ax, entries), "Geometric entry boundaries unavailable.")
    draw(9, "Merge route-change windows", lambda ax: windows(ax, merges), "Geometric merge boundaries unavailable.")
    draw(10, "Frozen 650 m guard by layout", lambda ax: bars(ax, {
        "Guard before the control line": 650, "Guard after the control line": 650}))
    draw(11, "New assignments on identical observations", lambda ax: bars(ax, {
        "Causal racing": metrics["racing_positions_added_causal"],
        "Retrospective racing": metrics["racing_positions_added_retrospective"],
        "Causal pit": metrics["pit_positions_added_causal"],
        "Retrospective pit": metrics["pit_positions_added_retrospective"]}))
    for n, driver in ((12, 63), (13, 4)):
        row = next(r for r in sg if r["driver"] == driver)
        draw(n, f"Singapore car {driver} at the archived cutoff", lambda ax, row=row: bars(ax, {
            "Racing projection residual (m)": row["racing_residual_m"] or 0}),
             f"{row['status']} · race projection only · no qualified pit curve")
    exact = [r for r in field if r["label"] == "Archived model cutoff" and r["mode"] == "EVENT_TIME_CAUSAL"]
    draw(14, "Singapore field at the D8R1 cutoff", lambda ax: bars(ax, dict(
        Counter(r["revised_route"] for r in exact))))
    draw(15, "Predecessor evidence across the pilot", lambda ax: bars(ax,
        doc["route_support"]["certificate_status"]), "The ledger separates bounded candidate order and certification.")
    draw(16, "Possible intervening cars", lambda ax: ax.hist(
        [len(p["possible_interveners"]) for p in dashboard["certificates"] if p["candidates"]], color=ORANGE),
         "Count per target with at least one candidate")
    cars = sorted(sc["cars"], key=lambda r: r["unwrapped_m"])
    draw(17, "Imola SC-clear race-distance estimates", lambda ax: ax.errorbar(
        [r["unwrapped_m"] for r in cars], list(map(str, [r["driver_number"] for r in cars])),
        xerr=[r["uncertainty_m"] for r in cars], fmt=".", color=BLUE, capsize=2))
    draw(18, "Imola SC-clear pairwise overlap", lambda ax: bars(ax, {
        "Distinct lap-unwrapped pair intervals": len(sc["lap_unwrapped_partial_order_pairs"]),
        "Overlapping lap-unwrapped pair intervals": len(sc["lap_unwrapped_overlapping_pairs"]),
        "Circular overlaps": len(sc["circular_overlap_pairs"])}))
    sample = next(r for r in merges if r["visit"] == "imola-vsc-1")["neighbors"]
    draw(19, "Imola car 1 possible merge neighbors", lambda ax: bars(ax, {
        "Candidate ahead": len(sample["ahead"]), "Candidate behind": len(sample["behind"]),
        "Possibly intervening or missing": len(sample["ambiguous_cars"])}),
         "Counterfactual projections across the observed route-change interval")
    draw(20, "Held route checks by separate visit", lambda ax: bars(ax, {
        r["visit"]: sum(x["identified"] for x in r["checks"].values()) for r in held}),
         "Witness counts cluster within each visit and are not independent trials.")
    draw(21, "Sparse sampling and rejection", lambda ax: bars(ax, {
        r["variant"]:r["localized"] for r in doc["sparse_sampling"]["rows"] if r["session"] == 9987}),
         "Imola SC clear · count retaining track-local progress")
    draw(22, "Causal and retrospective pit-route support", lambda ax: bars(ax, {
        "Causal": sum(r["causal_pit"] for r in doc["causal_vs_retrospective"]),
        "Retrospective": sum(r["retrospective_pit"] for r in doc["causal_vs_retrospective"])}))
    draw(23, "Historical field panel coverage", lambda ax: bars(ax, {
        "Race states": metrics["cutoffs"], "Archived visits": len(doc["pit_visit_cohort"])}),
         "S7-A.1 release panel loads these route comparisons and evidence ledgers.")
    draw(24, "Historical strategy prerequisites", lambda ax: missing(
        ax, "Unsupported · unused tyre inventory and pit box occupancy\n"
        "Unsupported · metric pit merge and complete predecessor evidence\n"
        "Unsupported · calibrated neutralization and opponent future policies"))
    draw(25, "Frozen research regression", lambda ax: bars(ax, {
        "S7-A deterministic outputs": doc["regression_parity"]["s7a_deterministic_files"],
        "S6-C deterministic outputs in frozen parity": len(doc["regression_parity"]["s6c_frozen_research_from_s7a"])}))
    draw(26, "S7-A.1 readiness", lambda ax: bars(ax, {
        "Races with held-checked pit cores": doc["independent_validation"]["independent_races_with_passed_route_checks"],
        "Certified historical predecessors": metrics["certified_predecessors"],
        "Exact geometric entry or merge boundaries": metrics["validated_exact_geometric_boundaries"]}),
         "Partial geometric improvement • live unsupported")
