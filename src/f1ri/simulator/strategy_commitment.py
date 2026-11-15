"""Explicit pit-instruction variants evaluated by the existing strategy runtime."""
from dataclasses import dataclass, replace

from f1ri.simulator.state import TrackState
from f1ri.simulator.strategy_policy import Policy, PolicyKind, Stop
from f1ri.simulator.strategy_robust import ObservedPolicy, policy_library

ORIGINAL = "adaptive-63-medium-or-soft"
FIXED = "63-L31-soft"
REVALIDATE = "p2-green-stay-out"
REPLAN = "p2-green-replan"
WINDOW = "p4-early-window"
SCHEDULED = "p5-scheduled-conditional"
ACTIVE = "p1-active-at-decision"
ANCHORS = ("reference", FIXED, ORIGINAL, "sc-63-soft", "vsc-63-soft")
FOCAL = (*ANCHORS, ACTIVE, REVALIDATE, REPLAN, WINDOW, SCHEDULED)

CONTRACT = {
    "version": "s6-b1-entry-commitment-v1",
    "proposal": "A policy resolves a proposed stop from its current PolicyView.",
    "decision": "REQUESTED and PIT_PLANNED record a pending instruction. The scheduled entry may be retimed.",
    "approach": "APPROACHING_PIT_ENTRY is a forecast of entry, subject to physical rescheduling.",
    "revision": ("P2 may revise a neutralization request on a committed GREEN_RELEASE while the car stays on track "
                 "and strictly before its current geometric entry line. It can revise once."),
    "irreversible": ("The geometric pit-entry line is the revision deadline. PIT_ENTRY_COMMITTED records the "
                     "actual route transition. Simultaneous race control is processed first but the geometric "
                     "guard disallows cancellation at equality."),
    "replacement": "A revised instruction has a distinct decision ID. Only service completion consumes a set.",
    "latency": ("The archived scripts provide committed event clocks. Late availability relative to the "
                "simulation event is rejected by the S6-A input gate. No empirical latency model is used."),
    "closure": "Red phases and unsupported geometry use existing scheduler gates. No independent pit closure model.",
    "receding_scope": "One bounded in-run plan revision on newly observed GREEN. No rewind or future-query optimizer.",
}


@dataclass(frozen=True, slots=True)
class RevalidatePolicy(ObservedPolicy):
    replan: bool = False

    def revise_on_green(self, requested_phase, view):
        return (requested_phase in (TrackState.VSC, TrackState.SC) and view.track_state is TrackState.GREEN
                and bool(view.committed_signals) and view.committed_signals[-1] == "GREEN_RELEASE")

    def resolve_fallback(self, stop, view):
        if (self.replan and view.track_state is TrackState.GREEN and self.green_lap == view.next_pit_lap
                and self.first_lap <= view.next_pit_lap <= self.last_lap):
            return replace(stop, lap=view.next_pit_lap)
        return None


@dataclass(frozen=True, slots=True)
class EarlierCommitment(ObservedPolicy):
    def resolve(self, stop, view):
        if view.track_state in (TrackState.VSC, TrackState.SC) and view.car.sector > 1:
            return None
        return ObservedPolicy.resolve(self, stop, view)


@dataclass(frozen=True, slots=True)
class ScheduledCompound(Policy):
    alternate_set: str = "soft"

    def resolve(self, stop, view):
        if view.next_pit_lap != stop.lap or view.car.sector != 2:
            return None
        phase = {"SC": "SC_DEPLOY", "VSC": "VSC_DEPLOY"}.get(view.track_state)
        selected = self.alternate_set if phase in view.committed_signals and phase is not None else stop.set_id
        return replace(stop, set_id=selected)


def alternatives(request):
    policies, _ = policy_library(request)
    original = next(p for p in policies if p.strategy_id == ORIGINAL)
    args = (original.kind, original.stops, original.first_lap, original.last_lap, original.neutralized_set,
            original.green_lap, original.deadline_us)
    first = original.first_lap
    return (replace(original, strategy_id=ACTIVE), RevalidatePolicy(REVALIDATE, *args),
            RevalidatePolicy(REPLAN, *args, replan=True), EarlierCommitment(WINDOW, *args),
            ScheduledCompound(SCHEDULED, PolicyKind.TARGET, (Stop(63, "medium", first),)))
