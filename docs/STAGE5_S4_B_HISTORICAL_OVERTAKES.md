# Stage 5 S4-B historical overtaking labels

## Frozen order contract

| S4-A field | Meaning and phase |
| --- | --- |
| `physical_before` | The racing predecessor cycle starting at the attacker. The pre-event hook reads it immediately before rewiring the physical chain for an admitted crossing. Each element's successor is the next car ahead around the loop. |
| `physical_after` | The same attacker-anchored cycle read after rewiring and the authoritative crossing. |
| `pair_at_boundary_before` | The validated front-to-back pair approaching the common boundary immediately before commitment. |
| `pair_at_boundary_after` | The front-to-back pair after the attacker commits the crossing first. |
| `classification_before` | Timing order immediately before the normal crossing transition, after any earlier same-time scheduler events. |
| `classification_after` | Timing order after the normal crossing transition, as recorded on the pass event frame. |

For an unresolved attempt the before and after fields capture the resolution state and the boundary pair is empty. With two cars, the cycle always returns from either driver to the other. Anchoring at car 2 therefore gives `[2, 1]` on both sides even when the checkpoint pair flips from `[1, 2]` to `[2, 1]`. In a larger field a cycle can be rotated without changing any predecessor links. The versioned `s4-a-order-view-v2` audit exposes both representations with explicit names and checks the recorded phase against the scheduler trace.

Historical reconstruction uses local signed longitudinal distance and uncertainty at both endpoints. It consumes neither synthetic ordering field.

## Archived evidence

The cache-only experiment approves the frozen Singapore pilot, Austria and Hungary development windows, and Imola reconstruction windows. Imola contributes a reconstruction check. Singapore retains its pilot role. All later validation and reserved cohorts retain their existing assignments. The source census verifies reconstructed geometry, input hashes, sample ranges and original cached payloads before outcomes are evaluated.

Each directed pair has a local relative-gap series. A candidate uses a sign change within the frozen width and distance screen. A supported event requires resolved adjacency and uncertainty bounds of opposite sign at the two endpoints. A movement screen uses consecutive unwrapped positions and preserves legitimate start/finish crossings. Route interruptions, flags and lap-alignment failures can exclude a candidate. Overlapping brackets for the same directed pair are grouped into one event. The strongest observed bracket is retained, with alternate source brackets listed in the evidence. Neighbor changes backed by pit or withdrawal evidence have their own identifiers.

Tier A requires complete field endpoints, independently compatible rank and timing observations, and a bracket within four sample periods. Tier B retains resolved events with explicit coverage, timing or resolution limits. Tier C retains ambiguous observations. Known competing events and projection failures are excluded from the competitive labels. Time bounds record the last confirmed behind and first confirmed ahead. Source observations, age and positional uncertainty are preserved.

## Opportunity and outcome contract

The cutoff ledger reads only the point-in-time position, speed, neighborhood and timestamped race context. Upper gap bounds define the frozen 40 m, 80 m and 120 m sensitivity populations. Separate arms record close following, uncertainty-supported closing, seeded random GREEN states within a 300 m upper gap, and different-lap interactions. Multiple arms may apply. Weather and prior lap pace are timestamped. The earlier-lap pace reference screens consecutive completed laps using GREEN status, pit records visible by the lap end and archived interval or leader evidence at both ends. Retrospective tire context is published independently because the stints table lacks receipt timestamps.

The future ledger uses retrospective physical samples at the native grid cadence, independent timing markers and observed route or flag interruptions. It records 5, 10, 20 and 30 second horizons plus the next corresponding observed sector or control-line marker. The marker end is a future label field. A pass requires a qualifying bracket within the horizon and prior supported observation. A supported non-pass requires continuous sampled support for the racing relationship. Interrupted observation, an uncertain exchange, or a bracket overlapping the horizon boundary retains an explicit status. Absence of a completed exchange cannot identify a driver's intention or a failed attempt.

The encounter ledger links nearby cutoffs of the unordered race pair within 30 seconds, including opposing directions and fragments. It records distinct event IDs. Race, pair and encounter are available as grouping keys. These encounter groups are dependence controls. Multiple groups from one race still share race context.

[Experiment configuration](../config/experiments/stage5_s4_b_historical_overtakes.yaml)
