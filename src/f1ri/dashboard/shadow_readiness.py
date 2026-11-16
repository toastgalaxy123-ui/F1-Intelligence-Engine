"""Read-only display of sealed S7-C0 evidence."""

from json import loads

import streamlit as st


def shadow_panel(root):
    st.subheader("Shadow-Mode Readiness")
    path = root / "artifacts/reports/stage7_s7_c0_dashboard.json"
    if not path.is_file():
        st.warning("The sealed shadow preflight evidence is unavailable.")
        return
    data = loads(path.read_bytes())
    state = data["readiness"]
    st.warning(f"{state['LIVE']} · {state['recommendations']}")
    st.caption(f"{data['mode']} · archived session {data['session']} · {data['capture_id']}")
    tab = st.segmented_control(
        "Shadow evidence", ["Polling feasibility", "Lap maturity", "Sealed checkpoints", "Race control"],
        default="Polling feasibility",
    )
    if tab == "Polling feasibility":
        feasibility = data["feasibility"]
        st.info("Continuous 2-second location freshness is infeasible with the declared polling limits.")
        columns = st.columns(3)
        columns[0].metric("Global request spacing", f"{feasibility['minimum_global_spacing_s']:.1f} s")
        columns[1].metric("Sustainable request ceiling", f"{feasibility['rate_ceiling_steady_requests_per_min']:.2f}/min")
        columns[2].metric("Required across four feeds", f"{feasibility['total_required_requests_per_min']:.1f}/min")
        old = data["baseline"]["endpoints"]
        usable = data["capture"]["endpoints"]
        st.table([{"Endpoint": ep, "Target (s)": row["freshness_limit_s"],
                   "S7-B usable gaps (s)": row["usable_intervals_s"],
                   "S4 usable gaps (s)": usable.get(ep, {}).get("usable_intervals_s")}
                  for ep, row in old.items() if row["freshness_limit_s"] is not None])
        st.bar_chart([{"Endpoint": ep, "Collector": collector, "Longest usable gap (s)": value}
                      for collector, sources in (("S7-B", old), ("S4", usable)) for ep, row in sources.items()
                      if (value := row["max_usable_interval_s"]) is not None],
                     x="Endpoint", y="Longest usable gap (s)", color="Collector", horizontal=True)
        st.caption("Observed HTTP retrieval of archived data. Gaps measure collector cadence.")
        st.write(f"Clock synchronization · {data['clock']['synchronization']}")
        st.caption("Provider publication time and original race-time first receipt are unavailable.")
        if data.get("synthetic"):
            st.caption("Controlled synthetic delivery. Freshness includes driver coverage.")
            st.bar_chart(data["synthetic"], x="Policy", y="Location fresh checkpoints (%)", color="Scenario")
            st.dataframe(data["synthetic"], hide_index=True)
    elif tab == "Lap maturity":
        lap = data["temporal"]
        st.write(f"Session {lap['session']} · driver {lap['driver']} · lap {lap['lap']}")
        st.table([{"Boundary": name, "UTC": lap[key]} for name, key in (
            ("Lap start", "start"), ("Implied finish", "implied_finish"), ("Next recorded line", "next_archived_line"),
            ("Later cutoff", "later_cutoff"))])
        st.caption("The later view uses archive event time. Original receipt evidence is unavailable.")
        st.json({"At lap start": lap["lap_start_visible"], "At later cutoff": lap["later_visible_event_time_only"]})
        st.dataframe(lap["lap_start_field_status"], hide_index=True)
    elif tab == "Sealed checkpoints":
        checkpoints = data["checkpoints"]
        selection = st.selectbox("Receipt cutoff (UTC)", range(len(checkpoints)),
                                 format_func=lambda i: checkpoints[i]["clock"])
        row = checkpoints[selection]
        st.code(row["seal_sha256"], language=None)
        st.write(f"Event cutoff · {row['source_event_cutoff']} · {row['earned_versions']} visible source versions")
        st.dataframe([{"Endpoint": ep, **status} for ep, status in row["endpoints"].items()], hide_index=True)
        st.write(" · ".join(row["withheld"]))
        st.line_chart([{"Usable receipt cutoff (UTC)": c["clock"], "Visible record versions": c["earned_versions"]}
                       for c in checkpoints], x="Usable receipt cutoff (UTC)", y="Visible record versions")
        st.line_chart([{"Usable receipt cutoff (UTC)": c["clock"], "Endpoint": ep, "Response age (s)": age}
                       for c in checkpoints for ep, status in c["endpoints"].items()
                       if (age := status["response_age_s"]) is not None],
                      x="Usable receipt cutoff (UTC)", y="Response age (s)", color="Endpoint")
        st.caption("Controlled shadow conditions and model or geometry gates")
        st.dataframe(data["controls"], hide_index=True)
        st.caption("Later evaluation of the controlled future-data mutation")
        st.json(data["later_evaluation"])
    elif tab == "Race control":
        delay = data["delay"]
        st.caption("Controlled receipt delivery. The simulated race-control physics stay fixed.")
        st.dataframe(delay["deadline_controls"], hide_index=True)
        st.bar_chart([{"SC receipt delay (s)": str(r["delay_s"]),
                       "Committed stops": len(r["pit_entries"])} for r in delay["runs"]],
                     x="SC receipt delay (s)", y="Committed stops")
        st.json(delay["independent_freshness"])
        st.json({"GREEN receipt": delay["green_delivery"], "Corrected GREEN": delay["correction"]})
