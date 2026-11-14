"""Adapt verified Pace V2 requests to the strategy scheduler's stint curves."""
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
from math import isfinite

from f1ri.models.contracts import SCENARIO_CONTINUE_STINT, StintForecastRequest
from f1ri.simulator.pace_adapter import (
    forecast_new_stint,
    forecast_pace,
    load_pace_bundle,
    reference_parity,
)
from f1ri.simulator.pit_execution import PitStopRequest, ReplacementTyres
from f1ri.simulator.pit_pace import replacement_pace_request
from f1ri.simulator.state import TrackState
from f1ri.simulator.strategy import StrategyInputError, fingerprint
from f1ri.simulator.strategy_pace import PaceCurve, PaceMode, PaceSource


@dataclass(frozen=True, slots=True)
class ForecastInput:
    request: StintForecastRequest
    evidence_as_of: tuple[tuple[str, datetime], ...]
    source_sha256: str


def read_forecast_inputs(document):
    """Read cutoff context for the CLI and dashboard; model files stay local."""
    try:
        return tuple(ForecastInput(
            StintForecastRequest(**(row["request"] | {
                "as_of_utc": datetime.fromisoformat(row["request"]["as_of_utc"]),
                **{k: tuple(row["request"].get(k, ())) for k in ("observed_stint_clean_ages",
                   "observed_stint_clean_times_s", "observed_stint_clean_progress")},
            })),
            tuple((k, datetime.fromisoformat(v)) for k, v in sorted(row["evidence_as_of"].items())),
            row["source_sha256"],
        ) for row in document)
    except (TypeError, KeyError, AttributeError, ValueError) as error:
        raise StrategyInputError("REQUEST_UNSUPPORTED", f"Invalid forecast feature document: {error}") from error


def _seed(*parts):
    return int(fingerprint(parts)[:16], 16)


def _curve(batch, tyre_id, sample):
    forecast = batch.forecast
    laps = (tuple(lap.expected_lap_time_s for lap in forecast.lap_forecasts) if sample is None else
            tuple(float(x) for x in batch.lap_samples_s[sample]))
    return PaceCurve(forecast.driver_number, tyre_id, forecast.tyre_age_at_start, laps,
                     forecast.support_status, forecast.planned_start_lap)


class StrategyForecastAdapter:
    """Read-only D8 forecasts. Initial feature values remain frozen across branches."""

    @classmethod
    def from_verified_files(cls, root, stage3, features, *, seed=42, samples=1000, sample=None):
        bundle = load_pace_bundle(root, stage3)
        reference_parity(bundle, root / "tests/fixtures/stage5_s1_d8_reference.json")
        return cls(bundle, features, seed=seed, samples=samples, sample=sample)

    def __init__(self, bundle, features, *, seed, samples, sample):
        self.bundle = bundle
        self.features = tuple(features)
        self.seed = seed
        self.samples = samples
        self.sample = sample
        if seed < 0 or not 2 <= samples <= 10000 or sample is not None and not 0 <= sample < samples:
            raise StrategyInputError("REQUEST_UNSUPPORTED", "Invalid realization configuration.")

    def _inputs(self, scenario):
        state = scenario.initial
        cutoff = state.race_start_utc+timedelta(microseconds=state.race_clock_us)
        known = {f.request.driver_number: f for f in self.features}
        if len(known) != len(self.features) or set(known) != {c.driver_number for c in state.cars}:
            raise StrategyInputError("REQUEST_UNSUPPORTED", "Supply one cutoff feature record per driver.")
        for c in state.cars:
            f = known[c.driver_number]
            r = f.request
            clocks = dict(f.evidence_as_of)
            required = {"identity", "timing", "tyres"}
            if r.track_temperature_c is not None or r.air_temperature_c is not None:
                required.add("weather")
            if r.clean_laps_observed_race or r.observed_stint_clean_times_s:
                required.add("clean_history")
            if r.prev_stint_compound_relative is not None:
                required.add("previous_stint")
            if r.grid_position is not None or r.qualifying_lap_time_s is not None:
                required.add("grid_qualifying")
            if (not required <= clocks.keys() or any(t.utcoffset() is None or t > cutoff for t in clocks.values())
                    or len(f.source_sha256) != 64 or any(x not in "0123456789abcdef" for x in f.source_sha256)):
                raise StrategyInputError("REQUEST_UNSUPPORTED", "Missing source clocks or features after the cutoff.")
            if (r.as_of_utc != cutoff or r.session_key != state.session_key or r.season != state.ruleset.season
                    or r.cutoff_lap_number != c.completed_laps or r.planned_start_lap != c.completed_laps+1
                    or r.scheduled_race_laps != state.ruleset.scheduled_laps or r.stint_number != c.tyres.stint_number
                    or (r.compound_relative, r.compound_absolute, r.tyre_age_at_start) !=
                    (c.tyres.compound, c.tyres.compound_absolute, c.tyres.age_laps)
                    or not c.tyres.set_id or not r.circuit_short_name or r.circuit_key is None
                    or r.scenario_type != SCENARIO_CONTINUE_STINT
                    or r.future_weather_scenario != "CONSTANT_LAST_OBSERVED"
                    or not all(x is None or isfinite(x) for x in (r.track_temperature_c, r.air_temperature_c))):
                raise StrategyInputError("REQUEST_UNSUPPORTED", f"Forecast context does not match car {c.driver_number}.")
            mandatory_horizon = min(state.ruleset.scheduled_laps, c.completed_laps+scenario.horizon_laps+bool(c.sector))
            if not mandatory_horizon-r.cutoff_lap_number <= r.horizon_laps <= 20:
                raise StrategyInputError("REQUEST_UNSUPPORTED", f"Insufficient forecast horizon for car {c.driver_number}.")
        return known

    def attach(self, scenario):
        features = self._inputs(scenario)
        curves, ledger = [], []
        for c in scenario.initial.cars:
            f = features[c.driver_number]
            r = f.request
            # Same seed namespace and driver/stint partition as frozen S1.
            raw = f"s1-d8-v1:{self.seed}:{r.session_key}:{r.as_of_utc.isoformat()}:{r.driver_number}:{r.stint_number}"
            seed = int.from_bytes(sha256(raw.encode()).digest()[:8], "big")
            request = replace(r, n_trajectory_samples=self.samples, random_seed=seed)
            try:
                batch = forecast_pace(self.bundle, request)
            except ValueError as error:
                raise StrategyInputError("REQUEST_UNSUPPORTED", str(error)) from error
            curves.append(_curve(batch, c.tyres.set_id, self.sample))
            ledger.append(_record(request, batch, f.source_sha256, None, self.sample))
        source = PaceSource(PaceMode.FULL_MODEL, tuple(curves),
                            (("D8", self.bundle.artifact_sha256),
                             ("features", fingerprint([asdict(f) for f in features.values()]))),
                            f"D8:{self.seed}:{self.samples}:{self.sample}",
                            "Verified D8; correlated within stint, independently keyed by driver and stint")
        return replace(scenario, pace=source), tuple(ledger)

    def branch(self, scenario, continuation):
        return _Branch(self, self._inputs(scenario), continuation)


def _record(request, batch, source_hash, fit_clock, sample):
    forecast = asdict(batch.forecast)
    forecast.pop("sampled_lap_trajectories_s", None)
    return {"request": asdict(request), "request_sha256": fingerprint(asdict(request)),
            "feature_sha256": source_hash, "fit_clock_us": fit_clock, "model_sha256": batch.artifact_sha256,
            "stream_id": batch.random_stream_id, "sample_index": sample, "forecast": forecast,
            "selected_lap_sample_s": batch.lap_samples_s[sample].tolist() if sample is not None else None}


class _Branch:
    def __init__(self, parent, evidence, continuation):
        self.parent = parent
        self.evidence = evidence
        self.ledger = list(continuation)
        self.current = {d: f.request for d, f in evidence.items()}

    def check_replacement(self, state, car, tyre, lap):
        r = self.current[car.driver_number]
        if not tyre.set_id or tyre.compound_absolute is None or lap < r.planned_start_lap:
            raise ValueError("Replacement requires a known allocated dry set and a future stint start.")
        if state.session_key != r.session_key or lap >= r.scheduled_race_laps:
            raise ValueError("Replacement request is outside the forecast race.")

    def replacement(self, state, car):
        d = car.driver_number
        r = self.current[d]
        tyre = car.tyres
        start = tyre.stint_start_lap
        # Pace V2 predicts a clean-air green trajectory. Actual flag and route
        # movement still use the simulator; the source observation clock stays fixed.
        stop = PitStopRequest(state.session_key, d, r.as_of_utc, start-1,
                              ReplacementTyres(tyre.compound, tyre.age_laps, tyre.compound_absolute, tyre.set_id),
                              state.simulation_id, TrackState.GREEN)
        self.check_replacement(state, car, stop.replacement, stop.pit_in_lap)
        horizon = min(20, r.scheduled_race_laps-start+1)
        p = self.parent
        # Match alternative tyres at the same stint start and draw shape.
        # Different stop laps or stint ordinals use separate innovations.
        seed = _seed("s5-c1-replacement", p.seed, r.session_key, r.as_of_utc, d, tyre.stint_number, start, horizon)
        req = replacement_pace_request(r, stop, horizon=horizon, seed=seed, samples=p.samples)
        try:
            batch = forecast_new_stint(p.bundle, req)
        except ValueError as error:
            self.ledger.append({"request": asdict(req), "fit_clock_us": state.race_clock_us,
                                "support": "UNSUPPORTED", "reason": str(error)})
            raise
        self.current[d] = req
        self.ledger.append(_record(req, batch, self.evidence[d].source_sha256, state.race_clock_us, p.sample))
        return _curve(batch, tyre.set_id, p.sample)
