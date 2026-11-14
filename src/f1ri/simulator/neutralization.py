"""Deterministic neutralization on the shared S4 field and checkpoint timeline."""
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from math import ceil, floor

from f1ri.simulator.engine import _PIT_ROUTE_CHANGES, _run_timeline
from f1ri.simulator.events import Event, EventKind
from f1ri.simulator.overtaking import (
    _CROSSINGS,
    _ROUTES,
    OvertakeStatus,
    OvertakingRun,
    _DynamicField,
    simulate_scripted_overtaking,
)
from f1ri.simulator.race_control import (
    LEGAL_TRANSITIONS,
    RED_PHASES,
    SC_PHASES,
    UNSUPPORTED_COMMANDS,
    VSC_PHASES,
    RaceControlProfile,
    coarse_track,
)
from f1ri.simulator.race_control import (
    ControlCommand as C,
)
from f1ri.simulator.race_control import (
    ControlOutcome as O,
)
from f1ri.simulator.race_control import (
    ControlPhase as P,
)
from f1ri.simulator.state import CarStatus, RacePhase, Route, SimulationMode, TrackState

_PIT_EVENTS = frozenset(_PIT_ROUTE_CHANGES)
_PIT_CROSSINGS = {"PIT_ROUTE_LINE", "COMMON_MERGE", "COARSE_PIT_ACCOUNTING"}
_DEFAULT_PROFILE = RaceControlProfile()


@dataclass(frozen=True, slots=True)
class _ControlTick(Event):
    attempt_id: str = ""


@dataclass(frozen=True, slots=True)
class NeutralizationRun:
    execution: OvertakingRun
    controls: tuple[dict, ...] = ()
    progress: tuple[dict, ...] = ()
    rule_profile: RaceControlProfile | None = None
    pit_ledger: tuple[dict, ...] = ()
    restart_permissions: tuple[dict, ...] = ()


def simulate_neutralization(
    initial, physical, clean_lap_s, controls, *, laps, attempts=(), environment=(),
    minimum_gap_s=.1, end_us=None, profile=_DEFAULT_PROFILE,
    pit_decisions=(), individual_restart=False, strategy_runtime=None,
):
    """Execute only the supplied intervention script. No random stream is read."""
    controls, attempts = tuple(controls), tuple(attempts)
    environment = tuple(replace(event, generation=None) for event in environment)
    pit_decisions = tuple(pit_decisions)
    if not controls and not pit_decisions and strategy_runtime is None:
        return NeutralizationRun(simulate_scripted_overtaking(
            initial, physical, clean_lap_s, attempts, laps=laps, environment=environment,
            minimum_gap_s=minimum_gap_s, end_us=end_us,
        ))
    if initial.ruleset.season != profile.season or initial.track_state not in (
        TrackState.GREEN, TrackState.YELLOW, TrackState.DOUBLE_YELLOW,
    ) or initial.phase is not RacePhase.RUNNING or initial.mode is not SimulationMode.CONDITIONAL_SIMULATION:
        raise ValueError("S5-A requires a 2025 running field and a declared green/caution snapshot")
    if (len({e.event_id for e in controls}) != len(controls)
            or len({a.attempt_id for a in attempts}) != len(attempts)
            or any(e.at_us < initial.race_clock_us or e.event_id == "@release" for e in controls)
            or any(a.proposed_us < initial.race_clock_us for a in attempts)):
        raise ValueError("S5-A requires unique script IDs and events at or after the snapshot")
    if any(e.kind is EventKind.TRACK_STATE_CHANGE for e in environment):
        raise ValueError("Declare race-control changes through RaceControlEvent")
    baseline = simulate_scripted_overtaking(replace(initial, track_state=TrackState.GREEN), physical,
                                           clean_lap_s, (), laps=laps, minimum_gap_s=minimum_gap_s)
    if baseline.unsupported and set(baseline.unsupported.values()) != {"PREDECESSOR_WINDOW_UNSUPPORTED"}:
        rows = tuple({"event": asdict(e), "outcome": O.UNSUPPORTED, "reason": "PHYSICAL_PREFIX_UNSUPPORTED"}
                     for e in controls)
        return NeutralizationRun(baseline, rows, rule_profile=profile)
    options = {}
    field_type = _NeutralizedField
    if pit_decisions or individual_restart or strategy_runtime is not None:
        from f1ri.simulator.pit_restart import StabilizedField

        field_type = StabilizedField
        options = {"pit_decisions": pit_decisions, "individual_restart": individual_restart,
                   "strategy_runtime": strategy_runtime}
    motion = field_type(initial, physical, clean_lap_s, laps, minimum_gap_s, attempts,
                        environment, end_us, controls=controls, profile=profile, **options)
    run = _run_timeline(initial, motion.events(), capability="S5-A", before_event=motion.before_event,
                        after_event=motion.on_event)
    for a in attempts:
        if a.attempt_id not in motion.finished_attempts:
            motion._finish(a, run.final_state, OvertakeStatus.EXPIRED, "SCENARIO_ENDED",
                           motion.active.get(a.attempt_id))
    for e in controls:
        if e.event_id not in motion.seen:
            motion.record(e, O.CANCELLED, "SCENARIO_ENDED", motion.phase, run.final_state.race_clock_us)
    if motion.release:
        motion.record(motion.release, O.CANCELLED, "RELEASE_CONDITION_UNMET_AT_END",
                      motion.phase, run.final_state.race_clock_us)
    execution = OvertakingRun(initial, run, tuple(motion.crossings), tuple(motion.results), tuple(motion.trace), {})
    return NeutralizationRun(execution, tuple(motion.controls), tuple(motion.progress), profile,
                             tuple(getattr(motion, "pit_ledger", ())),
                             tuple(getattr(motion, "restart_permissions", ())))


class _NeutralizedField(_DynamicField):
    def __init__(self, *args, controls, profile, **kwargs):
        super().__init__(*args, **kwargs)
        self.commands = {e.event_id: e for e in controls}
        self.profile = profile
        self.phase = P.GREEN_RUNNING
        self.local = self.initial.track_state
        self.controls = []
        self.progress = []
        self.seen = set()
        self.epoch = self.initial.race_clock_us
        self.boundary = None
        self.pacer = None
        self.paths = {}
        self.release = None
        self.release_lines = {}
        self.release_queued = False
        self.paused_us = 0
        self.red_at = None
        self.allow_equal = False
        self.skipped_pit = set()
        self.missed_entry = set()
        self.step = self.physical.length_m / 3
        if self.profile.train_gap_m * len(self.ahead) >= self.physical.length_m:
            raise ValueError("The pacing profile cannot fit the declared field on this circuit")

    def events(self):
        return [*super().events(), *(
            _ControlTick(e.at_us, EventKind.RACE_CONTROL, source=e.source, attempt_id=e.event_id)
            for e in self.commands.values() if self.end_us is None or e.at_us <= self.end_us
        )]

    def record(self, event, outcome, reason, before, clock, states=()):
        self.seen.add(event.event_id)
        self.controls.append({"event": asdict(event), "outcome": outcome, "reason": reason,
                              "before": before, "after": self.phase, "phases": states, "clock_us": clock})

    def _positions(self, state, clock, only_driver=None):
        if not self.paths or self.phase is P.GREEN_RUNNING:
            raw = super()._positions(state, clock)
            committed = {c.driver_number: c.completed_checkpoints for c in state.cars}
            result = {d: max(p, committed[d]) for d, p in raw.items()}
            # S4 commits the no-pass relation at checkpoints. Bound its
            # interior interpolation by the same physical predecessor cycle.
            for d, progress in tuple(result.items()):
                node, offset = d, 0
                for _ in range(len(result)-1):
                    leader = self.ahead[node]
                    if leader not in result:
                        break
                    offset += self.offsets[node, leader]
                    progress = min(progress, max(raw[leader], committed[leader]) - offset)
                    node = leader
                result[d] = max(progress, committed[d])
            return {d: p for d, p in result.items() if only_driver in (None, d)}
        elapsed = max(0, clock - self.epoch)
        return {d: min(origin + speed * elapsed for origin, speed in self.paths[d])
                for d in self.ahead if d in self.paths and only_driver in (None, d)}

    def _crossing_floor(self, state, key, start):
        # Race control precedes crossings at an identical timestamp.
        return max(state.race_clock_us + (0 if self.allow_equal else 1), self.motion_floor.get(key, start))

    def _minimum_free_time(self, driver, checkpoint, start, clean):
        if not self.paths:
            return clean
        arrival = max((checkpoint-origin) / rate for origin, rate in self.paths[driver] if rate > 0)
        return max(1, self.epoch + ceil(arrival - 1e-7) - start)

    def _reschedule(self, state, scheduler, *, preserve_motion=False):
        before = self.plan
        super()._reschedule(state, scheduler, preserve_motion=preserve_motion)
        self.trace[-1].update(invalidated_crossings=sum(r.crossing_us >= state.race_clock_us for r in before.values()),
                              regenerated_crossings=len(self.plan))

    def _rebuild(self, state, scheduler, positions, *, deployment=False, boundary_tick=False):
        clock = state.race_clock_us
        old_epoch = self.epoch
        self.epoch = clock
        self.motion_floor.clear()
        self.motion_origin.clear()
        self.paths.clear()
        for c in state.cars:
            d, cp = c.driver_number, c.completed_checkpoints + 1
            if d in positions:
                self.motion_origin[d, cp] = positions[d], clock
                self.motion_floor[d, cp] = clock + max(0, round((cp-positions[d]) * self._pace_at(d, cp)/3))
        if self.phase in RED_PHASES:
            invalidated = sum(r.crossing_us >= clock for r in self.plan.values())
            self.paths = {d: [(p, 0.)] for d, p in positions.items()}
            self.plan = {}
            for d in self.ahead:
                scheduler.invalidate(d)
            self.trace.append({"at_us": clock, "phase": "RESCHEDULED", "invalidated_crossings": invalidated,
                               "regenerated_crossings": 0})
            return
        if self.phase in VSC_PHASES and positions:
            # One common speed preserves separation for the whole circuit.
            speed = min(self.profile.vsc_speed_mps, *(self.physical.length_m*1e6/
                        self._pace_at(d, floor(positions[d])+1) for d in positions))
            self.paths = {d: [(p, speed/self.step/1e6)] for d, p in positions.items()}
            self.motion_floor.clear()
        elif self.phase in SC_PHASES and positions:
            self._sc_paths(state, positions, old_epoch, deployment)
            self.motion_floor.clear()
        else:
            self.boundary = self.pacer = None
        self.allow_equal = boundary_tick
        self._reschedule(state, scheduler)
        self.allow_equal = False

    def _sc_paths(self, state, positions, old_epoch, deployment):
        profile = self.profile
        restart = self.phase in (P.SC_IN_THIS_LAP, P.SC_RESTART_PENDING)
        base = profile.restart_speed_mps if restart else profile.sc_speed_mps
        unit = self.step * 1e6
        if self.boundary is None or deployment:
            self.pacer = next(d for d in state.timing_order if d in positions)
            virtual = positions[self.pacer] + profile.queue_gap_m / self.step
        else:
            previous, rate = self.boundary
            virtual = previous + rate * (self.epoch-old_epoch)
            if self.pacer not in positions:
                self.pacer = min(positions, key=lambda d: (virtual - positions[d]) % 3)
                virtual = positions[self.pacer] + (virtual-positions[self.pacer]) % 3
        # The virtual boundary uses the declared ceiling and remains
        # collectable by the pacing car in slow synthetic fields.
        pace = {d: self._pace_at(d, floor(p)+1) for d, p in positions.items()}
        base = min(base, self.physical.length_m*1e6/pace[self.pacer])
        self.boundary = virtual, base / unit
        # Each trajectory is the minimum of its own catch-up line and the
        # forward lines translated by their legal circular separation.
        edge = {}
        free = {}
        for d, p in positions.items():
            leader = self.ahead[d]
            free[d] = (p, min(profile.catchup_speed_mps, self.physical.length_m*1e6/pace[d]) / unit)
            if leader is not None:
                delta = positions[leader] - p - self.offsets[d, leader]
                if delta < -1e-7:
                    raise ValueError("Physical predecessor already behind its follower")
                edge[d] = self.offsets[d, leader] + min(max(0., delta), profile.queue_gap_m / self.step)
        p = self.pacer
        pacing_origin = positions[p] if restart else virtual - min(
            profile.queue_gap_m / self.step, max(0., virtual-positions[p])
        )
        pacing_line = pacing_origin, base/unit
        for d in positions:
            lines = [free[d]]
            if d == p:
                lines.append(pacing_line)
            translated = 0.
            node = d
            for _ in range(len(positions)-1):
                leader = self.ahead[node]
                if leader is None:
                    break
                translated += edge[node]
                node = leader
                lines.append((free[node][0] - translated, free[node][1]))
                if node == p:
                    lines.append((pacing_line[0] - translated, pacing_line[1]))
            self.paths[d] = lines

    def _snapshot(self, state, kind):
        clock = state.race_clock_us
        positions = self._positions(state, clock)
        gaps = {d: (positions[leader] - positions[d] - self.offsets[d, leader]) * self.step
                for d, leader in self.ahead.items() if d in positions and leader in positions}
        paused = self.paused_us + (clock-self.red_at if self.red_at is not None else 0)
        self.progress.append({
            "at_us": clock, "kind": kind, "phase": self.phase, "track_state": state.track_state,
            "positions_m": {d: p*self.step for d, p in positions.items()}, "gaps_m": gaps,
            "neighbors": dict(self.ahead), "checkpoint_counts": {c.driver_number: c.completed_checkpoints
                                                                   for c in state.cars},
            "routes": {c.driver_number: c.route for c in state.cars}, "classification": state.timing_order,
            "classification_phase": state.phase, "wall_elapsed_us": clock-self.initial.race_clock_us,
            "active_elapsed_us": clock-self.initial.race_clock_us-paused, "suspended_us": paused,
            "pacer": self.pacer,
            "virtual_sc_m": ((self.boundary[0] + self.boundary[1]*(clock-self.epoch))*self.step
                             if self.boundary and self.phase in SC_PHASES else None),
            "plan_size": len(self.plan),
        })
        return gaps

    def _control(self, tick, state):
        e = self.commands[tick.attempt_id]
        clock = tick.at_us
        previous = self.phase

        def reject(outcome, reason):
            self.record(e, outcome, reason, previous, clock)

        if e.simulation_id != state.simulation_id or e.profile_id != self.profile.identifier:
            return reject(O.UNSUPPORTED, "RULE_PROFILE_OR_REALIZATION_MISMATCH")
        if e.support != "SUPPORTED" or e.source not in ("DETERMINISTIC_SCRIPT", "ARCHIVED_RACE_CONTROL_REPLAY"):
            return reject(O.UNSUPPORTED, "SOURCE_UNSUPPORTED")
        effective_utc = state.race_start_utc + timedelta(microseconds=clock)
        if e.source == "ARCHIVED_RACE_CONTROL_REPLAY" and (
            e.observation_utc is None or e.observation_utc > effective_utc
        ):
            return reject(O.UNSUPPORTED, "ARCHIVED_OBSERVATION_CLOCK_UNSUPPORTED")
        if (e.scope != ("SECTOR" if e.command is C.LOCAL else "TRACK")
                or e.affected_cars and set(e.affected_cars) != {c.driver_number for c in state.cars
                                                               if c.status is CarStatus.ACTIVE}):
            return reject(O.UNSUPPORTED, "SCOPE_UNSUPPORTED")
        if e.previous is not None and e.previous is not previous:
            return reject(O.REJECTED, "PREVIOUS_PHASE_MISMATCH")
        if e.command in UNSUPPORTED_COMMANDS:
            return reject(O.UNSUPPORTED, UNSUPPORTED_COMMANDS[e.command])
        if e.command is C.RED and any(c.status is CarStatus.ACTIVE and c.route is not Route.ON_TRACK for c in state.cars):
            return reject(O.UNSUPPORTED, "RED_WITH_ACTIVE_PIT_ITINERARY")
        if e.command is C.LOCAL:
            if e.local_flag not in (TrackState.GREEN, TrackState.YELLOW, TrackState.DOUBLE_YELLOW):
                return reject(O.REJECTED, "INVALID_LOCAL_FLAG")
            phases = ()
        else:
            legal, phases = LEGAL_TRANSITIONS[e.command]
            if previous not in legal:
                return reject(O.REJECTED, "INVALID_TRANSITION")
        if e.requested is not None and e.requested is not (phases[-1] if phases else previous):
            return reject(O.REJECTED, "REQUESTED_PHASE_MISMATCH")
        if e.command is C.ROLLING and e.restart not in (None, "ROLLING"):
            return reject(O.UNSUPPORTED, "RESTART_ORDER_OR_MODE_UNSUPPORTED")
        if state.phase is RacePhase.FINISHING and e.command not in (C.LOCAL, C.GREEN):
            return reject(O.UNSUPPORTED, "NEW_INTERVENTION_AFTER_CHEQUERED")
        if e.command is C.GREEN and previous is P.SC_RESTART_PENDING and self._defer_team_release():
            if self.release:
                return reject(O.REJECTED, "RELEASE_ALREADY_PENDING")
            self.release = e
            return reject(O.DEFERRED, "WAITING_FOR_ALL_ACTIVE_RACING_CARS_TO_CROSS_LINE")
        positions = self._positions(state, clock)
        if e.command is C.LOCAL:
            self.local = e.local_flag
        else:
            self.phase = phases[-1]
        if self.release and e.command in (C.RED, C.SC):
            self.record(self.release, O.SUPERSEDED, "NEUTRALIZATION_REDEPLOYED", previous, clock)
            self.release = None
            self.release_queued = False
            self.release_lines.clear()
        if e.command is C.SC_WITHDRAWN:
            # An exact same-tick line crossing occurs after this command.
            # It can qualify as the first line following withdrawal.
            self.release_lines = {c.driver_number: (c.completed_laps+1)*3 for c in state.cars
                                  if c.driver_number in self.ahead}
        if e.command is C.RED:
            self.red_at = clock
        if e.command is C.ROLLING:
            self.paused_us += clock-self.red_at
            self.red_at = None
        self.transition = (positions, e.command in (C.SC, C.ROLLING), e.command is C.LOCAL)
        self.record(e, O.COMMITTED, None, previous, clock, phases)
        return Event(clock, EventKind.TRACK_STATE_CHANGE, track_state=coarse_track(self.phase, self.local),
                     source=e.source)

    def _defer_team_release(self):
        return bool(self.release_lines)

    def before_event(self, event, state):
        if event.kind is EventKind.RACE_CONTROL:
            if event.attempt_id == "@release":
                if self.release is None:
                    return None
                positions = self._positions(state, event.at_us)
                self.phase = P.GREEN_RUNNING
                self.transition = positions, False, False
                self.record(self.release, O.COMMITTED, None, P.SC_RESTART_PENDING, event.at_us, (self.phase,))
                self.release = None
                self.release_queued = False
                return Event(event.at_us, EventKind.TRACK_STATE_CHANGE, track_state=self.local)
            return self._control(event, state)
        if event.kind in _PIT_EVENTS or (
            event.kind in _CROSSINGS and event.source in _PIT_CROSSINGS
        ):
            if event.driver_number in self.skipped_pit:
                return None
            if event.kind in (EventKind.PIT_ROUTE_ENTRY, EventKind.COARSE_PIT_TRANSITION):
                return self._check_pit_entry(event, state)
            if event.kind in _CROSSINGS:
                car = next(c for c in state.cars if c.driver_number == event.driver_number)
                if car.status is CarStatus.ACTIVE and event.checkpoint != car.completed_checkpoints + 1:
                    raise ValueError("S2 pit crossing is out of sequence")
            return event
        return super().before_event(event, state)

    def _check_pit_entry(self, event, state):
        driver = event.driver_number
        car = next(c for c in state.cars if c.driver_number == driver)
        future = [e.checkpoint for e in self.fixed.values() if e.driver_number == driver
                  and e.at_us > event.at_us and e.source == "PIT_ROUTE_LINE"]
        if (self.phase in RED_PHASES or event.kind is EventKind.COARSE_PIT_TRANSITION
                or not future or min(future) != car.completed_checkpoints + 1):
            self.skipped_pit.add(driver)
            self.missed_entry.add(event)
            self.fixed = {k: e for k, e in self.fixed.items() if k[0] != driver}
            self.route_boundary.pop(driver, None)
            return _ControlTick(event.at_us, EventKind.SC_QUEUE_UPDATE, driver,
                                source="S2_ROUTE_ENTRY_UNSUPPORTED")
        return event

    def on_event(self, event, state, scheduler):
        clock = event.at_us
        if event.kind is EventKind.RACE_CONTROL:
            positions, deployment, local_only = self.transition
            if state.track_state is not TrackState.GREEN:
                for pending in tuple(self.active.values()):
                    self._finish(pending.attempt, state, OvertakeStatus.CANCELLED, "RACE_CONTROL", pending)
            if not local_only:
                self._rebuild(state, scheduler, positions, deployment=deployment, boundary_tick=True)
        elif event.kind in _ROUTES or event.kind is EventKind.SC_QUEUE_UPDATE:
            positions = self._positions(state, clock)
            if event in self.missed_entry:
                self.trace.append({"at_us": clock, "phase": "PIT_UNSUPPORTED", "driver": event.driver_number,
                                   "reason": "DECLARED_ENTRY_CHECKPOINT_OR_RED_ROUTE_UNSUPPORTED"})
                self._rebuild(state, scheduler, positions)
                self._snapshot(state, "PIT_UNSUPPORTED")
                return None
            if event.kind in (EventKind.PIT_ROUTE_ENTRY, EventKind.RETIREMENT):
                positions.pop(event.driver_number, None)
                self._drop_car(event.driver_number)
                self.release_lines.pop(event.driver_number, None)
            if event.kind is EventKind.TRACK_REJOIN:
                self._rejoin(state, event, self.rejoin[event])
                positions[event.driver_number] = self.rejoin[event]
                if self.phase is P.SC_RESTART_PENDING:
                    self.release_lines[event.driver_number] = (self.rejoin[event]//3+1)*3
            for pending in tuple(self.active.values()):
                self._finish(pending.attempt, state, OvertakeStatus.SUPERSEDED_BY_ROUTE_CHANGE,
                             event.kind.name, pending)
            self._rebuild(state, scheduler, positions,
                          boundary_tick=event.kind in (EventKind.RETIREMENT, EventKind.TRACK_REJOIN))
        elif event.kind is EventKind.TRACK_STATE_CHANGE:
            self._reschedule(state, scheduler)
        elif self.phase in SC_PHASES or self.phase in VSC_PHASES:
            if event.kind in _CROSSINGS:
                self.committed[event.driver_number, event.checkpoint] = clock
                if event.source == "S4_RACING":
                    self.crossings.append(self.plan[event.driver_number, event.checkpoint])
                car = next(c for c in state.cars if c.driver_number == event.driver_number)
                if self.release_lines.get(car.driver_number) == event.checkpoint:
                    self.release_lines.pop(car.driver_number)
                if car.status is CarStatus.FINISHED:
                    positions = self._positions(state, clock)
                    positions.pop(car.driver_number, None)
                    self._drop_car(car.driver_number)
                    self.release_lines.pop(car.driver_number, None)
                    self._rebuild(state, scheduler, positions)
            else:
                super().on_event(event, state, scheduler)
        else:
            super().on_event(event, state, scheduler)
        if (self.release and not self.release_lines and not self.release_queued
                and state.phase is RacePhase.RUNNING and (self.end_us is None or clock < self.end_us)):
            self.release_queued = True
            scheduler.schedule(_ControlTick(clock+1, EventKind.RACE_CONTROL, source="S5_RELEASE", attempt_id="@release"))
        gaps = self._snapshot(state, event.kind.name)
        if (self.phase is P.SC_COLLECTING_FIELD and self.pacer is not None and gaps
                and self.progress[-1]["virtual_sc_m"] - self.progress[-1]["positions_m"][self.pacer]
                <= self.profile.train_gap_m + 1e-6
                and all(gap <= self.profile.train_gap_m for d, gap in gaps.items() if d != self.pacer)):
            self.phase = P.SC_TRAIN_FORMED
            self._snapshot(state, "TRAIN_FORMED")
        return event
