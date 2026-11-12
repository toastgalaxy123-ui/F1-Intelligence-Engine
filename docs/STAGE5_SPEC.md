# Stage 5 specification

## Scope and release gates

Stage 5 models a field on one elapsed race clock. The first increment
implements the state contract, deterministic checkpoint engine, and historical
replay for Singapore 2025 (OpenF1 9896, 62 laps) and Imola 2025 (9987, 63 laps).
These races are engineering fixtures and may inform debugging. Reserve new
chronological cohorts before evaluating forecasting accuracy.

| Capability | Input and intended use |
| --- | --- |
| S0 | Observed replay and deterministic synthetic scenarios for engineering checks |
| S1 | Pace V2 trajectories with no future pits or incidents |
| S2 | Scripted synthetic pit costs or the provisional P1 green-flag fallback |
| S3 | Traffic and overtaking models for conditional competition forecasts |
| S4 | Experimental incident and race-control scenarios |
| S5 | Integrated forecasting with independent evidence of skill and calibration |

The current release implements S0 and the conditional Pace V2 integration
described in the [Stage 5.1 specification](STAGE5_S1_SPEC.md). An engineering release requires correct
state transitions, reproducible traces, and explicit handling of missing data.
Forecast approval requires chronological, race-weighted validation against
simple probabilistic baselines, useful calibration, and an explicit support
domain. Strategy optimization is Stage 6.

## Execution modes

| Mode | Entry point and data boundary |
| --- | --- |
| Historical reconstruction | `replay_session` reads recorded laps, pit events, track states and end-of-session evidence. Official position and lap totals serve as comparison targets. DNF/DNS flags may establish retrospective participation status. |
| Conditional simulation | `simulate_deterministic` accepts an initial grid and explicit lap, pit and retirement inputs. It has no data-source access. |
| Genuine forecasting | S1 uses cutoff-bound observations for clean-air pace under a declared continuation. Integrated race forecasting requires model-generated future events and independent calibration evidence. |

Persist the mode, capability, session identity, scenario or cutoff, ruleset,
model versions and seed with future probabilistic artifacts. Test forecast
invariance to mutation of future laps, pits, race control, retirements, final
classification and future training races.

## State, timing and rules

- Use session key and driver number together as identity. The grid is variable
  size, with unique driver numbers and unique known grid positions.
- The race clock uses integer microseconds relative to a timezone-aware race
  start. Observed feed accuracy is typically lower. The clock is monotonic,
  including during suspensions. Official timed-race and suspension accounting
  require a separate sporting procedure.
- Each committed crossing advances the next expected checkpoint. The timing
  line alone increments lap count and tire age. Pit entry changes the car's
  route. A resolved tire change sets the new compound and initial age at exit.
- Race progress orders cars by completed checkpoint distance, then crossing
  time, then grid and driver number. Route and checkpoint state describe
  physical progress. Physical order between checkpoints, exact track gaps,
  and relative order at a merge remain unresolved in S0. Position reports are
  discrete observations for comparison.
- The first scheduled-distance crossing raises the synthetic chequered flag.
  Other running cars finish on their next timing-line crossing, potentially a
  lap or more down. Finish order uses completed laps and crossing time.
  `2025/s0-v1` and `2026/s0-v1` version this distance-only subset. Time limits,
  official classification eligibility, penalties and restart procedures
  require additional rules. Replays expose timing order and official results.
- The scheduler orders by timestamp, event precedence, grid position, and
  driver number. At ties it starts the race, applies track-state changes and
  retirements, exits pits, crosses checkpoints, and then enters pits. A
  scheduled-distance lap completion precedes other lap completions at the
  same time so lapped cars can finish at that instant. A
  generation token can invalidate obsolete events. Source conflicts that
  cannot be resolved at this precision are reported.
- Sector proportions are explicit scenario inputs. Cumulative rounded
  offsets preserve the quantized lap total. Free-running timing estimates
  order from arrivals. The synthetic no-pass option tests checkpoint following
  on a shared route. Physical passing, lapped traffic and pit-merge arbitration
  require additional interactions.

## Replay evidence

Use the race start message, grid, driver list, timestamped laps and sectors,
existing track-status reconstruction, and Stage 4 pit-event reconstruction.
Estimate a crossing from lap start plus recorded duration, compare the
estimate with the next lap's start when available, and report discrepancies
above one second. Preserve source times and surface missing or nonmonotonic
crossings. Use sector timing when usable. Retrospective retirements use the
last observed progress as a lower bound. Pit windows from Stage 4 use the
provider report timestamp and approximate lane duration.

The replay compares estimated timing order with the last discrete position
report per driver. It applies independently sourced post-race time penalties
from `data/raw/reference/race_time_penalties.csv` when comparing reconstructed
classification to the official result. These adjustments never change
crossings, pit events or elapsed race time.

| Timing tier | Meaning |
| --- | --- |
| A | Independently verified timing and position evidence |
| B | Coherent crossing sequence with approximate position or event timing |
| C | Coherent partial state with missing or conflicting evidence |
| Unsupported | Insufficient evidence to construct a field or crossing sequence |

## Pace and pit ownership

Pace V2 owns normal clean-air pace. Consume complete correlated stint samples
and apportion them across checkpoints. Preserve one draw for structural stint
uncertainty and shared race effects. Track new-stint bias and long-horizon
coverage as validation limitations.

Observed lap durations already contain pit effects. In synthetic races add an
explicit net cost once. The S0 coarse transition applies it after the in-lap
and uses it as the route-exclusion window. Rejoin position is approximate.
Resolved physical pit timing requires independent route evidence and a
reconciliation against the normal time replaced.

Stage 4.1 locked P1 as `SIMPLE_NET_LOSS_FALLBACK_NO_APPROVED_SERVICE_RISK`.
Future S2 integration may use it experimentally for supported green-flag
conditions. The two-lap net transition includes stationary service and the
incremental pit-out effect. Lane time, service and warm-up carry no additional
race-time charges. Preserve the shared reference-error accounting when jointly
sampling pace and pit loss. P8 is research-only. VSC/SC pit costs require
explicit scenario values until physically supported estimates exist.

## Remaining implementation sequence

1. Verify replay and deterministic synthetic timing against known results.
2. Integrate Pace V2 with one correlated trajectory per stint, then integrate
   Stage 4's provisional green-flag pit contract.
3. Estimate traffic effects and passing opportunities with era-aware labels
   and held-out baselines. Validate retirement hazards separately.
4. Add scripted VSC, Safety Car and suspension procedures. Evaluate random
   deployment and duration hazards before enabling them in forecast modes.
5. Evaluate a point-in-time behavioral pit policy, then aggregate complete
   Monte Carlo outcomes and check probability conservation and convergence.
6. Backtest across predeclared races and cutoffs, report ablations and
   calibration, and evaluate 2026 transfer separately before freezing V1.
