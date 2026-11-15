"""Archived, observed-collector and controlled receipt cutoffs in the decision board."""

from json import loads

import streamlit as st


@st.cache_data(show_spinner=False)
def _decode(raw):
    return loads(raw)


def source_panel(root):
    st.subheader("Source Availability & Replay")
    path = root/"artifacts/reports/stage7_s7_b_dashboard.json"
    if not path.is_file():
        st.info("No frozen source availability study is installed.")
        return
    data = _decode(path.read_bytes())
    modes = ("Historical event time", "Observed collector receipts", "Synthetic delivery experiment")
    mode = st.radio("Observation mode", modes, horizontal=True, key="source-replay-mode")
    if mode == modes[0]:
        historical = data["historical"]
        race = st.selectbox("Archived cutoff", tuple(historical), format_func=lambda text: text.replace("_", " ").title())
        row = historical[race]
        st.caption(f"{row['mode']} · {row['receipt']} · Session {row['session']}")
        st.write(f"**Source event cutoff (UTC)**　{row['source_event_cutoff']}")
        st.write(f"{row['localized']}/{row['roster']} positions supported by the frozen historical reconstruction.")
        st.info("Historical receipt and provider publication clocks are unavailable.")
        return
    if mode == modes[1]:
        capture = data["capture"]
        st.caption(f"RETROSPECTIVE_RETRIEVAL · Session {capture['session']} · Capture {capture['capture_id'][:12]}")
        st.info("Real collector receipts were recorded when re-querying the archived race. Racing event freshness "
                "and original provider publication are unmeasured.")
        rows = capture["summary"]
        selection = "observed"
    else:
        choice = st.selectbox("Controlled delivery case", tuple(data["synthetic"]),
                              format_func=lambda text: text.replace("_", " ").title())
        rows = data["synthetic"][choice]
        selection = choice
        st.warning("SYNTHETIC_DELIVERY_EXPERIMENT · Injected timing and failures.")
    cutoffs = {row["receipt_cutoff"]: row for row in rows}
    t = st.select_slider("Virtual receipt cutoff (UTC)", options=list(cutoffs), value=next(iter(cutoffs)),
                         key=f"source-clock-{selection}")
    snapshot = cutoffs[t]
    col = st.columns(3)
    col[0].metric("Visible record versions", snapshot["records"])
    col[1].metric("Feeds with a receipt", sum(row["responses"] > 0 for row in snapshot["endpoints"].values()))
    col[2].metric("Endpoints withholding field input",
                  sum(not row["field_input_allowed"] for row in snapshot["endpoints"].values()))
    st.caption(f"Decision UTC {snapshot['decision_utc']} · Source event cutoff UTC {snapshot['event_cutoff']}")
    st.caption(f"Receipt prefix {snapshot['prefix_sha256'][:16]} · {data['strategy_support']}")
    st.dataframe([{"Endpoint": ep, "Support": ", ".join(row["status"]),
                   "Last response UTC": row["last_response_complete_utc"],
                   "Latest event UTC": row["latest_source_event_utc"], "Response age (s)": row["response_age_s"],
                   "Event age (s)": row["event_age_s"], "Missing or unverified drivers":
                   ", ".join(map(str, row["missing_or_unverified_drivers"])),
                   "Errors": row["errors"], "Conflicts": row["conflicts"]}
                  for ep, row in snapshot["endpoints"].items()], hide_index=True)
    with st.expander("Observation versions and first-seen clocks"):
        if snapshot["examples"]:
            st.dataframe([{key: row[key] for key in ("endpoint", "source_event_utc", "first_seen_utc", "usable_utc",
                                                     "version", "changed_fields")}
                          for row in snapshot["examples"]], hide_index=True)
            st.json(snapshot["examples"], expanded=False)
        else:
            st.caption("No observations are visible at this cutoff.")
    if mode == modes[2] and selection == "delayed_sc":
        st.subheader("Safety Car policy receipt")
        st.dataframe([{"Injected delay (s)": row["delay_s"], "Physical deployment (s)": row["physical_effect_us"]/1e6,
                       "Policy receipt (s)": row["policy_receipt_us"]/1e6,
                       "Requested pit lap": row["pit_entries"][0]["target_lap"] if row["pit_entries"] else None}
                      for row in data["delay"]["runs"]], hide_index=True)
        st.caption("The physical control script is fixed across all three runs.")
    archive = root/"artifacts/reports/stage7_s7_b_evidence.zip"
    if archive.is_file():
        st.download_button("Download source availability evidence", archive.read_bytes(), archive.name,
                           "application/zip", on_click="ignore")
