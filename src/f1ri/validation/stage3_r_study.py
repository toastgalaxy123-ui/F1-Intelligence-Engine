"""Stage 3-R reproduction runner and explicit promotion manifest."""
import json
import pickle
import platform
from dataclasses import fields
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

import duckdb
import pandas as pd

from f1ri.models.contracts import SCENARIO_CONTINUE_STINT, StintForecastRequest
from f1ri.simulator.pace_adapter import PaceBundle
from f1ri.validation.model_strategy_recovery import recover_bundle
from f1ri.validation.proximity_pilot import _json
from f1ri.validation.stage3_r_population import ready_splits, rebuild, validate_examples
from f1ri.validation.stage3_r_release import REQUIRED_MEAN_GATES
from f1ri.validation.stage3_r_report import finalize
from f1ri.validation.stage3_r_sources import PLAN, digest, prepare_reference, study_inputs
from f1ri.validation.stage3_r_strategy import execute_strategy, replacement_requests
from f1ri.validation.stage3_r_validation import (
    D8,
    adaptation_comparison,
    fit_and_evaluate,
    gates,
    summaries,
)
from f1ri.validation.stint_metrics import compute_cumulative_trajectory_metrics
from f1ri.validation.strategy_sources import audit_model_assets

FROZEN_C1_HASH = "500b99c186e144cf727515ad0db3c9f473c182dc45b2cc7ef186f35550e4a823"
FROZEN_C_HASH = "669312321029a882336ffdee8f39cb7d0ddb78eeea10df22febed1d2ae5795c1"
FROZEN_REVISION = "bcccf23806cd8414a02be64c6c728a427a8785d7"


def _parquet(data, path):
    with duckdb.connect() as db:
        db.register("stage3_r_rows", data)
        db.execute("COPY stage3_r_rows TO ? (FORMAT PARQUET)", [str(path)])


def _read_parquet(path):
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'")
        return db.execute("SELECT * FROM read_parquet(?)", [str(path)]).df()


def _refit_check(path, groups, plan, expected, model_hash):
    refitted, repeated = fit_and_evaluate(_read_parquet(path), groups, plan, successor_only=True)
    try:
        pd.testing.assert_frame_equal(repeated, expected.reset_index(drop=True), check_exact=True, check_dtype=False)
        exact_forecasts = True
    except AssertionError:
        exact_forecasts = False
    return {"exact_held_out_forecast_table": exact_forecasts, "initial_model_sha256": model_hash,
            "refit_model_sha256": sha256(pickle.dumps(refitted, protocol=4)).hexdigest(),
            "verified_rows": len(repeated), "input": "modeling_examples.parquet"}


def verify_run(run):
    sealed = {}
    for line in (run/"SHA256SUMS").read_text(encoding="utf-8").splitlines():
        expected, _, name = line.partition("  ")
        path = (run/name).resolve()
        if not path.is_relative_to(run.resolve()) or not path.is_file() or digest(path) != expected:
            raise ValueError(f"Stage 3-R evidence checksum failed for {name}")
        sealed[name] = expected
    if set(sealed) != {p.relative_to(run).as_posix() for p in run.rglob("*") if p.is_file()
                      and p != run/"SHA256SUMS"}:
        raise ValueError("Stage 3-R evidence file list differs from its checksum manifest")
    metrics = json.loads((run/"metrics.json").read_bytes())
    if "model_sha256" not in metrics:
        return {"checksum_verified": True, "model_refit": "NO_MODEL_IN_RUN"}
    plan = json.loads((run/"protocol.json").read_bytes())
    predictions = _read_parquet(run/"open_loop_validation.parquet")
    result = _refit_check(run/"modeling_examples.parquet", metrics["training_and_eval"], plan,
                          predictions[predictions.model_id.eq(D8)], metrics["model_sha256"])
    if not result["exact_held_out_forecast_table"] or result["initial_model_sha256"] != result["refit_model_sha256"]:
        raise ValueError(f"Stage 3-R refit differs from recorded evidence: {result}")
    return {"checksum_verified": True} | result


def _population(rows):
    if rows.empty:
        return {}
    result = {}
    for session, subset in rows.groupby("session_key"):
        clean = subset[subset.is_clean_green_pace_lap]
        result[int(session)] = {"laps_with_stint": len(subset), "clean_laps": len(clean), "drivers": subset.driver_number.nunique(),
                                "clean_stints": clean.stint_id.nunique(),
                                "compounds": subset.compound_relative.value_counts().to_dict(),
                                "exclusions": subset.exclusion_reason.fillna("ELIGIBLE").value_counts().to_dict(),
                                "missing_weather": int(subset.track_temperature_c.isna().sum())}
    return result


def run_reconstruction(root, cache, out):
    if out.exists():
        raise FileExistsError(out)
    plan, cohort = study_inputs(root)
    reference = json.loads((root/"config/experiments/stage3_frozen_reference.json").read_bytes())
    historical = Path("C:/Projects/f1-race-intelligence")
    audit, _original = recover_bundle(root, historical)
    _, frozen, _ = audit_model_assets(root, historical)
    actual = digest(root/"artifacts/reports/stage5_s5_c1_model_strategy_integration_metrics.json")
    s5_c = digest(root/"artifacts/reports/stage5_s5_c_strategy_scenarios_metrics.json")
    if actual != FROZEN_C1_HASH or s5_c != FROZEN_C_HASH or not all(audit["frozen_source_hashes"].values()):
        raise ValueError("A frozen research artifact or source differs")
    frozen["s5_c1"] = {"sha256": actual, "status": "METRICS_BYTES_VERIFIED"}
    frozen["s5_c"] = {"sha256": s5_c, "status": "METRICS_BYTES_VERIFIED"}
    prepare_reference(root, cache)
    sources, reconstruction, splits, examples, population, data_audit = rebuild(root, cache, plan, cohort)
    groups, gaps = ready_splits(plan, splits, reconstruction)
    out.mkdir(parents=True)
    (out/"config.yaml").write_bytes((root/PLAN).read_bytes())
    for name, payload in {
        "protocol": plan, "source_manifest": sources, "warehouse_reconstruction": reconstruction,
        "data_availability": {"ready": groups, "insufficiency": gaps, "clean_air_population": _population(population),
                              "legacy_model_recovery": audit, "warehouse_audit": data_audit},
        "splits": {"configured": splits.to_dict(), "eligible": groups},
        "feature_schema": {"request_fields": [f.name for f in fields(StintForecastRequest)],
                           "warehouse_example_types": {n: str(t) for n, t in examples.dtypes.items()},
                           "weather": "last source observation at or before completed-lap cutoff",
                           "clean_history": "strictly observed lap prefix",
                           "replacement": "historical fitted set conditions supplied for conditional evaluation",
                           "targets": "observed future durations, scored after forecasts",
                           "weather_availability": "source event clock; ingestion receipt unknown"},
        "regulation_transfer": {"status": "NOT_EVALUATED", "reason": "2026 era was excluded from acquisition and fitting",
                                "transfer_roles_preserved": splits.transfer_2026_keys},
        "regression_parity": frozen,
    }.items():
        _json(out/f"{name}.json", payload)
    if (cache/"acquisition_protocol.json").exists():
        (out/"acquisition_protocol.json").write_bytes((cache/"acquisition_protocol.json").read_bytes())
    if (cache/"acquisition_failures.json").exists():
        (out/"acquisition_failures.json").write_bytes((cache/"acquisition_failures.json").read_bytes())
    if not population.empty:
        population = population.assign(role=population.session_key.map({r["session_key"]: r["split"] for r in cohort}))
        _parquet(population, out/"training_population.parquet")
    if gaps:
        result = {"version": plan["version"], "model_recovery": audit["status"], "release": "UNSUPPORTED",
                  "decision": "PATH_C_TRAINING_EVIDENCE_INSUFFICIENT", "sufficiency": gaps, "cohorts": groups}
        _json(out/"promotion_gates.json", {"G0_data_provenance": False, "remaining": "NOT_RUN"})
        _json(out/"metrics.json", result)
        finalize(out)
        return out/"metrics.json"
    validate_examples(examples)
    _parquet(examples, out/"modeling_examples.parquet")
    for role, keys in groups.items():
        actual_races = set(examples.loc[examples.session_key.isin(keys) &
                                        examples.target_is_clean_green &
                                        examples.requested_horizon.eq(1), "session_key"])
        if actual_races != set(keys):
            raise ValueError(f"Missing eligible modeling targets for {role}: {sorted(set(keys)-actual_races)}")
    config = plan["validation"]
    model, predictions = fit_and_evaluate(examples, groups, plan)
    horizons = config["horizons"]
    compared = summaries(predictions, groups, horizons)
    d8_predictions = predictions[predictions.model_id.eq(D8)]
    _parquet(predictions, out/"open_loop_validation.parquet")
    _parquet(predictions[predictions.requested_horizon.eq(1)], out/"one_lap_validation.parquet")
    adapt = adaptation_comparison(predictions, horizons)
    _parquet(predictions[predictions.model_id.isin([D8, "D8R1_without_stint_adaptation"])],
             out/"online_adaptation_validation.parquet")
    _json(out/"online_adaptation_summary.json", adapt)
    _json(out/"baseline_comparison.json", compared)
    _json(out/"historical_comparability.json", {
        "estimator": "METHOD_REPRODUCED_ONLY", "new_inputs": "REACQUIRED_OPENF1_SNAPSHOTS",
        "endpoint_and_stint_metrics": "SAME_METRIC_DEFINITIONS_DIFFERENT_OBSERVATIONS_AND_CUTOFF_SAMPLE",
        "original_model_byte_or_prediction_parity": "UNAVAILABLE", "transfer_2026": "UNAVAILABLE",
        "protocol": "Frozen roles and model classes. Evaluated every third cutoff lap at 400 draws.",
    })
    calibration_cutoff = max(e.session_end_utc for e in splits.val if e.session_key in groups["calibration"])
    training_payload = pickle.dumps(model, protocol=4)
    # Only deserialize bytes produced by this process. No paths or uploaded pickles.
    checked = pickle.loads(training_payload)
    model_hash = sha256(training_payload).hexdigest()
    bundle = PaceBundle(checked, model_hash, checked.trained_through_utc_, calibration_cutoff,
                        tuple(groups["train"]), tuple(groups["calibration"]), "2023_2025")
    replacement = {"status": "UNSUPPORTED"}
    strategy = {"status": "UNSUPPORTED"}
    paired = {"status": "UNSUPPORTED"}
    numerical = False
    integrated = False
    try:
        replacement = replacement_requests(root, bundle, horizons, config["samples"])
        before_reload = replacement_requests(root, PaceBundle(model, model_hash, model.trained_through_utc_,
                                             calibration_cutoff, bundle.training_sessions, bundle.calibration_sessions,
                                             bundle.regulation_era), horizons, config["samples"])
        numerical = (replacement["all_numeric_integrity"] and checked.model_version == plan["model_version"]
                     and checked.trajectory_sampler_.is_calibrated_
                     and before_reload == replacement)
    except (ValueError, OSError) as error:
        replacement["reason"] = str(error)
    if numerical:
        try:
            strategy, invariants, paired, integrated = execute_strategy(root, bundle, config["samples"], config["seed"])
            _json(out/"strategy_invariants.json", invariants)
        except (ValueError, OSError) as error:
            strategy["reason"] = str(error)
    repeat = _refit_check(out/"modeling_examples.parquet", groups, plan, d8_predictions, model_hash)
    _json(out/"reproducibility.json", repeat)
    replacement_ok = numerical and replacement["weather_valid"] and replacement["replacement_state_valid"]
    decision = gates(compared, config, replacement_ok=replacement_ok, strategy_ok=integrated, serialization_ok=numerical,
                     reproducible=numerical and repeat["exact_held_out_forecast_table"]
                     and repeat["refit_model_sha256"] == model_hash)
    quality = all(decision[k] for k in REQUIRED_MEAN_GATES)
    release = ("STRATEGY_INTEGRATED" if decision["G3_uncertainty_quality"] else "MEAN_ONLY_VALIDATED") if quality else "UNSUPPORTED"
    if quality:
        model_dir = out/"models"/plan["model_version"]
        model_dir.mkdir(parents=True)
        (model_dir/"pace_v2_d8r1.pkl").write_bytes(training_payload)
    manifest = {"model_version": plan["model_version"], "model_sha256": model_hash,
                "published_model": f"models/{plan['model_version']}/pace_v2_d8r1.pkl" if quality else None,
                "regulation_era": "2023_2025", "release": release,
                "trained_through_utc": checked.trained_through_utc_.isoformat(),
                "calibrated_through_utc": calibration_cutoff.isoformat(),
                "train_sessions": groups["train"], "calibration_sessions": groups["calibration"],
                "feature_schema_sha256": digest(out/"feature_schema.json")}
    _json(out/"model_artifact_manifest.json", manifest)
    _json(out/"model_training_manifest.json", manifest | {
        "frozen_estimator_baseline": FROZEN_REVISION, "python": platform.python_version(),
        "archived_stage3_revision": reference["git_revision"],
        "packages": {p: version(p) for p in ("numpy", "pandas", "duckdb", "scikit-learn")},
        "seed": config["seed"], "configuration_sha256": digest(root/PLAN),
        "source_manifest_sha256": digest(out/"source_manifest.json"),
        "training_population_sha256": digest(out/"training_population.parquet"),
        "modeling_examples_sha256": digest(out/"modeling_examples.parquet"),
        "split_sha256": digest(out/"splits.json"), "estimator": checked.diagnostics_dict(),
    })
    for name, data in {
        "replacement_forecast_validation": replacement, "paired_trajectory_validation": paired,
        "strategy_integration": strategy, "promotion_gates": decision,
        "trajectory_calibration": {"scores": compared[D8], "evidence_unit": "RACE",
                                   "by_race": {int(race): {h: compute_cumulative_trajectory_metrics(rows, h)
                                                          for h in horizons}
                                               for race, rows in d8_predictions[
                                                   d8_predictions.scenario_type.eq(SCENARIO_CONTINUE_STINT)]
                                               .groupby("session_key")},
                                   "within_stint_rho": checked.trajectory_sampler_.ar1_rho,
                                   "cross_driver_covariance": "NOT_MODELED"},
    }.items():
        _json(out/f"{name}.json", data)
    metrics = {"version": plan["version"], "identity": plan["model_version"], "model_recovery": audit["status"],
               "decision": ("PATH_A_STRATEGY_INTEGRATED" if release == "STRATEGY_INTEGRATED" else
                            "PATH_B_MODEL_FITTED_WITH_LIMITS" if decision["G1_numerical_integrity"] and
                            decision["G2_mean_quality"] else "PATH_C_BASIC_FORECAST_VALIDATION_FAILED"),
               "release": release, "training_and_eval": groups,
               "promotion_gates": decision, "model_sha256": model_hash,
               "population": _population(population), "open_loop_opportunities": len(d8_predictions),
               "independent_test_races": len(groups["test"])}
    from f1ri.validation.stage3_r_plots import plot_reconstruction

    _json(out/"metrics.json", metrics)
    plot_reconstruction(out, reconstruction, population, predictions, compared, adaptation_comparison(predictions, [1]),
                        replacement, strategy, paired, manifest, decision)
    finalize(out)
    return out/"metrics.json"
