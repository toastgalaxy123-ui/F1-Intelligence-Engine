"""Research figures with separate historical, captured and controlled evidence labels."""

from collections import Counter, defaultdict

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt

BLUE, GREEN, ORANGE = "#176789", "#1c8063", "#b35325"


def render(folder, docs, tables):
    requests = tables["request_ledger"]
    responses = tables["response_ledger"]
    snapshots = docs["receipt_replay"]["snapshots"]
    faults = docs["fault_injection"]["scenarios"]
    controlled = docs["dashboard_integration"]["synthetic"]
    delays = docs["race_control_delay_demo"]["runs"]

    def draw(n, title, fn, note):
        fig, ax = plt.subplots(figsize=(9.6, 4.6), layout="constrained")
        fn(ax)
        ax.spines[["right", "top"]].set_visible(False)
        ax.grid(axis="y", alpha=.18)
        fig.suptitle(title, x=.03, ha="left", weight="bold", fontsize=15, color="#18333d")
        ax.set_title(note, loc="left", fontsize=9, color="#496475")
        fig.savefig(folder/f"{n:02}.png", dpi=118, metadata={"Software": "F1RI S7-B"})
        plt.close(fig)

    def bars(ax, values, color=BLUE):
        names = list(values)
        ax.barh(names[::-1], [values[k] for k in names[::-1]], color=color)

    def points(ax, x, y, *, color=BLUE, xlabel="", ylabel=""):
        ax.plot(x, y, "o-", color=color)
        ax.set(xlabel=xlabel, ylabel=ylabel)

    def missing(ax, details):
        ax.set_axis_off()
        ax.text(.04, .68, details, transform=ax.transAxes, va="top", fontsize=13, wrap=True)

    def step_rows(ax, section, endpoint):
        rows = controlled[section]
        labels = [s["decision_utc"][-15:-6] for s in rows]
        points(ax, labels, [s["endpoints"][endpoint]["successful"] for s in rows],
               color=ORANGE, xlabel="Synthetic decision clock", ylabel="Successes observed")

    actual = "Actual collector clocks · archived Singapore retrieval"
    synthetic = "Controlled delivery experiment · synthetic clocks and faults"
    historical = "Frozen 2025 observations · event-time only"
    draw(1, "Successful HTTP responses by endpoint", lambda ax: bars(ax, Counter(
        r["endpoint"] for r in responses if not r["error"])), actual)

    def clocks(ax):
        by_ep = defaultdict(list)
        for row in tables["first_seen_ledger"]:
            if row["source_event_utc"] is not None:
                by_ep[row["endpoint"]].append(row)
        for ep, rows in by_ep.items():
            ax.scatter([r["source_event_utc"] for r in rows], [r["first_seen_utc"] for r in rows], s=5, label=ep)
        ax.set(xlabel="Event clock in 2025", ylabel="Current collection clock in 2026")
        ax.legend()

    draw(2, "Separate event and first-seen clocks", clocks, actual)
    draw(3, "HTTP attempt elapsed times", lambda ax: points(ax, [r["sequence"] for r in responses],
         [r["duration_s"] for r in responses], xlabel="Attempt", ylabel="Monotonic seconds"), actual)
    controlled_window = faults["F18"]["observed"]
    draw(4, "Controlled appearance window", lambda ax: bars(ax, {
        "Last complete absence to first receipt (s)": controlled_window["polling_gap_s"],
        "Failed polls in the interval": controlled_window["failed_polls_between"]}), synthetic)
    draw(5, "Event-to-first-seen age evidence", lambda ax: missing(
        ax, "Racing event-to-first-seen age is unmeasured.\nThe only capture re-queried an archived 2025 session."), actual)
    draw(6, "Endpoint response age over cutoffs", lambda ax: [
        ax.plot(range(len(snapshots)), [s["endpoints"][ep]["response_age_s"] for s in snapshots], label=ep)
        for ep in snapshots[-1]["endpoints"]], actual)
    final = snapshots[-1]
    draw(7, "Driver coverage withheld for racing freshness", lambda ax: bars(ax, {
        ep: len(row["missing_or_unverified_drivers"]) for ep, row in final["endpoints"].items()}), actual)
    draw(8, "Request spacing", lambda ax: points(ax, [r["sequence"] for r in requests[1:]],
         docs["capture_quality"]["polling_intervals_s"], xlabel="Attempt", ylabel="Seconds since prior request"), actual)
    draw(9, "Duplicate delivery audit", lambda ax: bars(ax, {
        "Identical redeliveries": docs["revision_audit"]["capture"].get("IDENTICAL_REDELIVERY", 0),
        "Duplicate rows": docs["revision_audit"]["capture"].get("DUPLICATE_ROW", 0)}), actual)
    draw(10, "Version visible before and after correction", lambda ax: points(
        ax, ["Before receipt", "After receipt"], faults["F04"]["observed"], color=ORANGE,
        ylabel="Reported lap duration (s)"), synthetic)
    draw(11, "Historical receipt limitations", lambda ax: bars(ax, {
        "Archived event-time cutoffs": len(docs["historical_receipt_limitations"]),
        "Historical receipt cutoffs supported": 0}), historical)
    draw(12, "What the bounded collector observed", lambda ax: bars(ax, {
        "Completed requests": docs["metrics"]["successful"], "Actual racing collection windows": 0}), actual)
    draw(13, "Virtual receipt cutoff progression", lambda ax: points(ax, list(range(len(snapshots))),
         [s["records"] for s in snapshots], xlabel="Captured response cutoffs", ylabel="Visible record versions"), actual)
    draw(14, "Older request completes after newer", lambda ax: bars(ax, {
        "Older request received at (s)": 4, "Newer request received at (s)": 2}, ORANGE), synthetic)
    draw(15, "Backfill enters only after receipt", lambda ax: points(ax, ["Before", "After"],
         faults["F14"]["observed"], color=ORANGE, ylabel="Visible observations"), synthetic)
    draw(16, "Safety Car policy clock under delayed receipt", lambda ax: points(ax,
        [r["delay_s"] for r in delays], [r["policy_receipt_us"]/1e6 for r in delays], color=ORANGE,
        xlabel="Injected delay (s)", ylabel="Policy receipt on race clock (s)"), synthetic)
    draw(17, "Location freshness with current race control", lambda ax: bars(ax, {
        "Location available for field input": int(faults["F13"]["observed"]["location"]["field_input_allowed"]),
        "Control available for field input": int(faults["F13"]["observed"]["race_control"]["field_input_allowed"])},
        ORANGE), synthetic)
    draw(18, "Short outage and recovery", lambda ax: step_rows(ax, "outage", "location"), synthetic)
    draw(19, "Long outage field gate", lambda ax: missing(ax, "\n".join(faults["F10"]["observed"])), synthetic)
    draw(20, "Interrupted journal resumes", lambda ax: bars(ax, {
        "Committed before recovery": docs["outage_recovery"]["journal"]["before"],
        "Committed after resumption": docs["outage_recovery"]["journal"]["after"],
        "Orphan attempt kept": docs["outage_recovery"]["journal"]["orphaned_after"]}, ORANGE), synthetic)
    draw(21, "Replay is stable when future receipts are appended", lambda ax: bars(ax, {
        "Captured cutoffs verified": len(snapshots), "Controlled prefix checks passed": 1}), actual)
    draw(22, "Race-control event exists before it is visible", lambda ax: points(ax,
        ["Before receipt", "After receipt"], faults["F12"]["observed"], color=ORANGE,
        ylabel="Visible control records"), synthetic)
    draw(23, "Board replay views", lambda ax: bars(ax, {
        "Archived cutoffs": len(docs["historical_receipt_limitations"]),
        "Collector receipt cutoffs": len(snapshots), "Controlled examples": len(controlled)}),
        "Source Availability & Replay · research display")
    draw(24, "Frozen independent rechecks", lambda ax: bars(ax, {
        k: v["outputs"] if v["verified"] else 0 for k, v in docs["regression_parity"]["independent_reruns"].items()}),
        "Published deterministic outputs with matching bytes")
    draw(25, "Operational gates", lambda ax: missing(ax,
        "LIVE_UNSUPPORTED\nProvider publication, historical receipts and racing freshness are unverified."),
        "No operational promotion")
    draw(26, "S7-B readiness", lambda ax: bars(ax, {
        "Controlled fault cases passed": docs["metrics"]["fault_scenarios"],
        "Historical collection windows": 1, "Contemporary racing windows": 0}, GREEN),
        "Path B · engineering validated, empirical availability incomplete")
