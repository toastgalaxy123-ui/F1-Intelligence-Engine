# F1 Race Intelligence Engine

A reproducible, strictly point-in-time Formula 1 race analytics and stint-simulation engine built on **OpenF1**, **Jolpica F1**, **DuckDB**, and **Python 3.11+**.

## Implemented Milestones

- **Stage 0 — Data Ingestion & Typed Warehouse**: Immutable raw JSON snapshots (`data/raw/`), normalized `silver.*` DuckDB tables (`sessions`, `drivers`, `laps`, `stints`, `pits`, `intervals`, `race_control`, `weather`, `starting_grid`, `session_result`), and automated data-quality checks.
- **Stage 1 — Race Context & Verified Clean-Air Pace Labeling**: Event-driven track-status state machine (`GREEN`, `YELLOW`, `VSC`, `SC`, `RED`) and causal lap context table (`gold.lap_context`) separating true green-flag clean-air racing laps (`is_clean_pace_lap`) from pit in/out laps, neutralizations, and dirty-air traffic (`interval_to_car_ahead_s < 1.5s`).
- **Stage 2 — Validated Single-Lap Race Pace Modeling (`PaceV1`)**: Point-in-time training examples (`gold.lap_CleanPaceFeatures`, `gold.pace_training_examples`), empirical-Bayes hierarchical baseline + online exponentially weighted pace state (`PaceModelV1`), and rolling-origin evaluation across 15 races (`E0`–`E10`, [Stage 2 Validation Report](artifacts/reports/stage2_validation.md)).
- **Stage 3 — Tire Degradation & Open-Loop Future Stint Simulation (`PaceV2` / `DegradationV1`)**:
  - Verified Pirelli `C1`–`C5` compound allocations (`silver.compound_allocations`, [`data/raw/reference/pirelli_allocations.csv`](data/raw/reference/pirelli_allocations.csv)) across **32 Grand Prix sessions (11 circuits, 2023–2026)**.
  - Stint-level and open-loop horizon tables (`gold.stint_observations`, `gold.stint_lap_observations`, `gold.stint_training_examples` for $H \in \{1, 3, 5, 10, 15, 20\}$).
  - Hierarchical empirical-Bayes tire degradation (`HierarchicalTireDegradationModel`), online Bayesian stint-state filtering (`OnlineStintStateEstimator`), correlated multi-lap trajectory uncertainty (`CorrelatedTrajectorySampler`), and counterfactual strategy comparison (`StintTrajectoryForecaster`, [Stage 3 Validation Report](artifacts/reports/stage3_validation.md)).

- **Stage 3-R — Pace V2 reconstruction**: Rebuilt the historical training and evaluation inputs and validated
  `PaceV2_D8R1` for mean pace in declared strategy scenarios. See the
  [Stage 3-R report](artifacts/reports/stage3_r1_validation.md) for the coverage gate and reproduction commands.

- **Stage 4 — Pit-Loss Research**: Reconstructed pit visits, uncertain cross-fitted
  net-loss labels, matched controls, pooled route/service models, P0–P10 and
  conditional N-series evaluation, bounded telemetry checks, and explicit
  no-double-counting simulation contracts. See the [Stage 4 report](artifacts/reports/stage4_validation.md)
  for results and the outstanding model-approval gates.

## Windows / PowerShell Quick Start

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,modeling,dashboard]"

# Run the Stage 2 and Stage 3 pipelines:
f1ri stage2 --config config/experiments/stage2_pace_v1.yaml
f1ri stage3 --config config/experiments/stage3_degradation_v1.yaml

# Run unit tests and linter:
pytest -q
ruff check src tests
```

On macOS/Linux: `python3 -m venv .venv`, `source .venv/bin/activate`.
If PowerShell blocks activation: `Set-ExecutionPolicy -Scope Process Bypass`
or call `.\.venv\Scripts\python.exe` and `.\.venv\Scripts\f1ri.exe` directly.

## Stage 4 — Pit-loss research workflow

```bash
f1ri ingest-batch --config config/experiments/stage4_pit_loss_v1.yaml
f1ri stage4 --config config/experiments/stage4_pit_loss_v1.yaml
pytest -q
ruff check src tests
```

Stage 4 reconstructs pit visits, estimates uncertain two-lap net-loss labels,
cross-checks matched non-pitting controls, and compares circuit baselines with
pooled route/service/transition models. It preserves Pace V2 and separates
pre-stop requests from historical reconstruction. Stationary service is already
inside lane duration; the simulator adds only the net transition penalty.

See [Stage 4 contracts and limitations](docs/STAGE4_SPEC.md) and the
[validation report](artifacts/reports/stage4_validation.md). The research model
is not automatically approved for strategy optimization: superior held-out
accuracy, sourced pit-lane references and supported VSC/SC estimates remain
evidence-based acceptance gates. The local dashboard compares declared strategy scenarios with matched conditions.
See [the S5-C strategy guide](docs/STAGE5_S5_C_STRATEGY.md).

### Stage 4.1 stabilization

The [stabilization report](artifacts/reports/stage4_1_stabilization.md) documents
exceptional-event filtering, expanded historical service evidence, independent
label checks and separate familiar/unseen circuit tests.

```bash
f1ri stage4-stabilize --phase prepare --ingest
f1ri stage4-stabilize --phase validate
f1ri stage4-stabilize --phase evaluate --ingest
f1ri stage4-stabilize --phase report
```

Use `config/experiments/stage4_1_stabilization.yaml`. Preparation/validation never
fetch reserved tests. Validation writes a source/data/model selection lock;
evaluation verifies it and records test access before ingestion. Once test
outcomes are open, fitting and selection for that study are disabled. Frozen
evaluation/reporting can be reproduced without retuning. Keep a new study name
and preregister new tests for subsequent modeling changes. The reviewed Stage 4
run is preserved separately and is required for its before/after audit.

See [the stabilization protocol](docs/STAGE4_STABILIZATION.md) for eligibility,
conditional tire scenarios, reference sourcing and remaining acceptance gaps.
The [official FIA reference audit](artifacts/reports/stage4_1_fia_reference_audit.md)
adds dated source evidence separately from the frozen evaluation inputs.

## Stage 6-A — Conditional strategy optimization

Run `f1ri stage6-s6-a --output artifacts/runs/stage6_s6_a_reproduced` or select **Optimize strategies**
in the strategy dashboard. S6-A searches bounded pit and tyre policies with frozen D8R1 mean pace at
three-, five- and ten-lap horizons. See the [validation report](artifacts/reports/stage6_s6_a_validation.md)
for the scenario results, limits and reproduction checks.

### Stage 6-B — Robust strategy

Select **Robust strategy** in the existing dashboard or run
`f1ri stage6-s6-b --output artifacts/runs/stage6_s6_b_reproduced`. The same policy library runs
across declared futures using D8R1 mean pace. See the [S6-B validation report](artifacts/reports/stage6_s6_b_validation.md)
for regret, dominance, control-clock and support results.

### Stage 6-B.1 — Timing risk and pit commitment

Select **Timing risk** in the strategy dashboard to inspect the released timing-boundary evidence.
Run `f1ri stage6-s6-b1 --output artifacts/runs/stage6_s6_b1_reproduced` to replay the study.
See the [S6-B.1 report](artifacts/reports/stage6_s6_b1_validation.md) for the fixed and revisable policy comparisons.

### Stage 6-C — Race Intelligence Decision Board

Launch `python -m streamlit run src/f1ri/dashboard/app.py` to open the research board.
It links an archived or declared cutoff to D8R1 mean forecasts, physical strategy comparison,
robust evaluation and frozen timing-risk evidence. The board has six presets and a reproducible
analysis export. See the [S6-C validation report](artifacts/reports/stage6_s6_c_validation.md)
for the readiness decision, verification and commands.

### Stage 7-A — Historical multi-car field reconstruction

Open **Race State** in the decision board to inspect the Singapore and Imola 2025 pilot cutoffs.
The event-time view shows reconstructed progress, coverage, and neighbor candidates where the
observations support them. Run `f1ri stage7-s7-a --output artifacts/runs/stage7_s7_a_reproduced`
to regenerate the historical ledgers. See the [S7-A report](artifacts/reports/stage7_s7_a_validation.md)
for the pilot results, reproducibility checks and remaining evidence gaps.

### Stage 7-A.1 — Historical pit branch and predecessor evidence

Open **Race State → Pit routes and predecessor evidence** to inspect the held-checked Imola pit
interior, retrospective route-change windows and candidate-order certificates. Run
`f1ri stage7-s7-a1 --output artifacts/runs/stage7_s7_a1_reproduced` to regenerate the ledgers.
See the [S7-A.1 report](artifacts/reports/stage7_s7_a1_validation.md) for the supported cases and limits.

### Stage 7-B — Source availability and receipt replay

Open **Source Availability & Replay** in the Decision Board to inspect archived event clocks,
observed collector receipts and controlled delivery failures. Run
`f1ri stage7-s7-b study --output artifacts/runs/stage7_s7_b_reproduced` to reproduce the study.
The [S7-B report](artifacts/reports/stage7_s7_b_validation.md) documents the capture, outcome and limits.

### Stage 7-C0 — Shadow-mode preflight

Open **Shadow-Mode Readiness** in the Decision Board to inspect priority polling, field-level
timing and sealed research snapshots. Run
`f1ri stage7-s7-c0 study --output artifacts/runs/stage7_s7_c0_reproduced` to reproduce the
bounded study. The [S7-C0 report](artifacts/reports/stage7_s7_c0_validation.md) documents the
readiness gates and the evidence needed for a contemporary capture.

### Stage 8-A — Local web research workspace

The Next.js workspace and versioned FastAPI service expose archived D8R1 pace, legal declared
strategy comparisons, bounded optimization, timing risk and source evidence. Follow the
[local setup guide](docs/web/local.md) to start both services. The
[S8-A validation report](artifacts/reports/stage8_s8_a_validation.md) records the browser and
engine parity checks and the limits on historical and live strategy.

## Stage 5 — Deterministic race simulation and replay

The [Stage 5 specification](docs/STAGE5_SPEC.md) defines the simulation modes,
checkpoint clock, pit time accounting and forecasting acceptance gates.
`simulate_deterministic` in `f1ri.simulator.engine` runs explicit multi-car
scenarios with per-lap timing, optional pit delays and retirements.

Replay completed races from a built warehouse:

```bash
f1ri stage5-replay --session-key 9896 --scheduled-laps 62
f1ri stage5-replay --session-key 9987 --scheduled-laps 63
```

Use `--warehouse` for an existing DuckDB file or `--output` to save the JSON
audit. To build a missing session, run `f1ri ingest --year 2025 --session-key
<key> --full` and then `f1ri build --year 2025 --session-key <key>`.
See the [Stage 5 report](artifacts/reports/stage5_full_report.md) for results,
engineering validation, and the remaining forecasting work.

### Stage 5.1 — Pace V2 field simulation

Run the numerical parity checks, historical clean-air backtest, and
conditional field simulations with the frozen Stage 3 bundle.

```bash
f1ri stage5-s1 --stage3-root /path/to/original-stage3-project
```

The Stage 3 root supplies the saved model, run metadata, and original
warehouse. The local warehouse supplies the two new-to-Pace sessions
(`9920` and `9904`), or use `--holdout-warehouse`. The command writes
metrics, snapshots, trajectory samples, and diagnostics to
`artifacts/runs/stage5_s1_pace`. The frozen model covers the 2023–2025 era.
[Stage 5.1 validation report](artifacts/reports/stage5_s1_validation.md)

### Stage 5.2 — Pit execution and tire changes

```bash
f1ri stage5-s2 --stage3-root /path/to/original-stage3-project
```

The S2 runner verifies synthetic route and coarse timing, paired frozen D8
stop-versus-continue scenarios, and recorded pit windows. Use
`--local-warehouse` if the Netherlands and Azerbaijan sessions are in a
different warehouse. Supply `--p1-artifact` to use the original locked P1
pickle. The loader checks its digest against Stage 4.1's selection record.
[Stage 5.2 validation report](artifacts/reports/stage5_s2_validation.md)

### Stage 5.3 — Physical track proximity and following

The Singapore pilot estimates circular physical order from bounded telemetry
windows and checks it against timing. The synthetic engine enforces checkpoint
headway across the start/finish line and supports neighborhoods at resolved S2
rejoins. Traffic-loss model validation remains open.

```bash
f1ri stage5-s3 --warehouse /path/to/singapore/f1ri.duckdb
```

The default audit warehouse needs Zandvoort and Baku. Add `--ingest` to fetch
missing telemetry for the configured windows.
[Stage 5.3 validation report](artifacts/reports/stage5_s3_validation.md)

### Stage 5.3-C — Cross-circuit traffic research

Run the frozen Imola, Hungary, and Monza protocol from a warehouse with their
archived timing. The optional Stage 3 root supplies the verified D8 bundle.
Add `--ingest` to fetch missing bounded snapshots and install the
`modeling` extra to render the diagnostic.

```bash
f1ri stage5-s3-continue --warehouse /path/to/f1ri.duckdb \
  --stage3-root /path/to/original-stage3-project
```

[Continuation results](artifacts/reports/stage5_s3_c_validation.md)

The coverage replay audits missing cars and compares the original Hungary
windows with the frozen development extension. It writes to a separate run
directory by default.

```bash
f1ri stage5-s3-continue --warehouse /path/to/f1ri.duckdb \
  --stage3-root /path/to/original-stage3-project \
  --coverage-pilot config/experiments/stage5_s3_c_coverage_pilot.yaml
```

[Coverage and target-readiness report](artifacts/reports/stage5_s3_c_coverage_report.md)

The history study reuses the frozen coverage replay and measures whether
cutoff-qualified Austria and Hungary windows yield supported following
targets.

```bash
f1ri stage5-s3-continue --warehouse /path/to/f1ri.duckdb \
  --stage3-root /path/to/original-stage3-project \
  --history-pilot config/experiments/stage5_s3_c_history_pilot.yaml
```

[History and control readiness report](artifacts/reports/stage5_s3_c_history_report.md)

## Useful SQL

```python
import duckdb
con = duckdb.connect('data/warehouse/f1ri.duckdb', read_only=True)
print(con.execute("""
 SELECT driver_number, compound,
        COUNT(*) AS provisional_laps,
        MEDIAN(observed_lap_time_s) AS median_s
 FROM gold.lap_observations
 WHERE provisional_pace_candidate
 GROUP BY 1, 2 ORDER BY 1, 2
""").df())
```

`provisional_pace_candidate` is NOT a verified green-flag/clean-air label.
See `docs/DATA_CONTRACTS.md` before training a pace model.

## Development rules

1. Never edit `data/raw` JSON in place; use `--refresh` to obtain a new snapshot.
2. Dataset primary keys use `session_key`, `driver_number`, `lap_number`.
3. UTC for all stored timestamps; seconds for times; °C for temperatures.
4. Do not use post-race features in models intended for a mid-race cutoff.
5. Hold out future races as complete sessions; don't randomly split individual laps.
6. Never silently coerce an unverified race-control status into GREEN.
7. Don't download high-frequency telemetry as part of normal session ingestion.
8. Keep source adapters separate from models, and models separate from simulation.

Read `docs/ARCHITECTURE.md`, `docs/DATA_SOURCES.md`, `docs/DATA_CONTRACTS.md`,
`docs/BUILD_ORDER.md`, `docs/MODELING.md`, and `docs/DECISIONS.md`.

## What is deliberately deferred?

- F1 TV/FastF1 authentication (not necessary for the first milestones)
- Measured pit-route geometry, remaining historical speed coverage, and validated VSC/SC pit-loss references
- Predictive multi-car race simulation, precise pit rejoin positions, traffic, overtaking, and Safety Car queues
- Unrestricted strategy-model approval, pit-timing optimization, and opponent responses
- Full dashboard charts and live ingestion

## Attribution and licenses

OpenF1 is an unofficial third-party service. Jolpica's source data is
non-commercial under CC BY-NC-SA 4.0 and has attribution requirements.
Check each provider's current terms before redistribution or commercialization.
Do not commit large source datasets or third-party branded graphics.
