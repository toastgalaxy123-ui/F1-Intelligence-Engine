"""Evaluate frozen Pace V2 trajectories and point-in-time field scheduling."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml

from f1ri.features.race_state import build_field_snapshot
from f1ri.simulator.engine import TimingMode
from f1ri.simulator.pace_adapter import load_pace_bundle, reference_parity
from f1ri.simulator.pace_field import FieldPaceForecast, forecast_field, simulate_field_pace
from f1ri.simulator.state import TrackState
from f1ri.validation.stint_metrics import (
    compute_cumulative_trajectory_metrics,
    compute_lap_horizon_metrics,
)


def _empirical_crps(samples: np.ndarray, actual: float) -> float:
    ordered = np.sort(samples)
    size = len(ordered)
    spread = np.dot(2 * np.arange(size) - size + 1, ordered) / size**2
    return float(np.mean(np.abs(ordered - actual)) - spread)


def _target(row, snapshot, request) -> tuple[float | None, str | None]:
    if row is None or pd.isna(row.observed_lap_time_s):
        return None, "MISSING_OR_INCOMPLETE_LAP"
    if (
        pd.isna(row.stint_number) or row.stint_number != request.stint_number
        or row.is_pit_in_lap or row.is_pit_out_lap
    ):
        return None, "FUTURE_PIT_OR_STINT_CHANGE"
    if pd.isna(row.lap_start_utc) or row.lap_start_utc < snapshot.as_of_utc:
        return None, "TARGET_STARTED_BEFORE_CUTOFF"
    if not row.is_clean_green_pace_lap:
        return None, str(row.exclusion_reason or "INELIGIBLE_CLEAN_AIR")
    return float(row.observed_lap_time_s), None


def _evaluation_rows(
    field: FieldPaceForecast, observations: pd.DataFrame, cohort: str, leader_lap: int,
    horizons: list[int],
) -> list[dict]:
    """Join outcomes after prediction, scoring laps starting at or after cutoff."""
    observed = {(row.driver_number, row.lap_number): row for row in observations.itertuples()}
    rows = []
    snapshot = field.snapshot
    for driver in snapshot.drivers:
        if driver.driver_number not in field.batches:
            continue
        batch = field.batches[driver.driver_number]
        forecast = batch.forecast
        request = driver.request
        # Partial laps affect field mechanics. Evaluation starts with the first full future lap.
        offset = int(driver.elapsed_current_lap_s > 0)
        for horizon in horizons:
            end = horizon + offset
            if end > forecast.horizon_laps:
                continue
            laps = forecast.lap_forecasts[offset:end]
            targets = [_target(observed.get((driver.driver_number, lap.target_lap_number)),
                               snapshot, request) for lap in laps]
            actual, endpoint_reason = targets[-1]
            censor = next((reason for _, reason in targets if reason), None)
            endpoint = laps[-1]
            totals = batch.lap_samples_s[:, offset:end].sum(axis=1)
            p05, p25, p75, p95 = np.quantile(totals, (.05, .25, .75, .95))
            actual_total = sum(value for value, _ in targets) if censor is None else None
            rows.append({
                "cohort": cohort, "session_key": snapshot.session_key,
                "circuit_short_name": request.circuit_short_name,
                "driver_number": driver.driver_number, "leader_cutoff_lap": leader_lap,
                "forecast_as_of_utc": snapshot.as_of_utc,
                "stint_id": f"{snapshot.session_key}_{driver.driver_number}_{request.stint_number}",
                "requested_horizon": horizon, "target_lap_number": endpoint.target_lap_number,
                "projected_tyre_age": endpoint.projected_tyre_age,
                "prior_clean_stint_laps": request.clean_laps_observed_current_stint,
                "circuit_seen_in_training": batch.circuit_seen_in_training,
                "support_status": endpoint.support_status,
                "expected_endpoint_lap_time_s": endpoint.expected_lap_time_s,
                "std_endpoint_lap_time_s": endpoint.std_lap_time_s,
                "p05_endpoint_lap_time_s": endpoint.p05_lap_time_s,
                "p25_endpoint_lap_time_s": endpoint.p25_lap_time_s,
                "p75_endpoint_lap_time_s": endpoint.p75_lap_time_s,
                "p95_endpoint_lap_time_s": endpoint.p95_lap_time_s,
                "observed_target_lap_time_s": actual,
                "target_is_clean_green": endpoint_reason is None,
                "target_exclusion_reason": endpoint_reason,
                "expected_total_time_s": sum(p.expected_lap_time_s for p in laps),
                "std_total_time_s": float(np.std(totals, ddof=1)),
                "p05_total_time_s": p05, "p25_total_time_s": p25,
                "p75_total_time_s": p75, "p95_total_time_s": p95,
                "observed_cumulative_time_s": actual_total,
                "trajectory_is_complete_clean": actual_total is not None,
                "trajectory_censoring_reason": censor,
                "endpoint_sample_crps_s": (
                    _empirical_crps(batch.lap_samples_s[:, end - 1], actual)
                    if actual is not None else None
                ),
                "cumulative_sample_crps_s": (
                    _empirical_crps(totals, actual_total) if actual_total is not None else None
                ),
            })
    return rows


def _metrics(frame: pd.DataFrame, horizons: list[int]) -> dict:
    cohorts = {}
    if frame.empty:
        return cohorts
    for cohort, group in frame.groupby("cohort"):
        horizons_metrics = {}
        for horizon in horizons:
            laps = compute_lap_horizon_metrics(group, horizon)
            totals = compute_cumulative_trajectory_metrics(group, horizon)
            sub = group[group.requested_horizon == horizon]
            for col, metrics, lo, hi in (
                ("endpoint_sample_crps_s", laps, "p05_endpoint_lap_time_s", "p95_endpoint_lap_time_s"),
                ("cumulative_sample_crps_s", totals, "p05_total_time_s", "p95_total_time_s"),
            ):
                eligible = sub[sub[col].notna()]
                metrics["race_weighted_empirical_crps_s"] = (
                    float(eligible.groupby("session_key")[col].mean().mean())
                    if not eligible.empty else None
                )
                metrics["mean_90_interval_width_s"] = (
                    float((eligible[hi] - eligible[lo]).mean()) if not eligible.empty else None
                )
            horizons_metrics[str(horizon)] = {"endpoint": laps, "cumulative": totals}
        cohorts[str(cohort)] = horizons_metrics
    return cohorts


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, default=str, allow_nan=False) + "\n", encoding="utf-8")


def _write_parquet(path: Path, frame: pd.DataFrame) -> None:
    with duckdb.connect() as con:
        con.register("s1_frame", frame)
        con.execute("COPY s1_frame TO ? (FORMAT PARQUET)", [str(path)])


def _field_diagnostics(
    field: FieldPaceForecast, output: Path, realizations: int,
) -> tuple[dict, dict]:
    if not 1 <= realizations <= field.sample_count:
        raise ValueError("Field realizations must be between one and the saved trajectory count")
    mean = simulate_field_pace(field)
    first = simulate_field_pace(field, sample_index=0)
    if simulate_field_pace(field, sample_index=0) != first:
        raise AssertionError("Same field draw must produce identical events and accounting")
    ensemble = []
    for index in range(-1, realizations):
        if index == -1:
            run = mean
        elif index == 0:
            run = first
        else:
            run = simulate_field_pace(field, sample_index=index)
        totals = {}
        for lap in run.laps:
            driver = lap.driver_number
            totals.setdefault(driver, lap.crossing_us - lap.clean_lap_us)
            totals[driver] += lap.clean_lap_us
            if totals[driver] != lap.crossing_us or lap.interaction_delay_us:
                raise AssertionError("Free-running checkpoint durations failed conservation")
        if index >= 0:
            for car in field.snapshot.drivers:
                ensemble.append({
                    "sample_index": index, "driver_number": car.driver_number,
                    "final_crossing_s": totals.get(car.driver_number, 0) / 1e6
                    if car.driver_number in totals else None,
                    "unsupported_reason": run.unsupported.get(car.driver_number),
                })
    ensemble_frame = pd.DataFrame(ensemble)
    _write_parquet(output / "field_ensemble.parquet", ensemble_frame)
    np.savez_compressed(output / "trajectory_samples.npz", **{
        str(driver): batch.lap_samples_s for driver, batch in field.batches.items()
    })
    _write_json(output / "snapshot.json", asdict(field.snapshot))
    _write_json(output / "sample_trace.json", {
        "seed": field.master_seed, "sample_index": 0,
        "events": [{**asdict(frame.event), "kind": frame.event.kind.name}
                   for frame in first.run.trace],
        "laps": [asdict(lap) for lap in first.laps],
        "final_state": asdict(first.run.final_state),
        "unsupported": first.unsupported,
    })
    supported_by_lap = {}
    for car in field.snapshot.drivers:
        if car.driver_number in field.batches:
            supported_by_lap.setdefault(car.completed_laps, []).append(car)
    same_lap = max(supported_by_lap.values(), key=len)
    following = []
    if len(same_lap) >= 2:
        ordered = sorted(same_lap, key=lambda car: (
            field.batches[car.driver_number].forecast.expected_total_time_s
        ))
        pair = tuple(replace(car, reported_position=rank, elapsed_current_lap_s=0,
                             last_crossing_utc=field.snapshot.as_of_utc)
                     for rank, car in enumerate((ordered[-1], ordered[0]), start=1))
        synthetic = replace(field, snapshot=replace(field.snapshot, drivers=pair),
                            unsupported={}, batches={car.driver_number: field.batches[car.driver_number]
                                                      for car in pair})
        for gap in (0.05, 0.1, 0.25, 0.5):
            constrained = simulate_field_pace(synthetic, timing=TimingMode.NO_PASS, minimum_gap_s=gap)
            delay = sum(lap.interaction_delay_us for lap in constrained.laps)
            following.append({"minimum_gap_s": gap, "interaction_delay_s": delay / 1e6})
    return {
        "session_key": field.snapshot.session_key,
        "cutoff_utc": field.snapshot.as_of_utc.isoformat(),
        "sampled_drivers": len(field.batches), "sample_count": field.sample_count,
        "field_runs": realizations + 1, "mean_events": len(mean.run.trace),
        "mean_completed_laps": len(mean.laps), "mean_unsupported": mean.unsupported,
        "sample_zero_events": len(first.run.trace), "sample_zero_unsupported": first.unsupported,
        "reproduced_sample_zero": True, "synthetic_no_pass": following,
        "ensemble_driver_runs_excluded": int(ensemble_frame.unsupported_reason.notna().sum()),
    }, {"field": field, "mean": mean, "sample": first}


def run_stage5_s1(
    root: Path, stage3_root: Path, warehouse: Path, holdout_warehouse: Path,
    config_path: Path, output: Path,
) -> dict:
    """Run frozen numerical parity, field mechanics and historical clean-air evaluation."""
    config_bytes = config_path.read_bytes()
    config = yaml.safe_load(config_bytes)
    bundle = load_pace_bundle(root, stage3_root)
    fixture = root / "tests/fixtures/stage5_s1_d8_reference.json"
    parity = reference_parity(bundle, fixture)
    output.mkdir(parents=True, exist_ok=True)
    (output / "config.yaml").write_bytes(config_bytes)
    rows, skipped, opportunities = [], [], []
    diagnostics = None
    plot_data = None
    horizons = config["horizons"]
    for cohort, races in config["cohorts"].items():
        source = holdout_warehouse if cohort == "new_to_pace" else warehouse
        if not source.is_file():
            skipped.extend({"cohort": cohort, "session_key": race["session_key"],
                            "reason": "WAREHOUSE_MISSING"} for race in races)
            continue
        with duckdb.connect(str(source), read_only=True) as con:
            con.execute("SET TimeZone = 'UTC'")
            for race in races:
                session = race["session_key"]
                for leader_lap in config["leader_completed_laps"]:
                    cutoff = con.execute("""
                        SELECT min(date_start) FROM silver.laps
                        WHERE session_key = ? AND lap_number = ?
                    """, [session, leader_lap + 1]).fetchone()[0]
                    identity = {"cohort": cohort, "session_key": session, "leader_lap": leader_lap,
                                "cutoff_utc": cutoff.isoformat() if cutoff is not None else None}
                    if cutoff is None:
                        skipped.append({**identity, "reason": "CUTOFF_UNAVAILABLE"})
                        continue
                    if cohort == "transfer_2026":
                        skipped.append({**identity, "reason": "NO_FROZEN_2026_MODEL_BUNDLE"})
                        continue
                    snapshot = build_field_snapshot(source, session, cutoff, race["scheduled_laps"],
                                                    max(horizons))
                    if snapshot.track_state is not TrackState.GREEN:
                        skipped.append({**identity, "reason": f"TRACK_{snapshot.track_state}"})
                        continue
                    if not any(driver.request for driver in snapshot.drivers):
                        skipped.append({**identity, "reason": "NO_SUPPORTED_DRIVER_INPUTS"})
                        continue
                    field = forecast_field(snapshot, bundle, seed=config["seed"], samples=config["samples"])
                    opportunities.append({**identity, "forecast_drivers": len(field.batches),
                                          "unsupported_drivers": field.unsupported})
                    if cohort == "development" and leader_lap == 30:
                        diagnostics, plot_data = _field_diagnostics(
                            field, output, config.get("field_realizations", 10),
                        )
                    labels = con.execute("""
                        SELECT driver_number, lap_number, stint_number, lap_start_utc,
                            observed_lap_time_s, is_clean_green_pace_lap, exclusion_reason,
                            is_pit_in_lap, is_pit_out_lap
                        FROM gold.lap_context WHERE session_key = ?
                    """, [session]).df()
                    rows.extend(_evaluation_rows(field, labels, cohort, leader_lap, horizons))
    frame = pd.DataFrame(rows)
    if not frame.empty:
        _write_parquet(output / "forecast_summary.parquet", frame)
    report = {
        "model_version": bundle.model.model_version, "model_sha256": bundle.artifact_sha256,
        "trained_through_utc": bundle.trained_through_utc.isoformat(),
        "calibrated_through_utc": bundle.calibrated_through_utc.isoformat(),
        "training_sessions": bundle.training_sessions, "calibration_sessions": bundle.calibration_sessions,
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(), "seed": config["seed"],
        "warehouses": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in (warehouse, holdout_warehouse) if path.is_file()},
        "parity": parity, "field_mechanics": diagnostics, "opportunities": opportunities,
        "skipped": skipped, "metrics": _metrics(frame, horizons),
    }
    _write_json(output / "metrics.json", report)
    if plot_data is not None:
        from f1ri.validation.pace_integration_plots import plot_pace_integration

        plot_pace_integration(report, frame, plot_data, fixture, output)
    return report
