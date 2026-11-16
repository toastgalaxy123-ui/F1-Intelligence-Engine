"""Deterministic figures for the shadow-preflight evidence bundle."""

from datetime import datetime

import matplotlib

matplotlib.use("Agg")

from matplotlib import pyplot as plt

FEEDS = ("location", "position", "laps", "race_control")


def render(directory, documents):
    def plot(number, slug, title, scope, ylabel, values, *, timeline=False):
        fig, ax = plt.subplots(figsize=(9, 3.5), layout="constrained")
        if timeline:
            for label, points in values.items():
                ax.plot(*zip(*points, strict=True), marker="o", linewidth=1.3, label=label)
            ax.legend(fontsize=8)
            ax.set_xlabel("Elapsed seconds")
        else:
            labels, heights = zip(*values.items(), strict=True)
            ax.bar(range(len(labels)), heights, color="#16748a")
            ax.set_xticks(range(len(labels)), labels, rotation=24, ha="right", fontsize=8)
        ax.set_ylabel(ylabel)
        ax.set_title(f"{title}\n{scope}", loc="left")
        ax.grid(axis="y", alpha=.22)
        ax.set_axisbelow(True)
        fig.savefig(directory / f"{number:02d}_{slug}.png", dpi=120, metadata={"Software": "f1ri"})
        plt.close(fig)

    archived = "Observed archived HTTP retrieval"
    fixture = "Controlled synthetic delivery"
    audit = "Archived Singapore 2025 · original receipt unavailable"
    baseline = documents["polling_baseline"]["endpoints"]
    measured = documents["capture_quality"]["capture_metrics"]["endpoints"]
    traces = documents["polling_comparison"]
    nominal = [r for r in traces if r["experiment"] == "NOMINAL"]
    faults = next(r for r in traces if r["method"] == "S4" and r["experiment"] == "FAULTS")
    feasibility = documents["scheduler_feasibility"]
    mature = documents["lap_leakage_audit"]
    delay = documents["race_control_delay"]
    decisions = documents["shadow_decisions"]
    dashboard = documents["dashboard_integration"]

    plot(1, "frozen_polling", "S7-B sequential polling", archived, "Maximum usable gap (s)",
         {e: baseline[e]["max_usable_interval_s"] for e in FEEDS if baseline[e]["max_usable_interval_s"] is not None})
    plot(2, "response_intervals", "Location response completion gaps", archived, "Seconds",
         {f"{name} {i}": value for name, rows in (("S7-B", baseline), ("S4", measured))
          for i, value in enumerate(rows["location"]["response_intervals_s"], 1)})
    plot(3, "scheduler_policies", "Scheduling comparison", fixture, "Withheld checkpoints",
         {f"{r['method']} {r['experiment'].lower()}": r["withheld_checkpoints"] for r in traces})
    plot(4, "rate_ceiling", "Request rate and freshness targets", "Analytical lower bound", "Requests per minute",
         {"Sustainable ceiling": feasibility["rate_ceiling_steady_requests_per_min"],
          "Location alone": feasibility["required_requests_per_min"]["location"],
          "Combined targets": feasibility["total_required_requests_per_min"]})
    plot(5, "location_duty", "Location freshness duty cycle", fixture, "Fraction of checkpoints",
         {r["method"]: r["freshness"]["location"]["duty_cycle"] for r in nominal})
    plot(6, "endpoint_fairness", "S4 coverage including startup and tail", fixture, "Maximum refresh gap (s)",
         {e: nominal[-1]["freshness"][e]["max_refresh_s"] for e in FEEDS})
    scheduler = documents["capture_quality"]["scheduler"]
    attempts = dashboard["checkpoints"]
    instants = [datetime.fromisoformat(row["clock"]) for row in attempts]
    elapsed = [(x-instants[0]).total_seconds() for x in instants]
    start = mature["start"]
    plot(7, "event_receipt", "Source event cutoff and local usable receipts", archived, "Checkpoint sequence",
         {f"Receipts from {instants[0].strftime('%Y-%m-%d %H:%M:%S UTC')}": list(
             zip(elapsed, range(1, len(elapsed)+1), strict=True)),
          f"Archive source cutoff: {start.strftime('%Y-%m-%d %H:%M:%S UTC')} (off-axis)": [(0, 0)]},
         timeline=True)
    fields = ("lap_duration", "duration_sector_1", "duration_sector_2", "duration_sector_3", "is_pit_out_lap")
    plot(8, "lap_maturity", "Completed fields visible at two cutoffs", audit, "Visible outcome fields",
         {label: sum(f in mature[key] for f in fields) for label, key in
          (("Lap opening", "lap_start_visible"), ("Later cutoff", "later_visible_event_time_only"))})
    cutoff = documents["d8r1_cutoff_audit"]
    plot(9, "lap31_mutation", "Driver 63, lap 31", audit, "Control passed (0 or 1)",
         {"Exact frozen request": cutoff["same_frozen_request"],
          "Future mutation isolated": cutoff["mutated_request_unchanged"]})
    record = mature["source_record"]
    durations = [record.get(f"duration_sector_{n}") or 0 for n in (1, 2, 3)]
    plot(10, "sectors", "Sector completion bounds", "Archived durations rendered as fixture boundaries", "Sector",
         {"Declared sector witnesses": [(sum(durations[:i]), i) for i in (1, 2, 3)]}, timeline=True)
    correction = documents["fault_tests"]["shadow"]["corrected_lap_durations"]
    plot(11, "revision", "Corrected lap duration after its usable receipt", fixture, "Visible lap duration (s)",
         {"Checkpoint values": [(3, correction[0]), (5, correction[1])]}, timeline=True)
    plot(12, "event_receipt_support", "Archive versus receipt-qualified maturity", audit, "Visible outcome fields",
         {"Later archive event cutoff": sum(f in mature["later_visible_event_time_only"] for f in fields),
          "Original receipt evidence": int(mature["original_receipt"] is not None)})
    plot(13, "receipt_alignment", "Usable receipt arrivals by endpoint", archived, "Request sequence",
         {ep: [(t, i+1) for i, (t, r) in enumerate(zip(elapsed, scheduler, strict=True)) if r["selected"] == ep]
          for ep in {r["selected"]: None for r in scheduler}}, timeline=True)
    plot(14, "clock_quality", "Collector host synchronization", "Local clock status audit", "Available (0 or 1)",
         {"Synchronized clock": dashboard["readiness"]["clock_synchronized"],
          "Monotonic request spacing": measured["location"]["requests"] > 1})
    reasons = documents["decision_support"]["withholding_reason_counts"]
    plot(15, "stale_sources", "Source and receipt withholding gates", "Archived replay and controlled checks", "Cases",
         {r.replace("_INPUT_UNSUPPORTED", "").replace("_", " ").title(): n
          for r, n in reasons.items() if r.endswith("_INPUT_UNSUPPORTED")})
    plot(16, "missing_drivers", "Location driver coverage during partial delivery", fixture, "Missing drivers",
         {"S4 fault fixture": [(c["at"], c["feeds"]["location"]["missing_driver_count"])
                               for c in faults["checkpoints"]]}, timeline=True)
    plot(17, "race_control_delay", "GREEN event and usable receipt", fixture, "Visible GREEN (0 or 1)",
         {"GREEN visibility": [(delay["green_delivery"]["source_event_s"], 0),
                               (delay["green_delivery"]["usable_s"], 1)]}, timeline=True)
    selected = list(dashboard["controls"])[:8]
    plot(18, "support_matrix", "Controlled shadow readiness gates", fixture, "Withholding reasons",
         {r["case"].replace("_", " "): len(r["reasons"]) for r in selected})
    exemplar = next(iter(decisions.values()))
    plot(19, "sealed_snapshot", f"Sealed snapshot {exemplar['sha256'][:16]}", archived, "Count",
         {"Source versions": len(exemplar["snapshot"]["versions"]),
          "Withholding gates": len(exemplar["snapshot"]["withholding_reasons"])})
    plot(20, "model_era", "D8R1 era and model artifact gates", fixture, "Withholding reasons",
         {r["case"].replace("_", " "): len(r["reasons"]) for r in dashboard["controls"] if "model" in r["case"]
          or any("ERA" in reason for reason in r["reasons"])})
    physical = decisions["complete_receipt_fixture"]["snapshot"]["physical"]
    plot(21, "physical_predecessor", "Independent physical support", fixture, "Drivers or predecessor pairs",
         {k.replace("_", " "): v for k, v in physical.items()
          if type(v) is int and ("localiz" in k or "predecessor" in k)})
    metrics = documents["metrics"]
    plot(22, "capture_scope", "Observed transport and contemporary racing samples", "Evidence counts", "Count",
         {"Archived HTTP responses": metrics["observed_attempts"],
          "Contemporary racing windows": metrics["contemporary_racing_windows"]})
    plot(23, "outage_recovery", "Fault injection and recovery", fixture, "HTTP status",
         {"S4 location": [(r["start"], r["status"]) for r in faults["schedule"] if r["endpoint"] == "location"]},
         timeline=True)
    plot(24, "readiness_panel", "Shadow readiness evidence in the dashboard", archived, "Earned record versions",
         {"Sealed checkpoints": [(t, r["earned_versions"]) for t, r in zip(elapsed, attempts, strict=True)]},
         timeline=True)
    parity = documents["regression_parity"]
    plot(25, "frozen_parity", "Frozen release evidence", "Pinned hash-verification ledger", "Files / prior gates",
         {"S7-B outputs": parity["s7b_expected_outputs"], "S7-B published pins": len(parity["s7b_pins"]),
          "Prior stage parity gates": parity["prior_sealed_parity"]})
    plot(26, "final_readiness", "Readiness decision B", "Engineering validated · empirical limitations", "Count",
         {"Temporal checks": metrics["temporal_controls"], "Sealed snapshots": metrics["sealed"],
          "Withheld actions": metrics["withheld"], "Live readiness": dashboard["readiness"]["ready_for_live_strategy"]})
