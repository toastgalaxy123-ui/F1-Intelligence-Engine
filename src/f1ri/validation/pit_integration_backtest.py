"""Stage 5.2 mechanical experiments, cutoff-bound pace checks and replay audit."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml

from f1ri.features.pit_events import (
    add_transition_evidence,
    load_pit_annotations,
    reconstruct_pit_events,
)
from f1ri.features.pit_policy import apply_pit_eligibility
from f1ri.features.race_state import build_field_snapshot
from f1ri.features.stint_examples import DRY_COMPOUNDS
from f1ri.ingestion.compound_allocations import resolve_compound_allocation
from f1ri.simulator.events import EventKind
from f1ri.simulator.pace_adapter import (
    UnsupportedPaceForecast,
    forecast_new_stint,
    load_pace_bundle,
    reference_parity,
)
from f1ri.simulator.pace_field import forecast_field, simulate_field_pace
from f1ri.simulator.pit_execution import (
    PitCarPlan,
    PitStopRequest,
    PitTransitionPlan,
    ReplacementTyres,
    ResolvedPitRoute,
    simulate_pit_scenario,
)
from f1ri.simulator.pit_pace import (
    prepare_pace_pit_field,
    replacement_pace_request,
    simulate_pace_pit_field,
)
from f1ri.simulator.pit_providers import load_locked_p1
from f1ri.simulator.replay import replay_session
from f1ri.simulator.state import (
    CarState,
    RaceRuleset,
    RaceState,
    SimulationMode,
    TrackState,
    TyreState,
)


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, default=str, allow_nan=False) + "\n", encoding="utf-8")


def _write_parquet(path: Path, rows: list[dict]) -> None:
    if rows:
        with duckdb.connect() as con:
            con.register("s2_frame", pd.DataFrame(rows))
            con.execute("COPY s2_frame TO ? (FORMAT PARQUET)", [str(path)])


def _synthetic():
    start = datetime(2025, 1, 1, tzinfo=UTC)
    grid = RaceState(
        1, start, RaceRuleset(2025, 5), SimulationMode.CONDITIONAL_SIMULATION,
        "synthetic-s2", (CarState(63, 1, TyreState("MEDIUM", 2)),
                         CarState(4, 2, TyreState("MEDIUM", 1))), track_state=TrackState.GREEN,
    )
    route = ResolvedPitRoute(70, 74, 92, 3, 99, 103, 127, 157, 200, "A")
    request = PitStopRequest(1, 63, start, 2, ReplacementTyres("HARD", 4), "two-car")
    opponent = PitStopRequest(1, 4, start, 2, ReplacementTyres("HARD"), "double-stack")
    first = PitCarPlan(63, 0, (90.,) * 5, (PitTransitionPlan(request, route=route),))
    scenarios = {
        "resolved": simulate_pit_scenario(grid, (first, PitCarPlan(4, 0, (92.,) * 5))),
        "coarse": simulate_pit_scenario(grid, (
            PitCarPlan(63, 0, (90.,) * 5, (PitTransitionPlan(request, net_loss_s=20),)),
            PitCarPlan(4, 0, (92.,) * 5),
        )),
        "double_stack": simulate_pit_scenario(grid, (first, PitCarPlan(
            4, 0, (90.,) * 5, (PitTransitionPlan(opponent, route=route),),
        ))),
    }
    for label, expected in (("resolved", (20,)), ("coarse", (20,)), ("double_stack", (20, 23))):
        if tuple(stop.net_loss_us / 1e6 for stop in scenarios[label].stops) != expected:
            raise AssertionError(f"Synthetic {label} scenario failed the accounting identity")
    return scenarios


def _events(con, session: int, root: Path):
    frames = {name: con.execute(f"SELECT * FROM silver.{name} WHERE session_key = ?", [session]).df()
              for name in ("sessions", "pit", "laps", "stints", "drivers", "race_control")}
    intervals = con.execute(
        "SELECT * FROM gold.track_status_intervals WHERE session_key = ?", [session],
    ).df()
    if frames["sessions"].empty:
        return pd.DataFrame(), None
    info = frames["sessions"].iloc[0].to_dict()
    events = reconstruct_pit_events(
        frames["pit"], frames["laps"], frames["stints"], info, intervals,
        frames["drivers"], load_pit_annotations(root), frames["race_control"],
    )
    return apply_pit_eligibility(add_transition_evidence(
        events, frames["laps"], frames["race_control"],
    )), info


def _validate_replay(root: Path, warehouse: Path, race: dict, visits: pd.DataFrame) -> dict:
    session, distance = race["session_key"], race["scheduled_laps"]
    replay = replay_session(warehouse, session, distance, annotations_root=root)
    return {
        "session_key": session, "cohort": race["cohort"], "visits": len(visits),
        "recorded_lane_windows_replayed": replay.replayed_pit_visits,
        "screened_green_transition_candidates": int(visits.eligible_primary_label.sum()),
        "track_states_at_stops": dict(Counter(visits.transition_track_status)),
        "lap_count_agreement": sum(row.completed_laps == row.official_laps for row in replay.drivers),
        "classified_position_agreement": sum(row.classified_position == row.official_position
                                            and row.official_position is not None
                                            for row in replay.drivers),
        "official_classified": sum(row.official_position is not None for row in replay.drivers),
        "independently_resolved_merges": 0,
        "replay_issues": [asdict(issue) for issue in replay.issues],
    }


def _post_stop_rows(
    bundle, warehouse: Path, race: dict, visits: pd.DataFrame, info: dict,
    lap_context: pd.DataFrame, allocations: pd.DataFrame, seed: int, samples: int,
) -> tuple[list[dict], list[dict]]:
    """The realized tire choice is a declared condition for this retrospective evaluation."""
    rows, audit = [], []
    labels = {(r.driver_number, r.lap_number): r for r in lap_context.itertuples()}
    for visit in visits.itertuples():
        base = {"session_key": race["session_key"], "cohort": race["cohort"],
                "pit_event_id": visit.pit_event_id, "driver_number": visit.driver_number}
        if (
            not visit.eligible_direct_lane or visit.transition_track_status != "GREEN"
            or visit.old_compound not in DRY_COMPOUNDS or visit.new_compound not in DRY_COMPOUNDS
            or pd.isna(visit.new_tyre_initial_age) or pd.isna(visit.transition_start_utc)
        ):
            audit.append({**base, "status": "INELIGIBLE_OBSERVED_TRANSITION"})
            continue
        cutoff = visit.transition_start_utc.to_pydatetime()
        pit_lap = int(visit.resolved_in_lap)
        horizon = min(11, race["scheduled_laps"] - pit_lap)
        if horizon < 2:
            audit.append({**base, "status": "NO_FLYING_LAP_IN_WINDOW"})
            continue
        snapshot = build_field_snapshot(warehouse, race["session_key"], cutoff,
                                        race["scheduled_laps"], horizon)
        current = next((car.request for car in snapshot.drivers
                        if car.driver_number == visit.driver_number), None)
        if current is None or current.planned_start_lap != pit_lap:
            audit.append({**base, "status": "NO_CAUSAL_PRE_STOP_STATE"})
            continue
        mapping = resolve_compound_allocation(allocations, info["meeting_key"], visit.new_compound, cutoff)
        tires = ReplacementTyres(visit.new_compound, int(visit.new_tyre_initial_age),
                                 mapping["compound_absolute"])
        stop = PitStopRequest(race["session_key"], visit.driver_number, cutoff, pit_lap,
                              tires, "observed-choice-conditional")
        request = replacement_pace_request(current, stop, horizon=horizon, seed=seed, samples=samples)
        try:
            forecast = forecast_new_stint(bundle, request)
        except UnsupportedPaceForecast as error:
            audit.append({**base, "status": "PACE_UNSUPPORTED", "reason": str(error)})
            continue
        audit.append({**base, "status": "FORECAST", "cutoff_utc": cutoff.isoformat(),
                      "support": forecast.forecast.support_status})
        stint = int(visit.new_stint_id.rsplit(":", 1)[1])
        flying = forecast.forecast.lap_forecasts[1:]
        actuals = []
        for lap in flying:
            label = labels.get((visit.driver_number, lap.target_lap_number))
            actuals.append(float(label.observed_lap_time_s) if (
                label is not None and label.stint_number == stint and label.is_clean_green_pace_lap
                and pd.notna(label.observed_lap_time_s)
            ) else None)
        tags = {**base, "circuit": current.circuit_short_name,
                "circuit_seen": forecast.circuit_seen_in_training,
                "prior_clean_race_laps": current.clean_laps_observed_race,
                "old_compound": current.compound_relative, "new_compound": tires.compound,
                "initial_age": tires.initial_age_laps}
        eligible = [(lap, value) for lap, value in zip(flying, actuals, strict=True) if value is not None]
        for rank, (lap, actual) in enumerate(eligible[:3], start=1):
            rows.append({**tags, "target": f"ELIGIBLE_LAP_{rank}", "tyre_age": lap.projected_tyre_age,
                         "actual_s": actual, "predicted_s": lap.expected_lap_time_s,
                         "p05_s": lap.p05_lap_time_s, "p95_s": lap.p95_lap_time_s})
        for count in (5, 10):
            if len(actuals) < count or any(actual is None for actual in actuals[:count]):
                continue
            draws = forecast.lap_samples_s[:, 1:count + 1].sum(axis=1)
            low, high = np.quantile(draws, (.05, .95))
            rows.append({**tags, "target": f"COMPLETE_{count}", "tyre_age": flying[count - 1].projected_tyre_age,
                         "actual_s": sum(actuals[:count]),
                         "predicted_s": sum(lap.expected_lap_time_s for lap in flying[:count]),
                         "p05_s": float(low), "p95_s": float(high)})
    return rows, audit


def _score(rows: list[dict], group_field: str = "cohort") -> list[dict]:
    if not rows:
        return []
    frame = pd.DataFrame(rows)
    frame["error"] = frame.actual_s - frame.predicted_s
    frame["absolute_error"] = frame.error.abs()
    frame["covered"] = frame.actual_s.between(frame.p05_s, frame.p95_s)
    metrics = []
    for (population, target), group in frame.groupby([group_field, "target"], sort=True):
        metrics.append({
            group_field: population, "target": target, "n": len(group),
            "races": int(group.session_key.nunique()),
            "race_weighted_mae_s": float(group.groupby("session_key").absolute_error.mean().mean()),
            "observed_minus_predicted_s": float(group.error.mean()),
            "coverage_90": float(group.covered.mean()),
        })
    return metrics


def _last_line(run, driver: int, lap: int) -> int:
    return next(frame.event.at_us for frame in run.trace if frame.event.driver_number == driver
                and frame.event.kind is EventKind.LAP_COMPLETED and frame.event.checkpoint == lap * 3)


def _paired(bundle, warehouse: Path, config: dict, p1):
    scenario = config["scenario"]
    cutoff = datetime.fromisoformat(scenario["cutoff_utc"])
    driver, pit_lap = scenario["driver_number"], scenario["pit_in_lap"]
    snapshot = build_field_snapshot(warehouse, scenario["session_key"], cutoff,
                                    scenario["scheduled_laps"], scenario["horizon"])
    field = forecast_field(snapshot, bundle, seed=config["seed"], samples=config["pace_samples"])
    current = next(car.request for car in snapshot.drivers if car.driver_number == driver)
    target = field.batches[driver].forecast.planned_start_lap + scenario["horizon"] - 1
    variants = {}
    output = []
    for label in ("fresh", "used"):
        tires = ReplacementTyres(scenario["compound"], scenario[f"{label}_age"],
                                 scenario["compound_absolute"])
        request = PitStopRequest(scenario["session_key"], driver, cutoff, pit_lap, tires, label)
        plan = PitTransitionPlan(request, net_loss_s=scenario["synthetic_net_loss_s"])
        prepared = prepare_pace_pit_field(field, bundle, (plan,))
        batch = prepared.replacement_batches[driver]
        reference = replacement_pace_request(
            current, request, horizon=batch.forecast.horizon_laps, seed=batch.forecast.random_seed,
            samples=config["pace_samples"],
        )
        if batch.forecast != bundle.model.forecast_stint(reference):
            raise AssertionError("Stage 5.2 replacement trajectory differs from standalone D8")
        variants[label] = prepared
        if p1 is not None:
            variants[f"p1_{label}"] = prepare_pace_pit_field(
                field, bundle, (p1.expected_plan(request, current, seed=config["seed"]),),
            )
    for index in range(-1, config["paired_realizations"]):
        sample = None if index < 0 else index
        baseline = _last_line(simulate_field_pace(field, sample_index=sample).run, driver, target)
        for name, prepared in variants.items():
            run = simulate_pace_pit_field(prepared, sample_index=sample).scenario
            stop, = run.stops
            actual = _last_line(run.run, driver, target)
            expected = round(stop.plan.net_loss_s * 1e6)
            if stop.net_loss_us != expected:
                raise AssertionError("Integrated coarse forecast applied an incorrect pit adjustment")
            output.append({"scenario": name, "sample_index": index, "baseline_crossing_us": baseline,
                           "stop_crossing_us": actual, "delta_s": (actual - baseline) / 1e6,
                           "applied_net_s": expected / 1e6})
    return output, field, variants


def run_stage5_s2(
    root: Path, stage3_root: Path, warehouse: Path, local_warehouse: Path, config_path: Path,
    output: Path, p1_artifact: Path | None = None,
) -> dict:
    config_bytes = config_path.read_bytes()
    config = yaml.safe_load(config_bytes)
    if not 1 <= config["paired_realizations"] <= config["pace_samples"]:
        raise ValueError("Paired realizations must fit inside the saved pace ensemble")
    output.mkdir(parents=True, exist_ok=True)
    (output / "config.yaml").write_bytes(config_bytes)
    synthetic = _synthetic()
    bundle = load_pace_bundle(root, stage3_root)
    parity = reference_parity(bundle, root / "tests/fixtures/stage5_s1_d8_reference.json")
    if p1_artifact is None:
        relative = "artifacts/models/stage4_1_stabilization/P1.pkl"
        p1_artifact = next((path for path in (root / relative, stage3_root / relative)
                            if path.is_file()), None)
    p1 = load_locked_p1(root, p1_artifact) if p1_artifact is not None else None
    pairs, field, variants = _paired(bundle, warehouse, config, p1)
    audits, historical, scores, skips = [], [], [], []
    for race in config["historical"]:
        source = local_warehouse if race["source"] == "local" else warehouse
        if not source.is_file():
            skips.append({"session_key": race["session_key"], "reason": "WAREHOUSE_MISSING"})
            continue
        with duckdb.connect(str(source), read_only=True) as con:
            con.execute("SET TimeZone = 'UTC'")
            visits, info = _events(con, race["session_key"], root)
            if info is None or visits.empty:
                skips.append({"session_key": race["session_key"], "reason": "NO_RECORDED_VISITS"})
                continue
            audits.append(_validate_replay(root, source, race, visits))
            historical.extend(visits.to_dict("records"))
            if race["cohort"] == "replay_only":
                continue
            labels = con.execute(
                "SELECT * FROM gold.lap_context WHERE session_key = ?", [race["session_key"]],
            ).df()
            allocations = con.execute("SELECT * FROM silver.compound_allocations").df()
        rows, checked = _post_stop_rows(bundle, source, race, visits, info, labels, allocations,
                                        config["seed"], config["pace_samples"])
        scores.extend(rows)
        skips.extend(checked)
    _write_parquet(output / "paired_times.parquet", pairs)
    _write_parquet(output / "historical_pit_windows.parquet", historical)
    _write_parquet(output / "post_stop_scores.parquet", scores)
    _write_json(output / "post_stop_eligibility.json", skips)
    _write_json(output / "pit_transitions.json", {
        label: [asdict(stop) for stop in run.stops] for label, run in synthetic.items()
    })
    _write_json(output / "synthetic_trace.json", [
        {"event": asdict(frame.event), "kind": frame.event.kind.name,
         "cars": [asdict(car) for car in frame.state.cars]} for frame in synthetic["resolved"].run.trace
    ])
    paired_summary = []
    for name in variants:
        deltas = [row["delta_s"] for row in pairs if row["scenario"] == name and row["sample_index"] >= 0]
        low, high = np.quantile(deltas, (.05, .95))
        paired_summary.append({"scenario": name, "n": len(deltas), "mean_delta_s": float(np.mean(deltas)),
                               "p05_delta_s": float(low), "p95_delta_s": float(high)})
    report = {
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "warehouses": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in (warehouse, local_warehouse) if path.is_file()},
        "pace_model_sha256": bundle.artifact_sha256,
        "p1_model_sha256": p1.artifact_sha256 if p1 else None,
        "p1_status": "PROVISIONAL_EXPECTED_COST" if p1 else "LOCKED_ARTIFACT_UNAVAILABLE",
        "s1_parity_max_s": max(case["max_abs_delta_s"] for case in parity),
        "s2_replacement_parity": "EXACT_FRESH_AND_USED",
        "paired_means": [row for row in pairs if row["sample_index"] == -1],
        "paired_samples_per_scenario": config["paired_realizations"],
        "paired_sample_summary": paired_summary,
        "cross_stint_draws": variants["fresh"].cross_stint_draws,
        "historical_replay": audits,
        "eligibility": dict(Counter(row.get("status", row.get("reason")) for row in skips)),
        "post_stop_metrics": _score(scores),
        "post_stop_by_circuit": _score(scores, "circuit"),
    }
    _write_json(output / "metrics.json", report)
    from f1ri.validation.pit_integration_plots import plot_pit_integration

    plot_pit_integration(synthetic, field, variants, pairs, scores, historical, output)
    return report
