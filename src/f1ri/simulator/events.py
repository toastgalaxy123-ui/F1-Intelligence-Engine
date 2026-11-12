"""Checkpoint events and equal-time precedence for Stage 5."""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from f1ri.simulator.state import TrackState, TyreState


class EventKind(IntEnum):
    RACE_START = 0
    TRACK_STATE_CHANGE = 10
    RACE_SUSPENDED = 11
    RACE_RESUMED = 12
    RETIREMENT = 20
    PIT_EXIT = 30
    CHECKPOINT_CROSSING = 40
    LAP_COMPLETED = 41
    PIT_ENTRY = 50
    PIT_SERVICE_START = 51
    PIT_SERVICE_END = 52
    OVERTAKE_ATTEMPT = 60
    ON_TRACK_PASS = 61
    SC_QUEUE_UPDATE = 62
    CHEQUERED_FLAG = 70
    CAR_FINISHED = 71


@dataclass(frozen=True, slots=True)
class Event:
    at_us: int
    kind: EventKind
    driver_number: int | None = None
    checkpoint: int | None = None
    track_state: TrackState | None = None
    tyres: TyreState | None = None
    generation: int | None = None
    source: str = "SCENARIO"

    def __post_init__(self) -> None:
        if not isinstance(self.at_us, int) or self.at_us < 0:
            raise ValueError("Event time must be a nonnegative integer")
        if self.kind in {
            EventKind.CHECKPOINT_CROSSING, EventKind.LAP_COMPLETED,
            EventKind.PIT_ENTRY, EventKind.PIT_EXIT, EventKind.RETIREMENT,
        } and self.driver_number is None:
            raise ValueError("Car events require a driver number")
        if self.kind in {EventKind.CHECKPOINT_CROSSING, EventKind.LAP_COMPLETED}:
            if self.checkpoint is None or self.checkpoint <= 0:
                raise ValueError("Crossings require a positive checkpoint number")
            if (self.kind is EventKind.LAP_COMPLETED) != (self.checkpoint % 3 == 0):
                raise ValueError("Lap completion must occur at the timing line")
        if self.kind is EventKind.TRACK_STATE_CHANGE and self.track_state is None:
            raise ValueError("Track-state events require the new state")
        if self.generation is not None and (self.driver_number is None or self.generation < 0):
            raise ValueError("Generation tokens require a driver and nonnegative index")
