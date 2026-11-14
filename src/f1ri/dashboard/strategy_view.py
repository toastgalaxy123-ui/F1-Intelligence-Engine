"""Local strategy comparison using the existing Streamlit dashboard stack."""
import json
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from f1ri.simulator.strategy import StrategyInputError, compare_strategies, fingerprint
from f1ri.simulator.strategy_constraints import CheckpointConstraints, constraint_summary
from f1ri.simulator.strategy_forecast import StrategyForecastAdapter, read_forecast_inputs
from f1ri.validation.model_strategy_recovery import recover_bundle
from f1ri.validation.strategy_discrimination import cases
from f1ri.validation.strategy_fixtures import strategy_families
from f1ri.validation.strategy_io import scenario_from_document
from f1ri.validation.strategy_sources import audit_model_assets, frozen_pace_fixture


def dashboard():
    st.set_page_config(page_title="F1 Strategy Scenarios", layout="wide")
    st.title("F1 Strategy Scenarios")
    st.caption("Paired comparisons at the same racing markers")
    root = Path(__file__).resolve().parents[3]
    fixtures = strategy_families()
    extended = cases(4909.)
    fixtures.update({k: extended[k][0] for k in ("clean-air", "unblocked", "multi-car")})
    uploaded = st.sidebar.file_uploader("Scenario configuration", type=["json"])
    source = st.sidebar.selectbox("Pace source", ["DECLARED_SYNTHETIC", "FROZEN_PACE_REPLAY", "FULL_MODEL"])
    adapter = None
    status = "DECLARED_SYNTHETIC"
    try:
        if source == "FULL_MODEL":
            path = Path(st.sidebar.text_input("Local Stage 3 bundle folder", value="C:/Projects/f1-race-intelligence"))
            audit, bundle = recover_bundle(root, path)
            if bundle is None:
                st.warning(f"MODEL_UNAVAILABLE · {audit['status']}. Full-model forecasts are unsupported.")
                return
            st.caption(f"FULL_MODEL_VERIFIED · {bundle.model.model_version} · {bundle.artifact_sha256[:12]}")
            feature_file = st.sidebar.file_uploader("Cutoff feature records", type=["json"])
            if not uploaded or not feature_file:
                st.info("REQUEST_UNSUPPORTED · Upload a scenario and its cutoff feature records.")
                return
            features = read_forecast_inputs(json.loads(feature_file.getvalue()))
            stochastic = st.sidebar.checkbox("Use one paired D8 draw")
            index = st.sidebar.number_input("Paired draw index", 0, 999, 0) if stochastic else None
            adapter = StrategyForecastAdapter(bundle, features, seed=42, samples=1000, sample=index)
            s = scenario_from_document(json.loads(uploaded.getvalue()), root, forecast_adapter=adapter)
            status = "FULL_MODEL_VERIFIED"
        elif uploaded:
            s = scenario_from_document(json.loads(uploaded.getvalue()), root)
            status = "FROZEN_PACE_CONTINUATION" if s.pace.mode == "FROZEN_PACE_REPLAY" else status
        elif source == "FROZEN_PACE_REPLAY":
            spec, _, _ = audit_model_assets(root)
            index = st.sidebar.number_input("Saved sample", 0, 999, 0)
            s = frozen_pace_fixture(root, spec, index)
            status = "FROZEN_PACE_CONTINUATION"
        else:
            key = st.sidebar.selectbox("Scenario and race-control schedule", list(fixtures))
            s = fixtures[key]
    except (StrategyInputError, ValueError, OSError) as error:
        st.error(f"REQUEST_UNSUPPORTED · {error}")
        return
    by_driver = {c.driver_number: c for c in s.initial.cars}
    target_options = [str(d) for d, _ in s.targets]
    model_input = fingerprint((adapter.features, adapter.sample, adapter.bundle.artifact_sha256)) if adapter else None
    if len(target_options) > 1:
        target_options.insert(0, "Team combined")
    selected = st.sidebar.selectbox("Evaluation driver or team", target_options)
    if selected != "Team combined":
        s = replace(s, targets=tuple(x for x in s.targets if str(x[0]) == selected))
    choices = [p.strategy_id for p in s.policies]
    ref = st.sidebar.selectbox("Reference", choices)
    alts = [x for x in choices if x != ref]
    alternatives = st.sidebar.multiselect("Alternatives", alts, default=alts[:3])
    st.caption(f"{status} · {s.field_support} · {s.profile.identifier}")
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
            st.session_state["strategy_result"] = (s, model_input, compare_strategies(
                s, ref, alternatives, recorder_factory=CheckpointConstraints, forecast_adapter=adapter))
        except StrategyInputError as error:
            st.error(f"{error.code} · {error}")
    cached = st.session_state.get("strategy_result")
    if not cached:
        return
    used, used_model, result = cached
    if (used != s or used_model != model_input or result["reference"] != ref or
            set(result["results"]) != set(alternatives) | {ref}):
        st.info("Run the comparison to apply the selected inputs.")
        return
    st.subheader("Comparison")
    st.caption(f"{result['overtaking']} · {result['regulatory_scope']} · {result['information']}")
    st.dataframe([{"Strategy": key, "Δ time (s)": row["combined_delta_us"]/1e6
                   if row["combined_delta_us"] is not None else None,
                   "Status": result["results"][key]["status"],
                   "Pit stops": len(result["results"][key]["pit"])}
                  for key, row in result["pairs"].items()])
    tab1, tab2, tab3, tab4, tab5 = st.tabs(["Progress", "Position", "Pit events", "Pace and constraints", "Support"])
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
        marker = dict(used.targets)[target]
        st.dataframe([{"Strategy": key, **constraint_summary(run["checkpoint_constraints"], target, marker)}
                      for key, run in result["results"].items()])
        pace = pd.DataFrame([{"Strategy": key, "Checkpoint": r["checkpoint"], "Clean (s)": r["latent_us"]/1e6,
                              "Effective (s)": r["effective_us"]/1e6, "Constraint": r["category"],
                              "Tyre": r["compound"], "Age": r["tyre_age"]}
                             for key, run in result["results"].items() for r in run["checkpoint_constraints"]
                             if r["driver"] == target and r["latent_us"] is not None and r["checkpoint"] <= marker])
        if not pace.empty:
            st.plotly_chart(px.line(pace, x="Checkpoint", y=["Clean (s)", "Effective (s)"], color="Strategy",
                                   hover_data=["Constraint", "Tyre", "Age"]))
            st.dataframe(pace)
    with tab5:
        st.write("\n".join(f"- {x}" for x in result["assumptions"]))
        if status != "DECLARED_SYNTHETIC":
            st.caption("Pace draws exclude uncertainty about Safety Car deployment, overtaking and empirical traffic effects.")
        forecasts = [{"Strategy": key, "Driver": row["request"]["driver_number"],
                      "Set": row["request"]["compound_relative"], "Start lap": row["request"]["planned_start_lap"],
                      "Support": row.get("forecast", {}).get("support_status", row.get("support")),
                      "Model SHA": row.get("model_sha256"), "Draw": row.get("sample_index")}
                     for key, run in result["results"].items() for row in run.get("forecast_requests", ())]
        if forecasts:
            st.dataframe(forecasts)
        for k, run in result["results"].items():
            if run["unsupported"]:
                st.warning(f"{k} · " + " · ".join(run["unsupported"]))
    st.download_button("Export scenario", json.dumps(asdict(used), default=str, indent=2),
                       "strategy-scenario.json", "application/json")
    st.download_button("Export comparison", json.dumps(result, default=str, indent=2),
                       "strategy-comparison.json", "application/json")
