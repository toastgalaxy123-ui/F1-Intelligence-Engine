"""Resolved and coarse S2 pit execution on the shared checkpoint scheduler."""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from heapq import heappop, heappush

from f1ri.features.stint_examples import DRY_COMPOUNDS
from f1ri.simulator.engine import RaceRun, checkpoint_offsets_us, run_pit_window
from f1ri.simulator.events import Event, EventKind
from f1ri.simulator.state import CarState, RacePhase, RaceState, TrackState, TyreState, elapsed_us


class RejoinStatus(StrEnum):
    RESOLVED = "RESOLVED"
    APPROXIMATE = "APPROXIMATE"
    UNRESOLVED = "UNRESOLVED"


class PitExecutionMode(StrEnum):
    RESOLVED_ROUTE = "RESOLVED_ROUTE"
    COARSE_NET = "COARSE_NET"


@dataclass(frozen=True, slots=True)
class ReplacementTyres:
    compound: str
    initial_age_laps: int = 0
    compound_absolute: str | None = None
    set_id: str | None = None

    def __post_init__(self) -> None:
        if self.compound not in DRY_COMPOUNDS or self.initial_age_laps < 0:
            raise ValueError("A routine stop requires a dry compound and nonnegative tire age")
        if self.set_id is not None and not self.set_id.strip():
            raise ValueError("A supplied tire-set identity cannot be empty")

    def fit(self, stint: int, out_lap: int) -> TyreState:
        return TyreState(self.compound, self.initial_age_laps, stint, self.compound_absolute,
                         self.set_id, self.initial_age_laps, out_lap)


@dataclass(frozen=True, slots=True)
class PitStopRequest:
    session_key: int
    driver_number: int
    as_of_utc: datetime
    pit_in_lap: int
    replacement: ReplacementTyres
    scenario_id: str
    track_state: TrackState = TrackState.GREEN

    def __post_init__(self) -> None:
        if (min(self.session_key, self.driver_number, self.pit_in_lap) <= 0
                or self.as_of_utc.utcoffset() is None or not self.scenario_id.strip()):
            raise ValueError("A pit request requires race, driver, cutoff, lap and scenario identity")
        if self.track_state not in (TrackState.GREEN, TrackState.VSC, TrackState.SC):
            raise ValueError("S2 requires a declared green, VSC or SC timing scenario")


@dataclass(frozen=True, slots=True)
class ResolvedPitRoute:
    """Seconds after the in-lap start on the declared sector-2/sector-1 layout."""

    route_entry_s: float
    lane_entry_s: float
    box_arrival_s: float
    service_s: float
    timing_line_s: float
    lane_exit_s: float
    merge_s: float
    out_sector_2_s: float
    out_line_s: float
    box_id: str
    timing_uncertainty_s: float = 0.0

    def ticks(self) -> tuple[int, ...]:
        return tuple(elapsed_us(value) for value in (
            self.route_entry_s, self.lane_entry_s, self.box_arrival_s, self.service_s,
            self.timing_line_s, self.lane_exit_s, self.merge_s, self.out_sector_2_s,
            self.out_line_s,
        ))

    def __post_init__(self) -> None:
        entry, lane, box, service, line, exit_, merge, sector, end = self.ticks()
        if not (0 < entry < lane < box < box + service < exit_ < merge < sector < end
                and lane < line < exit_ and not box < line < box + service):
            raise ValueError("Pit-route offsets must be chronological with a moving timing-line crossing")
        if not self.box_id.strip():
            raise ValueError("Resolved pit timing requires a box identity")
        elapsed_us(self.timing_uncertainty_s)


@dataclass(frozen=True, slots=True)
class PitTransitionPlan:
    request: PitStopRequest
    route: ResolvedPitRoute | None = None
    net_loss_s: float | None = None
    cost_source: str = "SYNTHETIC"
    support_status: str = "SUPPORTED"
    model_version: str | None = None
    random_stream_id: str | None = None
    new_pace_trajectory_id: str | None = None

    def __post_init__(self) -> None:
        if (self.route is None) == (self.net_loss_s is None):
            raise ValueError("Supply either physical route timing or one net loss")
        if self.net_loss_s is not None and not math.isfinite(self.net_loss_s):
            raise ValueError("Net loss must be finite")
        if self.support_status == "UNSUPPORTED":
            raise ValueError("Unsupported pit costs cannot be executed")
        if self.request.track_state is not TrackState.GREEN and self.cost_source != "SYNTHETIC":
            raise ValueError("Neutralized pit execution requires explicitly supplied synthetic timing")

    @property
    def mode(self) -> PitExecutionMode:
        return PitExecutionMode.RESOLVED_ROUTE if self.route is not None else PitExecutionMode.COARSE_NET


@dataclass(frozen=True, slots=True)
class PitCarPlan:
    driver_number: int
    last_line_us: int
    normal_lap_times_s: tuple[float, ...]
    stops: tuple[PitTransitionPlan, ...] = ()
    available_sets: tuple[ReplacementTyres, ...] | None = None


@dataclass(frozen=True, slots=True)
class MergePassage:
    driver_number: int
    completed_laps: int
    delta_us: int
    uncertainty_us: int


@dataclass(frozen=True, slots=True)
class PitTransitionResult:
    pit_event_id: str
    plan: PitTransitionPlan
    execution_mode: PitExecutionMode
    old_stint_id: str
    new_stint_id: str | None
    normal_two_lap_us: int
    elapsed_two_lap_us: int | None
    net_loss_us: int | None
    queue_wait_us: int | None
    route_entry_us: int | None
    lane_entry_us: int | None
    service_start_us: int | None
    service_end_us: int | None
    lane_exit_us: int | None
    merge_us: int | None
    rejoin_status: RejoinStatus
    passages: tuple[MergePassage, ...]
    timing_quality: str
    position_change_cause: str | None


@dataclass(frozen=True, slots=True)
class PitScenarioRun:
    run: RaceRun
    stops: tuple[PitTransitionResult, ...]


def _crossing(at: int, driver: int, checkpoint: int, source: str = "NORMAL_PACE") -> Event:
    kind = EventKind.CHECKPOINT_CROSSING if checkpoint % 3 else EventKind.LAP_COMPLETED
    return Event(at, kind, driver, checkpoint, source=source)


@dataclass(slots=True)
class _Itinerary:
    car: CarState
    plan: PitCarPlan
    lap: int
    line: int
    offsets: dict[int, tuple[int, int, int]]
    stops: dict[int, PitTransitionPlan]
    events: list[Event] = field(default_factory=list)
    visits: list[_Visit] = field(default_factory=list)

    def normal_lap(self) -> None:
        base = (self.lap - 1) * 3
        self.events.extend(
            _crossing(self.line + offset, self.car.driver_number, base + sector)
            for sector, offset in enumerate(self.offsets[self.lap], 1)
            if base + sector > self.car.completed_checkpoints
        )
        self.line += self.offsets[self.lap][-1]
        self.lap += 1


@dataclass(frozen=True, slots=True)
class _Visit:
    plan: PitTransitionPlan
    start_us: int
    normal_us: int
    old_stint: int
    wait_us: int | None
    fit_event: Event
    phases: tuple[Event, ...]


def _advance(itinerary: _Itinerary, wait: int = 0) -> None:
    """Compile one stop and both of its timing-line crossings."""
    lap, start = itinerary.lap, itinerary.line
    plan = itinerary.stops[lap]
    number = itinerary.car.driver_number
    old_stint = itinerary.car.tyres.stint_number + len(itinerary.visits)
    tyres = plan.request.replacement.fit(old_stint + 1, lap + 1)
    normal = itinerary.offsets[lap][-1] + itinerary.offsets[lap + 1][-1]
    if plan.route is None:
        itinerary.normal_lap()
        itinerary.events[-1] = replace(itinerary.events[-1], source="COARSE_PIT_REFERENCE_LINE")
        fit = Event(itinerary.line, EventKind.COARSE_PIT_TRANSITION, number, tyres=tyres,
                    source=plan.cost_source)
        end = start + normal + round(plan.net_loss_s * 1_000_000)
        if end <= itinerary.line:
            raise ValueError("Coarse net adjustment leaves a nonpositive out-lap")
        itinerary.events.extend((fit, _crossing(end, number, (lap + 1) * 3, "COARSE_PIT_ACCOUNTING")))
        phases = ()
        queue = None
    else:
        entry, lane, box, service, line, exit_, merge, sector, end_offset = plan.route.ticks()
        first, second, clean_end = itinerary.offsets[lap]
        if not second <= entry < clean_end:
            raise ValueError("This pit route diverts between in-lap sector 2 and the normal line")
        base = (lap - 1) * 3
        itinerary.events.extend((_crossing(start + first, number, base + 1),
                                 _crossing(start + second, number, base + 2)))
        fit = Event(start + box + wait + service, EventKind.PIT_SERVICE_END, number, tyres=tyres,
                    source=plan.cost_source)
        phases = (
            Event(start + entry, EventKind.PIT_ROUTE_ENTRY, number, source=plan.cost_source),
            Event(start + lane, EventKind.PIT_LANE_ENTRY, number, source=plan.cost_source),
            Event(start + box + wait, EventKind.PIT_SERVICE_START, number, source=plan.cost_source,
                  pit_box=plan.route.box_id),
            fit,
            Event(start + exit_ + wait, EventKind.PIT_LANE_EXIT, number, source=plan.cost_source),
            Event(start + merge + wait, EventKind.TRACK_REJOIN, number, source=plan.cost_source),
        )
        end = start + end_offset + wait
        itinerary.events.extend((*phases,
            _crossing(start + line + (wait if line > box else 0), number, lap * 3, "PIT_ROUTE_LINE"),
            _crossing(start + merge + wait, number, lap * 3 + 1, "COMMON_MERGE"),
            _crossing(start + sector + wait, number, lap * 3 + 2),
            _crossing(end, number, (lap + 1) * 3),
        ))
        queue = wait
    itinerary.visits.append(_Visit(plan, start, normal, old_stint, queue, fit, phases))
    itinerary.lap, itinerary.line = lap + 2, end


def _next_box(itinerary: _Itinerary) -> int | None:
    while itinerary.lap in itinerary.offsets:
        stop = itinerary.stops.get(itinerary.lap)
        if stop is None:
            itinerary.normal_lap()
        elif stop.route is None:
            _advance(itinerary)
        else:
            return itinerary.line + stop.route.ticks()[2]
    return None


def _compile(
    state: RaceState, car: CarState, plan: PitCarPlan, fractions: tuple[float, float, float],
) -> _Itinerary:
    first = car.completed_laps + 1
    if plan.last_line_us < 0 or plan.last_line_us > state.race_clock_us:
        raise ValueError("Last timing line must be at or before the decision clock")
    if not plan.normal_lap_times_s or first + len(plan.normal_lap_times_s) - 1 > state.ruleset.scheduled_laps:
        raise ValueError("Normal pace plan must fit inside the scheduled race distance")
    if car.sector == 0 and car.last_crossing_us not in (None, plan.last_line_us):
        raise ValueError("Timing-line origin differs from the current car state")
    offsets = {lap: checkpoint_offsets_us(duration, fractions)
               for lap, duration in enumerate(plan.normal_lap_times_s, first)}
    first_future = offsets[first][car.sector] + plan.last_line_us
    if first_future <= state.race_clock_us:
        raise ValueError("First pending checkpoint must follow the cutoff")
    cutoff = state.race_start_utc + timedelta(microseconds=state.race_clock_us)
    stops = {stop.request.pit_in_lap: stop for stop in plan.stops}
    if len(stops) != len(plan.stops):
        raise ValueError("A car cannot pit twice on the same lap")
    known = None if plan.available_sets is None else set(plan.available_sets)
    used = {car.tyres.set_id} if car.tyres.set_id is not None else set()
    previous = first - 2
    for lap, stop in sorted(stops.items()):
        request = stop.request
        if ((request.session_key, request.driver_number, request.as_of_utc)
                != (state.session_key, car.driver_number, cutoff)):
            raise ValueError("Pit request must match the field, driver and decision cutoff")
        if request.track_state is not state.track_state:
            raise ValueError("Pit timing must use the field's declared track state")
        if lap not in offsets or lap + 1 not in offsets or lap <= previous + 1:
            raise ValueError("Pit accounting windows must be inside the plan and disjoint")
        if lap == first and plan.last_line_us < state.race_clock_us:
            raise ValueError("A partial current lap cannot be the S2 pit-in lap")
        tyres = request.replacement
        if known is not None and (tyres.set_id is None or tyres not in known):
            raise ValueError("Replacement set is absent from the declared inventory")
        if tyres.set_id is not None:
            if tyres.set_id in used:
                raise ValueError("Tire set is already fitted or committed")
            used.add(tyres.set_id)
        previous = lap
    return _Itinerary(car, plan, first, plan.last_line_us, offsets, stops)


def _passages(
    merge: Event, crossings: list[Event], merge_uncertainty: dict[tuple[int, int], int],
    window_us: int,
) -> tuple[MergePassage, ...]:
    nearest: dict[int, Event] = {}
    for event in crossings:
        if event.driver_number == merge.driver_number or abs(event.at_us - merge.at_us) > window_us:
            continue
        previous = nearest.get(event.driver_number)
        if previous is None or abs(event.at_us - merge.at_us) < abs(previous.at_us - merge.at_us):
            nearest[event.driver_number] = event
    return tuple(MergePassage(
        event.driver_number, event.checkpoint // 3, event.at_us - merge.at_us,
        merge_uncertainty.get((event.driver_number, event.at_us), 0),
    ) for event in sorted(nearest.values(), key=lambda event: (event.at_us, event.driver_number)))


def simulate_pit_scenario(
    initial: RaceState, plans: tuple[PitCarPlan, ...], *,
    sector_fractions: tuple[float, float, float] = (1 / 3, 1 / 3, 1 / 3),
    merge_window_s: float = 10.0,
) -> PitScenarioRun:
    """Execute explicit normal pace and pit inputs without access to historical outcomes."""
    if (len(sector_fractions) != 3 or not all(0 < fraction < 1 for fraction in sector_fractions)
            or not math.isclose(sum(sector_fractions), 1, rel_tol=0, abs_tol=1e-12)):
        raise ValueError("Three positive checkpoint fractions must sum to one")
    merge_window = elapsed_us(merge_window_s)
    by_driver = {plan.driver_number: plan for plan in plans}
    if len(by_driver) != len(plans) or set(by_driver) != {car.driver_number for car in initial.cars}:
        raise ValueError("Every car requires exactly one pit scenario itinerary")
    cars = sorted(initial.cars, key=lambda car: car.grid_key)
    itineraries = [_compile(initial, car, by_driver[car.driver_number], sector_fractions)
                   for car in cars]
    team_boxes, box_teams = {}, {}
    for itinerary in itineraries:
        team = itinerary.car.team
        if team in (None, "UNKNOWN_TEAM"):
            continue
        for stop in itinerary.stops.values():
            if stop.route is None:
                continue
            box = stop.route.box_id
            if team_boxes.setdefault(team, box) != box or box_teams.setdefault(box, team) != team:
                raise ValueError("Resolved scenarios require one declared pit box per known team")
    queue = []
    for index, itinerary in enumerate(itineraries):
        arrival = _next_box(itinerary)
        if arrival is not None:
            heappush(queue, (arrival, index))
    available: dict[str, int] = {}
    while queue:
        arrival, index = heappop(queue)
        itinerary = itineraries[index]
        route = itinerary.stops[itinerary.lap].route
        service_start = max(arrival, available.get(route.box_id, 0))
        available[route.box_id] = service_start + route.ticks()[3]
        _advance(itinerary, service_start - arrival)
        next_arrival = _next_box(itinerary)
        if next_arrival is not None:
            heappush(queue, (next_arrival, index))

    events = [Event(0, EventKind.RACE_START)] if initial.phase is RacePhase.PRESTART else []
    diversions = {}
    for itinerary in itineraries:
        if not itinerary.visits:
            events.extend(itinerary.events)
            continue
        first = itinerary.visits[0]
        diversion = first.phases[0] if first.phases else first.fit_event
        car, plan = itinerary.car, itinerary.plan
        normal = _compile(initial, car, replace(plan, stops=()), sector_fractions)
        _next_box(normal)
        events.extend(replace(
            event, generation=0, source=(
                "COARSE_PIT_REFERENCE_LINE"
                if first.plan.route is None and event.at_us == diversion.at_us
                and event.kind is EventKind.LAP_COMPLETED else event.source
            ),
        ) for event in normal.events)
        events.append(diversion)
        diversions[diversion] = tuple(event for event in itinerary.events if event.at_us > diversion.at_us)
    run = run_pit_window(initial, events, diversions)
    return PitScenarioRun(run, _results(initial, itineraries, run, merge_window))


def _results(state: RaceState, itineraries: list[_Itinerary], run: RaceRun,
             merge_window_us: int) -> tuple[PitTransitionResult, ...]:
    committed = {(frame.event.kind, frame.event.driver_number, frame.event.at_us)
                 for frame in run.trace}
    lines = {(frame.event.driver_number, frame.event.checkpoint): frame.event.at_us
             for frame in run.trace if frame.event.kind is EventKind.LAP_COMPLETED}
    crossing = [frame.event for frame in run.trace if frame.event.kind is EventKind.CHECKPOINT_CROSSING
                and frame.event.checkpoint % 3 == 1]
    prior_sector = {(frame.event.driver_number, frame.event.checkpoint): frame.event.at_us
                    for frame in run.trace if frame.event.kind is EventKind.CHECKPOINT_CROSSING
                    and frame.event.checkpoint % 3 == 2}
    visits = [visit for itinerary in itineraries for visit in itinerary.visits]
    coarse_windows = [
        (visit.start_us, lines.get((visit.plan.request.driver_number,
                                    (visit.plan.request.pit_in_lap + 1) * 3)))
        for visit in visits if not visit.phases
        and (visit.fit_event.kind, visit.plan.request.driver_number,
             visit.fit_event.at_us) in committed
    ]
    uncertainty = {(visit.plan.request.driver_number, visit.phases[-1].at_us):
                   elapsed_us(visit.plan.route.timing_uncertainty_s) for visit in visits if visit.phases}
    results = []
    for visit in visits:
        request = visit.plan.request
        driver, lap = request.driver_number, request.pit_in_lap
        fit = visit.fit_event
        fitted = (fit.kind, driver, fit.at_us) in committed
        end = lines.get((driver, (lap + 1) * 3))
        elapsed = end - visit.start_us if end is not None else None
        phases = tuple(event.at_us if (event.kind, driver, event.at_us) in committed else None
                       for event in visit.phases) if visit.phases else (None,) * 6
        merge = visit.phases[-1] if phases[-1] is not None else None
        passages = _passages(merge, crossing, uncertainty, merge_window_us) if merge else ()
        bound = uncertainty.get((driver, merge.at_us), 0) if merge else 0
        status = RejoinStatus.UNRESOLVED
        coarse_overlap = merge is not None and any(
            start <= merge.at_us + merge_window_us
            and (end is None or end >= merge.at_us - merge_window_us)
            for start, end in coarse_windows
        )
        if (merge and (passages or len(state.cars) == 1) and not coarse_overlap
                and all(abs(p.delta_us) > bound + p.uncertainty_us for p in passages)):
            status = (RejoinStatus.APPROXIMATE if bound or any(p.uncertainty_us for p in passages)
                      else RejoinStatus.RESOLVED)
        change_cause = None
        if status is RejoinStatus.RESOLVED:
            checkpoint = (lap - 1) * 3 + 2
            before = prior_sector.get((driver, checkpoint))
            if before is not None and any(
                passage.completed_laps == lap
                and (opponent := prior_sector.get((passage.driver_number, checkpoint))) is not None
                and (opponent - before) * passage.delta_us < 0
                for passage in passages
            ):
                change_cause = "PIT_CYCLE_POSITION_CHANGE"
        prefix = f"{state.session_key}:{state.simulation_id}:{driver}"
        results.append(PitTransitionResult(
            f"{prefix}:{request.scenario_id}:{lap}", visit.plan, visit.plan.mode,
            f"{prefix}:{visit.old_stint}",
            f"{prefix}:{visit.old_stint + 1}" if fitted else None,
            visit.normal_us, elapsed, elapsed - visit.normal_us if elapsed is not None else None,
            visit.wait_us if phases[2] is not None else None, *phases, status, passages,
            "INCOMPLETE" if elapsed is None else "DECLARED_ROUTE" if visit.phases else "COARSE_NET_ONLY",
            change_cause,
        ))
    return tuple(results)
