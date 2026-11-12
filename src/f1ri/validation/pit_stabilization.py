"""Stage 4.1: preserve the reviewed run, stabilize labels, lock validation, test once.

No race simulation or Stage 5 components are implemented here.
"""
from __future__ import annotations

import hashlib
import json
import pickle
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml

from f1ri.features.pit_context import request_from_json
from f1ri.features.pit_events import (
    add_transition_evidence,
    load_pit_annotations,
    populate_session_pit_events,
    replace_session_frame,
)
from f1ri.features.pit_examples import populate_pit_examples
from f1ri.features.pit_policy import PitEligibilityPolicy, apply_pit_eligibility
from f1ri.ingestion.pit_lane_reference import populate_pit_lane_reference
from f1ri.ingestion.raw_cache import read_snapshot, snapshot_path
from f1ri.models.pit_calibration import CalibratedPitLossModel
from f1ri.models.pit_loss import PitLossModelV1
from f1ri.models.pit_uncertainty import empirical_crps
from f1ri.validation.pit_backtest import _json, _safe_json, evaluate_pit_model
from f1ri.validation.pit_measurement import NormalLapReference, measure_pit_transitions
from f1ri.validation.pit_metrics import evaluate_pit_predictions, paired_pit_comparison
from f1ri.validation.pit_neutralization_measurement import measure_neutralized_pit_windows
from f1ri.warehouse.build import build_warehouse
from f1ri.warehouse.schema import PIT_OBSERVATION_SCHEMA


def load_stabilization_config(root: Path, path: Path | None = None) -> tuple[dict, Path]:
    path = path or Path("config/experiments/stage4_1_stabilization.yaml")
    path = path if path.is_absolute() else root/path
    cfg = yaml.safe_load(path.read_text())
    protocol = root/"config/experiments/stage4_1_preregistered_plan.json"
    if hashlib.sha256(protocol.read_bytes()).hexdigest() != cfg["experiment"]["preregistered_plan_sha256"]:
        raise ValueError("Preregistered Stage 4.1 plan has changed")
    return cfg, path


def _entries(cfg: dict, splits: tuple[str, ...]) -> list[dict]:
    return [{**e, "split": split} for split in splits for e in cfg["cohort"].get(split, [])]


def validate_study_design(cfg: dict) -> None:
    entries = _entries(cfg, ("train", "val", "test"))
    keys = [e["session_key"] for e in entries]
    if len(keys) != len(set(keys)):
        raise ValueError("Training, validation and reserved tests must be disjoint")
    for role in ("train", "val", "test"):
        if not cfg["cohort"].get(role):
            raise ValueError(f"Missing {role} population")
    populations = {e.get("population") for e in cfg["cohort"]["test"]}
    if populations != {"familiar", "unseen"}:
        raise ValueError("Reserved tests require separate familiar and unseen populations")


def _raw_frames(root: Path, year: int, key: int) -> dict[str, pd.DataFrame]:
    return {name: pd.DataFrame(read_snapshot(snapshot_path(root, year, name, key)))
            for name in ("laps", "race_control")}


def reclassify_reviewed_run(root: Path, output: Path, policy: PitEligibilityPolicy) -> dict:
    """Re-score fixed V1 predictions with ONE policy; never refit to old test errors."""
    baseline = root/"artifacts/runs/stage4_pit_loss_v1"
    events = pd.read_parquet(baseline/"pit_events.parquet")
    groups = []
    for (year, sid), group in events.groupby(["season", "session_key"]):
        raw = _raw_frames(root, int(year), int(sid))
        group = add_transition_evidence(group, raw["laps"], raw["race_control"])
        groups.append(apply_pit_eligibility(group, policy))
    screened = pd.concat(groups, ignore_index=True)
    predictions = pd.read_parquet(baseline/"predictions.parquet")
    columns = ["eligible_direct_lane", "eligible_direct_stationary", "eligible_primary_label",
               "routine_status", "classification_flags_json", "eligibility_exclusion_reasons_json"]
    joined = predictions.drop(columns=[c for c in columns if c in predictions]).merge(
        screened[["pit_event_id", *columns]], on="pit_event_id", validate="many_to_one")
    joined["eligible_primary_label"] &= joined.target_transition_loss_s.notna()
    joined["eligible_primary_label"] &= joined.track_status_at_cutoff.eq("GREEN")
    screened.to_parquet(output/"reviewed_events_reclassified.parquet", index=False)
    joined.to_parquet(output/"reviewed_predictions_reclassified.parquet", index=False)
    before, after = evaluate_pit_predictions(predictions), evaluate_pit_predictions(joined)
    raw_red = screened.classification_flags_json.str.contains("RED_FLAG_OVERLAP")
    report = {"policy": policy.to_dict(), "events_retained": len(screened),
              "red_overlap_events": int(raw_red.sum()),
              "red_overlap_still_eligible": int(screened.loc[raw_red, "eligible_direct_lane"].sum()),
              "direct_lane_eligible_before": int(events.eligible_direct_lane.sum()),
              "direct_lane_eligible_after": int(screened.eligible_direct_lane.sum()),
              "raw_metrics": before, "ordinary_policy_metrics": after}
    _json(output/"reviewed_run_reclassification.json", _safe_json(report))
    primary = pd.read_parquet(baseline/"pit_event_labels.parquet")
    primary = primary[primary.measurement_method == "TWO_LAP_CROSS_FIT"]
    audit = primary.merge(screened, on=["pit_event_id", "session_key"], suffixes=("_label", "_event"))
    audit["abs_method_disagreement_s"] = audit.method_disagreement_s.abs()
    columns = ["pit_event_id", "session_key", "circuit_short_name", "lane_duration_s", "stop_duration_s",
               "transition_loss_s", "matched_control_loss_s", "method_disagreement_s",
               "classification_flags_json", "net_label_exclusion_reasons_json", "eligible_primary_label_event"]
    audit.sort_values("abs_method_disagreement_s", ascending=False).reindex(columns=columns).to_csv(
        output/"reviewed_label_audit.csv", index=False)
    return report


def prepare_stabilization_data(
    root: Path, cfg: dict, *, splits: tuple[str, ...], name: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    entries = _entries(cfg, splits)
    output = root/"artifacts/runs"/cfg["experiment"]["name"]/name
    output.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for entry in entries:
        folder = snapshot_path(root, entry["year"], "sessions", entry["session_key"]).parent
        for endpoint in ("sessions", "drivers", "laps", "stints", "pit", "weather", "race_control",
                         "session_result", "starting_grid", "position", "intervals"):
            path = folder/f"{endpoint}.json"
            if not path.exists():
                raise FileNotFoundError(f"Missing Stage 4.1 input {path}")
            hashes[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        print(f"Stage 4.1 build {entry['session_key']}", flush=True)
        build_warehouse(root, entry["year"], entry["session_key"])
    all_events, all_labels, all_examples, neutral = [], [], [], []
    con = duckdb.connect(str(root/"data/warehouse/f1ri.duckdb"))
    try:
        refs = populate_pit_lane_reference(con, root)
        annotations = load_pit_annotations(root)
        development_keys = [e["session_key"] for e in _entries(cfg, ("train", "val"))]
        all_laps = con.execute("SELECT * FROM gold.lap_context").df()
        reference_laps = all_laps[all_laps.session_key.isin(development_keys)]
        sessions = con.execute("SELECT * FROM silver.sessions").df().set_index("session_key")
        ordered = sorted(entries, key=lambda e: sessions.loc[e["session_key"], "date_start"])
        for entry in ordered:
            sid = entry["session_key"]
            frames = {n: con.execute(f"SELECT * FROM silver.{n} WHERE session_key=?", [sid]).df()
                      for n in ("sessions", "drivers", "laps", "stints", "race_control", "weather", "position", "intervals")}
            events = populate_session_pit_events(con, sid, annotations)
            if events.empty:
                continue
            start = sessions.loc[sid, "date_start"]
            past = reference_laps[(reference_laps.session_key != sid)
                                  & (pd.to_datetime(reference_laps.lap_end_estimate_utc, utc=True) < start)]
            primary = NormalLapReference().fit(past, start)
            robust = NormalLapReference(estimator_kind="huber").fit(past, start)
            labels = measure_pit_transitions(events, all_laps[all_laps.session_key == sid], primary, robust)
            state_intervals = con.execute("SELECT * FROM gold.track_status_intervals WHERE session_key=?", [sid]).df()
            neutral.append(measure_neutralized_pit_windows(events, all_laps[all_laps.session_key == sid], state_intervals))
            replace_session_frame(con, "gold.pit_transition_observations", labels, PIT_OBSERVATION_SCHEMA, sid)
            examples = populate_pit_examples(
                con, sid, events=events, observations=labels, frames=frames,
                split=entry["split"], reference=refs,
                include_realized_used_scenarios=cfg["validation"]["include_realized_used_scenarios"],
            )
            all_events.append(events)
            all_labels.append(labels)
            all_examples.append(examples)
            print(f"Stage 4.1 labels {sid}: {len(events)} visits, {int(labels.eligible_primary_label.sum())} ordinary green estimates", flush=True)
    finally:
        con.close()
    if not all_events:
        raise ValueError("No Stage 4.1 visits reconstructed")
    events = pd.concat(all_events, ignore_index=True)
    labels = pd.concat(all_labels, ignore_index=True)
    examples = pd.concat(all_examples, ignore_index=True)
    for frame, file in ((events, "events"), (labels, "labels"), (examples, "examples")):
        frame.to_parquet(output/f"{file}.parquet", index=False)
    pd.concat(neutral, ignore_index=True).to_parquet(output/"neutralization_audit.parquet", index=False)
    _json(output/"data_manifest.json", {"snapshots_sha256": hashes, "policy": cfg["eligibility"],
                                       "primary_reference": "past-race-centered-ridge-reference-v1",
                                       "supporting_reference": "past-race-centered-huber-reference-v2",
                                       "reference_fit_population": "Development races strictly earlier than the target; no test race fits nuisance parameters"})
    return events, labels, examples


def fitting_frame(events: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    primary = labels[labels.measurement_method == "TWO_LAP_CROSS_FIT"]
    frame = events.drop(columns="eligible_primary_label").merge(primary, on=["session_key", "pit_event_id"], validate="one_to_one")
    frame["eligible_primary_label"] &= frame.transition_loss_s.notna()
    return frame


def _model_source_hashes(root: Path) -> dict[str, str]:
    files = [*sorted((root/"src/f1ri/models").glob("pit*.py")),
             root/"src/f1ri/features/pit_policy.py", root/"src/f1ri/features/pit_events.py",
             root/"src/f1ri/features/pit_context.py", root/"src/f1ri/features/pit_examples.py",
             root/"src/f1ri/validation/pit_measurement.py", root/"src/f1ri/validation/pit_metrics.py",
             root/"src/f1ri/validation/pit_backtest.py", root/"src/f1ri/warehouse/schema.py",
             root/"src/f1ri/ingestion/pit_lane_reference.py", Path(__file__),
             *sorted((root/"data/raw/reference").glob("*.csv"))]
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def _base_cases(model: PitLossModelV1, examples: pd.DataFrame, cfg: dict) -> list[tuple[dict, object]]:
    cases = []
    for row in examples[examples.is_realized_tire_scenario.fillna(False)].to_dict("records"):
        request = replace(request_from_json(row["request_json"]), retain_samples=True,
                          n_samples=int(cfg["experiment"]["n_samples"]),
                          random_seed=int(cfg["experiment"]["random_seed"]))
        cases.append((row, model.predict_pit_loss(request)))
    return cases


def _scale_score(cases: list[tuple[dict, object]], scale: float) -> float:
    scores: dict[int, list[float]] = {}
    for row, forecast in cases:
        if not row["eligible_primary_label"] or pd.isna(row["target_transition_loss_s"]):
            continue
        reference = np.asarray(forecast.sampled_reference_errors_s)
        physical = np.asarray(forecast.sampled_losses_s)+reference
        samples = physical.mean()+scale*(physical-physical.mean())-reference
        score = empirical_crps(samples, row["target_transition_loss_s"])
        scores.setdefault(int(row["session_key"]), []).append(score)
    return float(np.mean([np.mean(s) for s in scores.values()])) if scores else float("inf")


def _choose_scale(cases: list[tuple[dict, object]], scales: list[float]) -> float:
    eligible = [row for row, _ in cases if row["eligible_primary_label"]]
    if len(eligible) < 10 or len({row["session_key"] for row in eligible}) < 2:
        return 1.0
    return min(scales, key=lambda scale: (_scale_score(cases, scale), abs(np.log(scale))))


def _available_through(cases: list[tuple[dict, object]]) -> datetime | None:
    dates = [pd.Timestamp(row["observation_available_utc"]) for row, _ in cases
             if pd.notna(row["observation_available_utc"])]
    return max(dates).to_pydatetime() if dates else None


def validate_and_lock(root: Path, cfg: dict, config_path: Path) -> dict:
    validate_study_design(cfg)
    run = root/"artifacts/runs"/cfg["experiment"]["name"]
    if (run/"test_access_log.json").exists():
        raise ValueError("Reserved tests have been opened; do not retune this study on their outcomes")
    dev = run/"development"
    events, labels, examples = [pd.read_parquet(dev/f"{f}.parquet") for f in ("events", "labels", "examples")]
    fit = fitting_frame(events, labels)
    train_keys = [e["session_key"] for e in cfg["cohort"]["train"]]
    training = fit[fit.session_key.isin(train_keys)]
    validation = examples[examples.split == "val"]
    if validation.empty:
        raise ValueError("No chronological validation examples")
    cutoff = pd.to_datetime(validation.forecast_as_of_utc, utc=True).min().to_pydatetime()
    direct = training[training.eligible_direct_stationary.fillna(False)]
    service_counts = direct.groupby(["session_key", "circuit_short_name", "team_id"]).size().reset_index(name="measured_stops")
    service_counts.to_csv(run/"service_training_provenance.csv", index=False)
    service_audit = {"measured_service_events": len(direct), "independent_races": int(direct.session_key.nunique()),
                     "circuits": int(direct.circuit_short_name.nunique()), "teams": int(direct.team_id.nunique()),
                     "missing_values_not_imputed_as_observations": True}
    _json(run/"service_training_audit.json", service_audit)
    models = {}
    validation_rows, folds = [], []
    scales = cfg["validation"]["calibration_scales"]
    for name in cfg["validation"]["experiments"]:
        print(f"Stage 4.1 validate {name}", flush=True)
        base = PitLossModelV1(experiment_id=name, **cfg["model"]).fit(training, training_cutoff_utc=cutoff)
        prefix: list[tuple[dict, object]] = []
        # Explicit chronology guards callers that re-order saved example rows.
        race_order = validation.groupby("session_key").forecast_as_of_utc.min().sort_values().index
        for sid in race_order:
            target = validation[validation.session_key == sid]
            scale = _choose_scale(prefix, scales) if name == "P8" else 1.0
            through = _available_through(prefix) if name == "P8" else None
            if through is not None and through >= pd.to_datetime(target.forecast_as_of_utc, utc=True).min():
                raise ValueError("Calibration fold uses a future validation race")
            candidate = CalibratedPitLossModel(base, scale, through)
            prediction = evaluate_pit_model(candidate, target, events, name,
                                            n_samples=cfg["experiment"]["n_samples"], seed=cfg["experiment"]["random_seed"])
            validation_rows.append(prediction)
            folds.append({"model_id": name, "target_session_key": int(sid), "scale": scale,
                          "calibration_training_sessions": sorted({int(r["session_key"]) for r, _ in prefix}),
                          "calibration_trained_through_utc": through})
            prefix.extend(_base_cases(base, target, cfg))
        final_scale = _choose_scale(prefix, scales) if name == "P8" else 1.0
        final_through = _available_through(prefix) if name == "P8" else None
        models[name] = CalibratedPitLossModel(base, final_scale, final_through)
    predictions = pd.concat([p for p in validation_rows if not p.empty], ignore_index=True)
    predictions.to_parquet(run/"validation_predictions.parquet", index=False)
    metrics = evaluate_pit_predictions(predictions)
    _json(run/"validation_metrics.json", _safe_json(metrics))
    _json(run/"calibration_folds.json", folds)
    scores = {}
    for name, model in models.items():
        block = metrics[name]["val"]
        net, tail = block["net_loss_proxy"], block["slow_stop_calibration"]
        scores[name] = {"race_weighted_crps_s": net.get("race_weighted_crps_s", float("inf")),
                        "race_weighted_mae_s": net.get("race_weighted_mae_s", float("inf")),
                        "final_scale": model.scale,
                        "calibrated_through_utc": model.calibrated_through_utc,
                        "models_service_tail": bool(tail and tail.get("predicted_slow_rate", 0) > 0),
                        "service_brier_score": tail.get("brier_score"),
                        "coverage_50": net.get("coverage_50"), "coverage_90": net.get("coverage_90"),
                        "mean_width_90_s": net.get("mean_width_90_s")}
    simple = min(("P0", "P1", "P3"), key=lambda name: scores[name]["race_weighted_crps_s"])
    if not np.isfinite(scores[simple]["race_weighted_crps_s"]):
        raise ValueError("No evaluable chronological validation cases; cannot lock a model")
    probabilistic = [name for name in models if scores[name]["models_service_tail"]
                     and scores[name]["race_weighted_mae_s"] <= 1.10*scores[simple]["race_weighted_mae_s"]
                     and service_audit["independent_races"] >= 3]
    best = min(probabilistic, key=lambda name: scores[name]["race_weighted_crps_s"]) if probabilistic else None
    if best is not None and scores[best]["race_weighted_crps_s"] <= scores[simple]["race_weighted_crps_s"]:
        selected, role = best, "PROBABILISTIC_RESEARCH_CANDIDATE"
    else:
        selected, role = simple, "SIMPLE_NET_LOSS_FALLBACK_NO_APPROVED_SERVICE_RISK"
    folder = root/"artifacts/models"/cfg["experiment"]["name"]
    folder.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name, model in models.items():
        path = folder/f"{name}.pkl"
        with path.open("wb") as handle:
            pickle.dump(model, handle)
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    lock = {"locked_at_utc": datetime.now(UTC).isoformat(), "selected_model": selected,
            "selected_role": role, "model_scores": scores, "model_artifact_sha256": hashes,
            "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "model_and_policy_source_sha256": _model_source_hashes(root),
            "development_data_manifest_sha256": hashlib.sha256((dev/"data_manifest.json").read_bytes()).hexdigest(),
            "development_artifact_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                            for p in dev.glob("*.parquet")},
            "selection_rule": "Prequential race-weighted CRPS; tail-capable candidate must retain MAE within 10% of best simple benchmark; otherwise declare simple net-only fallback",
            "calibration_rule": "P8 only; scale learned from strictly earlier validation races, minimum two prior races and ten cases; final refit uses validation only",
            "holdouts_opened_during_selection": False}
    _json(run/"validation_selection.lock.json", _safe_json(lock))
    return lock


def verify_selection_lock(root: Path, cfg: dict, config_path: Path) -> dict:
    """Refuse to open tests if selection, sources, data or fitted models drifted."""
    run = root/"artifacts/runs"/cfg["experiment"]["name"]
    lock = json.loads((run/"validation_selection.lock.json").read_text())
    if hashlib.sha256(config_path.read_bytes()).hexdigest() != lock["config_sha256"]:
        raise ValueError("Study configuration changed after selection")
    if _model_source_hashes(root) != lock["model_and_policy_source_sha256"]:
        raise ValueError("Model, reference or policy sources changed after selection")
    for file, expected in lock["development_artifact_sha256"].items():
        if hashlib.sha256((run/"development"/file).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Development data changed after selection: {file}")
    for model, expected in lock["model_artifact_sha256"].items():
        path = root/"artifacts/models"/cfg["experiment"]["name"]/f"{model}.pkl"
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Frozen model changed after selection: {model}")
    return lock


def verify_preserved_references(root: Path, run: Path) -> dict:
    result = {}
    for name in ("stage3_frozen_reference.json", "stage4_accounting_frozen_reference.json"):
        record = json.loads((root/"config/experiments"/name).read_text())
        for file, expected in record["files_sha256"].items():
            if hashlib.sha256((root/file).read_bytes()).hexdigest() != expected:
                raise ValueError(f"Frozen reference changed: {file}")
        result[name] = "UNCHANGED"
    record = json.loads((run/"baseline_manifest.json").read_text())
    for file, expected in record["files_sha256"].items():
        if hashlib.sha256((root/file).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Reviewed Stage 4 artifact changed: {file}")
    result["reviewed_stage4_artifacts"] = "UNCHANGED"
    return result


def _ingest_entries(root: Path, entries: list[dict]) -> None:
    from f1ri.cli import ingest
    for entry in entries:
        ingest(root, entry["year"], entry["session_key"], full=True, refresh=False)


def evaluate_locked_holdouts(root: Path, cfg: dict, config_path: Path, *, ingest: bool = False) -> dict:
    lock = verify_selection_lock(root, cfg, config_path)
    run = root/"artifacts/runs"/cfg["experiment"]["name"]
    verify_preserved_references(root, run)
    access_path = run/"test_access_log.json"
    lock_sha = hashlib.sha256((run/"validation_selection.lock.json").read_bytes()).hexdigest()
    if access_path.exists():
        if json.loads(access_path.read_text())["selection_lock_sha256"] != lock_sha:
            raise ValueError("Reserved tests were opened under another selection lock")
    else:
        # Persist BEFORE ingestion, warehouse access or looking at a test outcome.
        _json(access_path, {"first_access_at_utc": datetime.now(UTC).isoformat(),
                            "selection_lock_sha256": lock_sha, "test_sessions": cfg["cohort"]["test"],
                            "retuning_permitted": False})
    if ingest:
        _ingest_entries(root, _entries(cfg, ("test",)))
    events, _labels, examples = prepare_stabilization_data(root, cfg, splits=("test",), name="reserved_tests")
    development_events = pd.read_parquet(run/"development/events.parquet")
    training_keys = [e["session_key"] for e in cfg["cohort"]["train"]]
    familiar = set(development_events[development_events.session_key.isin(training_keys)].circuit_short_name)
    populations = {e["session_key"]: e["population"] for e in cfg["cohort"]["test"]}
    for sid, block in events.groupby("session_key"):
        actual_familiar = block.iloc[0].circuit_short_name in familiar
        if actual_familiar != (populations[int(sid)] == "familiar"):
            raise ValueError("Reserved population disagrees with actual circuit fitting support")
    validation_start = pd.to_datetime(examples.forecast_as_of_utc, utc=True).min()
    if validation_start <= pd.Timestamp(lock["model_scores"]["P8"].get("calibrated_through_utc", "1900-01-01T00:00:00Z")):
        raise ValueError("Test precedes validation calibration")
    examples["split"] = examples.session_key.map(lambda key: "test_"+populations[int(key)])
    examples.to_parquet(run/"reserved_tests/examples.parquet", index=False)
    rows = []
    for name in cfg["validation"]["experiments"]:
        print(f"Stage 4.1 frozen test {name}", flush=True)
        with (root/"artifacts/models"/cfg["experiment"]["name"]/f"{name}.pkl").open("rb") as handle:
            model = pickle.load(handle)
        rows.append(evaluate_pit_model(model, examples, events, name,
                                      n_samples=cfg["experiment"]["n_samples"], seed=cfg["experiment"]["random_seed"]))
    predictions = pd.concat([r for r in rows if not r.empty], ignore_index=True)
    predictions.to_parquet(run/"test_predictions.parquet", index=False)
    metrics = evaluate_pit_predictions(predictions)
    _json(run/"test_metrics.json", _safe_json(metrics))
    comparisons = {}
    for split in ("test_familiar", "test_unseen"):
        comparisons[split] = {name: paired_pit_comparison(predictions, name, "P1", split=split)
                              for name in sorted({"P8", lock["selected_model"]})}
    _json(run/"paired_test_comparisons.json", _safe_json(comparisons))
    return {"events": len(events), "metrics": metrics, "paired_comparisons": comparisons}


def run_stage4_stabilization(root: Path, config_path: Path | None = None, *, phase: str = "all", ingest: bool = False) -> dict:
    cfg, path = load_stabilization_config(root, config_path)
    validate_study_design(cfg)
    run = root/"artifacts/runs"/cfg["experiment"]["name"]
    run.mkdir(parents=True, exist_ok=True)
    if phase in {"prepare", "all"}:
        if (run/"test_access_log.json").exists():
            raise ValueError("Test outcomes are already open; preserve this study and use report/evaluate only")
        if ingest:
            _ingest_entries(root, _entries(cfg, ("train", "val")))
        reclassify_reviewed_run(root, run, PitEligibilityPolicy(**cfg["eligibility"]))
        prepare_stabilization_data(root, cfg, splits=("train", "val"), name="development")
    if phase in {"validate", "all"}:
        validate_and_lock(root, cfg, path)
    if phase in {"evaluate", "all"}:
        evaluate_locked_holdouts(root, cfg, path, ingest=ingest)
    if phase in {"report", "all"}:
        from f1ri.validation.pit_stabilization_report import write_stabilization_report
        write_stabilization_report(root, cfg)
    return {"run_directory": str(run), "phase": phase,
            "selection_lock_exists": (run/"validation_selection.lock.json").exists()}
