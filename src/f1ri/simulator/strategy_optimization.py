"""Bounded mean-pace search on the S5 paired strategy scheduler."""

from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from itertools import combinations, product
from math import isfinite

from f1ri.models.contracts import SUPPORT_EXTRAPOLATED, SUPPORT_UNSUPPORTED
from f1ri.simulator.race_control import ControlCommand
from f1ri.simulator.state import CarStatus, Route, TrackState
from f1ri.simulator.strategy import (
    StrategyInputError,
    StrategyScenario,
    compare_strategies,
)
from f1ri.simulator.strategy_constraints import CheckpointConstraints
from f1ri.simulator.strategy_forecast import ForecastInput, StrategyForecastAdapter
from f1ri.simulator.strategy_policy import Policy, PolicyKind, Stop

MODEL_SHA256 = "f98e8444a5982dd482a29444ce2c06848a39829cc248cb6cfceb760b59eae16a"
HORIZONS = (3, 5, 10)
OBJECTIVES = ("COMMON_MARKER_TIME", "TEAM_COMBINED", "LEAD_DRIVER", "TEAM_WEIGHTED")
INFORMATION = ("POINT_IN_TIME", "PERFECT_FORESIGHT")
# Prespecified from the rounded-up race-weighted mean errors in Stage 3-R.
NEAR_US_PER_DRIVER = {3: 910_000, 5: 1_440_000, 10: 3_540_000}
REFERENCE = "reference"


@dataclass(frozen=True, slots=True)
class SearchWindow:
    driver: int
    first_lap: int
    last_lap: int
    set_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PaceSensitivity:
    """Declared seconds per lap, applied after a verified mean forecast."""
    level_s: float = 0
    compound: str | None = None
    compound_s: float = 0
    age_s_per_lap: float = 0


@dataclass(frozen=True, slots=True)
class OptimizationRequest:
    request_id: str
    scenario: StrategyScenario
    features: tuple[ForecastInput, ...]
    windows: tuple[SearchWindow, ...]
    max_stops: int = 1
    objective: str = "COMMON_MARKER_TIME"
    lead_driver: int | None = None
    weights: tuple[tuple[int, int], ...] = ()
    information_mode: str = "POINT_IN_TIME"
    budget: int = 64
    seed: int = 42
    model_sha256: str = MODEL_SHA256
    planned_stops: tuple[Stop, ...] = ()
    cache_forecasts: bool = True
    sensitivity: PaceSensitivity = PaceSensitivity()
    output_location: str | None = None
    randomness: str = "DETERMINISTIC_MEAN"


@dataclass(frozen=True, slots=True)
class WindowedPolicy(Policy):
    first_lap: int = 1
    last_lap: int = 1
    after_sc: bool = False

    def choose(self, stop, view):
        # Runtime exposes only committed signals, even when the evaluator has a script.
        if self.after_sc and (
            view.track_state is not TrackState.GREEN or "SC_WITHDRAWN" not in view.committed_signals
        ):
            return None
        lap = Policy.choose(self, stop, view)
        return lap if lap is not None and self.first_lap <= lap <= self.last_lap else None


def _fail(code, reason):
    raise StrategyInputError(code, reason)


def _next_lap(scene, driver):
    pos = next(p for p in scene.physical.cars if p.driver_number == driver)
    route = next(r for r in scene.resources if r.driver == driver)
    if pos.progress_m is None or pos.completed_laps is None:
        _fail("PHYSICAL_STATE_UNSUPPORTED", "Pit search requires a lap-aligned physical starting point.")
    return pos.completed_laps + (2 if pos.progress_m > route.entry_m + 1e-6 else 1)


def _weighting(request):
    drivers = tuple(dict(request.scenario.targets))
    if request.objective == "COMMON_MARKER_TIME" and len(drivers) != 1:
        _fail("OBJECTIVE_UNSUPPORTED", "Select a team objective for two target drivers.")
    if request.objective == "LEAD_DRIVER":
        if request.lead_driver not in drivers:
            _fail("OBJECTIVE_UNSUPPORTED", "The lead driver must have a declared marker.")
        return {driver: int(driver == request.lead_driver) for driver in drivers}
    if request.objective == "TEAM_WEIGHTED":
        weights = dict(request.weights)
        if (len(request.weights) != len(drivers) or weights.keys() != set(drivers)
                or any(type(x) is not int or not 1 <= x <= 100 for x in weights.values())):
            _fail("OBJECTIVE_UNSUPPORTED", "Give each target driver one weight from 1 to 100.")
        return weights
    return dict.fromkeys(drivers, 1)


def _validate(request, bundle):
    scene = request.scenario
    if (request.model_sha256 != MODEL_SHA256 or bundle.artifact_sha256 != MODEL_SHA256
            or bundle.model.model_version != "PaceV2_D8R1" or bundle.regulation_era != "2023_2025"):
        _fail("MODEL_UNVERIFIED", "S6-A requires the pinned PaceV2_D8R1 mean model.")
    if (scene.horizon_laps not in HORIZONS or scene.profile.season != 2025 or scene.initial.ruleset.season != 2025
            or scene.end_us-scene.initial.race_clock_us > 4_000_000_000):
        _fail("HORIZON_UNSUPPORTED", "Select a supported 2025 profile and a three-, five- or ten-lap window.")
    if (not request.request_id or request.randomness != "DETERMINISTIC_MEAN"
            or request.information_mode not in INFORMATION or request.objective not in OBJECTIVES
            or type(request.budget) is not int or not 1 <= request.budget <= 128
            or type(request.max_stops) is not int or request.max_stops not in (0, 1, 2)
            or type(request.seed) is not int or request.seed < 0):
        _fail("OPTIMIZATION_REQUEST", "Invalid search contract or budget.")
    if len(scene.controls) > 24 or len(scene.retirements) > 20 or len(scene.passes) > 24:
        _fail("SCENARIO_BUDGET", "The declared event script exceeds the bounded scenario size.")
    if any(
        (e.available_utc or e.observation_utc) is not None
        and (e.available_utc or e.observation_utc) > scene.initial.race_start_utc+timedelta(microseconds=e.at_us)
        for e in scene.controls
    ):
        _fail("CONTROL_AVAILABILITY", "Control signals must be available when the evaluator commits them.")
    if scene.field_support not in {"DECLARED_EXACT_SCENARIO", "DECLARED_POINT_WITHIN_HISTORICAL_BOUNDS"}:
        _fail("PHYSICAL_STATE_UNSUPPORTED", "Supply an exact declared field with supported geometry.")
    targets = dict(scene.targets)
    drivers = {w.driver for w in request.windows}
    roster = {car.driver_number for car in scene.initial.cars}
    resources = {r.driver: r for r in scene.resources}
    physical_drivers = {p.driver_number for p in scene.physical.cars}
    if (drivers != targets.keys() or not 1 <= len(drivers) <= 2 or len(request.windows) != len(drivers)
            or not drivers <= roster or resources.keys() != roster or physical_drivers != roster
            or any(len(w.set_ids) > 8 or len(set(w.set_ids)) != len(w.set_ids) or
                   type(w.first_lap) is not int or type(w.last_lap) is not int
                   or not 1 <= w.first_lap <= w.last_lap <= w.first_lap + 9 for w in request.windows)):
        _fail("SEARCH_SPACE_UNSUPPORTED", "Use one window of up to ten laps and eight sets for each target driver.")
    if len(drivers) == 2 and len({resources[d].team for d in drivers}) != 1:
        _fail("TEAM_UNSUPPORTED", "Select two drivers with declared resources from the same team.")
    if len(request.planned_stops) > request.max_stops or any(
        s.lap is None or s.delay_laps or s.review_us is not None or s.cancel_us is not None
        for s in request.planned_stops
    ):
        _fail("PLANNED_STOPS_UNSUPPORTED", "Existing plans must be fixed stops within the total stop limit.")
    p = request.sensitivity
    if (p.compound not in {None, "HARD", "MEDIUM", "SOFT"} or
            any(not isfinite(x) or abs(x) > limit for x, limit in (
                (p.level_s, 1), (p.compound_s, 1), (p.age_s_per_lap, .15)))):
        _fail("SENSITIVITY_UNSUPPORTED", "Pace sensitivities must stay within the declared experiment bounds.")
    return _weighting(request)


def _stop_limit(scene, driver):
    return min(scene.initial.ruleset.scheduled_laps-1, (dict(scene.targets)[driver]-2)//3)


def _candidate_reason(scene, stop, windows, allocation, earliest):
    if stop.driver not in windows:
        return "UNTARGETED_DRIVER"
    window = windows[stop.driver]
    tyre = allocation.get((stop.driver, stop.set_id))
    if tyre is None or stop.set_id not in window.set_ids:
        return "TYRE_SET_UNAVAILABLE"
    if tyre.compound_absolute is None:
        return "ABSOLUTE_COMPOUND_UNSUPPORTED"
    lower = max(window.first_lap, earliest[stop.driver])
    upper = min(window.last_lap, _stop_limit(scene, stop.driver))
    if lower > upper or stop.lap is not None and not lower <= stop.lap <= upper:
        return "PIT_LAP_OUTSIDE_EXECUTABLE_WINDOW"
    return None


def _enumerate(request):
    scene = request.scenario
    windows = {w.driver: w for w in request.windows}
    allocation = {(r.driver, t.set_id): t for r in scene.resources for t in r.tyres}
    earliest = {d: _next_lap(scene, d) for d in windows}
    planned = request.planned_stops
    base = Policy(REFERENCE, PolicyKind.SEQUENCE if planned else PolicyKind.CONTINUE, planned)
    rejected = []
    policies = [base]
    allowed = []

    def reason(stops):
        for stop in stops:
            if problem := _candidate_reason(scene, stop, windows, allocation, earliest):
                return problem
        if len({(s.driver, s.set_id) for s in stops}) < len(stops):
            return "TYRE_SET_REUSED"
        if any(abs(a.lap-b.lap) < 2 for a, b in combinations(stops, 2) if a.driver == b.driver):
            return "SECOND_STOP_BEFORE_SEPARATE_STINT"
        return None

    if invalid := reason(planned):
        _fail("PLANNED_STOPS_UNSUPPORTED", f"Existing plans failed the search constraints ({invalid}).")
    opponent_drivers = {s.driver for policy in scene.opponents for s in policy.stops}
    if opponent_drivers & windows.keys():
        _fail("OPPONENT_POLICY_CONFLICT", "A targeted driver also has an opponent policy.")

    ids_by_actions = {tuple(sorted((s.driver, s.lap, s.set_id) for s in planned)): REFERENCE}
    aliases = {"stay-out" if not planned else "existing-plan": REFERENCE}
    if len(planned) < request.max_stops:
        for driver, window in sorted(windows.items()):
            for lap, tyre_id in product(range(window.first_lap, window.last_lap+1), sorted(window.set_ids)):
                stop = Stop(driver, tyre_id, lap)
                if invalid := _candidate_reason(scene, stop, windows, allocation, earliest):
                    rejected.append({"stops": [asdict(stop)], "reason": invalid})
                else:
                    allowed.append(stop)
        for n in range(1, request.max_stops-len(planned)+1):
            for stops in combinations(allowed, n):
                full = tuple(sorted((*planned, *stops), key=lambda s: (s.lap, s.driver, s.set_id)))
                key = tuple(sorted((s.driver, s.lap, s.set_id) for s in full))
                if key in ids_by_actions:
                    invalid = "DUPLICATE_ACTIONS"
                else:
                    invalid = reason(full)
                if invalid:
                    rejected.append({"stops": list(map(asdict, full)), "reason": invalid})
                    continue
                ident = "__".join(f"{s.driver}-L{s.lap}-{s.set_id}" for s in full)
                ids_by_actions[key] = ident
                kind = PolicyKind.TEAM if len({s.driver for s in full}) == 2 else (
                    PolicyKind.SEQUENCE if len(full) == 2 else PolicyKind.TARGET)
                policies.append(Policy(ident, kind, full))

    if request.max_stops and not planned:
        for driver, window in sorted(windows.items()):
            lo = max(window.first_lap, earliest[driver])
            hi = min(window.last_lap, _stop_limit(scene, driver))
            if lo > hi:
                continue
            for set_id in sorted(window.set_ids):
                if _candidate_reason(scene, Stop(driver, set_id), windows, allocation, earliest):
                    continue
                for prefix, kind in (("vsc", PolicyKind.VSC), ("sc", PolicyKind.SC),
                                     ("after-sc", PolicyKind.NEXT)):
                    policies.append(WindowedPolicy(f"{prefix}-{driver}-{set_id}", kind, (Stop(driver, set_id),),
                                                   lo, hi, prefix == "after-sc"))
                if request.information_mode == "PERFECT_FORESIGHT":
                    for event in sorted(scene.controls, key=lambda e: (e.at_us, e.event_id)):
                        if event.command in (ControlCommand.GREEN, ControlCommand.VSC, ControlCommand.SC):
                            ident = f"oracle-{event.event_id}-{driver}-{set_id}"
                            policies.append(WindowedPolicy(ident, PolicyKind.NEXT,
                                                           (Stop(driver, set_id, review_us=event.at_us),), lo, hi))
        if allowed:
            benchmark = sorted(allowed, key=lambda s: (s.driver, s.set_id, s.lap))
            first = benchmark[0]
            series = [s for s in benchmark if (s.driver, s.set_id) == (first.driver, first.set_id)]
            aliases["pit-now" if first.lap == earliest[first.driver] else "earliest-window"] = (
                ids_by_actions[((first.driver, first.lap, first.set_id),)])
            middle = series[len(series)//2]
            aliases["fixed-target"] = ids_by_actions[((middle.driver, middle.lap, middle.set_id),)]
            last = series[-1]
            aliases["extend-current-stint"] = ids_by_actions[((last.driver, last.lap, last.set_id),)]
            aliases["fixed-one-stop"] = aliases["fixed-target"]
            two = next((p.strategy_id for p in policies if p.kind is PolicyKind.SEQUENCE), None)
            if two:
                aliases["fixed-two-stop"] = two
            for label, prefix in (("vsc-reactive", "vsc"), ("sc-reactive", "sc")):
                aliases[label] = f"{prefix}-{first.driver}-{first.set_id}"
    return policies, rejected, aliases


class _MeanAdapter(StrategyForecastAdapter):
    def __init__(self, request, bundle):
        super().__init__(bundle, request.features, seed=request.seed, samples=2, sample=None)
        self.enabled = request.cache_forecasts
        self.perturbation = request.sensitivity
        self.cache = {}
        self.hits = 0
        self.calls = 0
        scene = request.scenario
        self.end_laps = {c.driver_number: min(scene.initial.ruleset.scheduled_laps,
                         c.completed_laps+scene.horizon_laps+bool(c.sector)) for c in scene.initial.cars}

    def _inputs(self, scenario):
        known = super()._inputs(scenario)
        return {driver: replace(feature, request=replace(feature.request,
                horizon_laps=self.end_laps[driver]-feature.request.cutoff_lap_number))
                for driver, feature in known.items()}

    def replacement_horizon(self, request, start):
        return max(1, self.end_laps[request.driver_number]-start+1)

    def forecast_request(self, request, *, replacement):
        # This cache is scoped to one optimizer invocation and holds no simulator state.
        key = (self.bundle.artifact_sha256, request, replacement)
        if key in self.cache:
            self.hits += 1
            return self.cache[key]
        batch = super().forecast_request(request, replacement=replacement)
        self.calls += 1
        if self.enabled:
            self.cache[key] = batch
        return batch

    def pace_curve(self, batch, tyre_id):
        curve = super().pace_curve(batch, tyre_id)
        p = self.perturbation
        offset = p.level_s + (p.compound_s if batch.forecast.compound_relative == p.compound else 0)
        return replace(curve, lap_seconds=tuple(
            seconds+offset+p.age_s_per_lap*(curve.initial_age+i) for i, seconds in enumerate(curve.lap_seconds)
        ))

    def forecast_record(self, request, batch, source_hash, fit_clock):
        row = super().forecast_record(request, batch, source_hash, fit_clock)
        forecast = batch.forecast
        curve = self.pace_curve(batch, "forecast")
        row["forecast"] = {
            "model_version": forecast.model_version, "support_status": forecast.support_status,
            "lap_forecasts": [{"target_lap_number": lap.target_lap_number, "projected_tyre_age": lap.projected_tyre_age,
                               "support_status": lap.support_status, "extrapolation_warning": lap.extrapolation_warning,
                               "expected_lap_time_s": lap.expected_lap_time_s, "scenario_lap_time_s": seconds}
                              for lap, seconds in zip(forecast.lap_forecasts, curve.lap_seconds, strict=True)],
        }
        return row


def _known_scenario(scene):
    clock = scene.initial.race_clock_us
    return replace(scene, controls=tuple(e for e in scene.controls if e.at_us <= clock), retirements=(), passes=())


def _target_rows(run, targets):
    return [r for r in run.get("checkpoint_constraints", []) if r["checkpoint"] is not None
            and r["driver"] in targets and r["checkpoint"] <= targets[r["driver"]] and r["latent_us"] is not None]


def _forecast_support(run, targets):
    clock = max((v["clock_us"] for v in run["targets"].values() if v), default=0)
    used = {(r["driver"], r["stint"], (r["checkpoint"]-1)//3+1)
            for r in run.get("checkpoint_constraints", ()) if r["checkpoint"] is not None
            and r["latent_us"] is not None and r["at_us"] <= clock
            and r["checkpoint"] <= targets.get(r["driver"], r["checkpoint"])}
    rows = []
    for f in run.get("forecast_requests", []):
        if "forecast" not in f:
            rows.append("UNSUPPORTED")
            continue
        rows.extend(lap["support_status"] for lap in f["forecast"]["lap_forecasts"]
                    if (f["request"]["driver_number"], f["request"]["stint_number"],
                        lap["target_lap_number"]) in used)
    return tuple(sorted(set(rows)))


def _evaluation(run, pair, policy, targets, weights, near_us):
    rows = _target_rows(run, targets)
    visits = [p for p in run["pit"] if p["driver"] in targets and p["merge_us"] is not None
              and p["merge_checkpoint"] <= targets[p["driver"]]]
    forecast_support = _forecast_support(run, targets)
    reasons = list(run["unsupported"])
    driver_times = {driver: row["elapsed_us"] for driver, row in run["targets"].items() if row}
    status = run["status"]
    if status == "MARKERS_REACHED" and any(
        row["route"] is not Route.ON_TRACK and row["status"] is not CarStatus.FINISHED for row in run["targets"].values()
    ):
        status = "UNSUPPORTED"
        reasons.append("COMMON_MARKER_NOT_ON_RACING_ROUTE")
    if (any(r in (SUPPORT_UNSUPPORTED, SUPPORT_EXTRAPOLATED) for r in forecast_support)
            or status == "MARKERS_REACHED" and not forecast_support):
        status = "UNSUPPORTED"
        reasons.append("FORECAST_OUTSIDE_SUPPORTED_TRAJECTORY")
    fixed = sum(s.lap is not None for s in policy.stops)
    if status == "MARKERS_REACHED" and (len(visits) < fixed or any(
        d["outcome"] == "REQUESTED" and d["at_us"] < run["targets"].get(d["driver"], {}).get("clock_us", 0)
        and not any(p["decision_id"] == d["decision_id"] for p in visits)
        for d in run["decisions"] if d["driver"] in driver_times
    )):
        status = "INCOMPLETE_STRATEGY_ACTION"
        reasons.append("REQUESTED_STOP_NOT_COMPLETED_BY_MARKER")
    delta = {row["driver"]: row["delta_time_us"] for row in pair["drivers"]}
    objective = sum(weights[d]*driver_times[d] for d in targets) if status == "MARKERS_REACHED" else None
    return {"candidate": policy.strategy_id, "status": status, "reasons": reasons,
            "objective_us": objective, "objective_delta_us": (sum(weights[d]*delta[d] for d in targets)
                if objective is not None and all(v is not None for v in delta.values()) else None),
            "driver_elapsed_us": driver_times, "driver_delta_us": delta, "markers": run["targets"],
            "forecast_support": forecast_support, "near_margin_us": near_us,
            "latent_racing_us": sum(r["latent_us"] for r in rows),
            "effective_racing_us": sum(r["effective_us"] for r in rows),
            "following_us": sum(r["following_only_us"] or 0 for r in rows),
            "neutralization_us": sum(max(r["movement_floor_us"] or 0, r["clock_floor_us"] or 0, r["latent_us"])
                                     -r["latent_us"] for r in rows if r["category"] == "NEUTRALIZATION_LIMITED"),
            "restart_us": sum(max(r["movement_floor_us"] or 0, r["latent_us"])-r["latent_us"]
                              for r in rows if r["category"] == "RESTART_LIMITED"),
            "pit_route_us": sum((p["pit_elapsed_us"] or 0)-(p["wait_us"] or 0) for p in visits),
            "service_wait_us": sum(p["wait_us"] or 0 for p in visits), "pit_visits": len(visits)}


def _score(scene, policies, adapter, weights):
    paired = compare_strategies(replace(scene, policies=tuple(policies)), REFERENCE,
                                recorder_factory=CheckpointConstraints, forecast_adapter=adapter)
    targets = dict(scene.targets)
    margin = NEAR_US_PER_DRIVER[scene.horizon_laps]*sum(weights.values())
    rows = [_evaluation(paired["results"][p.strategy_id], paired["pairs"][p.strategy_id], p, targets, weights, margin)
            for p in policies]
    ordered = sorted((r for r in rows if r["objective_us"] is not None),
                     key=lambda r: (r["objective_us"], r["candidate"]))
    best = ordered[0]["objective_us"] if ordered else None
    best_count = sum(r["objective_us"] == best for r in ordered)
    for r in rows:
        gap = r["objective_us"]-best if r["objective_us"] is not None else None
        r["gap_to_best_us"] = gap
        if gap is None:
            r["comparison"] = "UNSUPPORTED_COMPARISON"
        elif gap == 0:
            r["comparison"] = "EXACT_SIMULATION_TIE" if best_count > 1 else "BEST_EVALUATED"
        else:
            r["comparison"] = "NEAR_EQUAL_MODEL_OUTCOME" if gap <= margin else "CLEAR_MODEL_DIFFERENCE"
    return {"rows": sorted(rows, key=lambda r: r["candidate"]),
            "ranked": [r["candidate"] for r in ordered],
            "best": [r["candidate"] for r in ordered if r["gap_to_best_us"] == 0],
            "near": [r["candidate"] for r in ordered if 0 < r["gap_to_best_us"] <= margin],
            "paired": paired}


def optimize(request, bundle):
    """Evaluate the bounded legal set, with a separate prospective decision when needed."""
    weights = _validate(request, bundle)
    policies, rejected, benchmarks = _enumerate(request)
    scene = request.scenario
    known = _known_scenario(scene)
    prospective = request.information_mode == "POINT_IN_TIME" and known != scene
    cost = 2 if prospective else 1
    if request.budget < cost:
        _fail("SEARCH_BUDGET", "This request needs budget for a reference in both evaluation scenarios.")
    # Reserve the same point-in-time candidate allocation with or without unseen future events.
    count = max(1, request.budget//2) if request.information_mode == "POINT_IN_TIME" else request.budget
    selected = policies[:count]
    adapter = _MeanAdapter(request, bundle)
    decision = _score(known if prospective else scene, selected, adapter, weights)
    conditional = _score(scene, selected, adapter, weights) if prospective else decision
    complete = len(selected) == len(policies)
    outcomes = Counter(row["status"] for row in conditional["rows"])
    return {"request_id": request.request_id, "objective": request.objective, "weights": weights,
            "information_mode": request.information_mode, "decision_basis": (
                "DECISION_KNOWN_EVENTS" if prospective else "DECLARED_SCENARIO"),
            "scope": "BEST_WITHIN_EVALUATED_CANDIDATES" if decision["best"] else "UNSUPPORTED_COMPARISON",
            "completion": "EXHAUSTED_LEGAL_SPACE" if complete else "BOUNDED_SEARCH_INCOMPLETE",
            "benchmark_aliases": benchmarks, "candidates": [asdict(p) for p in policies], "rejected": rejected,
            "unevaluated": [p.strategy_id for p in policies[len(selected):]], "decision": decision,
            "conditional": conditional, "search": {
                "budget": request.budget, "total_combinations": len(policies)+len(rejected),
                "legal": len(policies), "rejected": len(rejected), "evaluated": len(selected),
                "simulator_evaluations": len(selected)*cost, "outcomes": dict(outcomes),
                "candidate_allocation": count,
                "budget_exhausted": not complete, "forecast_cache_enabled": adapter.enabled,
                "forecast_calls": adapter.calls, "forecast_cache_hits": adapter.hits}}
