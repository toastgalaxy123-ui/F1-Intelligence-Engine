"""Integrated Streamlit views over the cutoff-aware Stage 6 research service."""

from dataclasses import replace
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from f1ri.decision_board import (
    FUTURES,
    MODES,
    OBJECTIVES,
    PRESETS,
    SETS,
    Settings,
    execute,
    export_run,
    frozen_timing,
    legal_manual,
    load_assets,
    prepare,
)

PAGES = ("Overview", "Race State", "Forecasts", "Strategy Comparison", "Robust Strategy",
         "Timing Risk", "Source Availability & Replay", "Evidence & Validation")
MODE_LABELS = dict(zip(MODES, ("Historical cutoff replay", "Declared counterfactual", "Hindsight analysis",
                              "Live · unavailable"), strict=True))


@st.cache_resource(show_spinner=False)
def _assets(root: Path):
    return load_assets(root)


@st.cache_data(show_spinner=False)
def _timing(root: Path):
    return frozen_timing(root)


def _s(value):
    return value/1_000_000 if value is not None else None


def _compact(value):
    if isinstance(value, dict):
        return " · ".join(f"{k.replace('_', ' ')} = {v}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return " · ".join(map(str, value))
    return str(value) if value is not None else "—"


def _field(label, field):
    return {"Field": label, "Value": _compact(field.value), "Provenance": field.provenance.value,
            "Observation UTC": str(field.at_utc) if field.at_utc else "—", "Source": field.source}


def _inputs(preset, mode, root, assets):
    editable = preset in ("two-car", "neutralization", "shared-box") and mode in MODES[1:3]
    with st.sidebar:
        prefix = f"board-{preset}"
        driver = st.selectbox("Forecast driver", (63, 4), key=f"{prefix}-driver",
                              disabled=preset in ("timing", "unsupported"),
                              format_func=lambda d: {63: "63 · Russell", 4: "4 · Norris"}[d])
        if preset == "shared-box":
            st.caption("Both drivers are scored at a common marker.")
        horizon = st.select_slider("Horizon (laps)", (3, 5, 10), value=5, key=f"{prefix}-horizon",
                                   disabled=preset in ("timing", "unsupported"))
        with st.expander("Replay clock"):
            cutoff = st.text_input("Override cutoff (ISO UTC)", placeholder="Use the verified archive cutoff",
                                   key=f"{prefix}-cutoff")
        settings = Settings(preset=preset, mode=mode, driver=driver, horizon=horizon, cutoff_utc=cutoff)
        if not editable:
            return settings
        with st.expander("Declared strategy inputs", expanded=True):
            sets = st.multiselect("Available replacement sets", SETS, SETS if preset != "shared-box"
                                  else ("medium", "soft"), key=f"{prefix}-sets")
            window = st.selectbox("Eligible laps from next pit lap", (1, 2, 3), 1, key=f"{prefix}-window")
            max_stops = st.selectbox("Maximum stops per driver", (1, 2), key=f"{prefix}-stops")
            budget = st.number_input("Conditional search budget", 2, 128, 64, step=2, key=f"{prefix}-budget")
            team = st.selectbox("Team time objective", ("TEAM_COMBINED", "LEAD_DRIVER", "TEAM_WEIGHTED"),
                                key=f"{prefix}-team") if preset == "shared-box" else "TEAM_COMBINED"
        with st.expander("Declared field and pit route"):
            gap = st.number_input("Track-local gap (m)", 1., 690., 24.635, step=1., format="%.3f",
                                  key=f"{prefix}-gap")
            original = st.checkbox("Use archived opponent pace history", key=f"{prefix}-original")
            offset = st.slider("Mean pace offset (s per lap)", -1., 1., 0., .05, key=f"{prefix}-pace")
            service = st.number_input("Box service (s)", 1., 10., 3., step=.25, key=f"{prefix}-service")
            travel = st.number_input("Additional pit travel (s)", 0., 10., 0., step=.25, key=f"{prefix}-travel")
        with st.expander("Declared futures and robust objective"):
            conditional = st.selectbox("Future for conditional comparison", FUTURES,
                                       2 if preset == "neutralization" else 0, key=f"{prefix}-conditional")
            futures = tuple(st.multiselect("Futures in robust family", FUTURES, FUTURES, key=f"{prefix}-futures"))
            objective = st.selectbox("Robust objective", OBJECTIVES, key=f"{prefix}-objective")
            robust_budget = st.number_input("Robust search budget", 4, 400, 220, step=4, key=f"{prefix}-robust")
            weights = tuple(st.number_input(f"{name} weight", 1, 10, 1, key=f"{prefix}-{name}-weight")
                            for name in futures) if objective == "DECLARED_WEIGHTED_DELTA" else ()
            vs = st.number_input("VSC deployment (+s from cutoff)", 1, 240, 5, key=f"{prefix}-vsc")
            vd = st.number_input("VSC duration (s)", 20, 240, 75, key=f"{prefix}-vsc-duration")
            ss = st.number_input("SC deployment (+s from cutoff)", 1, 240, 5, key=f"{prefix}-sc")
            sd = st.number_input("SC duration (s)", 20, 240, 92, key=f"{prefix}-sc-duration")
            st.caption("The late SC variant deploys 53 s after the base SC.")
        settings = Settings(
            preset=preset, mode=mode, driver=driver, horizon=horizon, cutoff_utc=cutoff, sets=sets,
            stop_options=window, max_stops=max_stops, budget=budget, team_objective=team,
            gap_m=gap, archived_opponent_pace=original, pace_offset_s=offset, service_s=service,
            extra_pit_travel_s=travel, conditional_future=conditional, futures=futures, objective=objective,
            robust_budget=robust_budget, weights=weights, vsc_at_s=vs, vsc_duration_s=vd, sc_at_s=ss, sc_duration_s=sd,
        )
        _, request, unsupported = prepare(root, settings, assets)
        if unsupported:
            return settings
        choices = legal_manual(request)
        with st.expander("Manual comparison"):
            selected = st.multiselect("Policies versus stay out", choices, choices[:3], max_selections=6,
                                      key=f"{prefix}-manual-{settings.driver}-{sets}-{window}-{max_stops}")
        return replace(settings, manual=tuple(selected))


def decision_board(root):
    st.title("Race Intelligence Decision Board")
    st.caption("RESEARCH_DECISION_BOARD · D8R1 mean pace · Live unavailable")
    with st.sidebar:
        st.markdown("#### Research context")
        preset = st.selectbox("Snapshot preset", list(PRESETS), format_func=PRESETS.__getitem__)
        default_mode = MODES[0] if preset in ("archive", "unsupported") else MODES[1]
        mode = st.selectbox("Information mode", MODES, index=MODES.index(default_mode),
                            format_func=MODE_LABELS.__getitem__, key=f"board-mode-{preset}")
    page = st.segmented_control("Board section", PAGES, default=PAGES[0], label_visibility="collapsed")
    try:
        assets = _assets(root)
        settings = _inputs(preset, mode, root, assets)
        prepared = prepare(root, settings, assets)
    except (ValueError, OSError) as error:
        st.error(f"Research input unavailable · {error}")
        return
    snapshot, request, unsupported = prepared
    identity = snapshot.snapshot_id
    saved = st.session_state.get("board-analysis")
    current = saved[1] if saved and saved[0] == identity and not unsupported else None
    run = st.sidebar.button("Run research analysis", type="primary", disabled=bool(unsupported))
    if run:
        try:
            with st.spinner("Evaluating the selected snapshot and policy space…"):
                current = execute(root, settings, assets, prepared=prepared)
                st.session_state["board-analysis"] = (identity, current)
        except (ValueError, OSError) as error:
            st.error(f"Analysis unavailable · {error}")
    st.caption(f"{PRESETS[preset]}  /  {MODE_LABELS[mode]}  /  Snapshot {identity[:12]}")
    if unsupported:
        st.error(" · ".join(unsupported))
    elif saved and current is None:
        st.info("Inputs changed. Run the analysis to evaluate the selected snapshot.")
    elif current is None and preset != "timing":
        st.info("Run the research analysis to calculate results for this snapshot.")
    if mode == MODES[2]:
        st.warning("REALIZED_HINDSIGHT · Retrospective results and oracle benchmarks cannot produce a recommendation.")
    if page == "Overview":
        _overview(snapshot, settings, current, unsupported)
    elif page == "Race State":
        from f1ri.dashboard.historical_field import race_state

        race_state(root, snapshot)
        _race_state(snapshot)
    elif page == "Forecasts":
        _forecasts(snapshot, current)
    elif page == "Strategy Comparison":
        _strategy(snapshot, current)
    elif page == "Robust Strategy":
        _robust(snapshot, current)
    elif page == "Timing Risk":
        _timing_panel(current.get("timing") if current and "timing" in current else _timing(root))
    elif page == "Source Availability & Replay":
        from f1ri.dashboard.source_availability import source_panel

        source_panel(root)
    elif page == "Evidence & Validation":
        from f1ri.dashboard.historical_field import evidence

        _evidence(snapshot, current, assets[1])
        evidence(root)
    with st.sidebar:
        st.caption(f"{request.scenario.profile.identifier}" if snapshot.rule_profile.value else "Archived forecast context")


def _overview(snapshot, settings, current, unsupported):
    st.subheader("Decision overview")
    metrics = st.columns(4)
    metrics[0].metric("Session", str(snapshot.session.value))
    metrics[1].metric("Forecast driver", f"#{settings.driver}")
    metrics[2].metric("Horizon", f"{settings.horizon} laps")
    metrics[3].metric("Race clock", f"{_s(snapshot.race_clock_us.value):,.3f} s")
    st.write(f"**Decision cutoff (UTC)**　{_compact(snapshot.cutoff_utc.value)}")
    st.write(f"**Support**　{_compact(snapshot.support.value)}")
    if unsupported:
        st.warning("Required inputs are unavailable. Affected analysis actions are disabled.")
    if current:
        r = current["recommendation"]
        if r["unique_action"]:
            st.success(f"BEST_WITHIN_DECLARED_COMPARISON · {r['unique_action']}")
        else:
            st.warning("No unique recommendation.")
        st.caption(" · ".join(r["statuses"]))
        if "futures_evaluated" in r:
            st.write("**Evaluated futures**　" + ", ".join(r["futures_evaluated"]))
            st.write("**Omitted preset futures**　" + (", ".join(r["preset_futures_not_evaluated"]) or "None"))
            st.write("**Near-equal policies**　" + (", ".join(r["near"]) or "None in the evaluated set"))
            st.caption(f"{r['comparator']} · Policy library {r['policy_library_sha256'][:12]}")
            if r["incomplete"]:
                st.warning("Search incomplete. Complete recommendation withheld.")
    if settings.preset in ("archive", "unsupported"):
        st.caption("Archived lap and tyre features support cutoff mean forecasts. Physical reconstruction requires declared inputs.")


def _race_state(snapshot):
    st.subheader("Race state at cutoff")
    drivers = [{"Driver": driver["identity"].value["code"], "Number": driver["identity"].value["number"],
                "Completed lap": driver["lap"].value, "Tyre": driver["fitted_tyre"].value["compound_relative"],
                "Tyre age": driver["fitted_tyre"].value["tyre_age_at_start"],
                "Stint": driver["fitted_tyre"].value["stint_number"],
                "Classification": driver["classification"].value,
                "Physical predecessor": driver["physical_predecessor"].value,
                "Track-local gap (m)": driver["predecessor_gap_m"].value,
                "Progress (m)": driver["progress"].value["progress_m"] if driver["progress"].value else None}
               for driver in snapshot.drivers]
    st.dataframe(drivers, hide_index=True)
    if snapshot.layout.value:
        st.caption("DECLARED · Track-local metre position and competitive classification use separate orders.")
        fig = px.scatter(drivers, x="Progress (m)", y="Driver", color="Tyre", hover_data=["Completed lap"])
        fig.update_xaxes(range=[0, snapshot.layout.value["length_m"]])
        fig.update_layout(height=245)
        st.plotly_chart(fig)
    else:
        st.info("Exact circuit progress, physical predecessor and service-box occupancy are unavailable.")
    with st.expander("Observed and declared fields", expanded=True):
        st.dataframe([_field(label, getattr(snapshot, label)) for label in (
            "simulated_track_state", "observed_track_state", "weather", "layout", "rule_profile",
            "pit_evidence", "box_occupancy", "data_freshness")], hide_index=True)
        st.dataframe([_field(f"#{row['identity'].value['number']} {label}", field)
                      for row in snapshot.drivers for label, field in row.items()], hide_index=True)


def _forecasts(snapshot, current):
    st.subheader("D8R1 stint pace")
    model = snapshot.model.value
    st.caption(f"{model['version']} · {model['release']} · {model['era']} · G3 {model['g3']}")
    st.write("Clean-air mean lap time. Forecasts use the last observed weather held constant.")
    if current and current["forecasts"]:
        rows = [{"Plan": plan["plan"], "Lap": row["lap"], "Stint": row["stint"], "Tyre": row["compound"],
                 "Tyre age": row["age"], "Mean lap (s)": row["seconds"],
                 "Cumulative stint forecast (s)": row["cumulative_seconds"], "Support": row["support"]}
                for plan in current["forecasts"] for row in plan["mean"]]
        choices = list(dict.fromkeys(row["Plan"] for row in rows))
        selected = st.multiselect("Displayed forecast plans", choices, choices)
        visible = [row for row in rows if row["Plan"] in selected]
        if visible:
            st.plotly_chart(px.line(visible, x="Lap", y="Mean lap (s)", color="Plan", markers=True,
                                    hover_data=["Tyre", "Tyre age", "Cumulative stint forecast (s)", "Support"]))
        st.dataframe(visible, hide_index=True, height=260)
        with st.expander("Forecast input lineage"):
            st.json([{key: plan[key] for key in ("plan", "request", "feature_as_of",
                                                "feature_source_sha256", "model_sha256")}
                     for plan in current["forecasts"]], expanded=False)
    st.caption("MEAN_ONLY_VALIDATED · Calibrated pace intervals and strategy probabilities are unavailable.")


def _rows(score):
    return [{"Policy": r["candidate"], "Δ vs stay out (s)": _s(r["objective_delta_us"]),
             "Gap to best (s)": _s(r["gap_to_best_us"]), "Clean (s)": _s(r["latent_racing_us"]),
             "Effective (s)": _s(r["effective_racing_us"]), "Following (s)": _s(r["following_us"]),
             "Neutralization (s)": _s(r["neutralization_us"]), "Restart (s)": _s(r["restart_us"]),
             "Pit route (s)": _s(r["pit_route_us"]), "Box wait (s)": _s(r["service_wait_us"]),
             "Comparison": r["comparison"], "Status": r["status"], "Reason": ", ".join(r["reasons"])}
            for r in sorted(score["rows"], key=lambda x: (x["objective_us"] is None, x["objective_us"] or 0))]


def _trace(score, snapshot, label):
    pairs = score["paired"]["results"]
    choice = st.selectbox("Inspect candidate trace", list(pairs), key=f"board-trace-{label}")
    row = pairs[choice]
    epoch = snapshot.race_clock_us.value
    st.caption("Common target markers · " + ", ".join(f"#{d} checkpoint {r['checkpoint']}"
               for d, r in row["targets"].items() if r))
    constraints = [{"Checkpoint": r["checkpoint"], "Category": r["category"], "Driver": str(r["driver"]),
                    "Clean estimate": _s(r["latent_us"]), "After physical constraints": _s(r["effective_us"])}
                   for r in row["checkpoint_constraints"] if r["latent_us"] is not None]
    if constraints:
        st.markdown("**Clean-air and effective checkpoint time**")
        st.plotly_chart(px.line(constraints, x="Checkpoint", y=["Clean estimate", "After physical constraints"],
                                facet_row="Driver", hover_data=["Category"], labels={"value": "Seconds"}))
    events = [{"Driver": v["driver"], "Pit lap": v["target_lap"], "Set": v["set_id"], "Event": event,
               "Seconds from cutoff": _s(v[key]-epoch), "Race clock (s)": _s(v[key])}
              for v in row["pit"] for event, key in (("Entry", "entry_us"), ("Box arrival", "arrival_us"),
              ("Service", "service_start_us"), ("Service end", "service_end_us"), ("Pit exit", "exit_us"),
              ("Rejoin", "merge_us"))
              if v[key] is not None]
    if events:
        st.markdown("**Pit entry, service and rejoin**")
        st.plotly_chart(px.scatter(events, x="Seconds from cutoff", y="Event", color="Set", hover_data=["Driver"]))
        st.dataframe(events, hide_index=True)
    else:
        st.caption("This trace has no completed pit visit.")
    with st.expander("Trajectory, commitment and constraints"):
        st.dataframe(constraints, hide_index=True)
        st.json({"targets": row["targets"], "decisions": row["decisions"],
                 "trajectory": row["trajectory"]}, expanded=False)


def _strategy(snapshot, current):
    st.subheader("Strategy comparison")
    if not current or not current["strategy"]:
        st.caption("Physical comparisons require a declared scenario and a completed analysis.")
        return
    result = current["strategy"]
    order = (("Manual · cutoff-known", "Conditional optimization") if current["settings"]["mode"] != MODES[2]
             else ("Retrospective optimization",))
    view = st.segmented_control("Comparison", order, default=order[0])
    if view == order[0] and len(order) == 2:
        manual_info = st.radio("Manual evaluation", ("KNOWN_AT_CUTOFF", "DECLARED_FUTURE"), horizontal=True)
        score = result["manual"] if manual_info == "KNOWN_AT_CUTOFF" else result["manual_future"]
        tag = f"manual-{manual_info}"
        st.caption(f"{manual_info} · Common racing markers · Reference = stay out")
    else:
        conditional = result["conditional"]
        hindsight = current["settings"]["mode"] == MODES[2]
        labels = ("REALIZED_HINDSIGHT",) if hindsight else ("KNOWN_AT_CUTOFF", "DECLARED_FUTURE")
        tag = st.radio("Decision information", labels, horizontal=True)
        score = conditional["decision"] if tag == "KNOWN_AT_CUTOFF" else conditional["conditional"]
        counts = conditional["search"]
        st.caption(f"{conditional['completion']} · {counts['evaluated']} of {counts['legal']} legal · "
                   f"{counts['rejected']} excluded combinations · Outcomes {_compact(counts['outcomes'])}")
        if conditional["unevaluated"]:
            st.warning("Search incomplete. Ranking covers the evaluated subset.")
        with st.expander("Candidate definitions, exclusions and search coverage"):
            st.json({k: conditional[k] for k in ("candidates", "rejected", "unevaluated", "search")}, expanded=False)
    st.dataframe(_rows(score), hide_index=True, height=295)
    st.caption(f"Exact best: {_compact(score['best']) or '—'} · Near-equal: {_compact(score['near']) or '—'}")
    _trace(score, snapshot, tag)


def _robust(snapshot, current):
    st.subheader("Robust strategy")
    if not current or not current["strategy"]:
        st.caption("Run a declared scenario to compare a common policy library across futures.")
        return
    data = current["strategy"]
    robust = data["robust"]
    scores = robust["analysis"]
    search = robust["search"]
    rec = current["recommendation"]
    st.caption(f"{robust['objective']} · {robust['comparator']} · {robust['status']}")
    st.caption(f"Policy library SHA-256 {rec['policy_library_sha256'][:12]} · Explicitly declared futures")
    st.write(f"**Coverage**　{search['supported_cells']} supported / {search['expected_policy_scenario']} cells")
    if rec["incomplete"]:
        st.warning("Search incomplete. Complete recommendation withheld.")
    elif rec["unique_action"] is None:
        st.warning("No unique recommendation.")
    st.caption(" · ".join(rec["statuses"]))
    view = st.segmented_control("Robust view", ("Regret matrix", "Pareto and oracle", "Declared futures"),
                                default="Regret matrix")
    if view == "Declared futures":
        epoch = snapshot.race_clock_us.value
        futures = [{"Future": name, "Event": event["command"], "Seconds from cutoff": _s(event["at_us"]-epoch),
                    "Race clock (s)": _s(event["at_us"])}
                   for name, events in data["scenario_scripts"].items() if name in robust["cases"] for event in events]
        if futures:
            st.plotly_chart(px.scatter(futures, x="Seconds from cutoff", y="Future", color="Event"))
        st.dataframe(futures, hide_index=True)
        st.write(f"**Evaluated**　{', '.join(rec['futures_evaluated'])}")
        st.write(f"**Omitted**　{', '.join(rec['preset_futures_not_evaluated']) or 'None'}")
        st.dataframe([{"Future": name, "Best realized policies": ", ".join(policies)}
                      for name, policies in rec["case_winners"].items()], hide_index=True)
        with st.expander("Binding constraints for selected policies"):
            st.json(rec["binding_constraints"], expanded=False)
    elif view == "Pareto and oracle":
        st.write("**Pareto frontier**　" + (", ".join(scores["pareto"]) or "Unavailable"))
        st.caption(scores["frontier_scope"])
        st.dataframe([{"Policy": row["policy"], "Worst regret (s)": _s(row["worst_regret_us"]),
                       "Worst loss (s)": _s(row["worst_delta_us"]), "Weighted delta (s)": _s(row["weighted_delta_us"]),
                       "Dominated by": ", ".join(row["dominated_by"]), "Unsupported": row["unsupported_scenarios"]}
                      for row in scores["rows"]], hide_index=True, height=320)
        with st.expander("Perfect-information oracle · hindsight benchmark"):
            st.dataframe([{"Future": name, "Oracle time (s)": _s(row["value_us"]),
                           "Complete": row["complete"], "Oracle-only scored": row["oracle_only_evaluated"]}
                          for name, row in robust["oracles"].items()], hide_index=True)
    else:
        measure = st.selectbox("Matrix measure", ("Regret (s)", "Delta vs stay out (s)"))
        values = scores["regret"] if measure == "Regret (s)" else scores["deltas"]
        matrix = pd.DataFrame.from_dict(values, orient="index").apply(lambda col: col.map(_s))
        if not matrix.empty:
            st.plotly_chart(px.imshow(matrix, aspect="auto", color_continuous_scale="Blues",
                                      labels={"x": "Declared future", "y": "Policy", "color": measure}))
        with st.expander("Policy coverage and pair equivalence"):
            st.json({"search": search, "near": robust["near"], "equivalence": robust["pairs"]}, expanded=False)


def _timing_panel(timing):
    st.subheader("SC deployment and pit commitment")
    st.caption("FROZEN S6-B.1 · B-stress / sc-deploy-58 · Declared timing demonstration")
    st.caption(" · ".join(f"{name}: {version}" for name, version in timing["versions"].items()))
    st.metric("Original reactive policy regret", f"{_s(timing['case']['sc-63-soft']['regret_us']):.6f} s")
    st.write("The SC signal arrived after the lap-31 entry opportunity for the original reactive policy. "
             "It committed on lap 32, after green returned, while the fixed lap-31 stop was already through service. "
             "Regret is measured against the comparator at the common racing marker.")
    st.dataframe([{"Policy": policy, "Pit lap": visits[0]["target_lap"] if visits else None,
                   "Δ vs stay out (s)": _s(row["delta_us"]), "Regret (s)": _s(row["regret_us"]),
                   "Following (s)": _s(row["following_us"])}
                  for policy, row in timing["case"].items() for visits in (row["visited_pits"],)], hide_index=True)
    with st.expander("Original library · checkpoint gap to the fixed stop"):
        st.plotly_chart(px.line([{"Checkpoint": row["checkpoint"],
                                 "Adaptive minus fixed (s)": _s(row["adaptive_minus_fixed_us"])}
                                for row in timing["baseline"]["marker_gap"]],
                               x="Checkpoint", y="Adaptive minus fixed (s)", markers=True))
    policy = st.selectbox("Frozen policy trace", list(timing["case"]), index=2)
    cutoff = 2_937_075_000
    events = [{"State": row["status"], "Seconds from cutoff": _s(row["at_us"]-cutoff),
               "Race clock (s)": _s(row["at_us"]), "Target lap": row["target_lap"],
               "Reason": row["reason"], "Decision": row["decision_id"]}
              for row in timing["commitments"] if row["policy"] == policy]
    if events:
        st.plotly_chart(px.scatter(events, x="Seconds from cutoff", y="State", color="Decision", hover_data=["Reason"]))
        st.dataframe(events, hide_index=True)
    st.dataframe([{"Flag": row["flag"], "Lower offset (s)": _s(row["observed_interval_us"][0]),
                   "Upper offset (s)": _s(row["observed_interval_us"][1]), "Width (s)": _s(row["width_us"])}
                  for row in timing["boundaries"]], hide_index=True)
    st.caption("Each boundary is bracketed by two tested cases. Interior times are unmeasured.")
    st.caption(" · ".join(timing["support"]["statuses"]))
    with st.expander("Physical commitment contract and control events"):
        st.json(timing["contract"], expanded=False)
        st.dataframe([row for row in timing["race_control"] if row["policy"] == policy], hide_index=True)


def _evidence(snapshot, current, hashes):
    st.subheader("Evidence and validation")
    st.write("**Archive and replay provenance**")
    st.dataframe([_field(label, getattr(snapshot, label)) for label in (
        "session", "cutoff_utc", "race_clock_us", "circuit", "regulation_era", "model", "data_freshness")],
        hide_index=True)
    with st.expander("Model support", expanded=True):
        st.json(snapshot.model.value)
    with st.expander("Frozen source hashes"):
        st.dataframe([{"Artifact": path, "SHA-256": value} for path, value in sorted(hashes.items())], hide_index=True)
    if current:
        st.download_button("Export reproducible analysis", export_run(current), "s6-c-research-analysis.zip",
                           "application/zip", on_click="ignore")
        st.code("python -m f1ri.cli stage6-s6-c --replay s6-c-research-analysis.zip", language="shell")
