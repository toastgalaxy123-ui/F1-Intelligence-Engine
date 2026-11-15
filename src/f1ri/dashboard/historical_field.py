"""S7-A evidence within the Race State and Evidence views."""

from json import loads
from pathlib import Path

import plotly.express as px
import streamlit as st

from f1ri.decision_board import Field, Provenance
from f1ri.features.historical_field import FieldMode
from f1ri.validation.historical_field_sources import stamp
from f1ri.validation.historical_field_study import PUBLISHED


@st.cache_data(show_spinner=False)
def _published(raw):
    return loads(raw)


def _load(root: Path):
    path = root / PUBLISHED
    return _published(path.read_bytes()) if path.is_file() else None


def _metres(value):
    return round(value, 1) if value is not None else None


def _position_field(row):
    value = row["progress_m"]
    return Field(value, Provenance.DERIVED if value is not None else Provenance.UNAVAILABLE,
                 "; ".join(row["location_sources"]), stamp(row["location_utc"]))


def _rows(field):
    result = []
    for row in field["drivers"]:
        position = _position_field(row)
        neighbor = row["subset_ahead"] or {}
        result.append({"Driver": row["driver_number"], "Lap completed": row["completed_laps"],
                       "Competitive position": row["classification"], "Loop progress (m)": _metres(position.value),
                       "Unwrapped (m)": _metres(row["unwrapped_m"]), "Route": row["route"],
                       "Provenance": position.provenance.value, "Age (s)": row["age_s"],
                       "Observation UTC": position.at_utc, "Physical predecessor": row["predecessor"],
                       "Ahead in observed subset": neighbor.get("driver_number"),
                       "Subset gap (m)": _metres(neighbor.get("gap_m")),
                       "Lower (m)": _metres(neighbor.get("lower_m")),
                       "Upper (m)": _metres(neighbor.get("upper_m")),
                       "Support": row["reason"] or row["ahead_status"]})
    return sorted(result, key=lambda row: (row["Loop progress (m)"] is None, row["Loop progress (m)"] or 0))


def _pace_join(board_snapshot, field):
    """Match evidence context without making the physical pilot simulation-ready."""
    if board_snapshot.cutoff_utc.value is None:
        return None
    if (field["session"] != board_snapshot.session.value or stamp(field["cutoff"]) != board_snapshot.cutoff_utc.value
            or board_snapshot.regulation_era.value["era"] != "2023_2025"):
        return None
    known = {r["identity"].value["number"]: r for r in board_snapshot.drivers
             if r["fitted_tyre"].provenance is Provenance.OBSERVED}
    return [{"driver": row["driver_number"], "lap_count_matches": row["completed_laps"] ==
             known[row["driver_number"]]["lap"].value, "has_supported_track_progress": row["progress_m"] is not None}
            for row in field["drivers"] if row["driver_number"] in known]


def race_state(root, board_snapshot):
    data = _load(root)
    if data is None:
        return
    st.subheader("Historical physical field")
    sessions = {"9896": "Singapore 2025", "9987": "Imola 2025"}
    selected = st.selectbox("Historical race", tuple(sessions), format_func=sessions.__getitem__)
    snapshots = data["snapshots"][selected]
    cutoff = st.selectbox("Historical cutoff", list(snapshots), key=f"historical-cutoff-{selected}")
    mode = st.selectbox("Reconstruction mode", list(FieldMode), format_func=lambda x: x.value.replace("_", " ").title())
    field = snapshots[cutoff][mode]
    st.caption(f"Session {field['session']} · {field['cutoff']} · {mode} · {field['source_availability']}")
    if mode == FieldMode.RECEIPT:
        st.warning("Receipt-qualified reconstruction is unavailable. The archive has no receipt clocks.")
        return
    st.write(f"**{field['support']}** · {field['supported_positions']}/{len(field['field_roster'])} track-local "
             f"positions · {field['supported_predecessors']} full-roster predecessors")
    status = field["track_status"]
    st.caption(f"Reported race-control status {status['value']} at {status['at']}" if status else
               "Track-control status unavailable for this moment in the source subset.")
    if join := _pace_join(board_snapshot, field):
        for row in join:
            st.caption(f"D8R1 cutoff context for #{row['driver']} · "
                       f"Lap anchor {'agrees' if row['lap_count_matches'] else 'unverified or disagrees'} · "
                       f"Track progress {'supported' if row['has_supported_track_progress'] else 'unavailable'}")
    st.info("Historical strategy execution is unavailable. Tyre-set inventory, physical pit routing and the "
            "complete local order have separate evidence requirements.")
    rows = _rows(field)
    plot_rows = [row | {"Car": f"#{row['Driver']}"} for row in rows if row["Loop progress (m)"] is not None]
    if plot_rows:
        st.markdown("**Track-local positions with event-time support**")
        figure = px.scatter(plot_rows, x="Loop progress (m)", y="Car", color="Route",
                            hover_data=["Age (s)", "Lap completed"], height=360)
        figure.update_xaxes(range=[0, field["length_m"]])
        st.plotly_chart(figure)
    st.markdown("**Field and neighbor evidence**")
    st.dataframe(rows, hide_index=True, height=340)
    with st.expander("Cutoff coverage and source timestamps"):
        st.caption(f"{field['layout']} · SHA-256 {field['source_sha256']}")
        st.dataframe([r for r in data["coverage"] if str(r["session"]) == selected and r["mode"] == mode],
                     hide_index=True)
        st.dataframe([{"Driver": row["driver_number"], "Latest fix": row["location_original"],
                       "Last timestamp used": row["maximum_fix_used_utc"], "Lap clock": row["lap_anchor_utc"],
                       "Source": "; ".join(row["location_sources"])} for row in field["drivers"]], hide_index=True)
        timeline_drivers = st.multiselect("Drivers in progress timeline", field["field_roster"],
                                          [d for d in (1, 4, 63) if d in field["field_roster"]])
        timeline = [{"UTC": item[mode]["cutoff"], "Car": f"#{row['driver_number']}",
                     "Track-local progress (m)": row["progress_m"], "Cutoff": label}
                    for label, item in snapshots.items() for row in item[mode]["drivers"]
                    if row["driver_number"] in timeline_drivers and row["progress_m"] is not None]
        if timeline:
            st.plotly_chart(px.scatter(timeline, x="UTC", y="Track-local progress (m)", color="Car",
                                      hover_data=["Cutoff"]))
            st.caption("Each point is a supported cutoff. Observation gaps remain open.")
    if selected == "9987":
        with st.expander("Neutralization and pit-lane evidence"):
            st.dataframe(data["neutralization"], hide_index=True)
            st.plotly_chart(px.scatter(data["pit_trace"], x="elapsed_s", y="projection_error_m", color="corridor",
                                      labels={"elapsed_s": "Seconds from archived lane entry",
                                              "projection_error_m": "Offset from sampled main loop (m)"}))
            st.caption("Car 1 VSC pit visit. Lane timing and nearby main-loop observations are shown separately "
                       "from the unavailable geometric entry and merge.")
            st.dataframe(data["pit_entry"], hide_index=True)
            st.dataframe(data["pit_exit"], hide_index=True)


def evidence(root):
    data = _load(root)
    if data is None:
        return
    with st.expander("Historical field provenance and readiness"):
        st.caption(f"{data['version']} · {data['release']} · LIVE_UNSUPPORTED")
        st.json({"geometry": data["geometry"], "live_readiness": data["live_readiness"]}, expanded=False)
        st.dataframe([{key: r[key] for key in ("source", "records", "sha256", "earliest", "latest", "receipt")}
                      for r in data["sources"]["sources"]], hide_index=True)
        bundle = root / "artifacts/reports/stage7_s7_a_evidence.zip"
        if bundle.is_file():
            st.download_button("Download historical field evidence", bundle.read_bytes(), bundle.name,
                               "application/zip", on_click="ignore")
