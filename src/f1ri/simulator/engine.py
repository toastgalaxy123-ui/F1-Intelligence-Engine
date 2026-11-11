"""An intentional extension point: no unvalidated 'strategy probabilities'."""
from f1ri.simulator.contracts import RaceState, SimulationSummary, StrategyCandidate


def simulate_remaining_race(
    state: RaceState, strategy: StrategyCandidate, n_simulations: int, seed: int
) -> SimulationSummary:
    raise NotImplementedError(
        "Build and backtest lap-pace, pit-loss and race-state modules before simulation. "
        "See docs/BUILD_ORDER.md."
    )
