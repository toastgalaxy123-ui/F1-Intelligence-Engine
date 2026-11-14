"""Paired deterministic strategy evaluation on the S5-A.1 scheduler."""
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from hashlib import sha256
from itertools import pairwise
from json import dumps
from math import isfinite

from f1ri.features.stint_examples import DRY_COMPOUNDS
from f1ri.simulator.events import Event, EventKind
from f1ri.simulator.neutralization import simulate_neutralization
from f1ri.simulator.overtaking import OvertakeAttempt
from f1ri.simulator.pit_execution import PitStopRequest, PitTransitionPlan
from f1ri.simulator.pit_restart import PitDecision, _PitTick
from f1ri.simulator.proximity import PhysicalSnapshot, PositionQuality, physical_neighborhoods
from f1ri.simulator.race_control import RaceControlEvent, RaceControlProfile
from f1ri.simulator.state import CarStatus, RacePhase, RaceState, Route, TrackState, elapsed_us
from f1ri.simulator.strategy_pace import PaceMode, PaceSource
from f1ri.simulator.strategy_policy import DriverResources, Policy, PolicyKind, PolicyView

SCHEMA = "s5-c-strategy-v1"
INFORMATION = "POINT_IN_TIME_POLICY"
NO_PASS = "T1_NO_UNSCRIPTED_PASS"
SCRIPTED = "SCRIPTED_PASS_EVENTS"


def fingerprint(value):
    return sha256(dumps(value, sort_keys=True, default=str, allow_nan=False).encode()).hexdigest()


class StrategyInputError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class StrategyScenario:
    scenario_id: str
    experiment_id: str
    initial: RaceState
    physical: PhysicalSnapshot
    resources: tuple[DriverResources, ...]
    pace: PaceSource
    policies: tuple[Policy, ...]
    targets: tuple[tuple[int, int], ...]
    end_us: int
    horizon_laps: int
    controls: tuple[RaceControlEvent, ...] = ()
    retirements: tuple[Event, ...] = ()
    passes: tuple[OvertakeAttempt, ...] = ()
    opponents: tuple[Policy, ...] = ()
    profile: RaceControlProfile = field(default_factory=RaceControlProfile)
    dry_race_rule: bool = False
    mandatory_compounds: tuple[str, ...] = ()
    field_support: str = "DECLARED_EXACT_SCENARIO"
    schema: str = SCHEMA


def validate_scenario(s, *, verified_forecast=False):
    state, physical = s.initial, s.physical
    cars = {c.driver_number: c for c in state.cars}
    ids = set(cars)
    resources = {r.driver: r for r in s.resources}
    curves = {(c.driver, c.set_id): c for c in s.pace.curves}
    if s.schema != SCHEMA or not s.scenario_id or not s.experiment_id or state.phase is not RacePhase.RUNNING:
        raise StrategyInputError("SCENARIO_ID_OR_PHASE", "Use a versioned scenario with a running initial field.")
    if (s.end_us <= state.race_clock_us or s.horizon_laps < 1 or not s.targets or
            len(dict(s.targets)) != len(s.targets) or not s.policies or
            len({p.strategy_id for p in s.policies}) != len(s.policies)):
        raise StrategyInputError("HORIZON_OR_POLICIES", "Supply a positive horizon and distinct targets and strategies.")
    if set(resources) != ids or len(resources) != len(s.resources) or len(curves) != len(s.pace.curves):
        raise StrategyInputError("FIELD_RESOURCES", "Driver resources and pace keys must be unique and match the field.")
    if {d for d, _ in curves} != ids:
        raise StrategyInputError("FIELD_RESOURCES", "Pace curve drivers must match the scenario roster.")
    if ({c.driver_number for c in physical.cars} != ids or physical.session_key != state.session_key or
            physical.as_of_utc != state.race_start_utc + timedelta(microseconds=state.race_clock_us) or
            s.field_support not in ("DECLARED_EXACT_SCENARIO", "DECLARED_POINT_WITHIN_HISTORICAL_BOUNDS") or
            any(c.quality is not PositionQuality.EXACT_SCENARIO or c.progress_m is None or c.uncertainty_m != 0
                or not c.lap_aligned
                for c in physical.cars) or any(c.status is not CarStatus.ACTIVE or c.route is not Route.ON_TRACK
                for c in state.cars)):
        raise StrategyInputError("PHYSICAL_STATE_UNSUPPORTED", "The scheduler requires an active exact declared field.")
    if len(ids) > 1 and any(n.ahead is None for n in physical_neighborhoods(physical).values()):
        raise StrategyInputError("UNRESOLVED_PREDECESSOR", "The initial physical predecessor order is unresolved.")
    if s.pace.mode is PaceMode.FULL_MODEL and not verified_forecast:
        raise StrategyInputError("FULL_MODEL_UNSUPPORTED",
                                 "Live full-model strategy forecasts require the verified D8 bundle and a paired request adapter.")
    if s.pace.mode is PaceMode.UNSUPPORTED or not s.pace.provenance:
        raise StrategyInputError("PACE_UNSUPPORTED", "Supply a declared pace curve or verified frozen forecast.")
    if s.pace.mode is PaceMode.FROZEN_PACE_REPLAY and (not s.pace.stream_id or not s.pace.source_hashes):
        raise StrategyInputError("FROZEN_PROVENANCE_MISSING", "Frozen pace requires a verified source and sample identity.")
    if s.pace.mode is PaceMode.FROZEN_PACE_REPLAY and any(
        c.set_id != cars[c.driver].tyres.set_id or c.start_lap is None
        for c in s.pace.curves if c.driver in cars
    ):
        raise StrategyInputError("FROZEN_REPLACEMENT_UNSUPPORTED", "The archived draw supports continuation only.")
    if s.dry_race_rule and (not s.mandatory_compounds or set(s.mandatory_compounds)-set(DRY_COMPOUNDS)):
        raise StrategyInputError("TYRE_RULE_UNSUPPORTED", "Declare the mandatory dry specifications for this race.")
    for d, cp in s.targets:
        if (d not in cars or not cars[d].completed_checkpoints < cp <=
                min(state.ruleset.scheduled_laps*3, cars[d].completed_checkpoints + 3*s.horizon_laps)):
            raise StrategyInputError("INVALID_TARGET", f"Invalid common marker for car {d}.")
    for position in physical.cars:
        car = cars[position.driver_number]
        if (position.completed_laps != car.completed_laps or
                int(position.progress_m//(physical.length_m/3)) != car.sector):
            raise StrategyInputError("LAP_ALIGNMENT", f"Physical and timing state disagree for car {car.driver_number}.")
    boxes = {}
    for d, r in resources.items():
        car = cars[d]
        if (not r.team or car.team not in (None, r.team) or not isfinite(r.entry_m) or
                not 2*physical.length_m/3 <= r.entry_m < physical.length_m or
                any(t.set_id is None for t in r.tyres) or set(r.prior_dry_compounds)-set(DRY_COMPOUNDS)):
            raise StrategyInputError("PIT_LAYOUT_OR_INVENTORY", f"Unsupported pit layout or tyre allocation for car {d}.")
        sets = {t.set_id for t in r.tyres}
        if car.tyres.compound not in DRY_COMPOUNDS or car.tyres.set_id in sets or len(sets) != len(r.tyres):
            raise StrategyInputError("PIT_LAYOUT_OR_INVENTORY", f"Tyre allocation repeats or conflicts for car {d}.")
        if r.route.box_id in boxes and boxes[r.route.box_id] != r.team:
            raise StrategyInputError("BOX_ALLOCATION", f"Pit box {r.route.box_id} belongs to two teams.")
        boxes[r.route.box_id] = r.team
        curve = curves.get((d, car.tyres.set_id))
        if curve is None or car.tyres.age_laps is None:
            raise StrategyInputError("PACE_UNSUPPORTED", f"Car {d} requires a pace curve and a known current tyre age.")
        try:
            curve.seconds(car.tyres.age_laps, car.completed_laps+1)
        except ValueError as error:
            raise StrategyInputError("PACE_UNSUPPORTED", str(error)) from error
    if any(e.simulation_id != state.simulation_id or e.at_us < state.race_clock_us or
           e.profile_id != s.profile.identifier for e in s.controls) or len({e.event_id for e in s.controls}) != len(s.controls):
        raise StrategyInputError("CONTROL_SCRIPT", "Control clocks, scenario identity or rule profile do not match.")
    if any(e.kind is not EventKind.RETIREMENT or e.driver_number not in ids or e.at_us <= state.race_clock_us
           for e in s.retirements):
        raise StrategyInputError("EXOGENOUS_EVENT", "Only future fixed-clock retirements are supported here.")
    if any(a.simulation_id != state.simulation_id or a.layout_version != physical.layout_version or
           a.driver_number not in ids or a.target_driver not in ids or a.proposed_us < state.race_clock_us
           for a in s.passes):
        raise StrategyInputError("PASS_SCRIPT", "Scripted pass realization, track layout or driver does not match.")
    for p in (*s.policies, *s.opponents):
        if not p.strategy_id or (p.kind is PolicyKind.CONTINUE) != (not p.stops):
            raise StrategyInputError("POLICY_PARAMETERS", f"Invalid action list for {p.strategy_id}.")
        for stop in p.stops:
            if stop.driver not in ids or stop.delay_laps < 0 or not stop.set_id or (
                stop.lap is not None and not 1 <= stop.lap < state.ruleset.scheduled_laps
            ) or any(at is not None and at < state.race_clock_us for at in (stop.review_us, stop.cancel_us)):
                raise StrategyInputError("POLICY_PARAMETERS", f"Invalid stop parameters for {p.strategy_id}.")
    opponent_ids = {stop.driver for p in s.opponents for stop in p.stops}
    if any(stop.driver in opponent_ids for p in s.policies for stop in p.stops):
        raise StrategyInputError("OPPONENT_POLICY_CONFLICT", "A driver cannot also have a matched opponent policy.")
    return s


class _PolicyRuntime:
    def __init__(self, s, policy, *, diagnostics=None, forecast=None):
        self.s = s
        self.policy = policy
        self.actions = [(p, stop) for p in (policy, *s.opponents) for stop in p.stops]
        self.resources = {r.driver: r for r in s.resources}
        self.curves = {(p.driver, p.set_id): p for p in s.pace.curves}
        self.inventory = {r.driver: {t.set_id: t for t in r.tyres} for r in s.resources}
        self.stints = {c.driver_number: (c.tyres.set_id, c.tyres.age_laps, c.completed_laps+1)
                       for c in s.initial.cars}
        self.compounds = {c.driver_number: {c.tyres.compound, *self.resources[c.driver_number].prior_dry_compounds}
                          for c in s.initial.cars}
        self.reservations = {}
        self.evaluated = set()
        self.ledger = []
        self.unsupported = []
        self.diagnostics = diagnostics
        self.forecast = forecast

    def record_prediction(self, row, start, headway, clock_floor, movement):
        if self.diagnostics is not None:
            self.diagnostics.prediction(row, start, headway, clock_floor, movement)

    def before_commit(self, event, state, field):
        if self.diagnostics is not None:
            self.diagnostics.commit(event, state, field)

    def review_clocks(self):
        return sorted({at for _, s in self.actions for at in (s.review_us, s.cancel_us) if at is not None})

    def pace_at(self, d, checkpoint):
        set_id, age, first_lap = self.stints[d]
        lap = max(first_lap, (checkpoint-1)//3+1)
        return elapsed_us(self.curves[d, set_id].seconds(age + lap-first_lap, lap))

    def fit(self, state, driver):
        car = next(c for c in state.cars if c.driver_number == driver)
        new_set = car.tyres
        if self.forecast is not None:
            self.curves[driver, new_set.set_id] = self.forecast.replacement(state, car)
        self.stints[driver] = new_set.set_id, new_set.age_laps, new_set.stint_start_lap
        self.inventory[driver].pop(new_set.set_id)
        self.reservations.pop(driver, None)

    def _record(self, i, stop, state, outcome, **details):
        self.ledger.append({"decision_id": f"stop-{i}", "driver": stop.driver, "at_us": state.race_clock_us,
                            "set_id": stop.set_id, "outcome": outcome, **details})

    def observe(self, event, state, field):
        if self.diagnostics is not None:
            self.diagnostics.prune(field)
        if event.kind is EventKind.PIT_LANE_EXIT and event.source == "S2_DYNAMIC":
            tyre = next(c.tyres for c in state.cars if c.driver_number == event.driver_number)
            self.compounds[event.driver_number].add(tyre.compound)
        if event.kind > EventKind.STRATEGY_REVIEW or event.source == "DYNAMIC_PIT_ENTRY" or state.phase in (
            RacePhase.FINISHED, RacePhase.FINISHING,
        ):
            return
        clock = state.race_clock_us
        motion = field.progress[-1]
        signals = tuple(c["event"]["command"] for c in field.controls if c["outcome"] == "COMMITTED")
        for i, (policy, stop) in enumerate(self.actions):
            d = stop.driver
            if stop.cancel_us is not None and stop.cancel_us <= clock:
                row = field.pending_pits.get(d)
                if row is not None and row.decision_id == f"stop-{i}":
                    field._decision(_PitTick(clock, EventKind.PIT_DECISION, d, source="CANCEL",
                                             attempt_id=row.decision_id), state)
                    self.reservations.pop(d, None)
                    self._record(i, stop, state, "CANCELLED")
                elif i not in self.evaluated:
                    self._record(i, stop, state, "CANCELLED_BEFORE_REQUEST")
                self.evaluated.add(i)
                continue
            if i in self.evaluated or d in field.pending_pits or d in field.active_pits:
                continue
            car = next(c for c in state.cars if c.driver_number == d)
            if car.status is not CarStatus.ACTIVE or car.route is not Route.ON_TRACK or state.track_state is TrackState.RED:
                continue
            r = self.resources[d]
            unwrapped = motion["positions_m"].get(d)
            if unwrapped is None:
                continue
            progress = unwrapped - car.completed_laps*self.s.physical.length_m
            available = self.inventory[d]
            view = PolicyView(clock, state.track_state, signals, car, progress,
                              car.completed_laps + (2 if progress > r.entry_m + 1e-6 else 1),
                              motion["neighbors"].get(d), state.timing_order.index(d)+1, motion["gaps_m"].get(d),
                              tuple(sorted(available)), field.box_free.get(r.route.box_id))
            lap = policy.choose(stop, view)
            if lap is None:
                continue
            self.evaluated.add(i)
            tyre = available.get(stop.set_id)
            curve = self.curves.get((d, stop.set_id))
            if lap < view.next_pit_lap:
                self._record(i, stop, state, "MISSED_ENTRY", target_lap=lap)
                continue
            if lap >= state.ruleset.scheduled_laps or not tyre or (not curve and self.forecast is None):
                reason = "INVENTORY_UNAVAILABLE" if not tyre else "PACE_UNSUPPORTED" if not curve else "RACE_DISTANCE"
                self.unsupported.append(reason)
                self._record(i, stop, state, "UNSUPPORTED", reason=reason)
                continue
            try:
                if self.forecast:
                    self.forecast.check_replacement(state, car, tyre, lap)
                else:
                    curve.seconds(tyre.initial_age_laps, lap+1)
            except ValueError as exc:
                self.unsupported.append("REPLACEMENT_PACE_UNSUPPORTED")
                self._record(i, stop, state, "UNSUPPORTED", reason=str(exc))
                continue
            track = state.track_state if state.track_state in (TrackState.VSC, TrackState.SC) else TrackState.GREEN
            request = PitStopRequest(state.session_key, d, state.race_start_utc+timedelta(microseconds=clock),
                                     lap, tyre, state.simulation_id, track)
            row = PitDecision(f"stop-{i}", state.simulation_id, clock, self.s.end_us,
                              PitTransitionPlan(request, r.route), self.s.physical.layout_version, r.entry_m,
                              "S5_C_DECLARED_BRANCH_AND_ROUTE")
            field.decisions[row.decision_id] = row
            field._decision(_PitTick(clock, EventKind.PIT_DECISION, d, attempt_id=row.decision_id), state)
            if d in field.pending_pits:
                self.reservations[d] = stop.set_id
            self._record(i, stop, state, "REQUESTED", target_lap=lap, track_state=state.track_state,
                         current_progress_m=progress, available_sets=view.available_sets,
                         predecessor=view.predecessor, classification=view.rank)
    def finish(self, state):
        for i, (_, stop) in enumerate(self.actions):
            if i not in self.evaluated:
                self._record(i, stop, state, "NO_TRIGGER_BEFORE_HORIZON")
        if self.s.dry_race_rule and state.phase is RacePhase.FINISHED:
            for car in state.cars:
                if car.status is CarStatus.FINISHED:
                    compounds = self.compounds[car.driver_number]
                    if len(compounds) < 2 or not compounds.intersection(self.s.mandatory_compounds):
                        self.unsupported.append(f"DRY_COMPOUND_RULE:{car.driver_number}")


def run_strategy(s, policy):
    validate_scenario(s)
    if policy not in s.policies:
        raise StrategyInputError("UNKNOWN_STRATEGY", "Choose from the policies in the scenario.")
    return _execute_strategy(s, policy)


def _execute_strategy(s, policy, *, diagnostics=None, forecast=None):
    runtime = _PolicyRuntime(s, policy, diagnostics=diagnostics, forecast=forecast)
    try:
        run = simulate_neutralization(
            s.initial, s.physical,
            {c.driver_number: runtime.pace_at(c.driver_number, c.completed_checkpoints+1)/1e6 for c in s.initial.cars},
            s.controls, laps=s.horizon_laps, attempts=s.passes, environment=s.retirements, end_us=s.end_us,
            profile=s.profile, individual_restart=True, strategy_runtime=runtime,
        )
    except ValueError as exc:
        return {"strategy_id": policy.strategy_id, "policy": asdict(policy), "status": "UNSUPPORTED",
                "unsupported": [str(exc)], "targets": {}, "trajectory": [], "pit": [], "finish_order": None,
                "decisions": runtime.ledger}
    race = run.execution.run
    if race is None:
        return {"strategy_id": policy.strategy_id, "policy": asdict(policy), "status": "UNSUPPORTED",
                "unsupported": list(run.execution.unsupported.values()), "targets": {}, "trajectory": [],
                "pit": [], "decisions": runtime.ledger, "finish_order": None}
    runtime.finish(race.final_state)
    return _result(s, policy, run, runtime)


def _result(s, policy, run, runtime):
    race = run.execution.run
    final = race.final_state
    targets = {d: None for d, _ in s.targets}
    marker = dict(s.targets)
    trajectory = []
    elapsed_by_state = {}
    for a, b in pairwise(run.progress):
        label = a["track_state"]
        elapsed_by_state[label] = elapsed_by_state.get(label, 0) + b["at_us"] - a["at_us"]
    progress = iter(run.progress)
    motion = next(progress, None)
    for i, frame in enumerate(race.trace):
        event, state = frame.event, frame.state
        # One mechanical snapshot per event; auxiliary SC-train snapshots
        # precede the next scheduler event.
        while motion and (motion["at_us"] < event.at_us or motion["kind"] == "TRAIN_FORMED"):
            motion = next(progress, None)
        here = motion if motion and motion["at_us"] == event.at_us else None
        if here:
            motion = next(progress, None)
        for car in state.cars:
            d = car.driver_number
            row = {"event_id": i, "clock_us": event.at_us, "event": event.kind.name, "event_driver": event.driver_number,
                   "driver": d, "checkpoint": car.completed_checkpoints, "track_state": state.track_state,
                   "route": car.route, "status": car.status, "source": event.source, "tyres": asdict(car.tyres),
                   "progress_m": here["positions_m"].get(d) if here else None,
                   "physical_predecessor": here["neighbors"].get(d) if here else None,
                   "classification": state.timing_order.index(d)+1}
            trajectory.append(row)
            if d == event.driver_number and marker.get(d) == event.checkpoint and event.kind in (
                EventKind.CHECKPOINT_CROSSING, EventKind.LAP_COMPLETED, EventKind.ON_TRACK_PASS,
            ):
                targets[d] = {"clock_us": event.at_us, "elapsed_us": event.at_us-s.initial.race_clock_us,
                              "classification": row["classification"], "route": car.route, "status": car.status,
                              "physical_predecessor": row["physical_predecessor"], "checkpoint": event.checkpoint}
    bad = runtime.unsupported + [r["reason"] for r in run.pit_ledger if r["status"] == "UNSUPPORTED"]
    bad += [r["reason"] for r in run.controls if r["outcome"] in ("UNSUPPORTED", "REJECTED")]
    pit_summary = []
    by_stop = {r["decision_id"]: r for r in run.pit_ledger if r["status"] == "PIT_ENTRY_COMMITTED"}
    for ident, entry in by_stop.items():
        rows = [r for r in run.pit_ledger if r["decision_id"] == ident]
        queue = next((r for r in rows if r["status"] == "SERVICE_QUEUE"), None)
        rejoin = next((r for r in rows if r["status"] == "REJOINED"), None)
        lane_entry = next((r for r in rows if r["status"] == "PIT_LANE_TRAVERSAL"), None)
        plan = next(r for r in runtime.ledger if r["decision_id"] == ident and r["outcome"] == "REQUESTED")
        lane_elapsed = queue["lane_exit_us"]-lane_entry["at_us"] if queue and lane_entry and rejoin else None
        pit_summary.append({"decision_id": ident, "driver": entry["driver"], "entry_us": entry["at_us"],
                            "decision_us": plan["at_us"], "set_id": plan["set_id"],
                            "target_lap": plan["target_lap"], "merge_checkpoint": plan["target_lap"]*3+1,
                            "track_state_at_entry": entry["track_state"],
                            "box": queue["box"] if queue else None,
                            "arrival_us": queue["at_us"] if queue else None,
                            "service_start_us": queue["service_start_us"] if queue else None,
                            "service_end_us": queue["service_end_us"] if queue else None,
                            "service_us": queue["service_us"] if queue else None,
                            "wait_us": queue["wait_us"] if queue else None,
                            "lane_elapsed_us": lane_elapsed,
                            "lane_motion_us": lane_elapsed-queue["service_us"]-queue["wait_us"]
                            if lane_elapsed is not None else None,
                            "exit_us": queue["lane_exit_us"] if queue and rejoin else None,
                            "merge_us": rejoin["at_us"] if rejoin else None,
                            "pit_elapsed_us": rejoin["at_us"]-entry["at_us"] if rejoin else None,
                            "rejoin_predecessor": rejoin["neighbors"].get(entry["driver"]) if rejoin else None,
                            "rejoin_classification": rejoin["classification"].index(entry["driver"])+1 if rejoin else None})
    status = "UNSUPPORTED" if bad else "MARKERS_REACHED" if all(targets.values()) else "INCOMPLETE"
    return {"strategy_id": policy.strategy_id, "policy": asdict(policy), "status": status, "targets": targets,
            "unsupported": bad, "trajectory": trajectory, "physical_progress": run.progress, "pit": pit_summary,
            "pit_events": run.pit_ledger, "decisions": runtime.ledger, "race_control": run.controls,
            "restart_permissions": run.restart_permissions,
            "pass_results": [asdict(x) for x in run.execution.results], "traversals": [asdict(x) for x in run.execution.traversals],
            "elapsed_by_state_us": elapsed_by_state, "terminal_elapsed_us": final.race_clock_us-s.initial.race_clock_us,
            "finish_order": final.timing_order if final.phase is RacePhase.FINISHED and not bad else None,
            "horizon_order": final.timing_order, "remaining_sets": {d: sorted(x) for d, x in runtime.inventory.items()},
            "remaining_checkpoints": {c.driver_number: max(0, final.ruleset.scheduled_laps*3-c.completed_checkpoints)
                                      for c in final.cars if c.status is CarStatus.ACTIVE},
            "termination": "RACE_FINISHED" if final.phase is RacePhase.FINISHED else "SCENARIO_WINDOW_END",
            "terminal_state": asdict(final),
            "suspended_us": run.progress[-1]["suspended_us"] if run.progress else 0}


def _pair(ref, result, targets):
    rows = []
    ok = ref["status"] != "UNSUPPORTED" and result["status"] != "UNSUPPORTED"
    for d, marker in targets:
        a = result["targets"].get(d)
        b = ref["targets"].get(d)
        supported = bool(ok and a and b and all(
            x["route"] is Route.ON_TRACK or x["status"] is CarStatus.FINISHED for x in (a, b)))
        rows.append({"driver": d, "checkpoint": marker,
                     "delta_time_us": a["clock_us"]-b["clock_us"] if supported else None,
                     "delta_classification": a["classification"]-b["classification"] if supported else None})
    all_supported = all(r["delta_time_us"] is not None for r in rows)
    pit_accounting = []
    for stop in result["pit"]:
        d = stop["driver"]
        stay = next((r for r in ref["trajectory"] if r["event_driver"] == r["driver"] == d
                     and r["checkpoint"] == stop["merge_checkpoint"] and r["source"] != "COMMON_MERGE"
                     and r["event"] in ("CHECKPOINT_CROSSING", "ON_TRACK_PASS")
                     and r["route"] is Route.ON_TRACK), None)
        matched = (ok and stay and stop["merge_us"] is not None and
                   not any(p["driver"] == d and p["entry_us"] <= stay["clock_us"] for p in ref["pit"]))
        pit_accounting.append({"driver": d, "decision_id": stop["decision_id"],
                               "reference_marker_us": stay["clock_us"] if matched else None,
                               "paired_clock_gap_at_merge_us": stop["merge_us"]-stay["clock_us"] if matched else None,
                               "actual_pit_elapsed_us": stop["pit_elapsed_us"], "wait_us": stop["wait_us"]})
    return {"reference": ref["strategy_id"], "candidate": result["strategy_id"], "drivers": rows,
            "combined_delta_us": sum(r["delta_time_us"] for r in rows) if all_supported else None,
            "pit_count_delta": len(result["pit"])-len(ref["pit"]),
            "pit_accounting": pit_accounting,
            "explanation": "Signed difference at the same racing marker. Pit, tyre and following effects interact."}


def compare_strategies(s, reference, candidates=None, *, recorder_factory=None, forecast_adapter=None):
    continuation = ()
    if forecast_adapter is not None:
        s, continuation = forecast_adapter.attach(s)
    validate_scenario(s, verified_forecast=forecast_adapter is not None)
    policies = {p.strategy_id: p for p in s.policies}
    choices = sorted(policies if candidates is None else set(candidates) | {reference})
    if reference not in policies or any(x not in policies for x in choices):
        raise StrategyInputError("UNKNOWN_STRATEGY", "Choose from the policies in the scenario.")
    results = {}
    for key in choices:
        recorder = recorder_factory() if recorder_factory is not None else None
        forecast = forecast_adapter.branch(s, continuation) if forecast_adapter is not None else None
        result = _execute_strategy(s, policies[key], diagnostics=recorder, forecast=forecast)
        if recorder is not None:
            result["checkpoint_constraints"] = recorder.rows
        if forecast is not None:
            result["forecast_requests"] = forecast.ledger
        results[key] = result
    pairs = {key: _pair(results[reference], res, s.targets) for key, res in results.items()}
    matched_ids = {"initial_state_hash": fingerprint((asdict(s.initial), asdict(s.physical), s.field_support)),
                   "exogenous_hash": fingerprint((tuple(map(asdict, s.controls)), tuple(map(asdict, s.retirements)),
                                                  tuple(map(asdict, s.passes)), tuple(map(asdict, s.opponents)),
                                                  asdict(s.profile), s.end_us, s.targets)),
                   "pace_hash": fingerprint(asdict(s.pace)), "stream_id": s.pace.stream_id}
    ranking = sorted(((k, r["combined_delta_us"]) for k, r in pairs.items() if r["combined_delta_us"] is not None),
                     key=lambda x: (x[1], x[0]))
    return {"schema": SCHEMA, "scenario_id": s.scenario_id, "experiment_id": s.experiment_id,
            "scenario_hash": fingerprint(asdict(s)), "matched": matched_ids, "reference": reference,
            "information": INFORMATION, "trigger_clock": "FIXED_RACE_WALL_CLOCK_US",
            "pace_mode": s.pace.mode, "field_support": s.field_support, "pace": asdict(s.pace),
            "overtaking": SCRIPTED if s.passes else NO_PASS, "rule_profile": s.profile.identifier,
            "regulatory_scope": "DRY_NON_MONACO" if s.dry_race_rule else "TACTICAL_WINDOW_COMPLIANCE_NOT_ASSESSED",
            "schedule": [asdict(e) for e in s.controls], "results": results, "pairs": pairs, "ranking": ranking,
            "objective": "MINIMUM_SUM_OF_TIMES_TO_COMMON_RACING_MARKERS",
            "assumptions": ["S5-B physical pace and train behavior remain uncalibrated",
                            "T1 follows with no empirical dirty-air model",
                            "No neutralization tyre thermal model or stochastic deployment model"]}
