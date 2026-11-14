"""Audit assets and load a pinned, immutable Pace V2 continuation sample."""
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import yaml

from f1ri.simulator.strategy import StrategyInputError
from f1ri.simulator.strategy_pace import PaceCurve, PaceMode, PaceSource
from f1ri.simulator.strategy_policy import Policy, PolicyKind
from f1ri.validation.historical_neutralization_sources import verify_releases
from f1ri.validation.overtake_historical_study import _load_json
from f1ri.validation.strategy_fixtures import strategy_families
from f1ri.validation.traffic_history_study import _sha

PLAN = "config/experiments/stage5_s5_c_strategy_scenarios.yaml"


def audit_model_assets(root, stage3=None):
    spec = yaml.safe_load((root / PLAN).read_bytes())
    frozen = verify_releases(root, spec["frozen"])
    report = root / "artifacts/reports/stage5_s5_b_historical_neutralization_validation_metrics.json"
    if _sha(report) != spec["frozen"]["s5_b"]:
        raise ValueError("Frozen S5-B metrics differ")
    frozen["s5_b"] = {"sha256": _sha(report), "status": "METRICS_BYTES_VERIFIED"}
    manifest = _load_json(root / "config/experiments/stage5_s1_d8_manifest.json")
    stage3 = stage3 or Path("C:/Projects/f1-race-intelligence")
    assets = {}
    for name, expected in manifest["files_sha256"].items():
        path = stage3 / name
        assets[name] = "MISSING" if not path.is_file() else "VERIFIED" if _sha(path) == expected else "HASH_MISMATCH"
    pace = spec["frozen_pace"]
    found = {name: (root / pace["path"] / name).is_file() for name in pace["files"]}
    verified = all(found.values()) and all(_sha(root / pace["path"] / name) == digest
                                           for name, digest in pace["files"].items())
    warehouses = {"local": root / "data/warehouse/f1ri.duckdb", "stage3": stage3 / "data/warehouse/f1ri.duckdb"}
    return spec, frozen, {
        "full_model": {"status": "UNSUPPORTED",
                       "reason": "D8 bundle missing or unverified" if set(assets.values()) != {"VERIFIED"}
                       else "Candidate-specific model requests and paired replacement streams are not implemented",
                       "bundle_files": assets},
        "frozen_pace_replay": {"status": "VERIFIED_CONTINUATION_ONLY" if verified else "UNAVAILABLE",
                               "files": found},
        "declared_synthetic": {"status": "AVAILABLE"},
        "warehouse": {k: {"available": v.is_file(), "sha256": _sha(v) if v.is_file() else None}
                      for k, v in warehouses.items()},
        "pace_model_schema": "f1ri.models.contracts.StintForecastRequest",
        "trajectory_sampler": "f1ri.models.trajectory_uncertainty.CorrelatedTrajectorySampler",
        "replacement_adapter": "f1ri.simulator.pit_pace",
        "pit_model_artifacts": sorted(p.relative_to(root).as_posix()
                                      for p in (root / "artifacts/models").rglob("*.pkl")),
    }


def frozen_pace_fixture(root, spec, sample=0):
    info = spec["frozen_pace"]
    folder = root / info["path"]
    for name, expected in info["files"].items():
        path = folder / name
        if not path.is_file() or _sha(path) != expected:
            raise StrategyInputError("FROZEN_PACE_UNAVAILABLE", f"Missing or unverified Pace V2 source {name}.")
    snapshot = _load_json(folder / "snapshot.json")
    metrics = _load_json(folder / "metrics.json")
    if metrics["model_sha256"] != spec["frozen"]["d8"] or not 0 <= sample < 1000:
        raise StrategyInputError("FROZEN_PACE_PROVENANCE", "Unknown sample or model identity.")
    base = strategy_families()["green"]
    now = datetime.fromisoformat(snapshot["as_of_utc"])
    epoch = datetime.fromisoformat(snapshot["race_start_utc"])
    at_us = int((now-epoch)/timedelta(microseconds=1))
    requests = {c["driver_number"]: c["request"] for c in snapshot["drivers"]}
    curves = []
    cars = []
    with np.load(folder / "trajectory_samples.npz", allow_pickle=False) as samples:
        for c in base.initial.cars:
            d = c.driver_number
            r = requests[d]
            values = samples[str(d)]
            if values.shape != (1000, r["horizon_laps"]):
                raise ValueError(f"Frozen sample shape mismatch for driver {d}")
            age = r["tyre_age_at_start"]
            lap = r["planned_start_lap"]
            curves.append(PaceCurve(d, "archived-continuation", age, tuple(float(x) for x in values[sample]),
                                    "ARCHIVED_D8_CONTINUATION", lap))
            cars.append(replace(c, completed_checkpoints=(lap-1)*3+1, last_crossing_us=at_us-10_000_000,
                                tyres=replace(c.tyres, compound=r["compound_relative"], age_laps=age,
                                              set_id="archived-continuation")))
    initial = replace(base.initial, race_clock_us=at_us, race_start_utc=epoch, session_key=snapshot["session_key"],
                      simulation_id="singapore-s1-frozen-pace", ruleset=replace(base.initial.ruleset, scheduled_laps=62),
                      cars=tuple(cars))
    length = 4927.
    physical = replace(base.physical, session_key=initial.session_key, as_of_utc=now, length_m=length,
                       layout_version="singapore-declared-loop-4927m",
                       cars=tuple(replace(p, completed_laps=c.completed_laps, progress_m=(.51-i*.04)*length)
                                  for i, c in enumerate(cars) for p in base.physical.cars
                                  if p.driver_number == c.driver_number))
    resources = tuple(replace(r, entry_m=.805*length, tyres=()) for r in base.resources)
    pace = PaceSource(PaceMode.FROZEN_PACE_REPLAY, tuple(curves),
                      tuple(sorted(info["files"].items())), f"D8_SEED_{metrics['seed']}_INDEX_{sample}",
                      "Exact saved Singapore continuation sample applied to a declared two-car field")
    return replace(base, scenario_id="frozen-pace", initial=initial, physical=physical, pace=pace,
                   policies=(Policy("continue", PolicyKind.CONTINUE), Policy("repeat", PolicyKind.CONTINUE)),
                   resources=resources, targets=((63, 96),), horizon_laps=5, end_us=at_us+600_000_000)
