# Stage 5 S4-A scripted overtaking

The checkpoint engine already owns committed progress, route and tire
state, and timing order. The scheduler orders equal-time events and
invalidates a driver's pending generation. S3 determines neighbors
from wrapped physical progress and evaluates static predecessor
dependencies. S2 owns the pit itinerary, timing line, common merge,
and shared-box accounting. S4-A adds a changing predecessor chain
and a scripted exchange at a common checkpoint.

`simulate_scripted_overtaking` accepts a conditional race snapshot,
exact declared racing positions, clean lap times, and immutable
`OvertakeAttempt` proposals. Each proposal identifies the driver,
neighbor, realization, layout, checkpoint and time window. Scripts
with no environment changes delegate to the existing T1 path when
the attempt list is empty. A later proposal generator can call the
same mechanical interface.

The engine requires both cars to be approaching the same next physical
checkpoint, with compatible routes, resolved lap relation and GREEN
status. The attacker must be able to arrive ahead of the old leader
by the configured checkpoint headway. It still follows any car ahead
of the pair. A late proposal cannot advance the attacker's elapsed
prefix. The declared clean traversal and a 105 m/s scenario ceiling
bound the move. S4-A admits one pending maneuver in the field at a time.
An active maneuver represents authorization at a future checkpoint.
Continuous lateral motion and exact within-sector passing time remain
unresolved.

On commit, the normal checkpoint transition advances the attacker.
The racing predecessor chain rewires, including offsets for lapped
cars. The scheduler invalidates obsolete crossing generations and
recomputes pending times. Already committed checkpoints and delay
stay in the trace. The prior physical leader becomes a follower.
The ranking comes from the resulting timing state and never drives
the physical-neighbor lookup.

At equal timestamps the existing track-state and retirement events
precede crossings, S2 route entry follows the crossing it may depend
on, and route events precede pass proposals and completions. Equal
scripts break ties by the existing grid/driver order and then attempt
ID. A red flag suspends racing checkpoint progression until resumption.
Other flags veto ordinary passes. FIA exceptions and neutralized pace
control remain outside this increment.

An explicit S2 itinerary can remove a car from the chain and reinsert
it at a resolved common merge. Its pit service and lane clocks remain
owned by S2. An ambiguous common merge is rejected. Coarse timing
accounting provides no racing position and blocks precision passing.
The prototype is bounded to declared checkpoint windows. It assumes
constant per-car clean pace and does not jointly optimize pit timing
when pass timing changes. Ordinary proposals at the scheduled finish
are unsupported.

Historical audit uses the approved cached pair observations, racing
route and GREEN checks. The replay reduces a supported pair to an
explicit scenario with cutoff-visible center estimates, sector-aligned
positions and constant local speeds. It preserves the original
uncertainties in the evidence ledger. A scripted replay is an
engineering comparison, with historical order and simulated
checkpoint times recorded separately.
[Validation plan](../config/experiments/stage5_s4_a_overtaking_mechanics.yaml)
