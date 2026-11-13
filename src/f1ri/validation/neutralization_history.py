"""Replay approved archived control clocks against declared mechanical fields."""
from dataclasses import replace
from datetime import timedelta

import duckdb

from f1ri.simulator.neutralization import simulate_neutralization
from f1ri.simulator.race_control import ControlCommand as C
from f1ri.simulator.race_control import RaceControlEvent
from f1ri.simulator.replay import replay_session
from f1ri.simulator.state import RaceRuleset, TrackState
from f1ri.validation.overtake_scenarios import field

APPROVED_RACES = {9896: ("Singapore", 62), 9987: ("Imola", 63)}


def _read(con, sql, key):
    rows = con.execute(sql, [key])
    columns = [c[0] for c in rows.description]
    return [dict(zip(columns, row, strict=True)) for row in rows.fetchall()]


def historical_cases(root, warehouse, profile):
    controls, runs, evidence = {}, {}, {}
    with duckdb.connect(str(warehouse), read_only=True) as con:
        con.execute("SET TimeZone='UTC'")
        for key, (name, laps) in APPROVED_RACES.items():
            intervals = _read(con, "SELECT * FROM gold.track_status_intervals WHERE session_key=? ORDER BY start_utc", key)
            messages = _read(con, "SELECT * FROM silver.race_control WHERE session_key=? ORDER BY date, message", key)
            relevant = intervals[1:]
            if not relevant:
                raise ValueError(f"Missing frozen race-control intervals for {name}")
            retrospective = replay_session(warehouse, key, laps, annotations_root=root)
            for i, interval in enumerate(relevant):
                status = TrackState(interval["status"])
                prior = TrackState(intervals[i]["status"])
                if status is TrackState.GREEN:
                    continue
                following = relevant[i+1] if i+1 < len(relevant) else None
                start = interval["start_utc"]
                epoch = start - timedelta(seconds=5)
                actual = [f.state for f in retrospective.run.trace
                          if retrospective.run.final_state.race_start_utc+timedelta(microseconds=f.event.at_us) <= epoch]
                snapshot = actual[-1] if actual else retrospective.run.trace[0].state
                active = [c for c in snapshot.cars if c.status.value == "ACTIVE" and c.route.value == "ON_TRACK"]
                order = [next((c for c in active if c.driver_number == d), None) for d in snapshot.timing_order]
                order = [c for c in order if c is not None]
                layout = {"Imola": 4909., "Singapore": 4927.}[name]
                # Declared within-sector spacing tests the observed clocks.
                # Timing laps and ranks give context, with no observed physical
                # gap substitution into the simulated trajectory.
                ident = f"{name.lower()}_{status.value.lower()}_{i}"
                declared, physical = field(tuple((c.driver_number, 1, 280-j*12)
                                                 for j, c in enumerate(order)), ident)
                declared = replace(
                    declared, session_key=key, race_start_utc=epoch, ruleset=RaceRuleset(2025, laps),
                    cars=tuple(replace(d, completed_checkpoints=c.completed_laps*3, tyres=c.tyres)
                               for d, c in zip(declared.cars, order, strict=True)),
                )
                physical = replace(physical, session_key=key, as_of_utc=epoch, length_m=layout,
                                   layout_version=f"{name.lower()}-declared-s5-a",
                                   cars=tuple(replace(p, completed_laps=c.completed_laps, progress_m=1400-j*65)
                                              for j, (p, c) in enumerate(zip(physical.cars, order, strict=True))))
                plan = []
                before = prior
                if status in (TrackState.VSC, TrackState.SC):
                    plan.append((C.VSC if status is TrackState.VSC else C.SC, start, interval, {}))
                    for msg in messages:
                        if not start < msg["date"] < interval["end_utc"]:
                            continue
                        body = (msg["message"] or "").upper()
                        if "VIRTUAL SAFETY CAR ENDING" in body:
                            plan.append((C.VSC_ENDING, msg["date"], interval, {}))
                        elif "SAFETY CAR IN THIS LAP" in body:
                            plan.append((C.SC_IN, msg["date"], interval, {}))
                        elif "LAPPED CAR" in body and "OVERTAKE" in body:
                            plan.append((C.UNLAP, msg["date"], interval, {}))
                    if following and following["status"] == "GREEN":
                        clear = following["start_utc"]
                        if status is TrackState.SC:
                            plan.append((C.SC_WITHDRAWN, clear, following, {}))
                        plan.append((C.GREEN, clear, following, {}))
                elif status in (TrackState.YELLOW, TrackState.DOUBLE_YELLOW):
                    plan.append((C.LOCAL, start, interval, {"scope": "SECTOR", "local_flag": status}))
                    if following and following["status"] == "GREEN":
                        plan.append((C.LOCAL, following["start_utc"], following,
                                     {"scope": "SECTOR", "local_flag": TrackState.GREEN}))
                    elif following and following["status"] in ("SC", "VSC"):
                        # End the local scope as the full-field sequence starts
                        # in its own replay window.
                        plan.append((C.LOCAL, following["start_utc"], following,
                                     {"scope": "SECTOR", "local_flag": TrackState.GREEN}))
                else:
                    continue
                events = []
                for j, (command, stamp, source, options) in enumerate(plan):
                    at = (stamp-epoch) // timedelta(microseconds=1)
                    matched = [r for r in messages if r["date"] == stamp and r["message"]]
                    events.append(RaceControlEvent(
                        f"{j:03d}-{command.name}", ident, command, at, source["source_rule"],
                        source="ARCHIVED_RACE_CONTROL_REPLAY", observation_utc=stamp,
                        message=" | ".join(r["message"] for r in matched),
                        confidence=source["confidence"], **options,
                    ))
                end = (interval["end_utc"]-epoch)//timedelta(microseconds=1) + 200_000_000
                finish = interval["end_utc"] + timedelta(seconds=200)
                pace = {c.driver_number: 94. for c in order}
                runs[ident] = simulate_neutralization(declared, physical, pace, events, laps=18, end_us=end,
                                                      profile=profile)
                controls[ident] = {"initial": declared, "physical": physical, "declared_pace_s": pace, "commands": events}
                observed_lines = [{"utc": retrospective.run.final_state.race_start_utc
                                   + timedelta(microseconds=f.event.at_us), "driver": f.event.driver_number,
                                   "completed_laps": f.event.checkpoint//3}
                                  for f in retrospective.run.trace if f.event.kind.name == "LAP_COMPLETED"
                                  and f.event.driver_number in pace
                                  and epoch <= retrospective.run.final_state.race_start_utc
                                  + timedelta(microseconds=f.event.at_us) <= finish]
                pit_frames = [{"utc": retrospective.run.final_state.race_start_utc
                               + timedelta(microseconds=f.event.at_us), "driver": f.event.driver_number,
                               "kind": f.event.kind.name}
                              for f in retrospective.run.trace if f.event.kind.name in ("PIT_ENTRY", "PIT_EXIT", "RETIREMENT")
                              and epoch <= retrospective.run.final_state.race_start_utc
                              + timedelta(microseconds=f.event.at_us) <= finish]
                evidence[ident] = {
                    "session": key, "race": name, "previous": before, "interval": interval,
                    "availability": "RETROSPECTIVE_ONLY", "observation_clocks": "ARCHIVE_RECEIPT_UNKNOWN",
                    "initialization": "ARCHIVED_LAPS_DECLARED_PHYSICAL_SPACING_AND_PACE_ACTIVE_TRACK_CARS",
                    "observed_lines": observed_lines, "observed_route_events": pit_frames,
                    "observed_messages": [r for r in messages if epoch <= r["date"] <= finish],
                    "supported_global_status": status in (TrackState.VSC, TrackState.SC),
                }
    return controls, runs, evidence
