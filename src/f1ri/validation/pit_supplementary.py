"""Fixed-model replication after primary-cohort attrition; never reselect models."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import pickle
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from f1ri.ingestion.raw_cache import snapshot_path
from f1ri.validation.pit_backtest import _json, _safe_json, evaluate_pit_model
from f1ri.validation.pit_metrics import evaluate_pit_predictions, paired_pit_comparison
from f1ri.validation.pit_stabilization import (
    _ingest_entries,
    load_stabilization_config,
    prepare_stabilization_data,
    verify_selection_lock,
)


def run_replication(root: Path, *, ingest: bool = False) -> dict:
    cfg, config_path = load_stabilization_config(root)
    lock = verify_selection_lock(root, cfg, config_path)
    plan_path = root/"config/experiments/stage4_1_supplementary_test_plan.json"
    plan = json.loads(plan_path.read_text())
    run = root/"artifacts/runs"/cfg["experiment"]["name"]
    if plan["selection_lock_sha256"] != hashlib.sha256((run/"validation_selection.lock.json").read_bytes()).hexdigest():
        raise ValueError("Supplementary plan refers to another model selection")
    if not plan["no_weight_calibration_eligibility_or_selection_changes"]:
        raise ValueError("Replication must preserve the frozen study")
    access = run/"supplementary_access_log.json"
    plan_sha = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    if access.exists():
        if json.loads(access.read_text())["plan_sha256"] != plan_sha:
            raise ValueError("Supplementary plan changed after access")
    else:
        # The plan is written before these fresh outcome snapshots are ingested.
        for entry in plan["sessions"]:
            if snapshot_path(root, entry["year"], "laps", entry["session_key"]).exists():
                raise ValueError("Supplementary outcomes are already cached before access registration")
        _json(access, {"first_access_at_utc": datetime.now(UTC).isoformat(), "plan_sha256": plan_sha,
                       "runner_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    if ingest:
        _ingest_entries(root, plan["sessions"])
    auxiliary = copy.deepcopy(cfg)
    auxiliary["cohort"]["supplementary_test"] = plan["sessions"]
    events, _, examples = prepare_stabilization_data(
        root, auxiliary, splits=("supplementary_test",), name="supplementary_tests")
    populations = {entry["session_key"]: entry["population"] for entry in plan["sessions"]}
    examples["split"] = examples.session_key.map(lambda key: "supplementary_"+populations[int(key)])
    rows = []
    for name in cfg["validation"]["experiments"]:
        with (root/"artifacts/models"/cfg["experiment"]["name"]/f"{name}.pkl").open("rb") as handle:
            model = pickle.load(handle)
        rows.append(evaluate_pit_model(model, examples, events, name,
                                      n_samples=cfg["experiment"]["n_samples"], seed=cfg["experiment"]["random_seed"]))
    predictions = pd.concat([r for r in rows if not r.empty], ignore_index=True)
    predictions.to_parquet(run/"supplementary_predictions.parquet", index=False)
    result = {"plan": plan, "selected_model_unchanged": lock["selected_model"],
              "supplementary_metrics": evaluate_pit_predictions(predictions)}
    combined = pd.concat([pd.read_parquet(run/"test_predictions.parquet"), predictions], ignore_index=True)
    combined["split"] = combined.split.str.replace("supplementary_", "test_", regex=False)
    result["combined_descriptive_metrics"] = evaluate_pit_predictions(combined)
    result["combined_descriptive_paired_comparisons"] = {
        split: paired_pit_comparison(combined, "P8", "P1", split=split)
        for split in ("test_familiar", "test_unseen")}
    _json(run/"supplementary_results.json", _safe_json(result))
    verify_selection_lock(root, cfg, config_path)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ingest", action="store_true")
    args = parser.parse_args()
    result = run_replication(Path.cwd(), ingest=args.ingest)
    print("Fixed-model supplementary evaluation complete; selection remains", result["selected_model_unchanged"])
