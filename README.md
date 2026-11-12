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
evidence-based acceptance gates. The dashboard remains a placeholder.

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
