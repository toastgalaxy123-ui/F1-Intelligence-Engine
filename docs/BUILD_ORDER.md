# Incremental build order, with exit criteria

## Stage 0 — Repo and one race (1-2 work sessions)
- Install project; choose 2025 Singapore (or another completed DRY GP).
- `f1ri discover`, `ingest`, `build`, `check`.
- Verify raw source session keys and column contracts.
- Inspect lap-by-lap plots and spot-check 2-3 drivers manually.
**Done when:** commands are rerunnable; no duplicate lap keys; raw snapshots
are unchanged without `--refresh`; a full race is inspectable in DuckDB.

## Stage 1 — Source-cleaning and track-status labeling
- Implement race_control event -> continuous state intervals (green/yellow,
  SC/VSC, red, unknown), with unit tests per transition.
- Map interval overlaps to individual laps; don't mark partial SC laps clean.
- Infer pit-in and pit-out with cross-check of lap/pit/stint records.
- Join positions/intervals *as of* each lap timestamp for traffic estimates.
- Create `gold.lap_context` with reasons for each exclusion, and plots.
**Done when:** conservative green-flag pace laps can be identified and
verified by manually inspecting several dry and disrupted races.

## Stage 2 — Validated lap-pace baseline (COMPLETED — see `docs/STAGE2_SPEC.md` and `artifacts/reports/stage2_validation.md`)
- Train on prior races (2023-2024); validate on early 2025; test on held-out late 2025.
- Baselines (`E0`-`E3`): historical circuit prior, recent clean-lap median, compound-specific stint median, race-progress drift.
- Regularized additive model (`E4`-`E8` `PaceModelV1`): race-progress trend + compound-specific tire-age effects + track temp + empirical-Bayes online pace adaptation + calibrated 50%/90% intervals.
- Report race-weighted and lap-weighted MAE, RMSE, bias, CRPS, 50%/90% coverage, sliced residuals, and 2026 transfer/adaptation (`E9`-`E10`).
**Done when:** at least one model beats simple baselines on held-out races
and uncertainty/residual diagnostics are acceptable.

## Stage 3 — Tire degradation curves
- Fit partial-pooling degradation by compound, driver/team, track,
  temperature; allow nonlinear wear and tire warmup.
- Explicitly confront confounding with fuel/traffic/stint selection.
- Predict pace on unseen stints; evaluate against observed green laps.
**Done when:** prediction beats a constant-pace baseline on unseen stints;
plot credible sensitivity to race and temperature.

## Stage 4 — Pit-loss and stop timing
- Infer net pit-loss vs driving at normal racing speed. Separate
  `lane_duration` and `stop_duration` from net race-time loss.
- Study traffic on pit exit and safety-car savings.
- Fit stop hazard as distinct predictive component if desired.
**Done when:** counterfactual stop costs are plausible on held-out events.

## Stage 5 — Race-state reconstruction + simple simulator
- Build second-by-second or lap-boundary state from position/interval/race
  control; handle lapped cars and retirements explicitly.
- Start with deterministic dry green-flag race simulations.
- Add pit windows, traffic, passing and stochastic lap-time distributions.
- Introduce SC/VSC and retirement hazards after base engine passes tests.
**Done when:** replayed historical races have credible error/calibration,
with non-physical outcomes caught by invariant tests.

## Stage 6 — Strategy engine + uncertainty
- Compare a finite candidate set of one- or two-stop strategies.
- Run reproducible Monte Carlo with common random numbers for comparison.
- Return position probability, expected finish, variance and risk.
- Backtest simulator calibration, do NOT claim real counterfactual labels.
**Done when:** differences are stable across random seeds and sensitive to
reasonable model uncertainty; show if confidence is too low.

## Stage 7 — Dashboard, live option, new regulation era
- Add user-facing race replay from saved historical snapshots first.
- Build a live ingestion adapter only if legal access/latency permit.
- Version every model; move to 2026+ training with regime-aware updating.
**Done when:** you can reconstruct a race at lap 20 without reading post-lap-20
observations, and show calibrated predictive distributions.

### Do not build early

High-frequency telemetry warehouse, neural nets, F1 TV integration, large
React front-end, microservices, automated scraping, or a live betting tool.
