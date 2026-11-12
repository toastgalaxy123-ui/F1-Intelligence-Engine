# F1 Race Intelligence Engine — Stage 5 Report

## Deterministic simulation, historical replay, and readiness for forecasting

Stage 5 completed its first engineering milestone, **S0**. The engine can
advance a field through a deterministic race and reconstruct two complete
Grands Prix from recorded timing. Both replays matched every driver's official
lap count. The reconstructed classifications also matched every classified
finisher after applying the documented post-race penalty in Singapore.

The evidence comes from controlled scenarios and two retrospective replays.
Race-outcome probabilities and forecast calibration remain open. This report
covers the S0 implementation introduced in `3364334`. [Stage 5 specification](../../docs/STAGE5_SPEC.md)

## 1. What shipped

| Component | S0 behavior |
| --- | --- |
| Race state | Tracks the field, elapsed clock, track status, completed laps, tire state, pit route, and finish or retirement status. |
| Event scheduler | Processes events in timestamp order with explicit tie-breaking and support for invalidating stale events. |
| Conditional simulation | Runs supplied lap times, pit costs, and retirements. The no-pass scenario holds checkpoint order and enforces a minimum gap on a shared, uninterrupted route. |
| Finish procedure | The first scheduled-distance crossing triggers the chequered flag. Other active cars finish at their next timing-line crossing, including cars a lap or more down. |
| Historical replay | Reads the warehouse in read-only mode and emits a JSON audit of crossing estimates, pit visits, track-state changes, and classification. |

The elapsed clock uses integer microseconds so equal timestamps have
deterministic behavior. The source timing remains approximate. Checkpoint order
supports lap counting and finish classification. Continuous physical order,
exact gaps, and pit-exit merge order remain unresolved.
[Race state](../../src/f1ri/simulator/state.py) · [Scheduler](../../src/f1ri/simulator/scheduler.py) · [Engine](../../src/f1ri/simulator/engine.py)

## 2. Historical replay results

| Measure | Singapore 2025 | Imola 2025 |
| --- | ---: | ---: |
| OpenF1 session key | 9896 | 9987 |
| Scheduled distance | 62 laps | 63 laps |
| Completed laps reconstructed across the field | 1,229 | 1,205 |
| Drivers matching official lap counts | 20/20 | 20/20 |
| Classified finishers matching official positions | 20/20 | 18/18 |
| Pit windows replayed | 23/23 | 37/37 |
| Retirements reconstructed | 0 | 2 |
| Lap boundaries compared | 1,209 | 1,187 |
| Largest lap-boundary disagreement | 0.637 s | 0.460 s |
| Timing quality | Tier B | Tier B |

Repeated runs produced identical traces for both fixtures. The boundary
disagreement compares a lap-start-plus-duration estimate with the next
recorded lap start. It measures agreement within the timing feed. Tier B
reflects a coherent sequence with approximate position or event timing.
[Replay audit](stage5_replay.md)

### Singapore — Lapped finishes and the post-race penalty

Eleven cars finished one lap down. The timing estimate put Lewis Hamilton
seventh, 0.437 seconds ahead of Fernando Alonso. The FIA added five seconds
to Hamilton's race time, which moved him to eighth. The replay applies the
penalty only to classification and preserves both drivers' crossing times.
[FIA penalty decision](https://www.fia.com/system/files/decision-document/2025_singapore_grand_prix_-_infringement_-_car_44_-_leaving_the_track_without_a_justifiable_reason_multiple_times.pdf) · [FIA final classification](https://www.fia.com/system/files/decision-document/2025_singapore_grand_prix_-_final_race_classification.pdf)

### Imola — Neutralization and retirement

The replay processed a recorded VSC interval of 220 seconds and a Safety Car
interval of 915 seconds. Esteban Ocon and Kimi Antonelli retained 27 and 44
completed laps, respectively. Their retirement events use the last observed
progress as a lower bound. The lap times during VSC and Safety Car came
from the observed race. Predicting field compression, restart behavior, or
pit loss under neutralization requires additional mechanics.
[Replay audit](stage5_replay.md)

## 3. Engineering validation

The full suite passed **103 tests**, including eight Stage 5 tests. Ruff
passed. The new tests compare elapsed times with an independent lap-time sum,
check that a faster car stays behind in the no-pass scenario, and verify that
a synthetic pit cost is applied once while the car changes route and tire
state. They also cover retirement, a lapped car crossing at the instant of
the chequered flag, duplicate events, and classification adjustments.
[Simulator tests](../../tests/test_race_simulator.py) · [Replay test](../../tests/test_race_replay.py)

## 4. Forecasting acceptance gates

| Increment | Remaining work |
| --- | --- |
| S1 — Pace | Integrate complete correlated Pace V2 stint trajectories. Preserve structural uncertainty across checkpoints and measure new-stint bias and long-horizon calibration. |
| S2 — Pits | Integrate the locked P1 fallback experimentally for supported green-flag conditions. Retain synthetic scenarios for unsupported costs and apply each net transition loss once. |
| S3–S4 — Race mechanics | Validate traffic, overtaking, retirement and pit decisions. Implement scripted neutralization procedures before evaluating random race-control hazards. |
| S5 — Forecast validation | Aggregate complete Monte Carlo outcomes, verify probability conservation and convergence, and compare calibrated forecasts with simple baselines on predeclared chronological holdouts. Evaluate 2026 transfer separately. |

Stage 4.1 selected P1 as a simple net-loss fallback. Physical pit-route
validation, approved service-tail probabilities, and VSC/SC loss estimates
remain open. The S0 replay uses historical timing, and conditional scenarios
use explicit inputs. Genuine forecasting requires cutoff-safe state
construction, training artifacts frozen before the forecast cutoff, and
tests that mutate future observations to detect leakage.
[Stage 3 validation](stage3_validation.md) · [Stage 4.1 stabilization](stage4_1_stabilization.md) · [Stage 5 specification](../../docs/STAGE5_SPEC.md)

## 5. Reproduce the replay

Run from the repository root with both races built into the warehouse.
Supply `--warehouse` to use an existing DuckDB file at another path and
`--output` to save the JSON audit.

```bash
f1ri stage5-replay --session-key 9896 --scheduled-laps 62
f1ri stage5-replay --session-key 9987 --scheduled-laps 63
pytest -q
ruff check src tests
```

Build instructions for missing sessions are in the
[Stage 5 README section](../../README.md#stage-5--deterministic-race-simulation-and-replay).
