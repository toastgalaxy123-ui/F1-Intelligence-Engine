"""Versioned race-control commands and bounded 2025 execution policy."""
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite

from f1ri.simulator.state import TrackState


class ControlPhase(StrEnum):
    GREEN_RUNNING = "GREEN_RUNNING"
    VSC_DEPLOYING = "VSC_DEPLOYING"
    VSC_ACTIVE = "VSC_ACTIVE"
    VSC_ENDING = "VSC_ENDING"
    SC_DEPLOYING = "SC_DEPLOYING"
    SC_COLLECTING_FIELD = "SC_COLLECTING_FIELD"
    SC_TRAIN_FORMED = "SC_TRAIN_FORMED"
    SC_IN_THIS_LAP = "SC_IN_THIS_LAP"
    SC_RESTART_PENDING = "SC_RESTART_PENDING"
    RED_SUSPENDING = "RED_SUSPENDING"
    RED_SUSPENDED = "RED_SUSPENDED"
    RED_RESUMPTION_PENDING = "RED_RESUMPTION_PENDING"
    RESTART_ROLLING = "RESTART_ROLLING"
    RESTART_STANDING = "RESTART_STANDING"
    UNSUPPORTED_TRANSITION = "UNSUPPORTED_TRANSITION"


class ControlCommand(StrEnum):
    VSC = "VSC_DEPLOY"
    VSC_ENDING = "VSC_ENDING"
    SC = "SC_DEPLOY"
    SC_IN = "SC_IN_THIS_LAP"
    SC_WITHDRAWN = "SC_WITHDRAWN"
    RED = "RED_SUSPEND"
    PREPARE = "RED_PREPARE_RESUMPTION"
    ROLLING = "RED_RESUME_ROLLING"
    GREEN = "GREEN_RELEASE"
    LOCAL = "LOCAL_CAUTION"
    UNLAP = "UNLAP"
    STANDING = "STANDING_RESTART"
    RED_SERVICE = "RED_SERVICE"


class ControlOutcome(StrEnum):
    COMMITTED = "COMMITTED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"
    SUPERSEDED = "SUPERSEDED"
    UNSUPPORTED = "UNSUPPORTED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class RaceControlProfile:
    identifier: str = "fia-2025-issue5-s5a-mechanical-v1"
    season: int = 2025
    revision: str = "Issue 5, 2025-04-30"
    source_url: str = ("https://www.fia.com/system/files/documents/"
                       "fia_2025_formula_1_sporting_regulations_-_issue_5_-_2025-04-30.pdf")
    vsc_speed_mps: float = 36
    sc_speed_mps: float = 25
    catchup_speed_mps: float = 58
    restart_speed_mps: float = 33
    queue_gap_m: float = 12
    train_gap_m: float = 18

    def __post_init__(self):
        values = (self.vsc_speed_mps, self.sc_speed_mps, self.catchup_speed_mps,
                  self.restart_speed_mps, self.queue_gap_m, self.train_gap_m)
        if (self.season != 2025 or not all(isfinite(x) and x > 0 for x in values)
                or not self.sc_speed_mps <= self.restart_speed_mps <= self.catchup_speed_mps <= 105
                or self.vsc_speed_mps > 105 or self.train_gap_m < self.queue_gap_m):
            raise ValueError("Invalid 2025 neutralization profile")

    def manifest(self):
        from dataclasses import asdict

        return asdict(self) | {
            "effective_reference": "FIA archive Issue 5, published 2025-04-30; 2025 simulations only",
            "articles": [55, 56, 57, 58],
            "movement": ["MECHANICAL_VSC_APPROXIMATION", "VIRTUAL_SC_PACING_BOUNDARY", "STATIONARY_RED_APPROXIMATION"],
            "restart": "ROLLING_ALL_ACTIVE_RACING_CARS_CROSS_LINE_AFTER_WITHDRAWAL",
            "pit": "S2_DECLARED_ROUTE_TIMING_WHEN_GEOMETRY_AND_ENTRY_SUPPORTED",
            "passing": "S4_A_GREEN_ONLY; LOCAL_CAUTION_CONSERVATIVE_PASS_VETO",
            "lapped_cars": "PRESERVE_PHYSICAL_RELATIONSHIPS_AND_LAP_COUNTS",
            "unsupported": ["UNLAPPING", "STANDING_RESTART", "RED_SERVICE", "RED_WITH_ACTIVE_PIT_ITINERARY",
                            "FIA_MARSHALLING_SECTOR_DELTAS", "REGULATORY_RESTART_REORDERING"],
        }


@dataclass(frozen=True, slots=True)
class RaceControlEvent:
    event_id: str
    simulation_id: str
    command: ControlCommand
    at_us: int
    provenance: str
    source: str = "DETERMINISTIC_SCRIPT"
    observation_utc: datetime | None = None
    available_utc: datetime | None = None
    previous: ControlPhase | None = None
    requested: ControlPhase | None = None
    scope: str = "TRACK"
    profile_id: str = "fia-2025-issue5-s5a-mechanical-v1"
    message: str = ""
    confidence: str = "DECLARED"
    affected_cars: tuple[int, ...] = ()
    restart: str | None = None
    support: str = "SUPPORTED"
    local_flag: TrackState | None = None

    def __post_init__(self):
        if not (self.event_id and self.simulation_id and self.provenance and type(self.at_us) is int and self.at_us >= 0):
            raise ValueError("Race control requires identity, provenance and an integer effective clock")
        if any(t is not None and t.utcoffset() is None for t in (self.observation_utc, self.available_utc)):
            raise ValueError("Source timestamps must include a timezone")
        if self.available_utc and self.observation_utc and self.available_utc < self.observation_utc:
            raise ValueError("Availability cannot precede observation")


P = ControlPhase
C = ControlCommand
SC_PHASES = frozenset({P.SC_COLLECTING_FIELD, P.SC_TRAIN_FORMED, P.SC_IN_THIS_LAP, P.SC_RESTART_PENDING, P.RESTART_ROLLING})
VSC_PHASES = frozenset({P.VSC_ACTIVE, P.VSC_ENDING})
RED_PHASES = frozenset({P.RED_SUSPENDED, P.RED_RESUMPTION_PENDING})

LEGAL_TRANSITIONS = {
    C.VSC: ({P.GREEN_RUNNING}, (P.VSC_DEPLOYING, P.VSC_ACTIVE)),
    C.VSC_ENDING: ({P.VSC_ACTIVE}, (P.VSC_ENDING,)),
    C.SC: ({P.GREEN_RUNNING, *VSC_PHASES, P.SC_RESTART_PENDING, P.RESTART_ROLLING},
           (P.SC_DEPLOYING, P.SC_COLLECTING_FIELD)),
    C.SC_IN: ({P.SC_COLLECTING_FIELD, P.SC_TRAIN_FORMED, P.RESTART_ROLLING}, (P.SC_IN_THIS_LAP,)),
    C.SC_WITHDRAWN: ({P.SC_IN_THIS_LAP}, (P.SC_RESTART_PENDING,)),
    C.RED: ({P.GREEN_RUNNING, *VSC_PHASES, *SC_PHASES}, (P.RED_SUSPENDING, P.RED_SUSPENDED)),
    C.PREPARE: ({P.RED_SUSPENDED}, (P.RED_RESUMPTION_PENDING,)),
    C.ROLLING: ({P.RED_RESUMPTION_PENDING}, (P.RESTART_ROLLING,)),
    C.GREEN: ({P.VSC_ENDING, P.SC_RESTART_PENDING}, (P.GREEN_RUNNING,)),
}
UNSUPPORTED_COMMANDS = {
    C.UNLAP: "UNLAPPING_PROCEDURE_UNSUPPORTED",
    C.STANDING: "STANDING_RESTART_UNSUPPORTED",
    C.RED_SERVICE: "STATIONARY_RED_SERVICE_UNSUPPORTED",
}


def coarse_track(phase, local=TrackState.GREEN):
    if phase in SC_PHASES:
        return TrackState.SC
    if phase in VSC_PHASES:
        return TrackState.VSC
    if phase in RED_PHASES:
        return TrackState.RED
    return local
