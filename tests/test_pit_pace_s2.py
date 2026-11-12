from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import duckdb
import numpy as np
import pytest
from test_stage5_s1 import _field, _warehouse
from test_stage5_s1 import bundle as _shared_bundle

from f1ri.features.race_state import build_field_snapshot
from f1ri.models.contracts import SCENARIO_NEW_FRESH, SCENARIO_NEW_USED, PitLossRequest
from f1ri.models.pit_calibration import CalibratedPitLossModel
from f1ri.models.pit_loss import PitLossModelV1
from f1ri.simulator.events import EventKind
from f1ri.simulator.pace_adapter import UnsupportedPaceForecast, forecast_new_stint
from f1ri.simulator.pace_field import forecast_field, simulate_field_pace
from f1ri.simulator.pit_execution import PitStopRequest, PitTransitionPlan, ReplacementTyres
from f1ri.simulator.pit_pace import (
    prepare_pace_pit_field,
    replacement_pace_request,
    simulate_pace_pit_field,
)
from f1ri.simulator.pit_providers import P1_ROLE, P1PitLossFallback, load_locked_p1
from f1ri.simulator.state import TrackState

bundle = _shared_bundle


@pytest.mark.parametrize("compound, initial_age, scenario", [
    ("MEDIUM", 0, SCENARIO_NEW_FRESH), ("HARD", 3, SCENARIO_NEW_USED),
])
def test_new_stint_matches_standalone_and_preserves_cutoff_history(
    bundle, compound, initial_age, scenario,
):
    snapshot = _field()
    current = replace(snapshot.drivers[0].request, clean_laps_observed_race=6,
                      clean_laps_observed_current_stint=3, median_last_3_clean_laps_s=94.5,
                      median_stint_clean_laps_s=94.5, last_clean_lap_age=2,
                      observed_stint_clean_ages=(8, 9, 10),
                      observed_stint_clean_times_s=(94.4, 94.5, 94.6),
                      observed_stint_clean_progress=(0.15, 0.17, 0.19))
    stop = PitStopRequest(9896, 1, snapshot.as_of_utc, 14,
                          ReplacementTyres(compound, initial_age, "C2"), "test")
    request = replacement_pace_request(current, stop, horizon=3, seed=12, samples=100)
    assert request.scenario_type == scenario
    assert request.as_of_utc == current.as_of_utc and request.cutoff_lap_number == 12
    assert request.planned_start_lap == 15 and request.prev_stint_end_tyre_age == 13
    assert request.clean_laps_observed_race == 6 and request.median_last_3_clean_laps_s == 94.5
    assert request.last_clean_lap_age == 4
    assert request.clean_laps_observed_current_stint == 0
    assert request.median_stint_clean_laps_s is None
    assert request.observed_stint_clean_times_s == ()
    standalone = bundle.model.forecast_stint(request)
    batch = forecast_new_stint(bundle, request)
    assert batch.forecast == standalone
    np.testing.assert_array_equal(batch.lap_samples_s[:50], standalone.sampled_lap_trajectories_s)
    assert [lap.projected_tyre_age for lap in standalone.lap_forecasts] == list(
        range(initial_age, initial_age + 3)
    )


def test_paired_field_preserves_pre_pit_samples_and_no_stop_trace(bundle):
    snapshot = _field()
    field = forecast_field(snapshot, bundle, seed=42, samples=100)
    baseline = simulate_field_pace(field, sample_index=17)
    no_stop = simulate_pace_pit_field(prepare_pace_pit_field(field, bundle, ()), sample_index=17)
    assert no_stop.scenario.run == baseline.run
    stop = PitStopRequest(9896, 1, snapshot.as_of_utc, 14, ReplacementTyres("MEDIUM"), "test")
    scenario = prepare_pace_pit_field(field, bundle, (PitTransitionPlan(stop, net_loss_s=22),))
    with_stop = simulate_pace_pit_field(scenario, sample_index=17)
    assert with_stop == simulate_pace_pit_field(scenario, sample_index=17)
    result, = with_stop.scenario.stops
    assert result.net_loss_us == 22_000_000
    assert result.plan.new_pace_trajectory_id == scenario.replacement_batches[1].random_stream_id
    assert "INDEPENDENT" in scenario.cross_stint_draws
    baseline_crossings = [f.event.at_us for f in baseline.run.trace if f.event.driver_number == 1
                          and f.event.kind is EventKind.LAP_COMPLETED]
    s2_crossings = [f.event.at_us for f in with_stop.scenario.run.trace if f.event.driver_number == 1
                    and f.event.kind is EventKind.LAP_COMPLETED]
    assert baseline_crossings[:2] == s2_crossings[:2]
    assert with_stop.scenario.run.final_state.cars[0].tyres.age_laps == 3


def test_unseen_circuit_cold_start_and_neutralized_pit_are_rejected(bundle):
    snapshot = _field()
    current = replace(snapshot.drivers[0].request, circuit_short_name="Baku")
    stop = PitStopRequest(9896, 1, snapshot.as_of_utc, 14, ReplacementTyres("MEDIUM"), "test")
    with pytest.raises(UnsupportedPaceForecast, match="observed clean race pace"):
        forecast_new_stint(bundle, replacement_pace_request(current, stop, horizon=3, seed=1,
                                                            samples=100))
    with pytest.raises(ValueError, match="future pit request"):
        replacement_pace_request(current, replace(stop, track_state=TrackState.SC),
                                 horizon=3, seed=1, samples=100)


def test_p1_loader_requires_original_frozen_bytes(tmp_path):
    root = Path(__file__).resolve().parents[1]
    artifact = tmp_path / "P1.pkl"
    with pytest.raises(FileNotFoundError, match="artifact is missing"):
        load_locked_p1(root, artifact)
    artifact.write_bytes(b"a different model")
    with pytest.raises(ValueError, match="digest does not match"):
        load_locked_p1(root, artifact)


def test_p1_adapter_preserves_expected_net_and_support_on_synthetic_fit(synthetic_pit_population):
    snapshot = _field()
    current = snapshot.drivers[0].request
    raw = PitLossModelV1(experiment_id="P1").fit(
        synthetic_pit_population, training_cutoff_utc=snapshot.as_of_utc,
    )
    model = CalibratedPitLossModel(raw)
    fallback = P1PitLossFallback(model, "synthetic-fixture", (), raw.trained_through_utc_)
    stop = PitStopRequest(9896, 1, snapshot.as_of_utc, 14, ReplacementTyres("HARD", 3), "test")
    plan = fallback.expected_plan(stop, current, seed=11, samples=100)
    standalone = model.predict_pit_loss(PitLossRequest(
        session_key=9896, driver_number=1, circuit_short_name="Sakhir", season=2025,
        team_id="UNKNOWN_TEAM", as_of_utc=snapshot.as_of_utc, latest_completed_lap=12,
        intended_pit_lap=14, old_compound="MEDIUM", old_tyre_age=14, new_compound="HARD",
        new_tyre_initial_age=3, rainfall=False, n_samples=100, random_seed=11,
    ))
    assert plan.net_loss_s == standalone.expected_loss_s
    assert plan.support_status == standalone.support_status
    assert plan.route is None and plan.cost_source == P1_ROLE
    with pytest.raises(ValueError, match="pre-stop"):
        fallback.expected_plan(replace(stop, track_state=TrackState.VSC), current)


def test_explicit_replacement_ignores_actual_future_tires_and_pit_service(tmp_path, bundle):
    warehouse = tmp_path / "race.duckdb"
    _warehouse(warehouse)
    start = _field().race_start_utc
    cutoff = start + timedelta(seconds=180)
    stop = PitStopRequest(9896, 1, cutoff, 4, ReplacementTyres("HARD", 2), "declared")
    plan = PitTransitionPlan(stop, net_loss_s=22)

    def forecast():
        snapshot = build_field_snapshot(warehouse, 9896, cutoff, 57, 5)
        field = forecast_field(snapshot, bundle, seed=42, samples=100)
        return prepare_pace_pit_field(field, bundle, (plan,))

    before = forecast()
    with duckdb.connect(str(warehouse)) as con:
        con.execute("UPDATE silver.stints SET lap_end = 3 WHERE driver_number = 1")
        con.execute("""
            INSERT INTO silver.stints (session_key, driver_number, stint_number, lap_start,
                                       lap_end, compound, tyre_age_at_start)
            VALUES (9896, 1, 2, 4, 5, 'SOFT', 7)
        """)
        con.execute("INSERT INTO silver.pit VALUES (9896,1,1,3,?,51,12)",
                    [cutoff + timedelta(seconds=85)])
        con.execute("UPDATE silver.session_result SET position = 8, dnf = true WHERE driver_number = 1")
    after = forecast()
    assert before.field.snapshot == after.field.snapshot
    first, second = before.replacement_batches[1], after.replacement_batches[1]
    assert first.forecast.compound_relative == "HARD" and first.forecast.tyre_age_at_start == 2
    np.testing.assert_array_equal(first.lap_samples_s, second.lap_samples_s)
    assert simulate_pace_pit_field(before, sample_index=2) == simulate_pace_pit_field(
        after, sample_index=2,
    )
