"""Attach outcomes to independently generated pit scenarios; never use outcomes as inputs."""
from __future__ import annotations

import duckdb
import pandas as pd

from f1ri.features.pit_context import build_pit_opportunity
from f1ri.features.pit_events import integer, number, replace_session_frame, utc


def build_pit_examples(
    *, events: pd.DataFrame, observations: pd.DataFrame, frames: dict[str, pd.DataFrame],
    split: str, reference: pd.DataFrame | None = None,
    include_realized_used_scenarios: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    contexts, examples = [], []
    if events.empty:
        return pd.DataFrame(), pd.DataFrame()
    session = frames["sessions"].iloc[0].to_dict()
    for event in events.to_dict("records"):
        inlap = integer(event.get("resolved_in_lap"))
        if inlap is None:
            continue
        drv = frames["drivers"][frames["drivers"].driver_number == event["driver_number"]]
        if drv.empty:
            continue
        laps = frames["laps"]
        prev = laps[(laps.driver_number == event["driver_number"]) & (laps.lap_number < inlap)].copy()
        if prev.empty:
            continue
        prev["_end"] = pd.to_datetime(prev.date_start, utc=True) + pd.to_timedelta(prev.lap_duration, unit="s")
        prev = prev.dropna(subset=["_end"]).sort_values("lap_number")
        if prev.empty:
            continue
        cutoff = utc(prev.iloc[-1]["_end"])
        if utc(event["transition_start_utc"]) is None or cutoff > utc(event["transition_start_utc"]) + pd.Timedelta(seconds=2):
            continue
        obs = observations[(observations.pit_event_id == event["pit_event_id"])
                           & (observations.measurement_method == "TWO_LAP_CROSS_FIT")]
        label = obs.iloc[0].to_dict() if not obs.empty else {}
        # Include a fixed scenario grid even if the driver chose a different tire.
        # The actual compound is consulted ONLY below when attaching outcomes.
        scenarios = [(c, 0) for c in ("HARD", "MEDIUM", "SOFT")]
        actual_age = integer(event["new_tyre_initial_age"])
        actual_compound = event.get("new_compound")
        if (include_realized_used_scenarios and actual_age is not None and actual_age >= 0
                and actual_compound in {"HARD", "MEDIUM", "SOFT", "INTERMEDIATE", "WET"}
                and (actual_compound, actual_age) not in scenarios):
            # Explicitly conditional on this tire scenario, not a forecast of
            # what the team will choose. The hypothetical fixed grid is intact.
            scenarios.append((actual_compound, actual_age))
        for compound, initial_age in scenarios:
            try:
                _request, context = build_pit_opportunity(
                    session=session, driver=drv.iloc[0].to_dict(), laps=laps,
                    stints=frames["stints"], race_control=frames["race_control"],
                    weather=frames["weather"], as_of_utc=cutoff,
                    intended_pit_lap=inlap, new_compound=compound,
                    new_tyre_initial_age=initial_age,
                    completed_events=events, position=frames["position"],
                    intervals=frames["intervals"], reference=reference,
                )
            except ValueError:
                continue
            context["pit_event_id"] = event["pit_event_id"]
            contexts.append(context)
            realized = compound == event["new_compound"] and actual_age == initial_age
            examples.append({
                **context, "split": split,
                "is_realized_tire_scenario": realized,
                "target_lane_duration_s": number(event["lane_duration_s"]) if realized else None,
                "target_stop_duration_s": number(event["stop_duration_s"]) if realized else None,
                "target_transition_loss_s": number(label.get("transition_loss_s")) if realized else None,
                "label_std_s": number(label.get("label_std_s")) if realized else None,
                "observation_available_utc": utc(event["transition_end_utc"]),
                "eligible_primary_label": bool(label.get("eligible_primary_label", False)) and realized
                and _request.track_status == "GREEN",
                "pit_event_type": event["pit_event_type"],
                "measurement_method": label.get("measurement_method"),
            })
    return pd.DataFrame(contexts), pd.DataFrame(examples)


def populate_pit_examples(con: duckdb.DuckDBPyConnection, session_key: int, **kwargs) -> pd.DataFrame:
    from f1ri.warehouse.schema import PIT_CONTEXT_SCHEMA, PIT_EXAMPLE_SCHEMA
    contexts, examples = build_pit_examples(**kwargs)
    replace_session_frame(con, "gold.pit_context", contexts, PIT_CONTEXT_SCHEMA, session_key)
    replace_session_frame(con, "gold.pit_training_examples", examples, PIT_EXAMPLE_SCHEMA, session_key)
    return examples
