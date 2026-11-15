"""Read-only timing-risk view for the published S6-B.1 study."""
from json import loads

import pandas as pd
import plotly.express as px
import streamlit as st


def commitment_panel(root):
    st.subheader("Neutralization timing and pit commitment")
    st.caption("D8R1 mean-only · Declared scenarios · No event probabilities")
    path = root/"artifacts/reports/stage6_s6_b1_timing_risk.json"
    if not path.is_file():
        st.info("No released timing-risk study is available in this checkout.")
        return
    evidence = loads(path.read_bytes())
    support = evidence["recommendation_support"]
    status = " · ".join(s.replace("_", " ").title() for s in support["statuses"])
    st.caption(status)
    if support["unique_action"] is None:
        st.info("The studied families do not support a unique strategy recommendation.")
    family = st.selectbox("Declared timing family", tuple(evidence["families"]))
    data = evidence["families"][family]
    scenario = st.selectbox("Inspect scenario", tuple(data["outcomes"]))
    rows = []
    events = []
    for policy, r in data["outcomes"][scenario].items():
        pit = next(iter(r.get("visited_pits", ())), None)
        rows.append({"Policy": policy, "Delta vs stay out (s)": _s(r["delta_us"]), "Regret (s)": _s(r["regret_us"]),
                     "Pit lap": pit["target_lap"] if pit else None, "Following (s)": _s(r.get("following_us")),
                     "Support": r["status"]})
        if pit:
            events.extend({"Policy": policy, "Event": label, "Race clock (s)": _s(pit[key])}
                          for label, key in (("Irreversible entry", "entry_us"), ("Service", "service_start_us"),
                                             ("Rejoin", "merge_us")) if pit[key] is not None)
    st.caption(f"{data['description'][scenario]} · {data['coverage']} policy/scenario cells supported")
    st.dataframe(rows, hide_index=True)
    if events:
        st.plotly_chart(px.scatter(pd.DataFrame(events), x="Race clock (s)", y="Policy", color="Event"))
    st.markdown("**Observed timing boundaries**")
    st.dataframe([{"Flag": item["flag"], "Lower (s)": _s(item["observed_interval_us"][0]),
                   "Upper (s)": _s(item["observed_interval_us"][1]), "Width (s)": _s(item["width_us"])}
                  for item in evidence["boundary_transitions"]], hide_index=True)
    st.caption("Boundary endpoints are tested cases. Times between them remain unmeasured.")
    with st.expander("Physical commitment rule"):
        st.write(evidence["commitment_contract"]["revision"])
        st.write(evidence["commitment_contract"]["irreversible"])


def _s(value):
    return value/1e6 if value is not None else None
