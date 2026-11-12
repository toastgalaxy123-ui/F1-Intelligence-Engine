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
- Racing-status interval reconstruction and verified clean-lap labels
- Green-flag pace, tire degradation, fuel and traffic models
- Pit-loss calibration, race-state reconstruction, overtake model
- Monte Carlo optimizer, dashboard charts, live ingestion

## Attribution and licenses

OpenF1 is an unofficial third-party service. Jolpica's source data is
non-commercial under CC BY-NC-SA 4.0 and has attribution requirements.
Check each provider's current terms before redistribution or commercialization.
Do not commit large source datasets or third-party branded graphics.
