"""Progress-triggered S2 diversions and driver-specific Safety Car release."""
from dataclasses import dataclass
from math import isfinite

from f1ri.simulator.events import Event
from f1ri.simulator.events import EventKind as K
from f1ri.simulator.neutralization import _NeutralizedField
from f1ri.simulator.overtaking import OvertakeStatus
from f1ri.simulator.pit_execution import PitTransitionPlan
from f1ri.simulator.race_control import RED_PHASES
from f1ri.simulator.race_control import ControlCommand as C
from f1ri.simulator.state import CarStatus, Route, TrackState


@dataclass(frozen=True, slots=True)
class PitDecision:
    decision_id: str
    simulation_id: str
    at_us: int
    valid_until_us: int
    plan: PitTransitionPlan
    layout_version: str
    entry_progress_m: float | None
    provenance: str
    cancel_us: int | None = None
    reference_entry_us: int | None = None

    def __post_init__(self):
        if (not all((self.decision_id, self.simulation_id, self.layout_version, self.provenance))
                or not 0 <= self.at_us <= self.valid_until_us
                or self.cancel_us is not None and self.cancel_us <= self.at_us):
            raise ValueError("Pit decisions require identity, provenance and a chronological window")

    @property
    def driver(self):
        return self.plan.request.driver_number

    @property
    def line(self):
        return self.plan.request.pit_in_lap * 3


@dataclass(frozen=True, slots=True)
class _PitTick(Event):
    attempt_id: str = ""


class StabilizedField(_NeutralizedField):
    def __init__(self, *args, pit_decisions, individual_restart, strategy_runtime=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.individual_restart = individual_restart
        self.strategy_runtime = strategy_runtime
        self.decisions = {row.decision_id: row for row in pit_decisions}
        if (len(self.decisions) != len(pit_decisions)
                or any(r.at_us < self.initial.race_clock_us for r in pit_decisions)
                or self.environment and any(e.kind is not K.RETIREMENT for e in self.environment)
                and pit_decisions):
            raise ValueError("Dynamic decisions require unique future IDs and exclusively dynamic pit timing")
        self.pending_pits = {}
        self.active_pits = {}
        self.pit_candidates = {}
        self.pit_ledger = []
        self.restart_permissions = []
        self.box_free = {}
        self.entry_clock = {}
        self.unresolved_merges = set()

    def events(self):
        items = super().events()
        if self.strategy_runtime:
            items.extend(Event(at, K.STRATEGY_REVIEW, source="S5_C_POLICY")
                         for at in self.strategy_runtime.review_clocks())
        for row in self.decisions.values():
            driver = row.driver if row.driver in self.ahead else None
            items.append(_PitTick(row.at_us, K.PIT_DECISION, driver, attempt_id=row.decision_id))
            if row.cancel_us is not None:
                items.append(_PitTick(row.cancel_us, K.PIT_DECISION, driver,
                                      source="CANCEL", attempt_id=row.decision_id))
            items.append(_PitTick(row.valid_until_us+1, K.PIT_DECISION, driver,
                                  source="EXPIRE", attempt_id=row.decision_id))
        return [e for e in items if self.end_us is None or e.at_us <= self.end_us]

    def _pace_at(self, driver, checkpoint):
        if self.strategy_runtime:
            return self.strategy_runtime.pace_at(driver, checkpoint)
        return super()._pace_at(driver, checkpoint)

    def _traversal_clean_us(self, driver, checkpoint, previous):
        if self.strategy_runtime:
            pace = self._pace_at(driver, checkpoint)
            return max(1, round(checkpoint * pace / 3) - round(previous * pace / 3))
        return super()._traversal_clean_us(driver, checkpoint, previous)

    def _defer_team_release(self):
        return False if self.individual_restart else super()._defer_team_release()

    def _control(self, tick, state):
        result = super()._control(tick, state)
        if result is None or not self.individual_restart:
            return result
        cmd = self.commands[tick.attempt_id].command
        if cmd in (C.SC, C.RED, C.ROLLING, C.SC_WITHDRAWN):
            if cmd is not C.SC_WITHDRAWN:
                for car in state.cars:
                    if car.driver_number in self.release_lines:
                        self._permission(car, tick.at_us, "SUPERSEDED_BY_RACE_CONTROL",
                                         self.release_lines[car.driver_number])
            self.release_lines.clear()
        if cmd is C.SC_WITHDRAWN:
            for car in state.cars:
                if car.status is CarStatus.ACTIVE:
                    line = (car.completed_laps+1)*3 if car.route is Route.ON_TRACK else None
                    self.release_lines[car.driver_number] = line
                    self._permission(car, tick.at_us, "WITHDRAWAL", line)
        return result

    def _permission(self, car, at, status, line):
        self.restart_permissions.append({
            "driver": car.driver_number, "at_us": at, "status": status,
            "required_checkpoint": line, "last_committed_crossing_us": car.last_crossing_us,
            "route": car.route, "profile_id": self.profile.identifier,
            "eligible_after_global_green": status == "CROSSED_RESTART_LINE",
            "rule_exception": "NONE_APPLIED",
        })

    def _release_crossing(self, event, state):
        d = event.driver_number
        if event.kind is K.LAP_COMPLETED and self.release_lines.get(d) == event.checkpoint:
            car = next(c for c in state.cars if c.driver_number == d)
            if d not in self.ahead or event.source == "PIT_ROUTE_LINE":
                return
            self.release_lines.pop(d)
            self._permission(car, event.at_us, "CROSSED_RESTART_LINE", event.checkpoint)

    def _propose(self, event, state, scheduler):
        a = self.attempts[event.attempt_id]
        if self.individual_restart and state.track_state is TrackState.GREEN and a.driver_number in self.release_lines:
            self._finish(a, state, OvertakeStatus.REJECTED, "DRIVER_RESTART_LINE_PENDING")
            return
        super()._propose(event, state, scheduler)

    def _pit_row(self, decision, at, status, **details):
        self.pit_ledger.append({
            "decision_id": decision.decision_id, "driver": decision.driver, "at_us": at, "status": status,
            "entry_progress_m": decision.entry_progress_m, "target_lap": decision.plan.request.pit_in_lap,
            "reference_entry_us": decision.reference_entry_us, "layout": decision.layout_version,
            "provenance": decision.provenance, **details,
        })

    def _boundary(self, row):
        return row.line-3 + row.entry_progress_m / self.step

    def _decision(self, event, state):
        row = self.decisions[event.attempt_id]
        d = row.driver
        if event.source in ("CANCEL", "EXPIRE"):
            if self.pending_pits.get(d) is row:
                self.pending_pits.pop(d)
                self._pit_row(row, event.at_us, "CANCELLED" if event.source == "CANCEL" else "MISSED_ENTRY")
            return
        if (row.plan.route is None or row.entry_progress_m is None or not isfinite(row.entry_progress_m)
                or not 2*self.step <= row.entry_progress_m < 3*self.step
                or row.layout_version != self.physical.layout_version):
            self._pit_row(row, event.at_us, "UNSUPPORTED", reason="PIT_ENTRY_GEOMETRY_UNSUPPORTED")
        elif (row.simulation_id != state.simulation_id or row.plan.request.session_key != state.session_key
              or d not in self.ahead or row.line >= state.ruleset.scheduled_laps * 3):
            self._pit_row(row, event.at_us, "UNSUPPORTED", reason="PIT_DECISION_CONTEXT_UNSUPPORTED")
        elif d in self.pending_pits or d in self.active_pits:
            self._pit_row(row, event.at_us, "UNSUPPORTED", reason="COMPETING_PIT_DECISION")
        elif (next(c.completed_checkpoints for c in state.cars if c.driver_number == d) >= row.line
              or self._positions(state, event.at_us, d)[d] > self._boundary(row) + 1e-8):
            self._pit_row(row, event.at_us, "MISSED_ENTRY", reason="BOUNDARY_ALREADY_PASSED")
        else:
            self.pending_pits[d] = row
            self._pit_row(row, event.at_us, "PIT_PLANNED")

    def _schedule_entries(self, state, scheduler):
        if self.phase in RED_PHASES:
            return
        for car in state.cars:
            d = car.driver_number
            row = self.pending_pits.get(d)
            if row is None or d not in self.ahead:
                continue
            boundary = self._boundary(row)
            if car.completed_checkpoints != row.line-1:
                continue
            crossing = self.plan.get((d, row.line))
            if crossing is None:
                continue
            clock = state.race_clock_us
            lo, hi = clock, crossing.crossing_us
            if hi < lo or self._positions(state, hi, d)[d] < boundary - 1e-8:
                continue
            while lo < hi:
                mid = (lo + hi)//2
                if self._positions(state, mid, d)[d] >= boundary - 1e-8:
                    hi = mid
                else:
                    lo = mid + 1
            # Decision processing precedes dynamic entry at equal clocks.
            if lo > row.valid_until_us or self.end_us is not None and lo > self.end_us:
                continue
            generation = scheduler.generation(d)
            candidate = row.decision_id, lo, generation
            if self.pit_candidates.get(d) == candidate:
                continue
            previous = self.pit_candidates.get(d)
            self.pit_candidates[d] = candidate
            scheduler.schedule(_PitTick(lo, K.DYNAMIC_PIT_ENTRY, d, generation=generation,
                                        source="DYNAMIC_PIT_ENTRY", attempt_id=row.decision_id))
            self._pit_row(row, clock, "APPROACHING_PIT_ENTRY", predicted_us=lo, generation=generation,
                          superseded_prediction_us=previous[1] if previous else None)

    def _enter(self, event, state):
        row = self.pending_pits.get(event.driver_number)
        if row is None or row.decision_id != event.attempt_id or self.phase in RED_PHASES:
            return None
        if row.cancel_us is not None and row.cancel_us <= event.at_us:
            return None
        cp = next(c.completed_checkpoints for c in state.cars if c.driver_number == row.driver)
        p = self._positions(state, event.at_us, row.driver)[row.driver]
        if cp != row.line-1 or p < self._boundary(row) - 1e-8:
            raise ValueError("Dynamic pit entry escaped its physical checkpoint bracket")
        self._pit_row(row, event.at_us, "PIT_ENTRY_COMMITTED", unwrapped_progress_m=p*self.step,
                      track_state=state.track_state, generation=event.generation,
                      route_before=Route.ON_TRACK, route_after=Route.PIT_ENTRY)
        self.pending_pits.pop(row.driver)
        self.pit_candidates.pop(row.driver, None)
        self.active_pits[row.driver] = row
        self.entry_clock[row.driver] = event.at_us
        return Event(event.at_us, K.PIT_ROUTE_ENTRY, event.driver_number, source=event.source)

    def _in_window(self, event, scheduler):
        if self.end_us is None or event.at_us <= self.end_us:
            scheduler.schedule(event)

    def _schedule_pit_arrival(self, event, scheduler):
        d = event.driver_number
        row = self.active_pits[d]
        entry, lane, box, _, line, *_ = row.plan.route.ticks()
        for clock, kind, source in ((lane, K.PIT_LANE_ENTRY, "S2_DYNAMIC"),
                                    (box, K.PIT_BOX_ARRIVAL, "S2_DYNAMIC")):
            self._in_window(Event(event.at_us+clock-entry, kind, d, source=source), scheduler)
        if line <= box:
            self._in_window(Event(event.at_us+line-entry, K.LAP_COMPLETED, d, row.line,
                                  source="PIT_ROUTE_LINE"), scheduler)

    def _reserve_box(self, event, state, scheduler):
        d = event.driver_number
        row = self.active_pits[d]
        route = row.plan.route
        _, _, box, service, line, exit_, merge, *_ = route.ticks()
        arrival = event.at_us
        start = max(arrival, self.box_free.get(route.box_id, 0))
        end = start + service
        self.box_free[route.box_id] = end
        wait = start-arrival
        current_tyres = next(c.tyres for c in state.cars if c.driver_number == d)
        tyres = row.plan.request.replacement.fit(current_tyres.stint_number+1,
                                                  row.plan.request.pit_in_lap+1)
        line_event = Event(arrival+line-box+wait, K.LAP_COMPLETED, d, row.line, source="PIT_ROUTE_LINE")
        exit_event = Event(arrival+exit_-box+wait, K.PIT_LANE_EXIT, d, source="S2_DYNAMIC")
        rejoin_event = Event(arrival+merge-box+wait, K.TRACK_REJOIN, d, source="S2_DYNAMIC")
        self.rejoin[rejoin_event] = row.line+1
        for e in (
            Event(start, K.PIT_SERVICE_START, d, source="S2_DYNAMIC", pit_box=route.box_id),
            Event(end, K.PIT_SERVICE_END, d, tyres=tyres, source="S2_DYNAMIC"),
            exit_event, rejoin_event,
            Event(rejoin_event.at_us, K.CHECKPOINT_CROSSING, d, row.line+1, source="COMMON_MERGE"),
        ):
            self._in_window(e, scheduler)
        if line > box:
            self._in_window(line_event, scheduler)
        self._pit_row(row, arrival, "SERVICE_QUEUE", box=route.box_id, service_start_us=start, service_end_us=end,
                      wait_us=wait, service_us=service, lane_exit_us=exit_event.at_us, merge_us=rejoin_event.at_us)

    def before_event(self, event, state):
        if event.kind is K.DYNAMIC_PIT_ENTRY:
            return self._enter(event, state)
        if event.source == "COMMON_MERGE" and (event.driver_number, event.at_us) in self.unresolved_merges:
            return None
        if event.kind is K.TRACK_REJOIN and event.source == "S2_DYNAMIC":
            cp = self.rejoin[event]
            if any(abs((p-cp+1.5) % 3 - 1.5) <= 1e-9
                   for p in self._positions(state, event.at_us).values()):
                self.unresolved_merges.add((event.driver_number, event.at_us))
                row = self.active_pits.pop(event.driver_number)
                self._pit_row(row, event.at_us, "UNSUPPORTED", reason="SIMULTANEOUS_MERGE_UNRESOLVED")
                return None
        if (event.source in ("S2_DYNAMIC", "PIT_ROUTE_LINE", "COMMON_MERGE")
                and any(c.driver_number == event.driver_number and c.status is not CarStatus.ACTIVE
                        for c in state.cars)):
            return None
        if event.kind is K.ON_TRACK_PASS and self.individual_restart and event.driver_number in self.release_lines:
            pending = self.active.get(event.attempt_id)
            if pending:
                self._finish(pending.attempt, state, OvertakeStatus.CANCELLED, "DRIVER_RESTART_LINE_PENDING", pending)
            return None
        return super().before_event(event, state)

    def on_event(self, event, state, scheduler):
        if event.kind is K.DYNAMIC_PIT_ENTRY:
            event = Event(event.at_us, K.PIT_ROUTE_ENTRY, event.driver_number, source=event.source)
        d = event.driver_number
        retained = self.release_lines.get(d)
        restricted = d in self.release_lines
        if self.individual_restart:
            self._release_crossing(event, state)
        if self.strategy_runtime and event.kind is K.PIT_SERVICE_END:
            self.strategy_runtime.fit(state, d)
        result = super().on_event(event, state, scheduler)
        if self.individual_restart and event.kind in (K.PIT_ROUTE_ENTRY, K.TRACK_REJOIN, K.RETIREMENT):
            car = next(c for c in state.cars if c.driver_number == d)
            if event.kind is K.PIT_ROUTE_ENTRY and restricted:
                self.release_lines[d] = None
                self._permission(car, event.at_us, "PIT_ROUTE_RESTRICTED", None)
            elif event.kind is K.TRACK_REJOIN and restricted:
                cp = (self.rejoin[event]//3+1)*3
                self.release_lines[d] = cp
                self._permission(car, event.at_us, "REJOIN_RESTRICTED", cp)
            elif event.kind is K.TRACK_REJOIN and not restricted:
                self.release_lines.pop(d, None)
            elif event.kind is K.RETIREMENT and restricted:
                self._permission(car, event.at_us, "RETIRED_WITHOUT_RELEASE", retained)
        if event.kind is K.PIT_DECISION:
            self._decision(event, state)
        if event.kind is K.SCENARIO_WINDOW_END:
            for row in self.pending_pits.values():
                self._pit_row(row, event.at_us, "CANCELLED", reason="SCENARIO_WINDOW_ENDED")
            self.pending_pits.clear()
        if event.kind is K.PIT_ROUTE_ENTRY and event.source == "DYNAMIC_PIT_ENTRY":
            self._schedule_pit_arrival(event, scheduler)
        if event.kind is K.PIT_BOX_ARRIVAL and d in self.active_pits:
            self._reserve_box(event, state, scheduler)
        if event.kind is K.RETIREMENT:
            stopped = self.pending_pits.pop(d, None) or self.active_pits.pop(d, None)
            if stopped:
                self._pit_row(stopped, event.at_us, "CANCELLED", reason="DRIVER_RETIRED")
        if d in self.active_pits and event.kind in (K.PIT_LANE_ENTRY, K.PIT_SERVICE_START,
                                                     K.PIT_SERVICE_END, K.PIT_LANE_EXIT, K.TRACK_REJOIN):
            row = self.active_pits[d]
            status = {K.PIT_LANE_ENTRY: "PIT_LANE_TRAVERSAL", K.PIT_SERVICE_START: "SERVICE_ACTIVE",
                      K.PIT_SERVICE_END: "SERVICE_COMPLETE", K.PIT_LANE_EXIT: "REJOIN_PENDING",
                      K.TRACK_REJOIN: "REJOINED"}[event.kind]
            self._pit_row(row, event.at_us, status, classification=state.timing_order,
                          neighbors=dict(self.ahead), track_state=state.track_state)
            if event.kind is K.TRACK_REJOIN:
                self.active_pits.pop(d)
        if self.strategy_runtime:
            self.strategy_runtime.observe(event, state, self)
        self._schedule_entries(state, scheduler)
        return result
