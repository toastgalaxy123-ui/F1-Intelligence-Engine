"""Read archived S5-A scenario inputs that were verified against the release ZIP."""
from datetime import datetime

from f1ri.simulator.neutralization import simulate_neutralization
from f1ri.simulator.proximity import PhysicalRoute, PhysicalSnapshot, PositionQuality, TrackPosition
from f1ri.simulator.race_control import ControlCommand, ControlPhase, RaceControlEvent
from f1ri.simulator.state import (
    CarState,
    CarStatus,
    RacePhase,
    RaceRuleset,
    RaceState,
    Route,
    SimulationMode,
    TrackState,
    TyreState,
)
from f1ri.validation.overtake_historical_study import _load_json


def _state(data):
    return RaceState(**(data | {
        "race_start_utc": datetime.fromisoformat(data["race_start_utc"]),
        "ruleset": RaceRuleset(**data["ruleset"]), "mode": SimulationMode(data["mode"]),
        "phase": RacePhase(data["phase"]), "track_state": TrackState(data["track_state"]),
        "cars": tuple(CarState(**(c | {"route": Route(c["route"]), "status": CarStatus(c["status"]),
                                      "tyres": TyreState(**c["tyres"])})) for c in data["cars"]),
    }))


def _physical(data):
    return PhysicalSnapshot(**(data | {
        "as_of_utc": datetime.fromisoformat(data["as_of_utc"]),
        "cars": tuple(TrackPosition(**(c | {
            "route": PhysicalRoute(c["route"]), "quality": PositionQuality(c["quality"]),
            "observed_at_utc": datetime.fromisoformat(c["observed_at_utc"]) if c["observed_at_utc"] else None,
        })) for c in data["cars"]),
    }))


def _command(data):
    return RaceControlEvent(**(data | {
        "command": ControlCommand(data["command"]),
        **{k: datetime.fromisoformat(data[k]) if data[k] else None for k in ("observation_utc", "available_utc")},
        **{k: ControlPhase(data[k]) if data[k] else None for k in ("previous", "requested")},
        "local_flag": TrackState(data["local_flag"]) if data["local_flag"] else None,
        "affected_cars": tuple(data["affected_cars"]),
    }))


def archived_replay(baseline, profile):
    definitions = _load_json(baseline/"scenario_manifest.json")["archived"]
    evidence = _load_json(baseline/"source_manifest.json")["historical"]
    runs = {}
    for name, data in definitions.items():
        for k in ("start_utc", "end_utc"):
            evidence[name]["interval"][k] = datetime.fromisoformat(evidence[name]["interval"][k])
        for k, clock in (("observed_lines", "utc"), ("observed_route_events", "utc"), ("observed_messages", "date")):
            for row in evidence[name][k]:
                row[clock] = datetime.fromisoformat(row[clock])
        state = _state(data["initial"])
        physical = _physical(data["physical"])
        end = (evidence[name]["interval"]["end_utc"]-state.race_start_utc).total_seconds()
        runs[name] = simulate_neutralization(
            state, physical, {int(d): p for d, p in data["declared_pace_s"].items()},
            tuple(map(_command, data["commands"])), laps=18, end_us=round((end+200)*1e6),
            profile=profile, individual_restart=True,
        )
    return runs, evidence
