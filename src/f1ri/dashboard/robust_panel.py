"""Explore executable policy performance across declared futures."""

from dataclasses import replace

import pandas as pd
import plotly.express as px
import streamlit as st

from f1ri.simulator.strategy import StrategyInputError
from f1ri.simulator.strategy_optimization import HORIZONS, REFERENCE
from f1ri.simulator.strategy_regret import BEST_REALIZED, PERFECT_INFORMATION
from f1ri.simulator.strategy_robust import INITIAL, ScenarioFamily, evaluate_family
from f1ri.validation.optimization_scenarios import fixture_request, scenario_requests
from f1ri.validation.robust_report import describe_policy, policy_label
from f1ri.validation.robust_scenarios import neutralization, variant
from f1ri.validation.robust_study import verified_robust_model

FAMILY_LABELS = ("Neutralization alternatives", "Physical gap alternatives", "Opponent actions")
OBJECTIVE_LABELS = {"MINIMAX_REGRET": "Minimax regret", "WORST_CASE_RELATIVE_LOSS": "Worst-case relative loss",
                    "PARETO": "Pareto frontier"}
TEAM_LABELS = {"TEAM_COMBINED": "Combined time", "LEAD_DRIVER": "Driver 63 time", "TEAM_WEIGHTED": "Weighted team time"}


def _seconds(value):
    return None if value is None else value/1e6


def robust_panel(root):
    st.subheader("Robust strategy")
    st.caption("MEAN_ONLY_VALIDATED · T1_NO_UNSCRIPTED_PASS · NEUTRALIZATION_PACE_UNCALIBRATED")
    st.caption("HISTORICAL_FIELD_AS_DECLARED · SCENARIO_PROBABILITIES_NOT_ESTIMATED")
    scope = st.sidebar.selectbox("Target", ("Driver 63", "Driver 4", "Team"))
    driver = 4 if scope == "Driver 4" else 63
    team = scope == "Team"
    offered_families = FAMILY_LABELS[:1] if team else FAMILY_LABELS[:2] if driver == 4 else FAMILY_LABELS
    family_label = st.sidebar.selectbox("Scenario family", offered_families)
    fields = ("two-car", "wider-gap", "original-opponent", "clean-air") if family_label == FAMILY_LABELS[0] else (
        ("two-car", "original-opponent") if family_label == FAMILY_LABELS[1] else ("two-car",))
    if driver == 4:
        fields = tuple(k for k in fields if k != "clean-air")
    field = st.sidebar.selectbox("Initial field", ("shared-box",) if team else fields)
    horizon = st.sidebar.selectbox("Evaluation horizon (laps)", HORIZONS, index=1)
    pit_window = st.sidebar.slider("Candidate pit lap offsets", 1, horizon-1, (1, min(3, horizon-1)))
    stops = st.sidebar.selectbox("Maximum total stops", (0, 1, 2), index=2 if team else 1)
    fixtures = scenario_requests(root)
    base = fixture_request(root, field, horizon=horizon, stop_options=pit_window[1], fixtures=fixtures,
                           driver=driver, max_stops=stops, budget=128)
    available = next(r.tyres for r in base.scenario.resources if r.driver == driver)
    sets = {t.set_id: f"{t.compound.title()} · {t.initial_age_laps} laps used" for t in available}
    chosen = st.sidebar.multiselect("Allowed tyre sets", tuple(sets), tuple(sets)[:2] if team else tuple(sets),
                                   format_func=sets.__getitem__)
    weights = tuple((d, st.sidebar.number_input(f"Driver {d} weight", 1, 100, value))
                    for d, value in ((63, 2), (4, 1))) if team else ()
    team_objective = st.sidebar.selectbox("Team objective", tuple(TEAM_LABELS), format_func=TEAM_LABELS.__getitem__
                                         ) if team else base.objective
    windows = tuple(replace(w, first_lap=w.first_lap+pit_window[0]-1, set_ids=tuple(chosen)) for w in base.windows)
    base = replace(base, windows=windows, weights=weights, objective=team_objective)
    if family_label == FAMILY_LABELS[0]:
        included = st.sidebar.multiselect("Future race control", ("GREEN", "VSC", "SC"), ("GREEN", "VSC", "SC"))
        vsc = st.sidebar.slider("VSC deployments after cutoff (s)", 1, 110, (5, 89))
        sc = st.sidebar.slider("SC deployment after cutoff (s)", 1, 90, 5)
        duration = st.sidebar.slider("Longer SC alternative", 45, 125, 97, format="%d s") if "SC" in included else 97
        cases = neutralization(base, vsc=tuple(dict.fromkeys(vsc)) if "VSC" in included else (),
                               sc=(sc,) if "SC" in included else (), green="GREEN" in included,
                               release_offset=duration)
    elif family_label == FAMILY_LABELS[1]:
        cases = (variant(base, "close-gap", "Close declared predecessor", category=INITIAL),
                 variant(base, "wide-gap", "Wider declared predecessor gap", category=INITIAL,
                         physical=fixtures[0]["wider-gap"].physical))
    else:
        cases = (variant(base, "stays", "Opponent stays out"),
                 variant(base, "opponent-pits", "Declared opponent pit action",
                         opponents=fixtures[0]["opponent-pits"].opponents),
                 variant(base, "retirement", "Declared retirement", retirements=fixtures[0]["retirement"].retirements))
    objective = st.sidebar.selectbox("Robust objective", tuple(OBJECTIVE_LABELS), format_func=OBJECTIVE_LABELS.__getitem__)
    comparator = st.sidebar.selectbox("Regret benchmark", (BEST_REALIZED, PERFECT_INFORMATION),
                                     format_func=lambda x: x.replace("_", " ").title())
    budget = st.sidebar.slider("Family simulator budget", 12, 768, 480, step=12)
    family = ScenarioFamily("dashboard", base, cases, total_budget=budget, objective=objective, comparator=comparator)
    if st.sidebar.button("Run robust optimization", type="primary", disabled=not cases):
        try:
            bundle, _ = verified_robust_model(root)
            with st.spinner("Evaluating the same policies across declared futures"):
                result = evaluate_family(family, bundle)
            st.session_state["robust_strategy_result"] = family, result
        except (StrategyInputError, ValueError, OSError) as error:
            st.error(str(error))
            return
    cached = st.session_state.get("robust_strategy_result")
    if cached and cached[0] == family:
        _display(cached[1])


def _display(result):
    data, search = result["analysis"], result["search"]
    st.caption(f"{result['status']} · {search['evaluated_policy_scenario']} / {search['expected_policy_scenario']} "
               f"policy/scenario cells · {search['simulator_evaluations']} simulator evaluations")
    for key in result["presentation"]:
        st.markdown(f"**{policy_label(key)}**")
        st.write(describe_policy(result["policies"][key]))
    if not result["selected"]:
        st.info("This matrix supports no complete recommendation under the selected objective and search budget.")
    if result["near"]:
        st.caption(f"Near-equality margin {result['near_margin_us']/1e6:.2f} s")
        st.write(", ".join(map(policy_label, result["near"])))
    tab_rank, tab_matrices, tab_actions, tab_oracle = st.tabs(("Ranking", "Scenario matrices", "Execution", "Oracle"))
    rows = data["rows"]
    with tab_rank:
        ordered = sorted(rows, key=lambda r: (r["worst_regret_us"] is None, r["worst_regret_us"] or 0, r["policy"]))
        st.dataframe([{"Policy": policy_label(r["policy"]), "Worst regret (s)": _seconds(r["worst_regret_us"]),
                       "Worst delta (s)": _seconds(r["worst_delta_us"]), "Valid cases": r["valid_scenarios"],
                       "Observed rank range": str(r["rank_range"]), "First failure": str(r["first_failure"] or "")}
                      for r in ordered], hide_index=True)
        st.markdown("**Pareto frontier**")
        st.caption(data["frontier_scope"])
        st.write(", ".join(map(policy_label, data["pareto"])) or "No fully comparable policy")
    with tab_matrices:
        for group, title, color in (("deltas", "Time versus the reference", "RdBu_r"),
                                   ("regret", "Regret versus the best realized policy", "YlOrBr"),
                                   ("perfect_information_regret", "Regret versus perfect information", "YlOrBr")):
            frame = pd.DataFrame.from_dict(data[group], orient="index").map(_seconds)
            frame.index = [policy_label(v) for v in frame.index]
            if frame.notna().any().any():
                st.plotly_chart(px.imshow(frame, color_continuous_scale=color, aspect="auto",
                                           labels={"color": "Seconds", "x": "Scenario", "y": "Policy"}, title=title))
            else:
                st.caption(f"{title} is unavailable at this coverage.")
    with tab_actions:
        selected = st.selectbox("Inspect policy", tuple(r["policy"] for r in rows),
                                index=next((i for i, r in enumerate(rows) if r["policy"] in result["presentation"]), 0),
                                format_func=policy_label)
        scene = st.selectbox("Inspect realized scenario", tuple(result["cases"]))
        record = result["cases"][scene]["rows"][selected]
        st.dataframe([{"Scenario": key, "Δ (s)": _seconds(data["deltas"][selected][key]),
                       "Following (s)": _seconds(case["rows"][selected].get("following_us")),
                       "Neutralized (s)": _seconds(case["rows"][selected].get("neutralization_us")),
                       "Support": case["rows"][selected]["status"], "Pit laps": ", ".join(
                           str(r["target_lap"]) for r in case["rows"][selected].get("visited_pits", ()))}
                      for key, case in result["cases"].items()], hide_index=True)
        if paired := result["cases"][scene]["paired"]:
            trace = paired["results"][selected]["trajectory"]
            ref = paired["results"][REFERENCE]["trajectory"]
            lines = []
            for trace_rows, label in ((trace, selected), (ref, REFERENCE)):
                start = min((r["clock_us"] for r in trace_rows), default=0)
                markers = record["markers"]
                lines.extend({"Elapsed (s)": _seconds(p["clock_us"]-start), "Checkpoint": p["checkpoint"],
                              "Driver": str(p["driver"]), "Tyre": p["tyres"]["compound"], "Policy": policy_label(label)}
                             for p in trace_rows if p["driver"] in markers and markers[p["driver"]]
                             and p["checkpoint"] <= markers[p["driver"]]["checkpoint"])
            if lines:
                st.plotly_chart(px.line(pd.DataFrame(lines), x="Elapsed (s)", y="Checkpoint", color="Policy",
                                        line_dash="Driver", hover_data=["Tyre"], title="Progress to the common marker"))
            if record["visited_pits"]:
                pits = [{k: p[k] for k in ("driver", "set_id", "target_lap")} | {
                    "Entry (s)": _seconds(p["entry_us"]), "Arrival (s)": _seconds(p["arrival_us"]),
                    "Service starts (s)": _seconds(p["service_start_us"]), "Exit (s)": _seconds(p["exit_us"]),
                    "Rejoin (s)": _seconds(p["merge_us"]), "Queue (s)": _seconds(p["wait_us"])}
                        for p in record["visited_pits"]]
                st.dataframe(pits, hide_index=True)
    with tab_oracle:
        st.dataframe([{"Scenario": s, "Oracle elapsed (s)": _seconds(row["value_us"]) if row["complete"] else None,
                       "Oracle complete": row["complete"], "Oracle winners": ", ".join(map(policy_label, row["winners"]))
                       if row["complete"] else "",
                       "Best realized": ", ".join(policy_label(p) for p, by in data["regret"].items() if by[s] == 0)}
                      for s, row in result["oracles"].items()], hide_index=True)
