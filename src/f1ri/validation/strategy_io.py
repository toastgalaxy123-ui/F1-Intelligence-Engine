"""Parse and export the versioned strategy scenario document."""
from dataclasses import asdict
from pathlib import Path

import yaml

from f1ri.simulator.events import Event, EventKind
from f1ri.simulator.overtaking import OvertakeAttempt
from f1ri.simulator.pit_execution import ReplacementTyres, ResolvedPitRoute
from f1ri.simulator.proximity import PhysicalRoute
from f1ri.simulator.race_control import RaceControlProfile
from f1ri.simulator.state import TrackState
from f1ri.simulator.strategy import StrategyInputError, StrategyScenario, validate_scenario
from f1ri.simulator.strategy_pace import PaceCurve, PaceMode, PaceSource
from f1ri.simulator.strategy_policy import DriverResources, Policy, PolicyKind, Stop
from f1ri.validation.frozen_neutralization_inputs import _command, _physical, _state
from f1ri.validation.proximity_pilot import _json


def scenario_from_document(doc, project_root=None):
    def policy(row):
        return Policy(row["strategy_id"], PolicyKind(row["kind"]), tuple(Stop(**x) for x in row["stops"]))

    try:
        source = doc["pace"]
        scenario = validate_scenario(StrategyScenario(**(doc | {
            "initial": _state(doc["initial"]),
            "physical": _physical(doc["physical"]),
            "resources": tuple(DriverResources(**(r | {
                "tyres": tuple(ReplacementTyres(**t) for t in r["tyres"]),
                "route": ResolvedPitRoute(**r["route"]),
                "prior_dry_compounds": tuple(r["prior_dry_compounds"]),
            })) for r in doc["resources"]),
            "pace": PaceSource(**(source | {
                "mode": PaceMode(source["mode"]),
                "curves": tuple(PaceCurve(**(c | {"lap_seconds": tuple(c["lap_seconds"])})) for c in source["curves"]),
                "source_hashes": tuple(map(tuple, source["source_hashes"])),
            })),
            "policies": tuple(map(policy, doc["policies"])),
            "opponents": tuple(map(policy, doc["opponents"])),
            "targets": tuple(map(tuple, doc["targets"])),
            "controls": tuple(map(_command, doc["controls"])),
            "retirements": tuple(Event(x["at_us"], EventKind(x["kind"]), x["driver_number"], source=x["source"])
                                 for x in doc["retirements"]),
            "passes": tuple(OvertakeAttempt(**(x | {"route": PhysicalRoute(x["route"]),
                                                   "track_state": TrackState(x["track_state"])})) for x in doc["passes"]),
            "profile": RaceControlProfile(**doc["profile"]),
            "mandatory_compounds": tuple(doc["mandatory_compounds"]),
        })))
        if scenario.pace.mode is PaceMode.FROZEN_PACE_REPLAY:
            from f1ri.validation.strategy_sources import audit_model_assets, frozen_pace_fixture

            if project_root is None:
                raise StrategyInputError("FROZEN_INPUT_UNVERIFIED", "Provide the project root to verify the archived draw.")
            sample = int(scenario.pace.stream_id.rsplit("_", 1)[-1])
            pinned = frozen_pace_fixture(project_root, audit_model_assets(project_root)[0], sample)
            if (scenario.pace != pinned.pace or scenario.initial != pinned.initial or
                    scenario.physical != pinned.physical):
                raise StrategyInputError("FROZEN_INPUT_UNVERIFIED", "The archived draw and cutoff must match the pinned fixture.")
        return scenario
    except StrategyInputError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise StrategyInputError("SCENARIO_DOCUMENT", str(error)) from error


def load_scenario(path, project_root=None):
    try:
        doc = yaml.safe_load(Path(path).read_bytes())
    except yaml.YAMLError as error:
        raise StrategyInputError("SCENARIO_DOCUMENT", str(error)) from error
    return scenario_from_document(doc, project_root)


def write_comparison(folder, scenario, result):
    folder.mkdir(parents=True, exist_ok=False)
    _json(folder / "scenario.json", asdict(scenario))
    _json(folder / "comparison.json", result)
    _json(folder / "metrics.json", {key: value for key, value in result.items() if key != "results"})
    best = result["ranking"][0][1] if result["ranking"] else None
    first = ", ".join(name for name, delta in result["ranking"] if delta == best) or "No supported comparison"
    (folder / "report.md").write_text(
        f"# Strategy comparison: {scenario.scenario_id}\n\n"
        f"First under the common-marker time objective: **{first}**.\n\n"
        f"Pace source: {result['pace_mode']}. Field: {result['field_support']}. Passing: {result['overtaking']}.\n\n"
        "Results are conditional on the declared inputs. See the comparison ledger for events and support.\n",
        encoding="utf-8", newline="\n",
    )
