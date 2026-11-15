"""Historical route and order evidence in the existing decision board."""

from json import loads

import plotly.express as px
import streamlit as st

from f1ri.features.historical_field import FieldMode
from f1ri.validation.pit_branch_sources import DASHBOARD, REPORTS


@st.cache_data(show_spinner=False)
def _data(raw):
    return loads(raw)


def _published(root):
    source = root/DASHBOARD
    return _data(source.read_bytes()) if source.is_file() else None


def _table(rows, keys):
    st.dataframe([{k.replace("_", " ").title(): row[k] for k in keys} for row in rows],
                 hide_index=True, height=285)


def _field(data, session, label, mode):
    rows = [r for r in data["route_comparison"] if r["session"] == session and r["mode"] == mode]
    choices = tuple(dict.fromkeys(r["label"] for r in rows))
    selected = st.selectbox("Route evidence cutoff", choices, choices.index(label) if label in choices else 0,
                            key=f"pit-branch-{session}-{label}")
    current = [r for r in rows if r["label"] == selected]
    st.caption(f"{mode} · {current[0]['cutoff']} · Observation event clocks")
    if mode == FieldMode.CAUSAL:
        st.caption("The causal view requires both the geometry fit and its held checks to predate this cutoff.")
    added = sum(r["strict_route"] != r["revised_route"] for r in current)
    st.write(f"{added} changed route assignments on identical location observations.")
    _table(current, ("driver", "strict_route", "revised_route", "revised_track_m", "lane_progress_m",
                     "racing_residual_m", "pit_residual_m", "route_reason", "match_reason"))
    proofs = [p for p in data["certificates"] if p["session"] == session and p["label"] == selected
              and p["mode"] == mode]
    car = st.selectbox("Inspect predecessor certificate", [r["target"] for r in proofs])
    proof = next(r for r in proofs if r["target"] == car)
    st.write(f"**#{car} · {proof['status']}**")
    first, second, bound = st.columns(3)
    first.metric("Certified predecessor", str(proof["certified"] or "Unavailable"))
    second.metric("Mapped candidates", ", ".join(map(str, proof["candidates"])) or "Unavailable")
    gap = proof["separation_interval_m"]
    bound.metric("Mapped gap bounds (m)", f"{gap[0]:.1f}–{gap[1]:.1f}" if gap else "Unavailable")
    st.caption("Possible interveners: " + (", ".join(map(str, proof["possible_interveners"])) or "None in this proof"))
    if proof["exclusions"]:
        st.json(proof["exclusions"], expanded=False)
    if session == 9896 and selected == "Archived model cutoff":
        st.markdown("**Singapore D8R1 cutoff drivers**")
        _table(data["singapore"], ("driver", "status", "age_s", "candidate_loop_m", "racing_residual_m",
                                   "pit_residual_m", "geometry_failure"))


def _visits(data, session):
    rows = [r for r in data["entry"] if r["session"] == session]
    visit = st.selectbox("Retrospective pit visit", [r["visit"] for r in rows])
    entry = next(r for r in rows if r["visit"] == visit)
    merge = next(r for r in data["merge"] if r["visit"] == visit)
    st.caption("Retrospective evidence · Time brackets describe a confirmed change of route. "
               "Geometric entry and merge points remain unavailable.")
    _table([entry | {"transition": "Entry"}, merge | {"transition": "Merge"}],
           ("transition", "timing_report_utc", "earliest_utc", "latest_utc", "width_s", "quality", "reason"))
    candidates = merge["neighbors"]
    st.write(f"**{candidates['status']}**")
    st.caption("These are route-change-window hypotheses. A later car identity supplies no certified merge order.")
    st.write(f"Ahead candidates {candidates['ahead']} · Behind candidates {candidates['behind']}")
    if visit == "imola-vsc-1":
        st.plotly_chart(px.scatter(data["trace"], x="seconds_from_lane_entry", y="racing_residual_m",
                                    color="qualified", hover_data=["pit_residual_m", "stationary_candidate"],
                                    labels={"seconds_from_lane_entry": "Seconds from reported lane entry",
                                            "racing_residual_m": "Racing-line residual (m)"}))


def _geometry(data, session):
    graph = data["graphs"][str(session)]
    source = data["geometry"][str(session)]
    st.caption(f"{graph['layout']} · {source['status']}")
    paths = [{"X": a, "Y": b, "Route": route} for route, row in graph["nodes"].items()
             for a, b in row.get("points_xy", ())]
    chart = px.line(paths, x="X", y="Y", color="Route", labels={"X": "OpenF1 X", "Y": "OpenF1 Y"})
    chart.update_yaxes(scaleanchor="x")
    if pit := graph["nodes"]["PIT_LANE"].get("points_xy"):
        chart.update_xaxes(range=[min(p[0] for p in pit)-650, max(p[0] for p in pit)+650])
        chart.update_yaxes(range=[min(p[1] for p in pit)-650, max(p[1] for p in pit)+650])
    st.plotly_chart(chart)
    st.json({k: v for k, v in source.items() if k != "fia_source"}, expanded=False)
    st.caption("Historical strategy remains unavailable. Pit geometry, full relevant order, tyre inventory "
               "and service-box state have separate requirements.")


def panel(root, session, cutoff, mode):
    data = _published(root)
    if data is None or mode == FieldMode.RECEIPT:
        return
    with st.expander("Pit routes and predecessor evidence · S7-A.1"):
        st.caption(data["release"])
        field, visits, geometry = st.tabs(("Cutoff field", "Retrospective pit visits", "Geometry and readiness"))
        with field:
            _field(data, int(session), cutoff, mode)
        with visits:
            _visits(data, int(session))
        with geometry:
            _geometry(data, int(session))


def provenance(root):
    data = _published(root)
    if data is None:
        return
    with st.expander("Pit-branch evidence and historical strategy gates · S7-A.1"):
        st.json({"geometry": data["geometry"], "strategy": data["strategy_support"], "live": data["live"]},
                expanded=False)
        bundle = root/REPORTS/"stage7_s7_a1_evidence.zip"
        if bundle.is_file():
            st.download_button("Download S7-A.1 evidence", bundle.read_bytes(), bundle.name, "application/zip",
                               on_click="ignore")
