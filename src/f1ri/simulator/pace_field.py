"""Stable stochastic field draws and event scheduling for S1 continuation."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import timedelta

from f1ri.features.race_state import FieldSnapshot
from f1ri.simulator.engine import (
    RaceRun,
    TimingMode,
    checkpoint_offsets_us,
    run_checkpoint_window,
)
from f1ri.simulator.events import Event, EventKind
from f1ri.simulator.pace_adapter import (
    PaceBundle,
    PaceTrajectoryBatch,
    UnsupportedPaceForecast,
    forecast_pace,
)
from f1ri.simulator.state import (
    CarState,
    RacePhase,
    RaceRuleset,
    RaceState,
    SimulationMode,
    TrackState,
    TyreState,
    elapsed_us,
)


@dataclass(frozen=True, slots=True)
class FieldPaceForecast:
    snapshot: FieldSnapshot
    master_seed: int
    sample_count: int
    batches: dict[int, PaceTrajectoryBatch]
    unsupported: dict[int, str]


def forecast_field(
    snapshot: FieldSnapshot, bundle: PaceBundle, *, seed: int, samples: int,
) -> FieldPaceForecast:
    if snapshot.track_state is not TrackState.GREEN or seed < 0 or samples < 2:
        raise ValueError("S1 requires green track status, nonnegative seed and at least two samples")
    batches, unsupported = {}, {}
    for driver in sorted(snapshot.drivers, key=lambda car: car.driver_number):
        request = driver.request
        if request is None:
            unsupported[driver.driver_number] = driver.support_reason or "UNKNOWN"
            continue
        if (
            request.session_key != snapshot.session_key or request.as_of_utc != snapshot.as_of_utc
            or request.driver_number != driver.driver_number
            or request.cutoff_lap_number != driver.completed_laps
            or request.scheduled_race_laps != snapshot.scheduled_laps
            or driver.last_crossing_utc is None
            or driver.elapsed_current_lap_s is None
            or not 0 <= driver.elapsed_current_lap_s
            == (snapshot.as_of_utc - driver.last_crossing_utc).total_seconds()
        ):
            raise ValueError("Driver pace request differs from the field snapshot")
        key = (f"s1-d8-v1:{seed}:{snapshot.session_key}:{snapshot.as_of_utc.isoformat()}:"
               f"{driver.driver_number}:{request.stint_number}")
        driver_seed = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")
        try:
            batches[driver.driver_number] = forecast_pace(
                bundle, replace(request, n_trajectory_samples=samples, random_seed=driver_seed),
            )
        except UnsupportedPaceForecast:
            unsupported[driver.driver_number] = "PACE_V2_UNSUPPORTED"
    if not batches:
        raise ValueError("No drivers have supported green-flag continuation inputs")
    return FieldPaceForecast(snapshot, seed, samples, batches, unsupported)


@dataclass(frozen=True, slots=True)
class LapAccounting:
    driver_number: int
    lap_number: int
    clean_lap_us: int
    observed_elapsed_us: int
    interaction_delay_us: int
    crossing_us: int


@dataclass(frozen=True, slots=True)
class FieldPaceRun:
    run: RaceRun
    laps: tuple[LapAccounting, ...]
    unsupported: dict[int, str]


def simulate_field_pace(
    field: FieldPaceForecast,
    *,
    sample_index: int | None = None,
    timing: TimingMode = TimingMode.FREE_RUNNING,
    minimum_gap_s: float = 0.1,
) -> FieldPaceRun:
    """Use the full mean or a joint sample row from each driver's original trajectory."""
    if sample_index is not None and not 0 <= sample_index < field.sample_count:
        raise ValueError("Sample index is outside the saved field ensemble")
    if timing not in (TimingMode.FREE_RUNNING, TimingMode.NO_PASS):
        raise ValueError("Unknown S1 timing mode")
    snapshot = field.snapshot
    fractions = snapshot.sector_fractions
    if len(fractions) != 3 or not all(0 < part < 1 for part in fractions) or abs(sum(fractions) - 1) > 1e-12:
        raise ValueError("Three positive checkpoint fractions must sum to one")
    cutoff = (snapshot.as_of_utc - snapshot.race_start_utc) // timedelta(microseconds=1)
    drivers = [driver for driver in snapshot.drivers if driver.driver_number in field.batches]
    if timing is TimingMode.NO_PASS:
        positions = {driver.reported_position for driver in drivers}
        if (
            field.unsupported or None in positions or len(positions) != len(drivers)
            or len({driver.completed_laps for driver in drivers}) != 1
            or any(driver.elapsed_current_lap_s != 0 for driver in drivers)
            or len({field.batches[d.driver_number].forecast.horizon_laps for d in drivers}) != 1
            or elapsed_us(minimum_gap_s) == 0
        ):
            raise ValueError("No-pass requires a synchronized, fully supported field with order")
    drivers.sort(key=lambda d: (d.reported_position is None, d.reported_position or 0,
                               d.driver_number))
    events, cars = [], []
    ledger: dict[tuple[int, int], LapAccounting] = {}
    ahead: dict[int, int] = {}
    unsupported = dict(field.unsupported)
    used_positions: set[int] = set()
    for driver in drivers:
        batch = field.batches[driver.driver_number]
        forecast = batch.forecast
        clean = (
            [lap.expected_lap_time_s for lap in forecast.lap_forecasts] if sample_index is None
            else batch.lap_samples_s[sample_index]
        )
        offsets = [checkpoint_offsets_us(value, fractions) for value in clean]
        elapsed = elapsed_us(driver.elapsed_current_lap_s)
        if elapsed >= offsets[0][-1]:
            unsupported[driver.driver_number] = "PARTIAL_LAP_OUTSIDE_SAMPLED_PACE"
            continue
        base_checkpoint = forecast.cutoff_lap_number * 3
        passed = sum(value <= elapsed for value in offsets[0])
        last = cutoff - elapsed
        position = driver.reported_position
        if position in used_positions:
            position = None
        if position is not None:
            used_positions.add(position)
        cars.append(CarState(
            driver.driver_number, position,
            TyreState(forecast.compound_relative, forecast.tyre_age_at_start,
                      driver.request.stint_number),
            completed_checkpoints=base_checkpoint + passed,
            last_crossing_us=(
                last + offsets[0][passed - 1] if passed else last if base_checkpoint else None
            ),
        ))
        now = last
        crossings = {}
        for i, (first, second, total) in enumerate(offsets):
            start = now
            previous_offset = 0
            for sector, offset in enumerate((first, second, total), 1):
                checkpoint = base_checkpoint + i * 3 + sector
                now += offset - previous_offset
                previous_offset = offset
                if i == 0 and sector <= passed:
                    continue
                if timing is TimingMode.NO_PASS and checkpoint in ahead:
                    now = max(now, ahead[checkpoint] + elapsed_us(minimum_gap_s))
                crossings[checkpoint] = now
                kind = EventKind.LAP_COMPLETED if sector == 3 else EventKind.CHECKPOINT_CROSSING
                events.append(Event(now, kind, driver.driver_number, checkpoint, source="PACE_V2"))
            ledger[driver.driver_number, base_checkpoint + (i + 1) * 3] = LapAccounting(
                driver.driver_number, forecast.planned_start_lap + i, total,
                elapsed if i == 0 else 0, now - start - total, now,
            )
        ahead = crossings
    if not cars:
        raise ValueError("No cars have a supported remaining lap in this realization")
    state = RaceState(
        snapshot.session_key, snapshot.race_start_utc,
        RaceRuleset(drivers[0].request.season, snapshot.scheduled_laps),
        SimulationMode.CONDITIONAL_SIMULATION,
        f"s1-{snapshot.session_key}-{cutoff}-{field.master_seed}-{sample_index}-{timing}",
        tuple(cars), race_clock_us=cutoff, phase=RacePhase.RUNNING, track_state=TrackState.GREEN,
    )
    run = run_checkpoint_window(state, events)
    laps = tuple(ledger[f.event.driver_number, f.event.checkpoint] for f in run.trace
                 if f.event.kind is EventKind.LAP_COMPLETED)
    return FieldPaceRun(run, laps, unsupported)
