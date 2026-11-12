# Architecture and boundaries

```
OpenF1 / Jolpica / future providers
           |
           v
INGESTION (HTTP adapters, rate limit, immutable-by-default raw snapshots)
           |
           v
RAW JSON (source truth + source URL, SHA-256, retrieval time)
           |
           v
SILVER DuckDB (typed data, canonical IDs, explicit nulls, quality gates)
           |
           v
GOLD DuckDB (observational lap/stint features, point-in-time race states)
           |
           +--> MODELS (pace / degradation / pit loss / overtaking / hazards)
                         |
                         v
                   PREDICTED distributions (mean, variance, model version)
                         |
                         v
                   SIMULATOR (events, constraints, strategies, RNG)
                         |
                         v
                   EVALUATION / DASHBOARD / ARTIFACTS
```

Layer boundaries:
- `ingestion`: knows provider URLs and rate limits; returns source bytes and curated reference tables (`openf1.py`, `raw_cache.py`, `batch.py`, `compound_allocations.py`).
- `warehouse`: owns stable typed silver and gold structures; never predicts (`schema.py`, `build.py`).
- `features`: feature builders, track-status state machine, longitudinal stint tables, point-in-time forecast examples, and quality flags; no future data (`lap_context.py`, `pace_features.py`, `pace_examples.py`, `stint_observations.py`, `degradation_features.py`, `stint_examples.py`).
- `models`: input/output protocols, baselines, regularized additive models, hierarchical tire degradation models, online Bayesian stint-state filtering, and correlated trajectory sampling (`contracts.py`, `pace_baselines.py`, `pace_linear.py`, `pace_calibration.py`, `pace.py`, `degradation.py`, `degradation_linear.py`, `degradation_nonlinear.py`, `degradation_hierarchical.py`, `stint_state.py`, `trajectory_uncertainty.py`, `stint_forecast.py`).
- `simulator`: pure, deterministic-with-seed race transitions; calls models via
  interfaces and does not know which vendor supplied the data (`contracts.py`).
- `validation`: data-integrity checks, chronological race splits, leakage/truncation-invariance tests, probabilistic calibration, and open-loop stint backtesting (`quality.py`, `splits.py`, `leakage.py`, `calibration.py`, `backtest.py`, `stint_metrics.py`, `stint_backtest.py`).
- `dashboard`: presentation only, never secretly refits models on user clicks.

Raw and warehouse layers should remain independently rebuildable. A future
new model never requires re-ingesting the same race. Source schema changes
should be handled by provider-specific adapters.

## Dataset keys and uniqueness

- Event: OpenF1 `meeting_key`; race session: OpenF1 `session_key`.
- Race lap: `(session_key, driver_number, lap_number)`.
- `silver.sessions` attaches race season, circuit and regulation-era features.
- Tire stint: `(session_key, driver_number, stint_number)`.
- Pit visit: `(session_key, driver_number, date)` or stable ingestion row ID;
  never assume one pit visit per lap without checking.
- Team/driver persistent cross-provider IDs: maintain explicit identity mapping;
  `driver_number` is only unique **within a session** for our data model.

## Snapshot and model provenance

Eventually each experiment must record:
- source file hashes, source/provider/version, processing code Git SHA
- training race session IDs, training cutoff UTC
- hyperparameters, seed, feature schema version
- evaluation periods, predictions, observed outcomes, calibration metrics
- distinction between **forecast time** and **event time**

## Data layout decision

- HTTP JSON: `data/raw/openf1/year=YYYY/session_key=NNNN/endpoint.json`.
- Small typed analytical tables: `data/warehouse/f1ri.duckdb`, schemas `silver`
  and `gold`.
- High-frequency telemetry in later phase: partitioned Parquet by season,
  session, driver and bounded time windows; never store all car_data in one SQL
  table or one massive JSON blob.
- Learned models/evaluation outputs: `artifacts/*` and Git-tracked scripts.

## Stage 4 pit intelligence

`features.pit_events` owns historical visit reconstruction. `pit_context` creates
pre-cutoff hypothetical requests, and `pit_examples` attaches separate outcomes.
`validation.pit_measurement` owns counterfactual labels and their provenance;
these are distinct from authoritative measured lane and stationary durations.
`models.pit_loss.PitLossModelV1` is the simulator-facing interface over transit,
service, transition, neutralization and shared uncertainty components.
`simulator.pit` combines equal-distance clean trajectories with net loss once;
positions, queue formation and strategy selection remain future stages.
See [the Stage 4 contract](STAGE4_SPEC.md) and its evidence gates.

## Stage 5.1 pace integration

`features.race_state` rebuilds the existing lap context on an observation
prefix and emits one field snapshot with independent driver lap counts.
`simulator.pace_adapter` verifies and calls the saved Pace V2 bundle.
`simulator.pace_field` allocates per-driver seeds and schedules checkpoint
events from complete pace draws. The backtest attaches retrospective
clean-air labels after prediction. [Stage 5.1 specification](STAGE5_S1_SPEC.md)

## Stage 5.2 pit integration

`simulator.pit_execution` builds resolved and coarse itineraries for the
shared checkpoint scheduler. The race engine commits route changes and tire
fits. `simulator.pit_pace` preserves S1's old-stint prefix and uses the
frozen D8 replacement request for subsequent laps. The optional P1 loader
consumes only ex-ante features and supplies a provisional net cost.
[Stage 5.2 specification](STAGE5_S2_SPEC.md)
