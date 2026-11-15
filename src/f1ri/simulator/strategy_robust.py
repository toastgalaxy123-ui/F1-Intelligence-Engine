"""Run one versioned library of executable policies across declared S6-A scenarios."""

from dataclasses import asdict, dataclass, replace
from itertools import combinations
from math import isfinite

from f1ri.models.contracts import SCENARIO_CONTINUE_STINT
from f1ri.simulator.state import Route, TrackState
from f1ri.simulator.strategy import StrategyInputError, fingerprint
from f1ri.simulator.strategy_optimization import (
    NEAR_US_PER_DRIVER,
    REFERENCE,
    OptimizationRequest,
    PaceSensitivity,
    WindowedPolicy,
    _candidate_reason,
    _enumerate,
    _MeanAdapter,
    _next_lap,
    _score,
    _stop_limit,
    _validate,
)
from f1ri.simulator.strategy_policy import Policy, PolicyKind, Stop
from f1ri.simulator.strategy_regret import (
    BEST_REALIZED,
    OBJECTIVES,
    PERFECT_INFORMATION,
    analyze_matrix,
)

FUTURE = "FUTURE_EVENTS"
INITIAL = "INITIAL_STATE_UNCERTAINTY"
PHYSICAL = "PHYSICAL_MODEL_SENSITIVITY"
PACE = "PACE_MODEL_SENSITIVITY"
CATEGORIES = (FUTURE, INITIAL, PHYSICAL, PACE)


@dataclass(frozen=True, slots=True)
class RobustPaceSensitivity(PaceSensitivity):
    new_stint_s: float = 0


@dataclass(frozen=True, slots=True)
class ObservedPolicy(Policy):
    """One conditional action per slot, evaluated from current committed observations."""

    first_lap: int = 1
    last_lap: int = 1
    neutralized_set: str = ""
    green_lap: int | None = None
    deadline_us: int | None = None

    def resolve(self, stop, view):
        if view.car.route is not Route.ON_TRACK:
            return None
        phase = view.track_state
        announced = ((phase is TrackState.VSC and "VSC_DEPLOY" in view.committed_signals) or
                     (phase is TrackState.SC and "SC_DEPLOY" in view.committed_signals))
        before_deadline = self.deadline_us is None or view.clock_us <= self.deadline_us
        opportunity = self.first_lap <= view.next_pit_lap <= self.last_lap
        if announced and before_deadline and opportunity:
            return replace(stop, lap=view.next_pit_lap, set_id=self.neutralized_set)
        if phase is TrackState.GREEN and self.green_lap == view.next_pit_lap and opportunity:
            return replace(stop, lap=view.next_pit_lap)
        return None


@dataclass(frozen=True, slots=True)
class ScenarioVariant:
    scenario_id: str
    category: str
    changed_assumptions: str
    provenance: str
    request: OptimizationRequest


@dataclass(frozen=True, slots=True)
class ScenarioFamily:
    family_id: str
    initial: OptimizationRequest
    scenarios: tuple[ScenarioVariant, ...]
    total_budget: int = 768
    policy_limit: int = 96
    objective: str = "MINIMAX_REGRET"
    comparator: str = BEST_REALIZED
    weights: tuple[tuple[str, float], ...] = ()
    include_oracle: bool = True
    contingent: bool = True
    library_reference: OptimizationRequest | None = None


def policy_document(policy):
    return {"type": type(policy).__name__, **asdict(policy)}


def _unsupported(code, details):
    raise StrategyInputError(code, details)


def _resource_shape(scene, *, used_age=False, duration=False):
    return tuple(replace(r, tyres=tuple(replace(t, initial_age_laps=0) if used_age and t.initial_age_laps else t
                                         for t in r.tyres),
                         route=None if duration else r.route) for r in scene.resources)


def _feature_shape(features):
    return tuple(replace(f, source_sha256="", request=replace(
        f.request, last_clean_lap_time_s=None, median_last_3_clean_laps_s=None, median_last_5_clean_laps_s=None,
        median_stint_clean_laps_s=None, observed_stint_clean_times_s=())) for f in features)


def _validate_variant(base, case):
    r, s, original = case.request, case.request.scenario, base.scenario
    if case.category not in CATEGORIES or not all((case.scenario_id, case.provenance, case.changed_assumptions)):
        _unsupported("FAMILY_VARIANT", "Every alternative requires identity, a category and declared provenance.")
    if s.horizon_laps != original.horizon_laps or s.targets != original.targets:
        _unsupported("HORIZON_MISMATCH", "Compare the same horizon and common markers inside each family.")
    if replace(r, request_id=base.request_id, scenario=original, features=base.features, sensitivity=base.sensitivity) != base:
        _unsupported("FAMILY_SHARED_REQUEST", "The scenario changes the shared search or objective contract.")
    allowed = {"scenario_id": original.scenario_id, "experiment_id": original.experiment_id}
    if case.category in (FUTURE, PHYSICAL):
        if s.opponents[:len(original.opponents)] != original.opponents:
            _unsupported("FAMILY_OPPONENT", "Existing opponent policies are shared. Declare future additions separately.")
        allowed |= {k: getattr(original, k) for k in ("controls", "retirements", "passes", "opponents")}
    if case.category == INITIAL:
        declared = s.physical
        positions = {p.driver_number: p.progress_m for p in original.physical.cars}
        if (set(positions) != {p.driver_number for p in declared.cars} or
                replace(declared, cars=tuple(replace(p, progress_m=positions[p.driver_number]) for p in declared.cars))
                != original.physical):
            _unsupported("FAMILY_INITIAL_STATE", "Initial alternatives may move cars only within the shared declared timing state.")
        allowed["physical"] = original.physical
    if case.category == PHYSICAL:
        controls = ("vsc_speed_mps", "sc_speed_mps", "catchup_speed_mps", "restart_speed_mps")
        if replace(s.profile, **{k: getattr(original.profile, k) for k in controls}) != original.profile:
            _unsupported("FAMILY_RULE_PROFILE", "Physical sensitivity must preserve the regulation profile and rules.")
        if (_resource_shape(s, duration=True) != _resource_shape(original, duration=True)
                or any(a.route.box_id != b.route.box_id for a, b in zip(s.resources, original.resources, strict=True))):
            _unsupported("FAMILY_PIT_RESOURCES", "Pit sensitivity may change route timing with identical inventory and box.")
        allowed |= {"profile": original.profile, "resources": original.resources}
    if case.category == PACE:
        if (_resource_shape(s, used_age=True) != _resource_shape(original, used_age=True)
                or _feature_shape(r.features) != _feature_shape(base.features)):
            _unsupported("FAMILY_PACE_INPUTS", "Pace sensitivities may vary used-set age and declared observed times only.")
        offset = getattr(r.sensitivity, "new_stint_s", 0)
        if not isfinite(offset) or abs(offset) > 1:
            _unsupported("FAMILY_PACE_INPUTS", "Declared new-stint offset exceeds one second per lap.")
        allowed["resources"] = original.resources
    elif r.features != base.features or r.sensitivity != base.sensitivity:
        _unsupported("FAMILY_SHARED_FORECAST", "Declare feature or mean-pace shifts in a separate pace sensitivity family.")
    if replace(s, **allowed) != original:
        _unsupported("FAMILY_INITIAL_STATE", "The alternative changes shared timing, geometry or cutoff inputs.")


def _validate_family(family, bundle):
    n = len(family.scenarios)
    if (not family.family_id or not 1 <= n <= 12 or len({v.scenario_id for v in family.scenarios}) != n
            or type(family.total_budget) is not int or not n <= family.total_budget <= 1536
            or type(family.policy_limit) is not int or not 1 <= family.policy_limit <= 96
            or family.objective not in OBJECTIVES or family.comparator not in (BEST_REALIZED, PERFECT_INFORMATION)
            or family.initial.information_mode != "POINT_IN_TIME"):
        _unsupported("ROBUST_REQUEST", "Supply one to twelve distinct futures and bounded point-in-time search limits.")
    if family.objective == "DECLARED_WEIGHTED_DELTA" and not family.weights:
        _unsupported("ROBUST_REQUEST", "The weighted objective requires explicitly declared scenario weights.")
    # Validate weights before launching simulator jobs.
    analyze_matrix({v.scenario_id: {} for v in family.scenarios}, {}, complete=False, oracle_complete={},
                   weights=family.weights)
    weights = _validate(family.initial, bundle)
    if source := family.library_reference:
        base = family.initial
        timeline = {"targets": base.scenario.targets, "end_us": base.scenario.end_us,
                    "horizon_laps": base.scenario.horizon_laps}
        if (source.scenario.horizon_laps < base.scenario.horizon_laps or replace(
            source, request_id=base.request_id, scenario=replace(source.scenario, **timeline)) != base
        ):
            _unsupported("HORIZON_MISMATCH", "The longer library reference may change only the scoring horizon.")
        _validate(source, bundle)
    for case in family.scenarios:
        _validate_variant(family.initial, case)
        if _validate(case.request, bundle) != weights:
            _unsupported("FAMILY_OBJECTIVE", "Objective weights must be shared across futures.")
    return weights


def _contingent(base):
    if base.max_stops < 1 or base.planned_stops:
        return []
    scene = base.scenario
    space = {w.driver: w for w in base.windows}
    allocation = {(r.driver, t.set_id): t for r in scene.resources for t in r.tyres}
    earliest = {d: _next_lap(scene, d) for d in space}
    extra = []
    for d, w in sorted(space.items()):
        lo, hi = max(w.first_lap, earliest[d]), min(w.last_lap, _stop_limit(scene, d))
        known = [t for t in sorted(w.set_ids) if not _candidate_reason(
            scene, Stop(d, t), space, allocation, earliest)]
        for green, neutralized in combinations(known, 2):
            for a, b in ((green, neutralized), (neutralized, green)):
                extra.append(ObservedPolicy(f"adaptive-{d}-{a}-or-{b}", PolicyKind.NEXT, (Stop(d, a),),
                                            lo, hi, b, hi, scene.initial.race_clock_us+120_000_000))
    return extra


def policy_library(base, *, contingent=True):
    candidates, rejected, _ = _enumerate(base)
    return (*candidates, *(_contingent(base) if contingent else ())), rejected


def _project(policy, scene):
    """Project the same fixed rule onto a shorter common-marker prefix."""
    beyond = tuple(s for s in policy.stops if s.lap is not None and s.lap > _stop_limit(scene, s.driver))
    if beyond:
        keep = tuple(s for s in policy.stops if s not in beyond)
        projected = replace(policy, stops=keep) if keep else Policy(policy.strategy_id, PolicyKind.CONTINUE)
        return projected, "PLANNED_ACTION_BEYOND_MARKER"
    if isinstance(policy, (ObservedPolicy, WindowedPolicy)):
        stop, = policy.stops
        upper = min(policy.last_lap, _stop_limit(scene, stop.driver))
        if upper < policy.first_lap:
            return Policy(policy.strategy_id, PolicyKind.CONTINUE), "TRIGGER_WINDOW_AFTER_MARKER"
        if isinstance(policy, ObservedPolicy):
            green = policy.green_lap if policy.green_lap is None or policy.green_lap <= upper else None
            return replace(policy, last_lap=upper, green_lap=green), (
                "PLANNED_ACTION_BEYOND_MARKER" if green != policy.green_lap else None)
        return replace(policy, last_lap=upper), None
    return policy, None


class _ScenarioAdapter(_MeanAdapter):
    def __init__(self, case, bundle):
        super().__init__(case.request, bundle)
        s = case.request.scenario
        self.scope = fingerprint((case.scenario_id, asdict(s.initial), asdict(s.physical), s.horizon_laps,
                                  asdict(s), asdict(case.request.sensitivity)))
        self.active = "initial"
        self.scoped_cache = {}
        self.scoped_hits = 0

    def set_policy_context(self, policy):
        self.active = fingerprint(policy_document(policy))

    def forecast_request(self, request, *, replacement):
        key = (self.bundle.artifact_sha256, self.scope, self.active, request, replacement)
        if key in self.scoped_cache:
            self.scoped_hits += 1
            return self.scoped_cache[key]
        # The second-level cache is scoped to this scenario and exact typed forecasts.
        batch = super().forecast_request(request, replacement=replacement)
        if self.enabled:
            self.scoped_cache[key] = batch
        return batch

    def pace_curve(self, batch, tyre_id):
        curve = super().pace_curve(batch, tyre_id)
        amount = getattr(self.perturbation, "new_stint_s", 0)
        if not amount or batch.forecast.scenario_type == SCENARIO_CONTINUE_STINT:
            return curve
        return replace(curve, lap_seconds=tuple(x+amount for x in curve.lap_seconds))


def _signature(run, markers):
    clocks = {d: val["clock_us"] for d, val in markers.items() if val}
    acts = [{k: row.get(k) for k in ("driver", "at_us", "set_id", "target_lap", "track_state")}
            for row in run["decisions"] if row["outcome"] == "REQUESTED"
            and row["driver"] in clocks and row["at_us"] < clocks[row["driver"]]]
    pits = [{k: row.get(k) for k in ("driver", "target_lap", "set_id", "entry_us", "arrival_us", "service_start_us",
                                    "service_end_us", "wait_us", "exit_us", "merge_us", "rejoin_predecessor")}
            for row in run["pit"] if row["driver"] in clocks and row["entry_us"] < clocks[row["driver"]]]
    return fingerprint((acts, pits)), acts, pits


def _case_result(case, library, oracle, request_weights, bundle):
    adapter = _ScenarioAdapter(case, bundle)
    scene = case.request.scenario
    mapped = [_project(p, scene) for p in (*library, *oracle)]
    try:
        scored = _score(scene, [p for p, _ in mapped], adapter, request_weights)
    except StrategyInputError as error:
        return {"rows": {p.strategy_id: {"candidate": p.strategy_id, "status": "UNSUPPORTED",
                                        "reasons": [error.code, str(error)], "objective_us": None,
                                        "forecast_support": (), "behavior": None}
                         for p in (*library, *oracle)}, "paired": None, "attempted": 0,
                "forecast_calls": adapter.calls, "cache_hits": adapter.hits, "policy_cache_hits": adapter.scoped_hits}
    by_id = {p.strategy_id: marker_note for p, marker_note in mapped}
    for row in scored["rows"]:
        run = scored["paired"]["results"][row["candidate"]]
        behavior, actions, visits = _signature(run, row["markers"])
        row.update(behavior=behavior, committed_actions=actions, visited_pits=visits,
                   marker_note=by_id[row["candidate"]], achieved_checkpoints={
                       d: max((p["checkpoint"] for p in run["trajectory"] if p["driver"] == d),
                              default=next(c.completed_checkpoints for c in scene.initial.cars if c.driver_number == d))
                       for d, _ in scene.targets})
        if row["status"] == "INCOMPLETE" and not row["reasons"]:
            row["reasons"] = ["EVALUATION_MARKER_UNREACHED"]
    return {"rows": {r["candidate"]: r for r in scored["rows"]}, "paired": scored["paired"], "attempted": len(mapped),
            "forecast_calls": adapter.calls, "cache_hits": adapter.hits, "policy_cache_hits": adapter.scoped_hits}


def _equivalence(cases, evaluated, margin, complete):
    output = []
    for a, b in combinations(sorted(evaluated), 2):
        shared, comparisons = [], {}
        for case, result in cases.items():
            x, y = result["rows"][a], result["rows"][b]
            if x["objective_us"] is None or y["objective_us"] is None:
                comparisons[case] = {"classification": "UNSUPPORTED_COMPARISON"}
                continue
            delta = x["objective_us"]-y["objective_us"]
            same = x["behavior"] == y["behavior"]
            if same and delta == 0:
                shared.append(case)
            comparisons[case] = {"delta_us": delta, "same_actions": same, "threshold_us": margin,
                 "classification": ("EXACT_SAME_ACTIONS" if same and delta == 0 else
                                    "EQUAL_OUTCOME_DIFFERENT_ACTIONS" if delta == 0 else
                                    "NEAR_EQUAL_OUTCOME" if abs(delta) <= margin else "CLEARLY_DIFFERENT_OUTCOME"),
                 "following_us": [x["following_us"], y["following_us"]],
                 "forecast_support": [x["forecast_support"], y["forecast_support"]]}
        label = ("POLICY_EQUIVALENT_ACROSS_FAMILY" if len(shared) == len(cases) and complete else
                 "EQUIVALENT_IN_SUBSET" if shared else None)
        output.append({"a": a, "b": b, "equivalence": label, "cases": comparisons})
    return output


def _representatives(winners, pairs, policies):
    parent = {p: p for p in winners}
    def complexity(p):
        return (len(policies[p].stops), isinstance(policies[p], (ObservedPolicy, WindowedPolicy)), p)

    for row in pairs:
        a, b = row["a"], row["b"]
        if row["equivalence"] == "POLICY_EQUIVALENT_ACROSS_FAMILY" and a in parent and b in parent:
            simple = min((parent[a], parent[b]), key=complexity)
            replaced = {parent[a], parent[b]}
            for p, representative in parent.items():
                if representative in replaced:
                    parent[p] = simple
    return sorted(set(parent.values()), key=complexity)


def evaluate_family(family, bundle, *, library=None):
    """Score a complete or rectangular truncated matrix over one shared rule library."""
    request_weights = _validate_family(family, bundle)
    policies, rejected = policy_library(family.library_reference or family.initial, contingent=family.contingent)
    if library is not None:
        allowed = set(policies)
        if not set(library) <= allowed or len(set(library)) != len(library):
            _unsupported("ROBUST_POLICY_LIBRARY", "Use a unique subset of the fixed policy definitions.")
        policies = tuple(library)
    if not any(p.strategy_id == REFERENCE for p in policies):
        _unsupported("ROBUST_POLICY_LIBRARY", "The matrix needs a shared reference policy.")
    ordered = sorted(family.scenarios, key=lambda case: case.scenario_id)
    per_scenario = min(family.total_budget//len(ordered), family.initial.budget)
    allocation = min(per_scenario, family.policy_limit)
    offered = {p.strategy_id: p for p in policies}
    if len(offered) != len(policies):
        _unsupported("ROBUST_POLICY_LIBRARY", "The policy library repeats an identifier.")
    # Deterministic reference-first truncation does not consult realized outcomes.
    selected = tuple(sorted(policies, key=lambda p: (p.strategy_id != REFERENCE, p.strategy_id))[:allocation])
    selected_ids = {p.strategy_id for p in selected}
    cases, comparators = {}, {}
    complete = selected_ids == offered.keys()
    for case in ordered:
        extra = []
        if family.include_oracle:
            oracle_policies, _, _ = _enumerate(replace(case.request, information_mode="PERFECT_FORESIGHT"))
            extra = sorted((p for p in oracle_policies if p.strategy_id.startswith("oracle-")),
                           key=lambda p: p.strategy_id)
        room = per_scenario-len(selected)
        evaluated_oracle = extra[:room]
        result = _case_result(case, selected, evaluated_oracle, request_weights, bundle)
        cases[case.scenario_id] = result
        best = min((r["objective_us"] for r in result["rows"].values() if r["objective_us"] is not None), default=None)
        comparators[case.scenario_id] = {
            "value_us": best, "winners": [k for k, row in result["rows"].items()
                                          if best is not None and row["objective_us"] == best],
            "complete": bool(family.include_oracle and complete and len(extra) == len(evaluated_oracle)
                             and best is not None),
            "oracle_only_expected": len(extra), "oracle_only_evaluated": len(evaluated_oracle),
            "excluded_for_support": sorted(k for k, r in result["rows"].items() if r["objective_us"] is None),
        }
    times = {s: {k: result["rows"][k]["objective_us"] for k in sorted(selected_ids)} for s, result in cases.items()}
    analysis = analyze_matrix(times, {s: r["value_us"] for s, r in comparators.items()}, complete=complete,
                              oracle_complete={s: r["complete"] for s, r in comparators.items()},
                              comparator=family.comparator, weights=family.weights)
    margin = NEAR_US_PER_DRIVER[family.initial.scenario.horizon_laps]*sum(request_weights.values())
    pairs = _equivalence(cases, selected_ids, margin, complete)
    winners = analysis["winners"][family.objective]
    full = [r for r in analysis["rows"] if r["comparable"]]
    for row in analysis["rows"]:
        key = row["policy"]
        invalid = [{"scenario": s, "status": result["rows"][key]["status"],
                    "reasons": result["rows"][key]["reasons"] or ["REFERENCE_UNSUPPORTED"]} for s, result in cases.items()
                   if result["rows"][key]["objective_us"] is None or analysis["deltas"][key][s] is None]
        row.update(evaluated_scenarios=len(cases), reaching_marker_scenarios=sum(
            bool(r["rows"][key].get("markers")) and all(r["rows"][key]["markers"].values()) for r in cases.values()),
            unsupported_scenarios=len(invalid), first_failure=invalid[0] if invalid else None)
    comparator_ready = family.comparator == BEST_REALIZED or all(r["complete"] for r in comparators.values())
    status = ("ROBUST_SEARCH_INCOMPLETE" if not complete or (family.objective == "MINIMAX_REGRET" and not comparator_ready)
              else "NO_FULLY_SUPPORTED_POLICY" if not full else "ROBUST_SEARCH_COMPLETE")
    metric = {"MINIMAX_REGRET": "worst_regret_us", "WORST_CASE_RELATIVE_LOSS": "worst_delta_us",
              "DECLARED_WEIGHTED_DELTA": "weighted_delta_us"}.get(family.objective)
    best_metric = min((r[metric] for r in full if r["policy"] in winners), default=None) if metric else None
    near = [r["policy"] for r in full if metric and best_metric is not None and r[metric] is not None
            and 0 < r[metric]-best_metric <= margin]
    return {"family": family.family_id, "objective": family.objective, "comparator": family.comparator, "status": status,
            "selected": winners if status == "ROBUST_SEARCH_COMPLETE" else [],
            "presentation": _representatives(winners, pairs, offered) if status == "ROBUST_SEARCH_COMPLETE" else [],
            "near": near, "near_margin_us": margin, "analysis": analysis, "cases": cases, "oracles": comparators,
            "pairs": pairs, "policies": {k: policy_document(p) for k, p in sorted(offered.items())}, "rejected": rejected,
            "search": {"offered_policies": len(policies), "evaluated_policies": len(selected), "scenarios": len(cases),
                       "expected_policy_scenario": len(policies)*len(cases),
                       "evaluated_policy_scenario": len(selected)*len(cases),
                       "unevaluated_policies": sorted(set(offered)-selected_ids),
                       "budget_rejected": (len(policies)-len(selected))*len(cases),
                       "supported_cells": sum(v is not None for r in times.values() for v in r.values()),
                       "unsupported_cells": sum(v is None for r in times.values() for v in r.values()),
                       "simulator_evaluations": sum(r["attempted"] for r in cases.values()),
                       "total_budget": family.total_budget, "per_scenario_budget": per_scenario,
                       "fixed_space_exhausted": complete, "selected_comparator_complete": comparator_ready,
                       "perfect_information_complete": all(o["complete"] for o in comparators.values())}}
