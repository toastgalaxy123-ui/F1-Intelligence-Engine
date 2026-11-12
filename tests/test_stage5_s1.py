from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pytest
from test_stint_forecast import _build_stage3_training_df

from f1ri.features.race_state import DriverSnapshot, FieldSnapshot, build_field_snapshot
from f1ri.models.contracts import StintForecastRequest
from f1ri.models.stint_forecast import build_stage3_experiment_models
from f1ri.simulator.engine import TimingMode
from f1ri.simulator.pace_adapter import PaceBundle, forecast_pace, load_pace_bundle
from f1ri.simulator.pace_field import forecast_field, simulate_field_pace
from f1ri.simulator.state import TrackState
from f1ri.warehouse.build import _load_silver_table

_START = datetime(2025, 10, 5, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def bundle():
    model = build_stage3_experiment_models()["D8_correlated_trajectory_uncertainty"]
    training = _build_stage3_training_df()
    model.fit(training, calibration_df=training)
    return PaceBundle(model, "synthetic", model.trained_through_utc_,
                      datetime(2025, 6, 1, tzinfo=UTC), (9472, 9590), (9987,), "2023_2025")


def _request(driver=1):
    return StintForecastRequest(
        9896, driver, 12, 13, 5, "MEDIUM", 12, _START + timedelta(seconds=1080),
        season=2025, circuit_short_name="Sakhir", compound_absolute="C2", scheduled_race_laps=57,
        qualifying_lap_time_s=95 if driver == 1 else 91,
        n_trajectory_samples=120, random_seed=42, retain_trajectory_samples=True,
    )


def test_adapter_preserves_full_joint_draw(bundle):
    request = _request()
    standalone = bundle.model.forecast_stint(request)
    batch = forecast_pace(bundle, request)
    assert batch.forecast == standalone
    assert batch.lap_samples_s.shape == (120, 5)
    np.testing.assert_array_equal(batch.lap_samples_s[:50], standalone.sampled_lap_trajectories_s)
    np.testing.assert_allclose(batch.lap_samples_s.cumsum(axis=1), batch.cumulative_samples_s,
                               atol=1e-10, rtol=0)
    assert np.corrcoef(batch.lap_samples_s.T)[0, 1] > 0.2


@pytest.mark.parametrize("changes, message", [
    ({"session_key": 9987}, "training and calibration"),
    ({"as_of_utc": datetime(2025, 5, 1, tzinfo=UTC)}, "training and calibration"),
    ({"regulation_era": "2026_PLUS"}, "supports"),
    ({"tyre_age_at_start": -1}, "continuation"),
    ({"horizon_laps": 21}, "continuation"),
])
def test_adapter_rejects_unsupported_requests(bundle, changes, message):
    with pytest.raises(ValueError, match=message):
        forecast_pace(bundle, replace(_request(), **changes))


def test_missing_model_fails_closed(tmp_path):
    with pytest.raises(FileNotFoundError, match="Frozen Stage 3 bundle is incomplete"):
        load_pace_bundle(Path(__file__).resolve().parents[1], tmp_path)


def _field():
    first, second = _request(1), _request(2)
    return FieldSnapshot(
        9896, _START, first.as_of_utc, 57, TrackState.GREEN, None,
        (DriverSnapshot(1, 12, first.as_of_utc, 0, 1, None, first, None),
         DriverSnapshot(2, 12, second.as_of_utc, 0, 2, "1.0", second, None)),
        (0.30, 0.38, 0.32),
    )


def test_seeded_field_samples_are_order_independent_and_no_pass_preserves_clean_time(bundle):
    snapshot = _field()
    field = forecast_field(snapshot, bundle, seed=99, samples=100)
    reordered = replace(snapshot, drivers=snapshot.drivers[::-1])
    reordered_field = forecast_field(reordered, bundle, seed=99, samples=100)
    different = forecast_field(snapshot, bundle, seed=100, samples=100)
    for driver, batch in field.batches.items():
        np.testing.assert_array_equal(batch.lap_samples_s,
                                      reordered_field.batches[driver].lap_samples_s)
        assert not np.array_equal(batch.lap_samples_s, different.batches[driver].lap_samples_s)
    sampled = simulate_field_pace(field, sample_index=17)
    assert sampled == simulate_field_pace(reordered_field, sample_index=17)
    free = simulate_field_pace(field)
    constrained = simulate_field_pace(field, timing=TimingMode.NO_PASS, minimum_gap_s=5)
    by_lap = {(lap.driver_number, lap.lap_number): lap.clean_lap_us for lap in free.laps}
    assert len(free.laps) == len(constrained.laps) == 10
    assert all(lap.interaction_delay_us == 0 for lap in free.laps)
    assert any(lap.interaction_delay_us > 0 for lap in constrained.laps)
    for lap in constrained.laps:
        assert lap.clean_lap_us == by_lap[lap.driver_number, lap.lap_number]
        assert lap.interaction_delay_us >= 0
    assert constrained.run.final_state.timing_order == (1, 2)


def test_partial_lap_consumes_elapsed_time_and_retains_tyre_age(bundle):
    snapshot = _field()
    first, second = snapshot.drivers
    second = replace(second, elapsed_current_lap_s=50,
                     last_crossing_utc=snapshot.as_of_utc - timedelta(seconds=50))
    field = forecast_field(replace(snapshot, drivers=(first, second)), bundle, seed=42, samples=100)
    run = simulate_field_pace(field, sample_index=0)
    assert run.run.capability == "S1"
    first_lap = next(lap for lap in run.laps if lap.driver_number == 2)
    assert first_lap.observed_elapsed_us == 50_000_000
    assert first_lap.crossing_us - 1_080_000_000 == first_lap.clean_lap_us - 50_000_000
    times = [frame.event.at_us for frame in run.run.trace]
    assert all(at > 1_080_000_000 for at in times) and times == sorted(times)
    assert all(car.tyres.age_laps == 17 for car in run.run.final_state.cars)
    with pytest.raises(ValueError, match="synchronized"):
        simulate_field_pace(field, timing=TimingMode.NO_PASS)


def _warehouse(path):
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE SCHEMA silver")
        tables = {
            "sessions": [{"meeting_key": 1, "session_name": "Race", "year": 2025,
                          "date_start": _START, "date_end": _START + timedelta(hours=2),
                          "circuit_key": 1, "circuit_short_name": "Sakhir"}],
            "drivers": [{"driver_number": d, "name_acronym": f"D{d}", "team_name": "Team"}
                        for d in (1, 2)],
            "starting_grid": [{"driver_number": d, "position": d} for d in (1, 2)],
            "race_control": [{"category": "SessionStatus", "message": "SESSION STARTED",
                              "date": _START}],
            "laps": [
                {"driver_number": d, "lap_number": lap, "lap_duration": 90.,
                 "date_start": _START + timedelta(seconds=(lap - 1) * 90 + 8 * (d - 1)),
                 "duration_sector_1": 27., "duration_sector_2": 35., "duration_sector_3": 28.,
                 "is_pit_out_lap": False, "meeting_key": 1}
                for d in (1, 2) for lap in range(1, 6)
            ],
            "stints": [{"driver_number": d, "stint_number": 1, "lap_start": 1, "lap_end": 5,
                        "tyre_age_at_start": 0, "compound": "MEDIUM"} for d in (1, 2)],
            "weather": [{"date": _START, "track_temperature": 32, "rainfall": False}],
            "position": [{"date": _START, "driver_number": d, "position": d} for d in (1, 2)],
            "intervals": [{"date": _START, "driver_number": 2, "gap_to_leader_raw": "8.0",
                           "interval_raw": "8.0"}],
            "pit": [],
            "session_result": [{"driver_number": d, "position": d, "number_of_laps": 5}
                               for d in (1, 2)],
        }
        for name, rows in tables.items():
            _load_silver_table(con, name, [dict(row, session_key=9896) for row in rows], 9896)


def test_asof_snapshot_and_forecast_ignore_future_source_mutations(tmp_path, bundle):
    warehouse = tmp_path / "race.duckdb"
    _warehouse(warehouse)
    cutoff = _START + timedelta(seconds=180)
    before = build_field_snapshot(warehouse, 9896, cutoff, 57, 5)
    assert before.track_state is TrackState.GREEN
    assert [d.completed_laps for d in before.drivers] == [2, 1]
    assert [d.elapsed_current_lap_s for d in before.drivers] == [0, 82]
    assert [d.request.tyre_age_at_start for d in before.drivers] == [2, 1]
    with duckdb.connect(str(warehouse)) as con:
        # Even a shorter eventual duration cannot create a completion before its observed next start.
        con.execute("""
            UPDATE silver.laps SET lap_duration = 1, is_pit_out_lap = true, duration_sector_1 = 1
            WHERE (driver_number = 1 AND lap_number >= 3) OR (driver_number = 2 AND lap_number >= 2)
        """)
        con.execute("UPDATE silver.stints SET lap_end = 999")
        con.execute("UPDATE silver.session_result SET position = 99, dnf = true")
        future = cutoff + timedelta(seconds=1)
        con.execute("INSERT INTO silver.pit VALUES (9896,1,2,2,?,20,2)", [future])
        con.execute("INSERT INTO silver.weather (session_key,date,rainfall) VALUES (9896,?,true)",
                    [future])
        con.execute("""
            INSERT INTO silver.race_control (session_key,date,category,flag,scope,message)
            VALUES (9896,?,'Flag','RED','Track','RED FLAG')
        """, [future])
    after = build_field_snapshot(warehouse, 9896, cutoff, 57, 5)
    assert before == after
    original = forecast_field(before, bundle, seed=42, samples=50)
    mutated = forecast_field(after, bundle, seed=42, samples=50)
    assert simulate_field_pace(original, sample_index=2) == simulate_field_pace(mutated, sample_index=2)
