"""Connect S1 continuation and frozen D8 replacement-tire forecasts to pit execution."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import timedelta

from f1ri.models.contracts import SCENARIO_NEW_FRESH, SCENARIO_NEW_USED, StintForecastRequest
from f1ri.simulator.engine import checkpoint_offsets_us
from f1ri.simulator.pace_adapter import PaceBundle, PaceTrajectoryBatch, forecast_new_stint
from f1ri.simulator.pace_field import FieldPaceForecast, simulate_field_pace
from f1ri.simulator.pit_execution import (
    PitCarPlan,
    PitScenarioRun,
    PitStopRequest,
    PitTransitionPlan,
    simulate_pit_scenario,
)
from f1ri.simulator.state import (
    CarState,
    RacePhase,
    RaceRuleset,
    RaceState,
    SimulationMode,
    TrackState,
    TyreState,
)


def replacement_pace_request(
    current: StintForecastRequest, stop: PitStopRequest, *, horizon: int, seed: int, samples: int,
) -> StintForecastRequest:
    """Preserve cutoff-known race history and clear the previous set's latent observations."""
    if (
        (current.session_key, current.driver_number, current.as_of_utc)
        != (stop.session_key, stop.driver_number, stop.as_of_utc)
        or stop.pit_in_lap < current.planned_start_lap or stop.track_state is not TrackState.GREEN
    ):
        raise ValueError("Replacement pace requires a matching future pit request")
    replacement = stop.replacement
    out_lap = stop.pit_in_lap + 1
    offset = out_lap - current.planned_start_lap
    return replace(
        current, planned_start_lap=out_lap, horizon_laps=horizon,
        compound_relative=replacement.compound, compound_absolute=replacement.compound_absolute,
        compound_mapping_confidence="SCENARIO" if replacement.compound_absolute else "UNKNOWN",
        tyre_age_at_start=replacement.initial_age_laps,
        scenario_type=SCENARIO_NEW_USED if replacement.initial_age_laps else SCENARIO_NEW_FRESH,
        stint_number=current.stint_number + 1,
        median_stint_clean_laps_s=None, clean_laps_observed_current_stint=0,
        observed_stint_clean_ages=(), observed_stint_clean_times_s=(),
        observed_stint_clean_progress=(),
        last_clean_lap_age=(current.last_clean_lap_age + offset
                            if current.last_clean_lap_age is not None else None),
        prev_stint_compound_relative=current.compound_relative,
        prev_stint_compound_absolute=current.compound_absolute,
        prev_stint_end_tyre_age=current.tyre_age_at_start + offset - 1,
        n_trajectory_samples=samples, random_seed=seed, retain_trajectory_samples=True,
    )


@dataclass(frozen=True, slots=True)
class PacePitForecast:
    field: FieldPaceForecast
    stops: dict[int, PitTransitionPlan]
    replacement_batches: dict[int, PaceTrajectoryBatch]
    cross_stint_draws: str = "INDEPENDENT_D8_STINT_DRAWS_WITH_SHARED_CUTOFF_HISTORY"


def prepare_pace_pit_field(
    field: FieldPaceForecast, bundle: PaceBundle, stops: tuple[PitTransitionPlan, ...],
) -> PacePitForecast:
    """Freeze one explicit pace-mode stop per driver, keeping S1's pre-stop sample prefix."""
    by_driver = {plan.request.driver_number: plan for plan in stops}
    if len(by_driver) != len(stops):
        raise ValueError("Pace V2 compatibility mode supports one planned replacement per driver")
    requests = {driver.driver_number: driver.request for driver in field.snapshot.drivers}
    batches = {}
    for driver, plan in sorted(by_driver.items()):
        current = field.batches.get(driver)
        if current is None or current.artifact_sha256 != bundle.artifact_sha256:
            raise ValueError("Pitting driver requires the same supported S1 model bundle")
        forecast = current.forecast
        after = forecast.planned_start_lap + forecast.horizon_laps - plan.request.pit_in_lap - 1
        if after <= 0:
            raise ValueError("S2 requires both in-lap and out-lap within the forecast window")
        key = (f"s2-d8-v1:{current.random_stream_id}:{plan.request.pit_in_lap}:"
               f"{plan.request.replacement.compound}:{plan.request.replacement.initial_age_laps}")
        seed = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")
        request = replacement_pace_request(
            requests[driver], plan.request, horizon=after, seed=seed, samples=field.sample_count,
        )
        batch = forecast_new_stint(bundle, request)
        batches[driver] = batch
        by_driver[driver] = replace(plan, new_pace_trajectory_id=batch.random_stream_id)
    return PacePitForecast(field, by_driver, batches)


@dataclass(frozen=True, slots=True)
class PacePitRun:
    scenario: PitScenarioRun
    unsupported: dict[int, str]


def _lap_times(batch: PaceTrajectoryBatch, sample_index: int | None) -> tuple[float, ...]:
    if sample_index is None:
        return tuple(lap.expected_lap_time_s for lap in batch.forecast.lap_forecasts)
    return tuple(batch.lap_samples_s[sample_index])


def simulate_pace_pit_field(
    forecast: PacePitForecast, *, sample_index: int | None = None,
) -> PacePitRun:
    """Run a paired stop scenario using identical S1 samples up to each declared diversion."""
    field = forecast.field
    if not forecast.stops:
        run = simulate_field_pace(field, sample_index=sample_index)
        return PacePitRun(PitScenarioRun(run.run, ()), run.unsupported)
    if sample_index is not None and not 0 <= sample_index < field.sample_count:
        raise ValueError("Sample index is outside the saved field ensemble")
    snapshot = field.snapshot
    cutoff = (snapshot.as_of_utc - snapshot.race_start_utc) // timedelta(microseconds=1)
    cars, plans, unsupported, used_positions = [], [], dict(field.unsupported), set()
    drivers = sorted(snapshot.drivers, key=lambda driver: (
        driver.reported_position is None, driver.reported_position or 0, driver.driver_number,
    ))
    for driver in drivers:
        number = driver.driver_number
        if number not in field.batches:
            continue
        batch = field.batches[number]
        request = driver.request
        clean = _lap_times(batch, sample_index)
        pit = forecast.stops.get(number)
        if pit is not None:
            before = pit.request.pit_in_lap - batch.forecast.planned_start_lap + 1
            clean = clean[:before] + _lap_times(forecast.replacement_batches[number], sample_index)
        last_line = (driver.last_crossing_utc - snapshot.race_start_utc) // timedelta(microseconds=1)
        elapsed = cutoff - last_line
        offsets = checkpoint_offsets_us(clean[0], snapshot.sector_fractions)
        if elapsed >= offsets[-1]:
            if pit is not None:
                raise ValueError("Pitting driver has unsupported current-lap progress")
            unsupported[number] = "PARTIAL_LAP_OUTSIDE_SAMPLED_PACE"
            continue
        passed = sum(offset <= elapsed for offset in offsets)
        checkpoint = driver.completed_laps * 3 + passed
        crossing = last_line + offsets[passed - 1] if passed else last_line if checkpoint else None
        position = driver.reported_position
        if position in used_positions:
            position = None
        if position is not None:
            used_positions.add(position)
        cars.append(CarState(
            number, position,
            TyreState(request.compound_relative, request.tyre_age_at_start, request.stint_number,
                      compound_absolute=request.compound_absolute),
            team=request.team_id, completed_checkpoints=checkpoint, last_crossing_us=crossing,
        ))
        plans.append(PitCarPlan(number, last_line, clean, (pit,) if pit is not None else ()))
    initial = RaceState(
        snapshot.session_key, snapshot.race_start_utc,
        RaceRuleset(next(d.request.season for d in drivers if d.request), snapshot.scheduled_laps),
        SimulationMode.CONDITIONAL_SIMULATION,
        f"s2-{snapshot.session_key}-{cutoff}-{field.master_seed}-{sample_index}-"
        + hashlib.sha256(repr(sorted(forecast.stops.items())).encode()).hexdigest()[:12],
        tuple(cars), race_clock_us=cutoff, phase=RacePhase.RUNNING, track_state=snapshot.track_state,
    )
    return PacePitRun(simulate_pit_scenario(initial, tuple(plans),
                                           sector_fractions=snapshot.sector_fractions), unsupported)
