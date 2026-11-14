"""Scripted adjacent exchanges at a shared racing checkpoint."""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum

from f1ri.simulator.engine import RaceRun, _run_timeline
from f1ri.simulator.events import Event, EventKind
from f1ri.simulator.following import (
    FollowingTraversal,
    effective_traversal_us,
    simulate_circular_following,
)
from f1ri.simulator.proximity import (
    PhysicalRoute,
    PhysicalSnapshot,
    physical_neighborhoods,
)
from f1ri.simulator.state import (
    CarStatus,
    RacePhase,
    RaceState,
    Route,
    SimulationMode,
    TrackState,
    elapsed_us,
)


class OvertakeStatus(StrEnum):
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    UNSUPPORTED = "UNSUPPORTED"
    SUPERSEDED_BY_ROUTE_CHANGE = "SUPERSEDED_BY_ROUTE_CHANGE"


@dataclass(frozen=True, slots=True)
class OvertakeAttempt:
    attempt_id: str
    simulation_id: str
    driver_number: int
    target_driver: int
    layout_version: str
    proposed_us: int
    window_start_us: int
    window_end_us: int
    checkpoint: int
    provenance: str
    pace_provenance: str = "DECLARED_CLEAN_PACE"
    route: PhysicalRoute = PhysicalRoute.RACING
    track_state: TrackState = TrackState.GREEN
    expected_relationship: str | None = None
    attempt_source: str = "DETERMINISTIC_SCRIPT"
    support_status: str = "SUPPORTED"

    def __post_init__(self):
        if (not all((self.attempt_id, self.simulation_id, self.layout_version, self.provenance, self.pace_provenance))
                or any(type(value) is not int for value in (
                    self.driver_number, self.target_driver, self.proposed_us, self.window_start_us,
                    self.window_end_us, self.checkpoint,
                ))
                or self.driver_number <= 0 or self.target_driver <= 0 or self.driver_number == self.target_driver
                or self.checkpoint <= 0 or not 0 <= self.proposed_us <= self.window_start_us <= self.window_end_us):
            raise ValueError("Overtake script requires a unique identity, valid pair, checkpoint and chronological window")


@dataclass(frozen=True, slots=True)
class OvertakeResult:
    attempt_id: str
    status: OvertakeStatus
    reason: str | None
    resolved_us: int
    scheduled_us: int | None
    committed_us: int | None
    physical_before: tuple[int, ...]
    physical_after: tuple[int, ...]
    classification_before: tuple[int, ...]
    classification_after: tuple[int, ...]
    neighbors_after: tuple[tuple[int, int | None], ...]
    timing_adjustment_us: int | None
    relationship: str | None
    track_state: TrackState
    trace_provenance: str
    pair_at_boundary_before: tuple[int, ...] = ()
    pair_at_boundary_after: tuple[int, ...] = ()
    ruleset_version: str = "s4-a-green-only-v1"


@dataclass(frozen=True, slots=True)
class OvertakingRun:
    initial: RaceState
    run: RaceRun | None
    traversals: tuple[FollowingTraversal, ...]
    results: tuple[OvertakeResult, ...]
    interactions: tuple[dict, ...]
    unsupported: dict[int, str]


@dataclass(frozen=True, slots=True)
class _PassEvent(Event):
    attempt_id: str = ""


@dataclass(frozen=True, slots=True)
class _PendingPass:
    attempt: OvertakeAttempt
    scheduled_us: int
    target_checkpoint: int
    predecessor_checkpoint: int | None
    relationship: str


_CROSSINGS = {EventKind.CHECKPOINT_CROSSING, EventKind.LAP_COMPLETED}
_ROUTES = {EventKind.PIT_ROUTE_ENTRY, EventKind.TRACK_REJOIN, EventKind.COARSE_PIT_TRANSITION, EventKind.RETIREMENT}
_MAXIMUM_SCENARIO_SPEED_MPS = 105


def simulate_scripted_overtaking(
    initial: RaceState, physical: PhysicalSnapshot, clean_lap_s: Mapping[int, float | None],
    attempts: Iterable[OvertakeAttempt], *, laps: int, minimum_gap_s: float = .1,
    environment: Iterable[Event] = (), end_us: int | None = None,
) -> OvertakingRun:
    """An opt-in conditional window with exact checkpoint-level pass scripts.

    Optional environment events are committed S2 itinerary inputs, not
    precomputed racing events. Pit route and box timing remain owned by S2.
    """
    attempts = tuple(attempts)
    environment = tuple(replace(event, generation=None) for event in environment)
    if len({row.attempt_id for row in attempts}) != len(attempts):
        raise ValueError("Attempt IDs must be unique within a realization")
    if not attempts and not environment and end_us is None:
        run = simulate_circular_following(initial, physical, dict(clean_lap_s), laps=laps, minimum_gap_s=minimum_gap_s)
        return OvertakingRun(initial, run.run, run.traversals, (), (), run.unsupported)
    if initial.mode is not SimulationMode.CONDITIONAL_SIMULATION or initial.phase is not RacePhase.RUNNING:
        raise ValueError("Scripted passes require a running conditional simulation")
    if any(a.proposed_us < initial.race_clock_us for a in attempts):
        raise ValueError("Attempts cannot precede the physical snapshot")
    no_pass = simulate_circular_following(replace(initial, track_state=TrackState.GREEN), physical,
                                         dict(clean_lap_s), laps=laps, minimum_gap_s=minimum_gap_s)
    if no_pass.unsupported:
        results = tuple(OvertakeResult(
            a.attempt_id, OvertakeStatus.UNSUPPORTED, "PHYSICAL_OR_PACE_PREFIX_UNSUPPORTED",
            initial.race_clock_us, None, None, (), (), initial.timing_order, initial.timing_order, (),
            None, None, initial.track_state, a.provenance, ruleset_version=f"{initial.ruleset.version}/s4-a-green-only-v1",
        ) for a in attempts)
        return OvertakingRun(initial, None, (), results, (), no_pass.unsupported)
    motion = _DynamicField(initial, physical, clean_lap_s, laps, minimum_gap_s, attempts, environment, end_us)
    run = _run_timeline(initial, motion.events(), capability="S4-A", before_event=motion.before_event,
                        after_event=motion.on_event)
    for attempt in attempts:
        if attempt.attempt_id not in motion.finished_attempts:
            motion._finish(attempt, run.final_state, OvertakeStatus.EXPIRED, "SCENARIO_ENDED",
                           motion.active.get(attempt.attempt_id))
    return OvertakingRun(initial, run, tuple(motion.crossings), tuple(motion.results), tuple(motion.trace), {})


class _DynamicField:
    def __init__(self, initial, physical, pace, laps, gap_s, attempts, environment, end_us):
        self.initial = initial
        self.physical = physical
        self.pace = {driver: elapsed_us(seconds) for driver, seconds in pace.items()}
        self.gap = elapsed_us(gap_s)
        self.attempts = {row.attempt_id: row for row in attempts}
        self.environment = environment
        self.end_us = end_us
        if end_us is not None and end_us <= initial.race_clock_us:
            raise ValueError("End of window must follow the initial clock")
        self.origins = {c.driver_number: (c.unwrapped_m(physical.length_m) / physical.length_m * 3,
                                         initial.race_clock_us) for c in physical.cars}
        self.limits = {c.driver_number: min(c.completed_checkpoints + 3 * laps,
                                          initial.ruleset.scheduled_laps * 3) for c in initial.cars}
        self.ahead = {d: n.ahead.driver_number if n.ahead else None for d, n in physical_neighborhoods(physical).items()}
        self.offsets = {}
        for d, other in self.ahead.items():
            if other is not None:
                self.offsets[d, other] = math.floor((self.origins[other][0] - self.origins[d][0]) / 3) * 3
        self.committed = {}
        self.plan = {}
        self.active = {}
        self.results = []
        self.crossings = []
        self.trace = []
        self.finished_attempts = set()
        self.exchange = None
        self.suspended_at = None
        self.pause_floor = {}
        self.motion_floor = {}
        self.motion_origin = {}
        self.rejoin = {}
        self.route_boundary = {}
        self.fixed = {}
        self._validate_environment()

    def _validate_environment(self):
        allowed = _ROUTES | _CROSSINGS | {EventKind.TRACK_STATE_CHANGE, EventKind.PIT_LANE_ENTRY,
                                        EventKind.PIT_LANE_EXIT, EventKind.PIT_SERVICE_START, EventKind.PIT_SERVICE_END}
        if any(e.kind not in allowed or e.at_us <= self.initial.race_clock_us
               for e in self.environment):
            raise ValueError("Use future S2 route and track events")
        for event in sorted(self.environment, key=lambda e: (e.at_us, e.kind)):
            if event.kind in (EventKind.PIT_ROUTE_ENTRY, EventKind.COARSE_PIT_TRANSITION):
                self.route_boundary.setdefault(event.driver_number, []).append(event.at_us)
            if event.kind is EventKind.TRACK_REJOIN:
                candidates = [e for e in self.environment if e.at_us == event.at_us and e.driver_number == event.driver_number
                              and e.kind in _CROSSINGS and e.source == "COMMON_MERGE" and e.checkpoint % 3 == 1]
                if len(candidates) != 1:
                    raise ValueError("Resolved rejoin requires its committed S2 common-merge crossing")
                self.rejoin[event] = candidates[0].checkpoint
            if event.kind in _CROSSINGS:
                if event.source not in {"PIT_ROUTE_LINE", "COMMON_MERGE", "COARSE_PIT_ACCOUNTING"}:
                    raise ValueError("Only S2 pit-specific crossings may be supplied externally")
                key = (event.driver_number, event.checkpoint)
                if key in self.fixed:
                    raise ValueError("Duplicate S2 crossing")
                self.fixed[key] = event

    def events(self):
        return [Event(self.initial.race_clock_us, EventKind.TRACK_STATE_CHANGE,
                      track_state=self.initial.track_state, source="S4_WINDOW_START"),
                *(e for e in self.environment if self.end_us is None or e.at_us <= self.end_us),
                *(_PassEvent(a.proposed_us, EventKind.OVERTAKE_ATTEMPT,
                             a.driver_number if a.driver_number in self.ahead else None, source=a.provenance,
                             attempt_id=a.attempt_id) for a in self.attempts.values()
                  if self.end_us is None or a.proposed_us <= self.end_us),
                *([Event(self.end_us, EventKind.SCENARIO_WINDOW_END, source="S4_WINDOW_END")] if self.end_us is not None else [])]

    def _wrapped_order(self):
        if not self.ahead:
            return ()
        first = min(self.ahead)
        order = [first]
        while self.ahead[order[-1]] not in (None, first):
            order.append(self.ahead[order[-1]])
            if len(order) > len(self.ahead):
                raise ValueError("Racing order is not a single circular chain")
        if len(order) != len(self.ahead):
            raise ValueError("Racing order disconnected")
        return tuple(order)

    def _replace_order(self, order, positions):
        self.ahead = {driver: order[(i + 1) % len(order)] if len(order) > 1 else None for i, driver in enumerate(order)}
        self.offsets = {(driver, leader): math.floor((positions[leader] - positions[driver]) / 3) * 3
                        for driver, leader in self.ahead.items() if leader is not None}

    def _relative_order(self, first):
        order = self._wrapped_order()
        if first not in order:
            return order
        i = order.index(first)
        return order[i:] + order[:i]

    def _before(self, driver, checkpoint):
        row = self.committed.get((driver, checkpoint - 1))
        return (checkpoint - 1, row) if row is not None else self.origins[driver]

    def _pass_predecessor(self, driver, checkpoint, leader):
        return checkpoint + self.offsets[driver, leader]

    def _traversal_clean_us(self, driver, checkpoint, previous):
        origin = self.origins[driver][0]
        return max(1, round((checkpoint - origin) * self.pace[driver] / 3)
                   - round((previous - origin) * self.pace[driver] / 3))

    def _pace_at(self, driver, checkpoint):
        return self.pace[driver]

    def _crossing_floor(self, state, key, start):
        return max(state.race_clock_us + 1, self.motion_floor.get(key, start), self.pause_floor.get(key, start))

    def _minimum_free_time(self, driver, checkpoint, start, clean):
        return clean

    def _predict(self, state, bypass=None):
        """Resolve dependency constraints only for uncommitted crossings."""
        pending = {}
        for car in state.cars:
            d = car.driver_number
            if d not in self.ahead or car.status is not CarStatus.ACTIVE:
                continue
            for cp in range(car.completed_checkpoints + 1, self.limits[d] + 1):
                if (d, cp) not in self.fixed and cp > self.origins[d][0]:
                    pending[d, cp] = None
        planned = {}
        visiting = set()

        def passage(driver, checkpoint):
            key = driver, checkpoint
            if key in planned:
                return planned[key].crossing_us if planned[key] is not None else None
            if key in self.committed:
                return self.committed[key]
            if key in self.fixed:
                return self.fixed[key].at_us
            if checkpoint <= self.origins[driver][0]:
                return self.origins[driver][1]
            if key not in pending:
                # A leader outside the finite forecast horizon cannot be
                # assumed to have crossed. Stop its dependent suffix.
                return None
            if key in visiting:
                raise ValueError("Contradictory circular checkpoint dependencies")
            visiting.add(key)
            prev_cp, start = self._before(driver, checkpoint)
            if (driver, checkpoint - 1) in pending or (driver, checkpoint - 1) in self.fixed:
                prev_cp, start = checkpoint - 1, passage(driver, checkpoint - 1)
            clean = self._traversal_clean_us(driver, checkpoint, prev_cp)
            leader = self.ahead[driver]
            other = self._pass_predecessor(driver, checkpoint, leader) if leader is not None else None
            if bypass and key == (bypass.attempt.driver_number, bypass.attempt.checkpoint):
                leader = self.ahead[bypass.attempt.target_driver]
                if leader == driver:
                    leader = None
                other = bypass.predecessor_checkpoint
            elif bypass and key == (bypass.attempt.target_driver, bypass.target_checkpoint):
                leader, other = bypass.attempt.driver_number, bypass.attempt.checkpoint
            leading = passage(leader, other) if leader is not None else None
            if start is None or (leader is not None and leading is None):
                planned[key] = None
            else:
                admissible = (leading + self.gap - start) if leading is not None else 0
                minimum = max(admissible, self._crossing_floor(state, key, start) - start, 0)
                movement = self._minimum_free_time(driver, checkpoint, start, clean)
                effective = max(movement, effective_traversal_us(clean, 0, minimum))
                planned[key] = FollowingTraversal(driver, checkpoint, clean, effective, effective-clean,
                                                 leader, other, start+effective)
            visiting.remove(key)
            return planned[key].crossing_us if planned[key] is not None else None

        for driver, checkpoint in sorted(pending):
            passage(driver, checkpoint)
        return {key: value for key, value in planned.items() if value is not None}

    def _preserve_motion(self, state):
        positions = self._positions(state, state.race_clock_us)
        for car in state.cars:
            driver = car.driver_number
            if driver not in positions:
                continue
            cp = car.completed_checkpoints + 1
            progress = positions[driver]
            self.motion_origin[driver, cp] = progress, state.race_clock_us
            self.motion_floor[driver, cp] = state.race_clock_us + round((cp-progress)*self._pace_at(driver, cp)/3)

    def _reschedule(self, state, scheduler, *, preserve_motion=False):
        if preserve_motion:
            self._preserve_motion(state)
        self.plan = self._predict(state) if all(
            car.status is not CarStatus.ACTIVE or car.route is not Route.ON_TRACK or car.driver_number in self.ahead
            for car in state.cars
        ) else {}
        generations = {}
        for car in sorted(state.cars, key=lambda row: row.driver_number):
            driver = car.driver_number
            generation = scheduler.invalidate(driver)
            generations[driver] = generation
            for (d, cp), row in sorted(self.plan.items()):
                if driver != d or self.end_us is not None and row.crossing_us > self.end_us:
                    continue
                if any(state.race_clock_us < boundary < row.crossing_us for boundary in self.route_boundary.get(d, ())):
                    continue
                kind = EventKind.LAP_COMPLETED if cp % 3 == 0 else EventKind.CHECKPOINT_CROSSING
                scheduler.schedule(Event(row.crossing_us, kind, d, cp, generation=generation, source="S4_RACING"))
        self.trace.append({"at_us": state.race_clock_us, "phase": "RESCHEDULED",
                           "physical_cycle": self._wrapped_order(), "pending_crossings": len(self.plan),
                           "invalidated_generations": generations})

    def _finish(self, attempt, state, status, reason, pending=None, before=None, classification=None, adjustment=None):
        self.active.pop(attempt.attempt_id, None)
        self.finished_attempts.add(attempt.attempt_id)
        order = self._relative_order(attempt.driver_number)
        self.results.append(OvertakeResult(
            attempt.attempt_id, status, reason, state.race_clock_us, pending.scheduled_us if pending else None,
            state.race_clock_us if status is OvertakeStatus.COMPLETED else None,
            before if before is not None else order, order, classification or state.timing_order, state.timing_order,
            tuple(sorted(self.ahead.items())), adjustment, pending.relationship if pending else None,
            state.track_state, attempt.provenance,
            (attempt.target_driver, attempt.driver_number) if status is OvertakeStatus.COMPLETED else (),
            (attempt.driver_number, attempt.target_driver) if status is OvertakeStatus.COMPLETED else (),
            f"{state.ruleset.version}/s4-a-green-only-v1",
        ))
        self.trace.append({"at_us": state.race_clock_us, "phase": status.value, "attempt_id": attempt.attempt_id,
                           "reason": reason, "physical_cycle": order})

    def _propose(self, event, state, scheduler):
        a = self.attempts[event.attempt_id]
        self.trace.append({"at_us": event.at_us, "phase": "PROPOSED", "attempt_id": a.attempt_id})
        driver, leader = a.driver_number, a.target_driver
        if (a.simulation_id != state.simulation_id or a.layout_version != self.physical.layout_version
                or a.attempt_source != "DETERMINISTIC_SCRIPT" or a.support_status != "SUPPORTED"
                or a.route is not PhysicalRoute.RACING or a.track_state is not TrackState.GREEN):
            self._finish(a, state, OvertakeStatus.UNSUPPORTED, "SCRIPT_CONTEXT_UNSUPPORTED")
            return
        if state.track_state is not TrackState.GREEN or state.phase is not RacePhase.RUNNING:
            self._finish(a, state, OvertakeStatus.REJECTED, "NON_GREEN_OR_RACE_PHASE")
            return
        if driver not in self.ahead or leader not in self.ahead:
            self._finish(a, state, OvertakeStatus.SUPERSEDED_BY_ROUTE_CHANGE, "PAIR_NOT_ON_RACING_ROUTE")
            return
        if any(c.status is CarStatus.ACTIVE and c.route is Route.ON_TRACK and c.driver_number not in self.ahead
               for c in state.cars):
            self._finish(a, state, OvertakeStatus.UNSUPPORTED, "FIELD_PHYSICAL_ORDER_UNRESOLVED")
            return
        if self.ahead[driver] != leader:
            self._finish(a, state, OvertakeStatus.REJECTED, "TARGET_NOT_PHYSICAL_NEIGHBOR")
            return
        if self.active:
            self._finish(a, state, OvertakeStatus.REJECTED, "CONFLICT_WITH_PENDING_PASS")
            return
        leader_cp = self._pass_predecessor(driver, a.checkpoint, leader)
        laps = (a.checkpoint - leader_cp) // 3
        relation = "LAPPING" if laps > 0 else "BEING_LAPPED" if laps < 0 else "SAME_LAP"
        if a.expected_relationship is not None and a.expected_relationship != relation:
            self._finish(a, state, OvertakeStatus.REJECTED, "LAP_RELATION_MISMATCH")
            return
        car = next(c for c in state.cars if c.driver_number == driver)
        other_car = next(c for c in state.cars if c.driver_number == leader)
        if a.checkpoint != car.completed_checkpoints + 1 or leader_cp != other_car.completed_checkpoints + 1:
            self._finish(a, state, OvertakeStatus.REJECTED, "PAIR_REQUIRES_SHARED_NEXT_BOUNDARY")
            return
        if (a.checkpoint >= state.ruleset.scheduled_laps * 3
                or leader_cp >= state.ruleset.scheduled_laps * 3):
            self._finish(a, state, OvertakeStatus.UNSUPPORTED, "PASS_AT_FINISH_UNSUPPORTED")
            return
        start = self._before(driver, a.checkpoint)[1]
        target = self.plan.get((leader, leader_cp))
        normal = self.plan.get((driver, a.checkpoint))
        if not target or not normal:
            self._finish(a, state, OvertakeStatus.UNSUPPORTED, "CROSSING_BRACKET_UNAVAILABLE")
            return
        after_leader = self.ahead[leader]
        predecessor = (leader_cp + self.offsets[leader, after_leader]
                       if after_leader not in (None, driver) else None)
        pending = _PendingPass(a, 0, leader_cp, predecessor, relation)
        # A pass authorizes the alternate dependency at one checkpoint only.
        candidate = self._predict(state, pending).get((driver, a.checkpoint))
        progress = self._positions(state, event.at_us, driver)[driver]
        proposal_floor = event.at_us + round((a.checkpoint-progress)*self._pace_at(driver, a.checkpoint)/3)
        candidate_us = max(candidate.crossing_us, proposal_floor) if candidate else None
        distance_m = (a.checkpoint - self._before(driver, a.checkpoint)[0]) * self.physical.length_m / 3
        if (candidate is None or candidate_us + self.gap > target.crossing_us
                or start + candidate.clean_us > candidate_us):
            self._finish(a, state, OvertakeStatus.REJECTED, "INSUFFICIENT_CLEAN_PACE_ADVANTAGE")
            return
        if distance_m * 1_000_000 > (candidate_us - start) * _MAXIMUM_SCENARIO_SPEED_MPS:
            self._finish(a, state, OvertakeStatus.UNSUPPORTED, "PASS_SPEED_EXCEEDS_SCENARIO_BOUND")
            return
        if (not a.window_start_us <= candidate_us <= a.window_end_us
                or self.end_us is not None and candidate_us > self.end_us):
            self._finish(a, state, OvertakeStatus.EXPIRED, "CROSSING_OUTSIDE_SCRIPT_WINDOW")
            return
        pending = replace(pending, scheduled_us=candidate_us)
        self.active[a.attempt_id] = pending
        self.trace.append({"at_us": event.at_us, "phase": "VALIDATED", "attempt_id": a.attempt_id,
                           "scheduled_us": pending.scheduled_us, "old_crossing_us": normal.crossing_us,
                           "lap_relationship": relation, "classification_before": state.timing_order})
        self.trace.append({"at_us": event.at_us, "phase": "PASS_IN_PROGRESS", "attempt_id": a.attempt_id,
                           "meaning": "AUTHORIZED_BOUNDARY_PENDING"})
        # Keep the no-pass schedule provisional until the boundary. A route
        # or flag event at this same tick has priority over the exchange.
        scheduler.schedule(_PassEvent(pending.scheduled_us, EventKind.ON_TRACK_PASS, driver,
                                      a.checkpoint, source=a.provenance, attempt_id=a.attempt_id))

    def _positions(self, state, clock, only_driver=None):
        positions = {}
        for car in state.cars:
            d = car.driver_number
            if d not in self.ahead or only_driver is not None and d != only_driver:
                continue
            cp = car.completed_checkpoints + 1
            origin, before = self._before(d, cp)
            origin, before = self.motion_origin.get((d, cp), (origin, before))
            next_row = self.plan.get((d, cp))
            if next_row is None:
                positions[d] = origin
            elif next_row.crossing_us == before:
                positions[d] = cp
            else:
                fraction = min(1., max(0., (clock - before) / (next_row.crossing_us - before)))
                positions[d] = origin + (cp-origin) * fraction
        return positions

    def _drop_car(self, driver):
        if driver not in self.ahead:
            return
        leader = self.ahead[driver]
        follower = next((d for d in self.ahead if self.ahead[d] == driver), None)
        if follower is not None:
            self.ahead[follower] = leader if leader != follower else None
            if leader not in (None, follower):
                self.offsets[follower, leader] = self.offsets[follower, driver] + self.offsets[driver, leader]
        self.ahead.pop(driver)

    def _rejoin(self, state, event, cp):
        positions = self._positions(state, event.at_us)
        if any(abs((p-cp+1.5) % 3 - 1.5) <= 1e-9 for p in positions.values()):
            raise ValueError("Simultaneous merge conflicts with a racing-route crossing")
        positions[event.driver_number] = cp
        original = dict(self.ahead)
        old_offsets = dict(self.offsets)
        proposed = sorted(positions, key=lambda d: (positions[d] % 3, d))
        prior = [d for d in proposed if d != event.driver_number]
        if len(prior) > 1 and any(original[d] != prior[(i + 1) % len(prior)] for i, d in enumerate(prior)):
            raise ValueError("Unresolved racing order at the S2 common merge")
        self.origins[event.driver_number] = (cp, event.at_us)
        self._replace_order(proposed, positions)
        for d, leader in self.ahead.items():
            if leader is not None and original.get(d) == leader:
                self.offsets[d, leader] = old_offsets[d, leader]
        self.trace.append({"at_us": event.at_us, "phase": "RESOLVED_REJOIN", "driver": event.driver_number,
                           "predecessors_before": original, "predecessors_after": dict(self.ahead)})

    def before_event(self, event, state):
        if event.kind is not EventKind.ON_TRACK_PASS:
            if event.kind in _CROSSINGS and event.source == "S4_RACING" and event.driver_number not in self.ahead:
                return None
            if state.phase is RacePhase.SUSPENDED and event.kind in _CROSSINGS:
                raise ValueError("A fixed S2 crossing cannot occur during suspension")
            return event
        pending = self.active.get(event.attempt_id)
        if pending is None:
            return None
        a = pending.attempt
        driver, leader = a.driver_number, a.target_driver
        state = replace(state, race_clock_us=event.at_us)
        if (state.track_state is not TrackState.GREEN or self.ahead.get(driver) != leader
                or state.phase is not RacePhase.RUNNING):
            self._finish(a, state, OvertakeStatus.CANCELLED, "RELATIONSHIP_OR_TRACK_STATE_CHANGED", pending)
            return None
        before = self._relative_order(driver)
        normal = self.plan.get((driver, a.checkpoint))
        if normal is None:
            self._finish(a, state, OvertakeStatus.CANCELLED, "CROSSING_PLAN_INVALIDATED", pending)
            return None
        preceding_driver = self.ahead[leader] if pending.predecessor_checkpoint is not None else None
        if preceding_driver is not None:
            preceding = self.committed.get((preceding_driver, pending.predecessor_checkpoint))
            if preceding is None and pending.predecessor_checkpoint <= self.origins[preceding_driver][0]:
                preceding = self.origins[preceding_driver][1]
            if preceding is None or event.at_us < preceding + self.gap:
                self._finish(a, state, OvertakeStatus.CANCELLED, "PASS_PREDECESSOR_NOT_COMMITTED", pending)
                return None
        # The swap occurs as the attacker crosses the shared boundary. Until
        # then the two cars remain in the old order at supported checkpoints.
        crossing = replace(normal, predecessor_driver=preceding_driver,
                           predecessor_checkpoint=pending.predecessor_checkpoint,
                           effective_us=event.at_us-self._before(driver, a.checkpoint)[1],
                           traffic_delay_us=event.at_us-self._before(driver, a.checkpoint)[1]-normal.clean_us,
                           crossing_us=event.at_us)
        if crossing.traffic_delay_us < 0:
            self._finish(a, state, OvertakeStatus.CANCELLED, "CLEAN_TRAVERSAL_FLOOR", pending)
            return None
        old = self.offsets[driver, leader]
        follower = next(d for d in self.ahead if self.ahead[d] == driver)
        forward = self.ahead[leader]
        if len(self.ahead) == 2:
            self.offsets[leader, driver] = -old
            self.offsets[driver, leader] = old - 3
        else:
            self.ahead[follower], self.ahead[leader], self.ahead[driver] = leader, driver, forward
            self.offsets[follower, leader] = self.offsets[follower, driver] + old
            self.offsets[leader, driver] = -old
            self.offsets[driver, forward] = old + self.offsets[leader, forward]
        self.exchange = (crossing, before, state.timing_order, normal.crossing_us - event.at_us)
        kind = EventKind.LAP_COMPLETED if a.checkpoint % 3 == 0 else EventKind.CHECKPOINT_CROSSING
        return replace(event, kind=kind)

    def on_event(self, event, state, scheduler):
        if event.kind is EventKind.ON_TRACK_PASS:
            crossing, before, classification, adjustment = self.exchange
            pending = self.active[event.attempt_id]
            self.committed[event.driver_number, event.checkpoint] = event.at_us
            self.crossings.append(crossing)
            self._finish(pending.attempt, state, OvertakeStatus.COMPLETED, None, pending, before, classification,
                         adjustment)
            self.exchange = None
            self._reschedule(state, scheduler, preserve_motion=True)
        elif event.kind is EventKind.OVERTAKE_ATTEMPT:
            self._propose(event, state, scheduler)
        elif event.kind is EventKind.SCENARIO_WINDOW_END:
            for pending in tuple(self.active.values()):
                self._finish(pending.attempt, state, OvertakeStatus.EXPIRED, "END_OF_SCENARIO", pending)
        elif event.kind in _CROSSINGS:
            self.committed[event.driver_number, event.checkpoint] = event.at_us
            if event.source == "S4_RACING":
                self.crossings.append(self.plan[event.driver_number, event.checkpoint])
            car = next(c for c in state.cars if c.driver_number == event.driver_number)
            if car.status is CarStatus.FINISHED:
                self._drop_car(car.driver_number)
                for pending in tuple(self.active.values()):
                    self._finish(pending.attempt, state, OvertakeStatus.CANCELLED, "FIELD_FINISH_TRANSITION", pending)
                self._reschedule(state, scheduler, preserve_motion=True)
            elif event.source == "COMMON_MERGE":
                self._reschedule(state, scheduler, preserve_motion=True)
            elif event.source == "COARSE_PIT_ACCOUNTING":
                self.trace.append({"at_us": event.at_us, "phase": "COARSE_REJOIN_POSITION_UNRESOLVED",
                                   "driver": event.driver_number})
                for pending in tuple(self.active.values()):
                    self._finish(pending.attempt, state, OvertakeStatus.CANCELLED, "FIELD_PHYSICAL_ORDER_UNRESOLVED", pending)
                self._reschedule(state, scheduler)
        elif event.kind in _ROUTES or event.kind is EventKind.TRACK_STATE_CHANGE:
            if event.kind in (EventKind.PIT_ROUTE_ENTRY, EventKind.COARSE_PIT_TRANSITION, EventKind.RETIREMENT):
                self._drop_car(event.driver_number)
                self.trace.append({"at_us": event.at_us, "phase": event.kind.name, "driver": event.driver_number,
                                   "predecessors_after": dict(self.ahead)})
            if event.kind is EventKind.TRACK_REJOIN:
                self._rejoin(state, event, self.rejoin[event])
            for pending in tuple(self.active.values()):
                a = pending.attempt
                if event.driver_number in (a.driver_number, a.target_driver):
                    self._finish(a, state, OvertakeStatus.SUPERSEDED_BY_ROUTE_CHANGE, event.kind.name, pending)
                elif state.track_state is not TrackState.GREEN or event.kind in _ROUTES:
                    self._finish(a, state, OvertakeStatus.CANCELLED, event.kind.name, pending)
            if state.track_state is TrackState.RED:
                if self.suspended_at is None:
                    self._preserve_motion(state)
                    self.suspended_at = event.at_us
                for driver in self.ahead:
                    scheduler.invalidate(driver)
            elif event.kind is not EventKind.TRACK_REJOIN:
                resumed = self.suspended_at is not None
                if resumed:
                    paused = event.at_us - self.suspended_at
                    for car in state.cars:
                        key = car.driver_number, car.completed_checkpoints + 1
                        row = self.plan.get(key)
                        if key in self.motion_origin:
                            if row is not None:
                                self.pause_floor[key] = row.crossing_us + paused
                            progress, _ = self.motion_origin[key]
                            self.motion_origin[key] = progress, event.at_us
                            self.motion_floor[key] += paused
                    self.suspended_at = None
                self._reschedule(state, scheduler, preserve_motion=not resumed and event.source != "S4_WINDOW_START")
        return event
