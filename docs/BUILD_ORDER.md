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

## Stage 3 — Tire degradation curves & future stint simulation (COMPLETED — see `docs/STAGE3_SPEC.md` and `artifacts/reports/stage3_validation.md`)
- Expand warehouse cohort to 32 races across 11 circuits (`2023–2026`), including 4 brand-new untouched 2025 test races (`9955`, `9928`, `9888`, `9839`).
- Populate `silver.compound_allocations` (Pirelli `C1`–`C6` by era), `gold.stint_observations`, `gold.stint_lap_observations`, and `gold.stint_training_examples` ($H \in \{1, 3, 5, 10, 15, 20\}$).
- Fit partial-pooling degradation by compound (`C1`–`C6` and relative), circuit, and temperature with three-phase nonlinear wear (`NonlinearTireDegradationModel`, `HierarchicalTireDegradationModel`).
- Separate fuel-burn progression from tire wear jointly; update latent stint state $(b_s, d_s)$ online via `BayesianStintStateEstimator` with pit-stop state reset.
- Sample correlated multi-lap trajectories (`CorrelatedTrajectorySampler`) and evaluate open-loop cumulative stint forecasts (`D0`–`D10`) against `B0` (`E2`) and `B4`.
**Done when:** `PaceModelV2` (`D8`) beats `D0` (frozen `PaceV1`) and `B0` (`E2`) on held-out multi-lap and cumulative stint forecasts with calibrated 50%/90% trajectory coverage.

## Stage 4 — Pit-loss and stop timing
- Infer net pit-loss vs driving at normal racing speed. Separate
  `lane_duration` and `stop_duration` from net race-time loss.
- Study traffic on pit exit and safety-car savings.
- Fit stop hazard as distinct predictive component if desired.
**Done when:** counterfactual stop costs are plausible on held-out events.

## Stage 5 — Multi-car race simulation and probabilistic forecasting
- S0 implements race state, a deterministic checkpoint engine, and historical
  replay. See [the specification](STAGE5_SPEC.md) and
  [Singapore and Imola audits](../artifacts/reports/stage5_replay.md).
- S1 integrates correlated Pace V2 trajectories and checks parity, snapshot
  leakage, and historical clean-air performance. [Stage 5.1 validation](../artifacts/reports/stage5_s1_validation.md)
- S2 gate A requires synthetic two-lap conservation and stale-checkpoint
  invalidation. Gate B requires fresh/used D8 parity before using P1. Gate C
  requires supported synthetic merges and exclusive box service before
  interpreting physical rejoin order. [Stage 5.2 specification](STAGE5_S2_SPEC.md)
- Run the locked P1 artifact before interpreting provisional model-based pit
  costs.
- S3 adds bounded physical reconstruction, circular no-pass scenarios, and a
  synthetic S2 rejoin adapter. Traffic-loss fitting requires independent
  episode labels and held-out races. [Stage 5.3 specification](STAGE5_S3_SPEC.md)
- S3-C freezes the Singapore pilot, validates localization across circuits
  and constructs out-of-sample clean-air references and following targets.
  Compare T0–T3 on common whole-race splits when label support permits.
  Validate traversal-level predictions and dynamic pit and queue transitions
  before general simulation integration. [S3-C protocol](STAGE5_S3_C_PROTOCOL.md)
- Use the paired field-coverage replay and bounded Hungary extension to audit
  following targets and earlier controls before selecting Traffic V1.
  [S3-C coverage results](../artifacts/reports/stage5_s3_c_coverage_report.md)
- Backtest complete race probabilities against simple baselines with frozen,
  chronological cohorts and evaluate 2026 transfer separately.
**Done when:** the mechanical invariants pass, and forecasting capability
and calibration are established for a documented support domain.

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

## Stage 4 implementation and approval

The Stage 4 research pipeline is implemented in the existing package boundaries:
contract → event audit → reference provenance → cross-fitted measurements →
chronological baselines → component models → calibration → time accounting.
Run `f1ri stage4 --config config/experiments/stage4_pit_loss_v1.yaml` after its
cohort ingestion. Bounded telemetry auditing is optional with
`f1ri pit-telemetry-audit`; it is not a full-session telemetry warehouse.

See `artifacts/reports/stage4_validation.md` for measured results and outstanding
approval gates. Software readiness does not establish model superiority, verify
unsourced historical pit speed limits, or make SC queue effects supported.
Stage 5/6 implementation must preserve these limits and the frozen Stage 3 APIs.
