"""Interactive S6-A search within the existing strategy dashboard."""

from dataclasses import replace

import pandas as pd
import plotly.express as px
import streamlit as st

from f1ri.simulator.strategy import StrategyInputError
from f1ri.simulator.strategy_optimization import HORIZONS, REFERENCE, PaceSensitivity, optimize
from f1ri.validation.optimization_scenarios import fixture_request, scenario_requests
from f1ri.validation.optimization_study import verified_optimizer

NAMES = {"clean-air": "Clean air", "two-car": "Close, slower predecessor",
         "wider-gap": "Wider opening gap", "original-opponent": "Original opponent pace",
         "retirement": "Declared predecessor retirement", "opponent-pits": "Declared opponent stop",
         "shared-box": "Shared pit box"}
OBJECTIVES = {"TEAM_COMBINED": "Combined time", "LEAD_DRIVER": "Driver 63 time", "TEAM_WEIGHTED": "Weighted team time"}


def _label(name):
    return name.replace(REFERENCE, "Stay out").replace("__", " + ")


def _seconds(value):
    return value/1e6 if value is not None else None


def optimization_panel(root):
    st.subheader("Conditional strategy optimization")
    st.caption("D8R1_MEAN_ONLY_VALIDATED · T1_NO_UNSCRIPTED_PASS · HISTORICAL_FIELD_SUPPORT_AS_DECLARED")
    st.caption("NEUTRALIZATION_PACE_UNCALIBRATED · Times are conditional on the declared field and controls.")
    fixtures = scenario_requests(root)
    scenes, _ = fixtures
    scope = st.sidebar.selectbox("Optimize", ("Driver 63", "Driver 4", "Team"))
    team = scope == "Team"
    driver = 4 if scope == "Driver 4" else 63
    choices = ("shared-box",) if team else tuple(
        k for k in NAMES if k != "shared-box" and (driver == 63 or k not in {"clean-air", "retirement", "opponent-pits"}))
    key = st.sidebar.selectbox("Initial field", choices, format_func=NAMES.__getitem__)
    control = st.sidebar.selectbox("External race control", ("Green", "Declared VSC", "Declared Safety Car"))
    horizon = st.sidebar.selectbox("Forecast horizon (laps)", HORIZONS, index=1)
    tiers = {t.set_id: f"{t.compound.title()} · {t.initial_age_laps} laps used" for r in scenes[key].resources
             if r.driver == driver for t in r.tyres}
    sets = st.sidebar.multiselect("Eligible tyre sets", tuple(tiers), default=tuple(tiers), format_func=tiers.__getitem__)
    budget = st.sidebar.slider("Simulator evaluation budget", 2, 128, 64, step=2)
    window = st.sidebar.slider("Permitted pit laps after cutoff", 1, horizon-1, (1, min(3, horizon-1)))
    max_stops = st.sidebar.selectbox("Maximum stops across selected drivers", (0, 1, 2), index=1)
    objective = st.sidebar.selectbox("Objective", tuple(OBJECTIVES), format_func=OBJECTIVES.__getitem__) if team else (
        "COMMON_MARKER_TIME")
    weights = tuple(
        (d, st.sidebar.number_input(f"Driver {d} objective weight", 1, 100, default))
        for d, default in ((63, 2), (4, 1))
    ) if objective == "TEAM_WEIGHTED" else ()
    information = st.sidebar.selectbox("Policy information", ("POINT_IN_TIME", "PERFECT_FORESIGHT"),
                                      format_func=lambda x: x.replace("_", " ").title())
    sensitivity = st.sidebar.checkbox("Check sensitivity to +0.3 seconds per lap")
    req = fixture_request(root, key, horizon=horizon, sets=tuple(sets), stop_options=window[1],
                          max_stops=max_stops, information=information, budget=budget, objective=objective,
                          fixtures=fixtures, driver=driver)
    windows = tuple(replace(w, first_lap=w.first_lap+window[0]-1) for w in req.windows)
    neutralization = {"Green": (), "Declared VSC": scenes["vsc"].controls, "Declared Safety Car": scenes["sc"].controls}
    req = replace(req, windows=windows, weights=weights, scenario=replace(req.scenario, controls=neutralization[control]))
    if st.sidebar.button("Run optimization", type="primary"):
        try:
            bundle, _ = verified_optimizer(root)
            with st.spinner("Evaluating bounded strategies"):
                result = optimize(req, bundle)
                varied = optimize(replace(req, sensitivity=PaceSensitivity(level_s=.3)), bundle) if sensitivity else None
            st.session_state["optimization"] = req, result, varied
        except (StrategyInputError, ValueError, OSError) as error:
            st.error(str(error))
            return
    cached = st.session_state.get("optimization")
    if not cached or cached[0] != req or bool(cached[2]) != sensitivity:
        return
    _, result, varied = cached
    _display(result, varied)


def _display(result, varied):
    decision, conditional = result["decision"], result["conditional"]
    search = result["search"]
    st.caption(f"{result['completion']} · {search['evaluated']} / {search['legal']} legal candidates · "
               f"{search['simulator_evaluations']} simulator evaluations")
    st.markdown("**Best evaluated at the decision cutoff**"
                if result["information_mode"] == "POINT_IN_TIME" else "**Best evaluated with perfect foresight**")
    st.write(", ".join(map(_label, decision["best"])) or "No supported candidate reached the evaluation marker.")
    if decision["near"]:
        st.caption("Near-equal alternatives within the prespecified model-error margin")
        st.write(", ".join(map(_label, decision["near"])))
    if result["decision_basis"] == "DECISION_KNOWN_EVENTS":
        st.caption("The table uses the full declared script. Its first-place outcomes include hindsight about the script.")
    if varied:
        st.caption("Declared +0.3 seconds per lap sensitivity")
        st.write(", ".join(map(_label, varied["conditional"]["best"])) or "No supported candidate")
    rows = {r["candidate"]: r for r in conditional["rows"]}
    order = conditional["ranked"] + sorted(set(rows)-set(conditional["ranked"]))
    st.dataframe([{"Candidate": _label(k), "Δ vs reference (s)": _seconds(rows[k]["objective_delta_us"]),
                   "Elapsed objective (s)": _seconds(rows[k]["objective_us"]), "Stops": rows[k]["pit_visits"],
                   "Following (s)": _seconds(rows[k]["following_us"]), "Comparison": rows[k]["comparison"],
                   "Support": ", ".join(rows[k]["forecast_support"]) or rows[k]["status"]} for k in order],
                 hide_index=True)
    events = conditional["paired"]["results"]
    one_stop = []
    for name in order:
        pits = events[name]["pit"]
        delta = rows[name]["objective_delta_us"]
        if len(pits) == 1 and delta is not None:
            one_stop.append({"Lap": pits[0]["target_lap"], "Delta (s)": _seconds(delta),
                             "Set": pits[0]["set_id"], "Candidate": _label(name)})
    if one_stop:
        st.plotly_chart(px.scatter(pd.DataFrame(one_stop), x="Lap", y="Delta (s)", color="Set",
                                   hover_name="Candidate", title="Pit lap versus reference time"))
    selected = st.selectbox("Inspect candidate", order, format_func=_label)
    branch = events[selected]
    start = min((r["clock_us"] for r in branch["trajectory"]), default=0)
    marks = rows[selected]["markers"]
    progress = [{"Elapsed (s)": _seconds(r["clock_us"]-start), "Checkpoint": r["checkpoint"],
                 "Driver": str(r["driver"]), "Tyre": r["tyres"]["compound"], "Age": r["tyres"]["age_laps"],
                 "Physical predecessor": r["physical_predecessor"]} for r in branch["trajectory"]
                if r["driver"] in marks and marks[r["driver"]] and r["clock_us"] <= marks[r["driver"]]["clock_us"]]
    if progress:
        st.plotly_chart(px.line(pd.DataFrame(progress), x="Elapsed (s)", y="Checkpoint", color="Driver",
                               hover_data=["Tyre", "Age", "Physical predecessor"], title="Cumulative field progress"))
    st.dataframe([{"Driver": d, "Δ at marker (s)": _seconds(delta)}
                  for d, delta in rows[selected]["driver_delta_us"].items()], hide_index=True)
    if branch["pit"]:
        st.dataframe([{"Driver": p["driver"], "Set": p["set_id"], "Lap": p["target_lap"],
                       "Entry (s)": _seconds(p["entry_us"]), "Service start (s)": _seconds(p["service_start_us"]),
                       "Service end (s)": _seconds(p["service_end_us"]), "Exit (s)": _seconds(p["exit_us"]),
                       "Rejoin (s)": _seconds(p["merge_us"]), "Queue wait (s)": _seconds(p["wait_us"])}
                      for p in branch["pit"]], hide_index=True)
    st.caption(f"Model {result['conditional']['paired']['pace']['source_hashes'][0][1][:12]} · "
               "Reported elapsed time uses each selected driver's common marker.")
