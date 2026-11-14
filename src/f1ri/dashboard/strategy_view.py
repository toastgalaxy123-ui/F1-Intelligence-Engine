"""Local strategy comparison using the existing Streamlit dashboard stack."""
import json
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from f1ri.simulator.strategy import StrategyInputError, compare_strategies
from f1ri.validation.strategy_fixtures import strategy_families
from f1ri.validation.strategy_io import scenario_from_document
from f1ri.validation.strategy_sources import audit_model_assets, frozen_pace_fixture


def dashboard():
    st.set_page_config(page_title="F1 Strategy Scenarios", layout="wide")
    st.title("F1 Strategy Scenarios")
    st.caption("Paired comparisons at the same racing markers")
    root = Path(__file__).resolve().parents[3]
    fixtures = strategy_families()
    uploaded = st.sidebar.file_uploader("Scenario configuration", type=["json"])
    source = st.sidebar.selectbox("Pace source", ["DECLARED_SYNTHETIC", "FROZEN_PACE_REPLAY", "FULL_MODEL"])
    try:
        if uploaded:
            s = scenario_from_document(json.loads(uploaded.getvalue()), root)
        elif source == "FULL_MODEL":
            _, _, assets = audit_model_assets(root)
            st.warning(f"FULL_MODEL unsupported. {assets['full_model']['reason']}.")
            return
        elif source == "FROZEN_PACE_REPLAY":
            spec, _, _ = audit_model_assets(root)
            index = st.sidebar.number_input("Saved sample", 0, 999, 0)
            s = frozen_pace_fixture(root, spec, index)
        else:
            key = st.sidebar.selectbox("Scenario and race-control schedule", list(fixtures))
            s = fixtures[key]
    except (StrategyInputError, ValueError) as error:
        st.error(str(error))
        return
    by_driver = {c.driver_number: c for c in s.initial.cars}
    target_options = [str(d) for d, _ in s.targets]
    if len(target_options) > 1:
        target_options.insert(0, "Team combined")
    selected = st.sidebar.selectbox("Evaluation driver or team", target_options)
    if selected != "Team combined":
        s = replace(s, targets=tuple(x for x in s.targets if str(x[0]) == selected))
    choices = [p.strategy_id for p in s.policies]
    ref = st.sidebar.selectbox("Reference", choices)
    alts = [x for x in choices if x != ref]
    alternatives = st.sidebar.multiselect("Alternatives", alts, default=alts[:3])
    st.caption(f"{s.pace.mode} · {s.field_support} · {s.profile.identifier}")
    left, right = st.columns(2)
    with left:
        st.subheader("Starting field")
        st.dataframe([{"Driver": p.driver_number, "Team": by_driver[p.driver_number].team,
                       "Lap completed": p.completed_laps, "Lap progress (m)": p.progress_m,
                       "Tyre": by_driver[p.driver_number].tyres.compound,
                       "Tyre age": by_driver[p.driver_number].tyres.age_laps} for p in s.physical.cars])
    with right:
        st.subheader("Race control")
        st.dataframe([{"At (s)": e.at_us/1e6, "Command": e.command} for e in s.controls] or
                     [{"Command": "GREEN · no interventions"}])
    if st.sidebar.button("Compare strategies", type="primary"):
        try:
            st.session_state["strategy_result"] = (s, compare_strategies(s, ref, alternatives))
        except StrategyInputError as error:
            st.error(f"{error.code} · {error}")
    cached = st.session_state.get("strategy_result")
    if not cached:
        return
    used, result = cached
    if (used != s or result["reference"] != ref or set(result["results"]) != set(alternatives) | {ref}):
        st.info("Run the comparison to apply the selected inputs.")
        return
    st.subheader("Comparison")
    st.caption(f"{result['overtaking']} · {result['regulatory_scope']} · {result['information']}")
    st.dataframe([{"Strategy": key, "Δ time (s)": row["combined_delta_us"]/1e6
                   if row["combined_delta_us"] is not None else None,
                   "Status": result["results"][key]["status"],
                   "Pit stops": len(result["results"][key]["pit"])}
                  for key, row in result["pairs"].items()])
    tab1, tab2, tab3, tab4 = st.tabs(["Progress", "Position", "Pit events", "Support"])
    target = used.targets[0][0]
    timeline = pd.DataFrame([{"Strategy": k, "Seconds": (r["clock_us"]-used.initial.race_clock_us)/1e6,
                              "Racing progress (m)": r["progress_m"], "Competitive position": r["classification"],
                              "Checkpoint": r["checkpoint"], "Tyre": r["tyres"]["compound"]}
                             for k, run in result["results"].items() for r in run["trajectory"] if r["driver"] == target])
    with tab1:
        if not timeline.empty:
            st.plotly_chart(px.line(timeline, x="Seconds", y="Racing progress (m)", color="Strategy",
                                   hover_data=["Checkpoint", "Tyre"]))
    with tab2:
        if not timeline.empty:
            chart = px.line(timeline, x="Seconds", y="Competitive position", color="Strategy",
                            line_shape="hv")
            chart.update_yaxes(autorange="reversed", dtick=1)
            st.plotly_chart(chart)
    with tab3:
        pits = [{"Strategy": k, **p} for k, run in result["results"].items() for p in run["pit"]]
        st.dataframe(pits) if pits else st.write("No pit entries committed.")
    with tab4:
        st.write("\n".join(f"- {x}" for x in result["assumptions"]))
        for k, run in result["results"].items():
            if run["unsupported"]:
                st.warning(f"{k} · " + " · ".join(run["unsupported"]))
    st.download_button("Export scenario", json.dumps(asdict(used), default=str, indent=2),
                       "strategy-scenario.json", "application/json")
    st.download_button("Export comparison", json.dumps(result, default=str, indent=2),
                       "strategy-comparison.json", "application/json")
