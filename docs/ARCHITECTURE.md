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
- `ingestion`: knows provider URLs and rate limits; returns source bytes only (`openf1.py`, `raw_cache.py`, `batch.py`).
- `warehouse`: owns stable typed silver and gold structures; never predicts (`schema.py`, `build.py`).
- `features`: feature builders, track-status state machine, point-in-time forecast examples, and quality flags; no future data (`lap_context.py`, `pace_features.py`, `pace_examples.py`).
- `models`: input/output protocols, baselines, regularized additive models, and residual interval calibration (`contracts.py`, `pace_baselines.py`, `pace_linear.py`, `pace_calibration.py`, `pace.py`).
- `simulator`: pure, deterministic-with-seed race transitions; calls models via
  interfaces and does not know which vendor supplied the data.
- `validation`: data-integrity checks, chronological race splits, leakage/truncation-invariance tests, and probabilistic calibration (`quality.py`, `splits.py`, `leakage.py`, `calibration.py`, `backtest.py`).
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
