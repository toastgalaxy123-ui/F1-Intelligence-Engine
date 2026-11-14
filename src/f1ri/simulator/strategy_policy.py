"""Immutable decision inputs for the first deterministic strategy policies."""
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from f1ri.simulator.pit_execution import ReplacementTyres, ResolvedPitRoute
from f1ri.simulator.state import CarState, Route, TrackState


class PolicyKind(StrEnum):
    CONTINUE = "CONTINUE"
    NEXT = "NEXT_OPPORTUNITY"
    TARGET = "TARGET_LAP"
    EXTEND = "EXTEND_STINT"
    COMPOUND = "COMPOUND_ALTERNATIVE"
    VSC = "VSC_REACTIVE"
    SC = "SC_REACTIVE"
    TEAM = "STAGGERED_TEAM"
    SEQUENCE = "PRECOMMITTED_SEQUENCE"


@dataclass(frozen=True, slots=True)
class Stop:
    driver: int
    set_id: str
    lap: int | None = None
    review_us: int | None = None
    cancel_us: int | None = None
    delay_laps: int = 0


@dataclass(frozen=True, slots=True)
class PolicyView:
    clock_us: int
    track_state: TrackState
    committed_signals: tuple[str, ...]
    car: CarState
    progress_m: float
    next_pit_lap: int
    predecessor: int | None
    rank: int
    gap_m: float | None
    available_sets: tuple[str, ...]
    box_free_us: int | None


class StrategyPolicy(Protocol):
    def choose(self, stop: Stop, view: PolicyView) -> int | None:
        """Return the requested lap or wait for another decision point."""


@dataclass(frozen=True, slots=True)
class Policy:
    strategy_id: str
    kind: PolicyKind
    stops: tuple[Stop, ...] = ()

    def choose(self, stop, view):
        if self.kind is PolicyKind.CONTINUE or view.car.route is not Route.ON_TRACK:
            return None
        if stop.review_us is not None and view.clock_us < stop.review_us:
            return None
        if self.kind is PolicyKind.VSC and (
            view.track_state is not TrackState.VSC or "VSC_DEPLOY" not in view.committed_signals
        ):
            return None
        if self.kind is PolicyKind.SC and (
            view.track_state is not TrackState.SC or "SC_DEPLOY" not in view.committed_signals
        ):
            return None
        return (stop.lap if stop.lap is not None else view.next_pit_lap) + stop.delay_laps


@dataclass(frozen=True, slots=True)
class DriverResources:
    driver: int
    team: str
    tyres: tuple[ReplacementTyres, ...]
    entry_m: float
    route: ResolvedPitRoute
    prior_dry_compounds: tuple[str, ...] = ()
