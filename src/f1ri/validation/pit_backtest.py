"""Stage 4 runner: reconstruct first, freeze splits, fit on past races, then evaluate."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import pickle
import subprocess
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml

from f1ri.features.pit_context import request_from_json
from f1ri.features.pit_events import (
    load_pit_annotations,
    populate_session_pit_events,
    replace_session_frame,
)
from f1ri.features.pit_examples import populate_pit_examples
from f1ri.ingestion.batch import load_experiment_cohort
from f1ri.ingestion.pit_lane_reference import populate_pit_lane_reference
from f1ri.ingestion.raw_cache import snapshot_path
from f1ri.models.pit_loss import PitLossModelV1
from f1ri.models.pit_uncertainty import empirical_crps
from f1ri.validation.leakage import validate_pit_forecast_timestamp
from f1ri.validation.pit_measurement import NormalLapReference, measure_pit_transitions
from f1ri.validation.pit_metrics import evaluate_pit_predictions, paired_pit_comparison
from f1ri.validation.quality import check_pit_events
from f1ri.validation.splits import build_split_manifest, validate_stage4_holdout
from f1ri.warehouse.build import build_warehouse
from f1ri.warehouse.schema import PIT_OBSERVATION_SCHEMA


def _git_revision(root: Path) -> str:
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()


def _json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, default=str, allow_nan=False)+"\n")


def _safe_json(value):
    """Use strict JSON null for missing floating point statistics."""
    if isinstance(value, dict):
        return {str(k): _safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json(v) for v in value]
    if isinstance(value, (float, np.floating)) and not np.isfinite(value):
        return None
    if isinstance(value, np.integer):
        return int(value)
    return value


def evaluate_pit_model(
    model: PitLossModelV1, examples: pd.DataFrame, events: pd.DataFrame,
    model_id: str, *, n_samples: int = 1000, seed: int = 42,
) -> pd.DataFrame:
    rows = []
    if examples.empty:
        return pd.DataFrame()
    selected = examples[examples.is_realized_tire_scenario.fillna(False)]
    lookup = events.set_index("pit_event_id")
    for example in selected.to_dict("records"):
        request = replace(request_from_json(example["request_json"]),
                          n_samples=n_samples, random_seed=seed, retain_samples=True)
        prediction = model.predict_pit_loss(request)
        validate_pit_forecast_timestamp(request, prediction)
        event = lookup.loc[example["pit_event_id"]]
        row = {k: v for k, v in asdict(prediction).items() if not k.startswith("sampled_")}
        row.update({k: example[k] for k in (
            "pit_event_id", "session_key", "split", "forecast_as_of_utc", "track_status_at_cutoff",
            "target_lane_duration_s", "target_stop_duration_s", "target_transition_loss_s",
            "label_std_s", "eligible_primary_label", "pit_event_type",
        )})
        row.update(model_id=model_id, circuit_short_name=request.circuit_short_name,
                   season=request.season, team_id=request.team_id,
                   timing_quality=event.timing_quality,
                   pit_sequence=int(event.pit_sequence), eligible_direct_lane=bool(event.eligible_direct_lane),
                   pit_lap_bucket=f"{request.intended_pit_lap//10*10}-{request.intended_pit_lap//10*10+9}",
                   pit_speed_limit_kph=request.pit_speed_limit_kph,
                   historical_support_bucket="0" if prediction.n_circuit_events == 0 else (
                       "1-19" if prediction.n_circuit_events < 20 else "20+"),
                   service_category="MISSING" if pd.isna(example["target_stop_duration_s"]) else (
                       "SLOW" if example["target_stop_duration_s"] > 4.5 else "ORDINARY"))
        target = example["target_transition_loss_s"]
        row["crps_s"] = empirical_crps(np.asarray(prediction.sampled_losses_s), target) if pd.notna(target) else None
        row["residual_s"] = target-prediction.expected_loss_s if pd.notna(target) else None
        # Keep structured diagnostics as JSON, not mixed Arrow object columns.
        row["assumptions"] = json.dumps(row["assumptions"])
        row["exclusions"] = json.dumps(row["exclusions"])
        rows.append(row)
    return pd.DataFrame(rows)


def _audit(events: pd.DataFrame, labels: pd.DataFrame) -> dict:
    audit = check_pit_events(events)
    slices = {}
    for column in ("circuit_short_name", "season", "team_id", "transition_track_status",
                   "quality_category", "pit_event_type", "timing_quality"):
        slices[column] = {str(k): {"n_events": len(v), "n_races": int(v.session_key.nunique()),
                                   "stationary_measured": int(v.stop_duration_s.notna().sum())}
                          for k, v in events.groupby(column, dropna=False)}
    primary = labels[labels.measurement_method == "TWO_LAP_CROSS_FIT"]
    disagreements = primary.method_disagreement_s.dropna()
    audit.update(slices=slices, reconstructed_labels=int(primary.transition_loss_s.notna().sum()),
                 primary_training_labels=int(primary.eligible_primary_label.sum()),
                 independent_control_checks=len(disagreements),
                 median_absolute_method_disagreement_s=float(disagreements.abs().median()) if len(disagreements) else None,
                 label_error_cold_start_investigation="Separate past-race reference used; frozen Pace V2 cold-start bias is not fitted into pit coefficients")
    return audit


def run_stage4_pipeline(root: Path, config_path: Path | None = None) -> dict:
    path = config_path or Path("config/experiments/stage4_pit_loss_v1.yaml")
    path = path if path.is_absolute() else root/path
    cfg = yaml.safe_load(path.read_text())
    entries = load_experiment_cohort(root, path)
    if not entries:
        raise ValueError("Stage 4 requires a nonempty frozen cohort")
    # Check all inputs before mutating the warehouse; never present missing data
    # as zero-event success or silently shrink a final evaluation cohort.
    endpoints = ["sessions", "drivers", "laps", "stints", "pit", "weather", "race_control",
                 "session_result", "starting_grid", "position", "intervals"]
    missing = [str(snapshot_path(root, e["year"], ep, e["session_key"]))
               for e in entries for ep in endpoints
               if not snapshot_path(root, e["year"], ep, e["session_key"]).exists()]
    if missing:
        raise FileNotFoundError(f"Missing {len(missing)} snapshots; run ingest-batch with the Stage 4 config. First: {missing[0]}")
    for role in ("train", "val", "test"):
        if not any(e["split"] == role for e in entries):
            raise ValueError(f"Stage 4 requires a nonempty {role} cohort")
    frozen = root/"config/experiments/stage3_frozen_reference.json"
    if frozen.exists():
        for file, expected in json.loads(frozen.read_text())["files_sha256"].items():
            if hashlib.sha256((root/file).read_bytes()).hexdigest() != expected:
                raise ValueError(f"Frozen Stage 3 reference changed: {file}")
    run = root/"artifacts/runs"/cfg["experiment"]["name"]
    run.mkdir(parents=True, exist_ok=True)
    (run/"config.yaml").write_text(path.read_text())
    input_hashes = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for e in entries for ep in endpoints
                    for p in [snapshot_path(root, e["year"], ep, e["session_key"]) ]}
    for p in (root/"data/raw/reference").glob("*.csv"):
        input_hashes[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    _json(run/"data_manifest.json", {"files_sha256": input_hashes,
                                  "pit_lane_reference_sha256": hashlib.sha256((root/"data/raw/reference/pit_lane_reference.csv").read_bytes()).hexdigest()})
    # Preserve all other session data; the existing builder replaces per-session tables.
    for entry in entries:
        print(f"Stage 4 build session {entry['session_key']}", flush=True)
        build_warehouse(root, entry["year"], entry["session_key"])
    con = duckdb.connect(str(root/"data/warehouse/f1ri.duckdb"))
    all_labels, all_examples, event_frames = [], [], []
    try:
        refs = populate_pit_lane_reference(con, root)
        annotations = load_pit_annotations(root)
        sessions = con.execute("SELECT * FROM silver.sessions").df()
        all_laps = con.execute("SELECT * FROM gold.lap_context").df()
        # Ignore incidental warehouse sessions outside the hashed experiment.
        all_laps = all_laps[all_laps.session_key.isin([e["session_key"] for e in entries])]
        bounds = {int(r.session_key): (r.date_start.to_pydatetime(), r.date_end.to_pydatetime())
                  for _, r in sessions.iterrows()}
        manifest = build_split_manifest(entries, bounds)
        prior_path = root/"config/experiments/stage3_degradation_v1.yaml"
        prior_keys = {r["session_key"] for rows in yaml.safe_load(prior_path.read_text())["cohort"].values() for r in rows} if prior_path.exists() else set()
        validate_stage4_holdout(manifest, prior_keys)
        _json(run/"splits.json", manifest.to_dict())
        ordered = sorted(entries, key=lambda e: bounds[e["session_key"]][0])
        for entry in ordered:
            sid = entry["session_key"]
            frames = {name: con.execute(f"SELECT * FROM silver.{name} WHERE session_key=?", [sid]).df()
                      for name in ("sessions", "drivers", "laps", "stints", "race_control", "weather", "position", "intervals")}
            events = populate_session_pit_events(con, sid, annotations)
            if events.empty:
                continue
            start = bounds[sid][0]
            # Same-race observations are allowed as local historical anchors;
            # fitting nuisance parameters always excludes the entire target race.
            past = all_laps[(all_laps.session_key != sid)
                            & (pd.to_datetime(all_laps.lap_end_estimate_utc, utc=True) < start)]
            reference = NormalLapReference().fit(past, start)
            current_laps = all_laps[all_laps.session_key == sid]
            labels = measure_pit_transitions(events, current_laps, reference)
            replace_session_frame(con, "gold.pit_transition_observations", labels, PIT_OBSERVATION_SCHEMA, sid)
            examples = populate_pit_examples(con, sid, events=events, observations=labels,
                                              frames=frames, split=entry["split"], reference=refs)
            all_labels.append(labels)
            all_examples.append(examples)
            event_frames.append(events)
            print(f"Stage 4 measurements {sid}: {len(events)} events, {int(labels.eligible_primary_label.sum())} eligible labels", flush=True)
    finally:
        con.close()
    if not event_frames:
        raise ValueError("No pit events in the configured cohort")
    events = pd.concat(event_frames, ignore_index=True)
    labels = pd.concat(all_labels, ignore_index=True)
    examples = pd.concat(all_examples, ignore_index=True)
    labels.to_parquet(run/"pit_event_labels.parquet", index=False)
    events.to_parquet(run/"pit_events.parquet", index=False)
    examples.to_parquet(run/"pit_opportunities.parquet", index=False)
    audit = _audit(events, labels)
    _json(run/"data_audit.json", _safe_json(audit))
    primary = labels[labels.measurement_method == "TWO_LAP_CROSS_FIT"].drop(columns=["eligible_primary_label"])
    fitting = events.merge(primary, on=["pit_event_id", "session_key"], validate="one_to_one")
    # Eligibility combines event integrity with successful counterfactual measurement.
    fitting["eligible_primary_label"] &= fitting.transition_loss_s.notna()
    train_keys, val_keys = manifest.train_keys, manifest.val_keys
    training = fitting[fitting.session_key.isin(train_keys)]
    train_end = max(bounds[k][1] for k in train_keys)
    n_samples = int(cfg["experiment"].get("n_samples", 1000))
    seed = int(cfg["experiment"].get("random_seed", 42))
    kwargs = cfg.get("model", {})
    predictions, models = [], {}
    for i in range(9):
        name = f"P{i}"
        model = PitLossModelV1(experiment_id=name, **kwargs).fit(training, training_cutoff_utc=train_end)
        val_examples = examples[examples.split == "val"]
        pred_val = evaluate_pit_model(model, val_examples, events, name, n_samples=n_samples, seed=seed)
        predictions.append(pred_val)
        if name == "P8":
            model.calibrate(val_examples)
        pred_test = evaluate_pit_model(model, examples[examples.split == "test"], events, name, n_samples=n_samples, seed=seed)
        predictions.append(pred_test)
        models[name] = model
    # P9/P10 compare identical frozen historical data and differ ONLY in whether
    # completed 2026 races are available; neither learns from reserved test stops.
    historical = fitting[fitting.session_key.isin(train_keys+val_keys)]
    historical_end = max(bounds[k][1] for k in train_keys+val_keys)
    base = PitLossModelV1(experiment_id="P9", **kwargs).fit(historical, training_cutoff_utc=historical_end)
    past_transfer = []
    for entry in sorted(manifest.transfer_2026, key=lambda e: bounds[e.session_key][0]):
        key = entry.session_key
        target = examples[examples.session_key == key]
        predictions.append(evaluate_pit_model(base, target, events, "P9", n_samples=n_samples, seed=seed))
        available = fitting[fitting.session_key.isin(train_keys+val_keys+past_transfer)]
        cutoff = bounds[key][0]-pd.Timedelta(microseconds=1)
        adapted = PitLossModelV1(experiment_id="P10", **kwargs).fit(available, training_cutoff_utc=cutoff)
        predictions.append(evaluate_pit_model(adapted, target, events, "P10", n_samples=n_samples, seed=seed))
        past_transfer.append(key)
        models["P10"] = adapted
    models["P9"] = base
    # Neutralization experiments explicitly report unsupported cases until a
    # route-equivalent reference dataset exists, instead of making up a discount.
    for name, states in {"N0": {"VSC", "SC"}, "N1": {"VSC"}, "N2": {"MIXED"}, "N3": {"SC"}}.items():
        model = PitLossModelV1(experiment_id=name, **kwargs).fit(training, training_cutoff_utc=train_end)
        target = examples[(examples.split.isin(["val", "test"])) & examples.track_status_at_cutoff.isin(states)]
        if not target.empty:
            predictions.append(evaluate_pit_model(model, target, events, name, n_samples=n_samples, seed=seed))
        models[name] = model
    predictions = pd.concat([p for p in predictions if not p.empty], ignore_index=True)
    predictions.to_parquet(run/"predictions.parquet", index=False)
    metrics = evaluate_pit_predictions(predictions)
    comparisons = {f"{name}_vs_P1": paired_pit_comparison(predictions, name, "P1", seed=seed)
                   for name in ["P0", "P2", "P3", "P4", "P5", "P6", "P7", "P8"]}
    # Select on validation MAE only, never final-test outcomes. P8 validation here
    # is pre-calibration; validation calibration scores are never sold as tests.
    scores = {name: splits.get("val", {}).get("net_loss_proxy", {}).get("race_weighted_mae_s", float("inf"))
              for name, splits in metrics.items() if name.startswith("P") and name not in {"P9", "P10"}}
    if not scores or not any(np.isfinite(value) for value in scores.values()):
        raise ValueError("No eligible validation predictions; cannot select a pit model")
    selected = min(scores, key=scores.get)
    loco = []
    if cfg.get("validation", {}).get("leave_one_circuit_out", True):
        validation = examples[examples.split == "val"]
        for circuit in sorted(training.circuit_short_name.unique()):
            reduced = training[training.circuit_short_name != circuit]
            held_ids = set(events[events.circuit_short_name == circuit].pit_event_id)
            held = validation[validation.pit_event_id.isin(held_ids)]
            if held.empty or not reduced.eligible_primary_label.any():
                continue
            candidate = PitLossModelV1(experiment_id="P8", **kwargs).fit(reduced, training_cutoff_utc=train_end)
            loco.append(evaluate_pit_model(candidate, held, events, "P8_LOCO", n_samples=n_samples, seed=seed))
    loco_predictions = pd.concat(loco, ignore_index=True) if loco else pd.DataFrame()
    loco_predictions.to_parquet(run/"unseen_circuit_predictions.parquet", index=False)
    _json(run/"metrics.json", _safe_json({"models": metrics, "comparisons": comparisons,
                                         "unseen_circuit": evaluate_pit_predictions(loco_predictions) if not loco_predictions.empty else {}}))
    model_folder = root/"artifacts/models/pit_loss_v1"
    model_folder.mkdir(parents=True, exist_ok=True)
    for name, model in models.items():
        with (model_folder/f"{name}.pkl").open("wb") as handle:
            pickle.dump(model, handle)
    metadata = {"generated_at_utc": datetime.now(UTC).isoformat(), "git_revision": _git_revision(root),
                "working_tree_dirty": bool(subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"], text=True)),
                "selected_on_validation": selected, "random_seed": seed, "n_samples": n_samples,
                "reference_version": NormalLapReference().version,
                "neutralization_status": "UNVALIDATED_NO_ROUTE_MATCHED_REFERENCE_DATA",
                "pace_v2_status": "Frozen; 0B bias and 20-lap limitations remain",
                "test_selection": cfg["experiment"].get("final_test_policy"),
                "completion_status": "RESEARCH_IMPLEMENTATION_NOT_APPROVED_FOR_UNRESTRICTED_STRATEGY"}
    metadata["source_sha256"] = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((root/"src/f1ri").rglob("*.py"))
    }
    metadata["dependencies"] = {name: importlib.metadata.version(name) for name in (
        "duckdb", "pandas", "numpy", "scikit-learn", "matplotlib", "PyYAML", "httpx",
    )}
    metadata["model_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in sorted(model_folder.glob("*.pkl"))}
    _json(run/"acceptance.json", {
        "engineering_pipeline_executed": True,
        "p8_reliable_improvement_over_p1": comparisons["P8_vs_P1"].get("p95_delta_s", float("inf")) < 0,
        "pit_lane_reference_independently_sourced": False,
        "neutralization_model_validated": False,
        "physical_route_anchors_verified": False,
        "unrestricted_strategy_approval": False,
    })
    _json(run/"run_metadata.json", metadata)
    generate_pit_diagnostics(events, labels, predictions, run/"plots")
    write_stage4_report(root, run, audit, metrics, comparisons, metadata)
    return {"run_directory": str(run), "selected_model": selected, "events": len(events),
            "primary_labels": audit["primary_training_labels"], "metrics": metrics,
            "completion_status": metadata["completion_status"]}


def generate_pit_diagnostics(events, labels, predictions, output: Path) -> None:
    import os
    os.environ.setdefault("MPLCONFIGDIR", str(output/".matplotlib-cache"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    output.mkdir(parents=True, exist_ok=True)
    primary = labels[labels.measurement_method == "TWO_LAP_CROSS_FIT"]
    p = predictions[(predictions.model_id == "P8") & (predictions.split == "test")]
    valid = p[p.eligible_primary_label.fillna(False)]
    fig, axes = plt.subplots(3, 4, figsize=(22, 14), constrained_layout=True)
    axes = axes.ravel()
    titles = ["A · Measured lane duration by circuit", "B · Measured stationary service",
              "C · Direct lane timing", "D · Reconstructed net-loss proxy",
              "E · Residual by circuit", "F · Residual by stop type",
              "G · Common-event model MAE by circuit", "H · Nominal interval coverage",
              "I · Reconstructed loss by track state", "J · Measurement-method agreement",
              "K · Statistical components (not physical identification)", "L · Error by historical support"]
    for ax, title in zip(axes, titles):
        ax.set_title(title, fontsize=10)
    groups = [(k, g.lane_duration_s.dropna()) for k, g in events.groupby("circuit_short_name")]
    axes[0].boxplot([v for _, v in groups], tick_labels=[k for k, _ in groups], showfliers=False)
    axes[0].tick_params(axis="x", rotation=90)
    axes[1].hist(events.stop_duration_s.dropna(), bins=40)
    axes[1].set_xlabel("Stationary seconds (missing excluded)")
    for ax, target, prediction in ((axes[2], "target_lane_duration_s", "expected_lane_duration_s"),
                                    (axes[3], "target_transition_loss_s", "expected_loss_s")):
        sub = p.dropna(subset=[target, prediction])
        ax.scatter(sub[target], sub[prediction], s=10, alpha=.5)
        if len(sub):
            bounds = [float(sub[target].min()), float(sub[target].max())]
            ax.plot(bounds, bounds, "k--")
        ax.set_xlabel("Measured / proxy seconds")
        ax.set_ylabel("Predicted seconds")
    for ax, column in ((axes[4], "circuit_short_name"), (axes[5], "pit_event_type"), (axes[11], "support_status")):
        grouped = [(str(k), g.residual_s.dropna()) for k, g in valid.groupby(column)]
        if grouped:
            ax.boxplot([v for _, v in grouped], tick_labels=[k for k, _ in grouped], showfliers=False)
            ax.tick_params(axis="x", rotation=35)
        ax.axhline(0, color="black", linestyle="--")
    for name, group in predictions[(predictions.split == "test") & predictions.eligible_primary_label.fillna(False)].groupby("model_id"):
        if name in {"P0", "P1", "P3", "P8"}:
            scores = group.groupby("circuit_short_name").residual_s.apply(lambda s: s.abs().mean())
            axes[6].plot(scores.index, scores, marker="o", label=name)
    axes[6].legend()
    if not valid.empty:
        coverage = [((valid.target_transition_loss_s >= valid[f"p{lo:02}_loss_s"])
                     & (valid.target_transition_loss_s <= valid[f"p{hi:02}_loss_s"])).mean()
                    for lo, hi in ((25, 75), (5, 95))]
        axes[7].bar(["50%", "90%"], coverage)
        axes[7].plot([0, 1], [.5, .9], "r--", label="nominal")
        axes[7].set_ylim(0, 1)
        axes[7].legend()
        components = [valid.expected_route_loss_s.mean(), valid.expected_stationary_duration_s.mean(),
                      valid.expected_transition_loss_s.mean()]
        axes[10].bar(["Route/residual", "Service", "Transition"], np.nan_to_num(components))
    axes[8].text(.05, .4, "Green labels only.\nVSC/SC: insufficient independent\nroute references; no invented discount.", transform=axes[8].transAxes)
    matched = primary.dropna(subset=["transition_loss_s", "matched_control_loss_s"])
    axes[9].scatter(matched.transition_loss_s, matched.matched_control_loss_s, s=10, alpha=.5)
    axes[9].set_xlabel("Two-lap cross-fit (s)")
    axes[9].set_ylabel("Matched control (s)")
    fig.suptitle("Stage 4 · Pit-loss diagnostics · P8 final reserved races", fontsize=15)
    fig.savefig(output/"stage4_diagnostics.png", dpi=150)
    plt.close(fig)
    eligible = primary[primary.eligible_primary_label.fillna(False)]
    if not eligible.empty:
        row = eligible.iloc[0]
        event = events[events.pit_event_id == row.pit_event_id].iloc[0]
        fig, ax = plt.subplots(figsize=(9, 5), constrained_layout=True)
        ax.bar(["Observed in", "Observed out", "Normal in", "Normal out", "Lane (overlapping)", "Stationary (in lane)", "Net loss"],
               [event.observed_in_lap_s, event.observed_out_lap_s, row.normal_in_reference_s,
                row.normal_out_reference_s, event.lane_duration_s,
                event.stop_duration_s if pd.notna(event.stop_duration_s) else np.nan, row.transition_loss_s])
        ax.errorbar(6, row.transition_loss_s, yerr=row.label_std_s*1.645, color="black", capsize=5)
        ax.set_title(f"Pit reconstruction {row.pit_event_id} · {event.circuit_short_name} · {event.transition_track_status}\nLane overlaps observed laps; missing service is not a measured zero")
        ax.set_ylabel("Seconds")
        ax.tick_params(axis="x", rotation=25)
        fig.savefig(output/"historical_reconstruction.png", dpi=150)
        plt.close(fig)
    generate_telemetry_diagnostics(output.parent, output)


def generate_telemetry_diagnostics(run: Path, output: Path) -> None:

    import matplotlib.pyplot as plt

    from f1ri.validation.pit_measurement import audit_stationary_trace
    path = run/"telemetry_audit.json"
    if not path.exists():
        return
    audit = json.loads(path.read_text())
    root = run.parents[2]
    available = [r for r in audit if r.get("snapshot_directory")]
    if not available:
        return
    fig, axes = plt.subplots(5, 2, figsize=(15, 17), constrained_layout=True)
    checks = []
    for ax, record in zip(axes.ravel(), available):
        folder = root/record["snapshot_directory"]
        trace = pd.DataFrame(json.loads((folder/"car_data.json").read_text()))
        if trace.empty:
            ax.text(.1, .5, "No samples", transform=ax.transAxes)
            continue
        event = pd.Timestamp(record["provider_event_utc"])
        check = audit_stationary_trace(trace, event_utc=event,
                                       lane_duration_s=record["raw_lane_duration_s"],
                                       recorded_stationary_s=record["raw_stop_duration_s"])
        checks.append({"pit_event_id": record["pit_event_id"], "circuit": record["circuit"], **check})
        seconds = (pd.to_datetime(trace.date, utc=True, format="ISO8601")-event).dt.total_seconds()
        ax.plot(seconds, trace.speed, linewidth=1)
        ax.axvline(0, linestyle="--", color="orange", label="Provider report time")
        ax.axvline(-record["raw_lane_duration_s"], linestyle="--", color="red", label="Report - lane duration (approx.)")
        ax.set_title(f"{record['circuit']} · {record['pit_event_id']} · service {record['raw_stop_duration_s']:.2f}s")
        ax.set_xlabel("Seconds from provider pit event")
        ax.set_ylabel("Sampled speed (km/h)")
    axes[0, 0].legend(fontsize=7)
    fig.suptitle("Targeted telemetry timing checks · approximate samples, unverified legal route anchors")
    fig.savefig(output/"telemetry_timing_checks.png", dpi=130)
    plt.close(fig)
    _json(run/"telemetry_timing_checks.json", _safe_json(checks))


def write_stage4_report(root, run, audit, metrics, comparisons, metadata) -> None:
    import shutil
    reports = root/"artifacts/reports"
    reports.mkdir(parents=True, exist_ok=True)
    for original, destination in (
        ("stage4_diagnostics.png", "stage4_diagnostics.png"),
        ("historical_reconstruction.png", "stage4_historical_reconstruction.png"),
        ("telemetry_timing_checks.png", "stage4_telemetry_timing_checks.png"),
    ):
        source = run/"plots"/original
        if source.exists():
            shutil.copyfile(source, reports/destination)
    _json(reports/"stage4_model_manifest.json", {
        "git_revision": metadata["git_revision"],
        "selected_on_validation": metadata["selected_on_validation"],
        "completion_status": metadata["completion_status"],
        "random_seed": metadata["random_seed"],
        "reference_version": metadata["reference_version"],
        "model_artifact_sha256": metadata.get("model_sha256", {}),
        "dependencies": metadata.get("dependencies", {}),
        "data_manifest_sha256": hashlib.sha256((run/"data_manifest.json").read_bytes()).hexdigest(),
        "configuration_sha256": hashlib.sha256((run/"config.yaml").read_bytes()).hexdigest(),
        "note": "Model artifacts remain local under artifacts/models/pit_loss_v1; reproduce with the hashed config/data and recorded package versions",
    })
    p8_test = metrics.get("P8", {}).get("test", {}).get("net_loss_proxy", {})
    p1_test = metrics.get("P1", {}).get("test", {}).get("net_loss_proxy", {})
    outcome = "Insufficient final-test evidence for model approval."
    if p8_test.get("mae_s") is not None and p1_test.get("mae_s") is not None:
        outcome = (f"P8 reconstructed-loss MAE is {p8_test['mae_s']:.3f}s versus {p1_test['mae_s']:.3f}s "
                   f"for P1 on {p8_test['n_events']} common eligible events from {p8_test['n_races']} reserved races. "
                   "The required reliable-improvement gate is not met; retain the simple benchmark for comparison. "
                   "These races also test unseen circuits, and three independent races are too few for broad claims.")
    lines = ["# Stage 4 — PitLoss V1 validation", "",
             "Research implementation; not approved for unrestricted race-strategy optimization.", "",
             outcome, "",
             f"Source revision: `{metadata['git_revision']}` (working-tree implementation; see run metadata).",
             f"Run: `{run.relative_to(root)}`. Seed {metadata['random_seed']}; {metadata['n_samples']} samples per prediction.", "",
             "## Data and measurement", "",
             ("Timing audit finding: the OpenF1 pit timestamp is a timing-feed report, not a guaranteed "
              "pit-entry time. Independent speed traces showed stationary service preceding that record. "
              "The warehouse preserves the raw report time and marks backwards lane-window estimates "
              "as approximate; exact legal boundaries and feed latency remain unverified."), "",
             (f"Reconstructed **{audit['events']} pit events**, including {audit['missing_stationary']} with no measured stationary duration. "
             f"{audit['reconstructed_labels']} two-lap loss estimates; {audit['primary_training_labels']} pass primary-label eligibility."), "",
             (f"Independent matched-control checks: {audit['independent_control_checks']}; median absolute disagreement "
             f"{audit['median_absolute_method_disagreement_s']} seconds. Both methods estimate counterfactual quantities, not physical ground truth."), "",
             ("Reference parameters are fitted on earlier races only, never the target race. Car pace is anchored on pre-stop clean laps. "
             "Pace V2 is frozen; its reviewed new-stint bias is not silently fitted into the pit model. "
             "Separate matched controls may still be confounded by tire choices; disagreement is retained with inflated label uncertainty."), "",
             "## Chronological evaluation", "",
             ("Training: configured 2023–2024 races. Validation: early 2025. "
             "Newly reserved test: Mexico, Brazil and Qatar 2025 (absent from Stage 3). "
             "They include unseen circuits, so final-test results test both temporal and circuit transfer. "
             "The split manifest was written before fitting. Model selection uses validation only; P8 calibration uses validation only. "
             "A separate leave-one-circuit-out experiment uses validation races."), "",
             "| Model | Split | Net events / races | Proxy MAE (s) | Bias (s) | 90% coverage | CRPS (s) | Lane MAE (s) | Service MAE (s) |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, splits in metrics.items():
        for split, block in splits.items():
            net = block["net_loss_proxy"]
            fmt = lambda v: f"{v:.3f}" if v is not None else "—"
            lines.append(f"| {name} | {split} | {net['n_events']} / {net['n_races']} | "
                         f"{fmt(net.get('mae_s'))} | {fmt(net.get('bias_s'))} | {fmt(net.get('coverage_90'))} | "
                         f"{fmt(net.get('crps_s'))} | {fmt(block['direct_lane'].get('mae_s'))} | {fmt(block['direct_stationary'].get('mae_s'))} |")
    lines += ["", (f"Validation-selected model: **{metadata['selected_on_validation']}**. "
              "This selection does not establish that P8 beats circuit medians or meets final acceptance."), "",
              ("The reserved test was not used to tune a replacement model. Reruns after correctness fixes "
               "are regression evaluations of that same reserved set, not additional untouched evidence."), "",
              "Paired final-test comparisons use identical eligible events and race-level bootstrap resampling:", "",
              "```json", json.dumps(comparisons, indent=2), "```", "",
              "## Diagnostics and artifacts", "",
              "![Stage 4 diagnostics](stage4_diagnostics.png)", "",
              "![Historical reconstruction](stage4_historical_reconstruction.png)", "",
              ("The run retains config, input hashes, splits, event labels, predictions, metrics, per-slice counts, "
              "unseen-circuit predictions and run provenance. Model pickles are local trusted outputs under `artifacts/models/pit_loss_v1/`."), "",
              "## Limits and outstanding acceptance gates", "",
              ("- VSC/SC/mixed requests have an explicit conditional API, but no validated route-matched neutralization reference set. "
              "They return unsupported, wide fallback distributions and cannot be consumed as supported simulation costs. N0–N3 are unvalidated where targets are unavailable."),
              "- No exact pit entry/exit geometry or historical speed limit is invented. Reference CSV rows are explicitly unverified and unavailable as of forecasts until sourced.",
              ("- High-frequency acquisition is optional and bounded to one driver and ≤180 seconds. Approximate GPS is not a legal timing loop. "
              "Telemetry validation is reported separately and does not automatically promote events to quality A."),
              ("- Repairs, drive-throughs, penalties and double-stack delays need recorded classifications. Long duration alone is never a cause label. "
              "Unclassified/ambiguous visits remain in the audit and outside primary fitting."),
              ("- The model describes observed stop-selection support, not random assignment of all possible pit decisions. "
              "Fresh-tire scenario evaluation does not claim coverage of every used-tire scenario."),
              ("- Stage 3's 0B bias, fixed-cohort 20-lap diagnostics and long-horizon calibration remain separate stabilization tasks. "
              "No Stage 3 model, existing report or experiment configuration was overwritten."),
              "- Small independent race samples limit claims of superiority and calibration. Stage 4 final acceptance remains evidence-gated.", "",
              "## Time accounting", "",
              ("Net loss = observed in-lap + observed out-lap − normal old-tire lap − normal new-tire lap. "
              "Stationary time is already inside lane time. Pace V2 owns normal clean-air laps; Stage 4 owns incremental pit-out transition effects. "
              "The strategy integration adds net loss once, never lane time plus net loss plus a warm-up penalty. "
              "Shared reference/route draws can be supplied across pit samples; correlated pace uncertainty must be supplied by the caller, not assumed independent."), ""]
    telemetry = run/"telemetry_audit.json"
    if telemetry.exists():
        records = json.loads(telemetry.read_text())
        sampled = [r for r in records if r.get("data_status") == "SAMPLED_UNVERIFIED_ANCHORS"]
        lines += ["## Independent targeted telemetry checks", "",
                  (f"Acquired {len(sampled)} bounded pit windows across "
                   f"{len({r['circuit'] for r in sampled})} circuits, with car-data and location samples. "
                   "This verifies acquisition and permits resolution-limited stationary timing comparisons. "
                   "It does not identify legal pit entry/exit anchors or validate a physical net-loss label."), "",
                  "![Bounded speed traces](stage4_telemetry_timing_checks.png)", "",
                  ("Per-event source hashes, sample gaps and acquisition bounds are in `telemetry_audit.json`; "
                   "stationary timing differences and boundary resolution are in `telemetry_timing_checks.json`."), ""]
        checks = run/"telemetry_timing_checks.json"
        if checks.exists():
            timing = json.loads(checks.read_text())
            lines += ["| Event | Circuit | Recorded service (s) | Trace midpoint (s) | Difference (s) |",
                      "| --- | --- | --- | --- | --- |"]
            for check in timing:
                estimate, difference = check.get("stationary_trace_estimate_s"), check.get("difference_s")
                estimate_text = f"{estimate:.3f}" if estimate is not None else "unidentified"
                difference_text = f"{difference:+.3f}" if difference is not None else "unidentified"
                lines.append(f"| {check['pit_event_id']} | {check['circuit']} | {check.get('recorded_stationary_s')} | {estimate_text} | {difference_text} |")
            lines += ["", ("These sensor checks assume one uninterrupted service between adjacent low-speed samples. "
                           "Gaps, quantized wheel speed and source-report latency can invalidate exact duration reconstruction. "
                           "Disagreements are retained, not filtered away; the large Sakhir discrepancy requires independent review. "
                           "None of these events is promoted to verified physical-route quality A."), ""]
    report = root/"artifacts/reports/stage4_validation.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines))
