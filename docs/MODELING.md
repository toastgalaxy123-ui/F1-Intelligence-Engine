# Modeling design and backtest protocols

## Recommended pace decomposition

Observed lap_time = latent car/driver/circuit pace + fuel-burn trend +
compound/tyre-age degradation + weather/track evolution + traffic/flags +
noise. Some terms are observationally confounded; restrict claims accordingly.

Start with low-parameter mixed effects or regularized regressions. A baseline
should ALWAYS be assessed before CatBoost or Bayesian model. Use hierarchical
shrinkage so short stints do not produce enormous tire-wear estimates.

## First validation split

- Train: races earlier in time, e.g. 2023-2024.
- Validation: a later block, e.g. early/mid 2025.
- Final untouched test: later 2025.
- 2026: distinct regime; keep separate results and examine adaptation.
- Each whole GP is one split unit. Split by session/circuit/year in reports.
- For live-style forecasts, each prediction has a timestamp cutoff; never
  train on the target event or later information.

## Metrics

Pace model: MAE, RMSE, bias by driver/compound/track/age/race phase.
Probabilistic pace: coverage and sharpness of 50/90% prediction intervals,
plus CRPS or negative log likelihood where distribution assumptions fit.
Pit model: event probability Brier/log loss and stop timing error.
Race ranking model: distribution over finish positions, ranked probability
score, winner/podium calibration; inspect driver-level calibration.
Strategy: paired Monte Carlo estimates with common random numbers,
confidence intervals, sensitivity to assumptions; not a naive 'hit rate'
against unobserved alternative strategies.

## Reproducibility

Record Git revision, raw response file SHA256s, exact train/val/test sessions,
features and their as-of times, fitted parameters, random seed and software
versions. Train only on available historical data at each decision cutoff.

## 2026 warning

The 2026 regulation change affects aero and energy deployment. A model trained
on 2023-25 may transfer poorly, especially in passing behavior. ## Stage 2 Implementation (`STAGE2_SPEC.md`)

See `docs/STAGE2_SPEC.md` for the full Stage 2 specification, mathematical decomposition, controlled experiment matrix (`E0`–`E10`), and validation protocol.
- Primary selection metric: **race-weighted next-lap MAE** on chronological validation races (`val`), with secondary evaluation on held-out test (`test`, reporting both pooled and untouched test races) and 2026 regulation transfer (`transfer_2026`).
- All models consume `PaceRequest` and return `PaceDistribution` (`expected_seconds`, `std_seconds`, `median_seconds`, 50% PI `[p25_seconds, p75_seconds]`, and 90% PI `[p05_seconds, p95_seconds]`).

## Stage 3 Implementation (`STAGE3_SPEC.md`)

See `docs/STAGE3_SPEC.md` for the full Stage 3 specification, 3-component non-double-counted decomposition (`PaceModelV2`), hierarchical compound/circuit pooling (`HierarchicalTireDegradationModel`), online Bayesian stint-state filter (`BayesianStintStateEstimator`), correlated trajectory uncertainty sampler (`CorrelatedTrajectorySampler`), and open-loop evaluation across $H \in \{1, 3, 5, 10, 15, 20\}$.
