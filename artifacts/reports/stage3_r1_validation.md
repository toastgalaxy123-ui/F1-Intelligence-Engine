# F1 Race Intelligence Engine — Stage 3-R
## Pace V2/D8 reconstruction and strategy validation

**Decision: `PATH_B_MODEL_FITTED_WITH_LIMITS`. Release: `MEAN_ONLY_VALIDATED`.**

The original fitted D8 pickle and matching warehouse could not be recovered in the available workspace, Git history or local archives. The surviving source, experiment configuration and earlier scorecards remain the reference for method and scope. Any fitted `PaceV2_D8R1` is a versioned successor trained on reacquired inputs (reconstruction outcome C).

## Sources and cohort

OpenF1 supplied 297 race-endpoint snapshots within the frozen acquisition window. 26/27 races passed the warehouse checks. The retained cohort has 16 training, 4 calibration and 6 test races, including 4 marked historically uninspected. Session roles and prior inspection flags come from the hash-verified Stage 3 configuration. The five declared 2026 transfer sessions retained their roles and were outside the acquisition scope.

Across the ready races, the warehouse retained 30,044 stint lap rows, 12,443 clean green laps and 1,072 stints with clean laps, across 12 named circuits and three seasons.

- 2024 Japanese GP (9496): `QUALITY_FAILED`. No eligible clean-air laps
  The run applies the frozen track-status and clean-air filters to every race.

Empty pit streams (2023 Bahrain GP, 2023 Saudi Arabian GP) use the existing stint boundary and pit-out masks.

Whole-race train/calibration/test clocks are checked for chronology. The 2-second clean-air mask uses the existing lap context and green-flag rules. Forecasts use completed-lap history, pre-race grid and qualifying features, and the last weather observation at or before the cutoff. Historical feed receipt times are unavailable. Full lead/lag trajectories are retained as targets.

## Held-out pace

Errors below are race-weighted MAE in seconds. Models scored the same eligible opportunities.

| Estimator | All test H1 lap | All test H5 total | Historically uninspected H1 lap | Historically uninspected H5 total |
| --- | ---: | ---: | ---: | ---: |
| E2 compound median | 0.337 | 1.629 | 0.363 | 1.768 |
| Adaptive reset and progress | 0.327 | 1.604 | 0.350 | 1.717 |
| Pace V1 definition refit | 0.312 | 1.465 | 0.324 | 1.524 |
| Nonlinear degradation | 0.323 | 1.548 | 0.338 | 1.589 |
| Hierarchical degradation | 0.319 | 1.498 | 0.342 | 1.607 |
| PaceV2_D8R1 | 0.308 | 1.433 | 0.331 | 1.554 |

### D8R1 open-loop continuation

| Laps | Clean endpoints | Lap MAE (s) | Complete clean trajectories | Races | Total MAE (s) | CRPS (s) | 50% | 90% |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 934 | 0.308 | 934 | 6 | 0.308 | 0.234 | 61.0% | 90.5% |
| 3 | 858 | 0.455 | 633 | 6 | 0.904 | 0.683 | 57.8% | 87.4% |
| 5 | 815 | 0.568 | 457 | 6 | 1.433 | 1.047 | 52.9% | 86.2% |
| 10 | 670 | 0.722 | 212 | 6 | 3.537 | 2.508 | 42.4% | 78.8% |
| 15 | 475 | 0.897 | 77 | 6 | 5.730 | 4.418 | 31.2% | 72.7% |
| 20 | 343 | 1.069 | 22 | 4 | 13.698 | 9.250 | 13.6% | 45.5% |

Scores condition on observed clean endpoints or fully clean trajectories. Censoring removes interrupted stints from long-horizon scoring. The row count therefore overstates independent evidence. Each race contributes one unit to race-weighted MAE. Coverage and Gaussian CRPS pool eligible forecasts. Per-race calibration and the censoring breakdown are in the ledger.

The original D8 report recorded pooled H1 lap MAE of 0.321 s, H5 cumulative MAE of 1.516 s, and H5/H10 90% coverage of 87.1%/80.9%. The new run uses reacquired observations and every third cutoff lap. Those original values provide historical context only.

## Online adaptation

| History regime | Eligible H1 forecasts | Full model race MAE (s) | Without stint state (s) |
| --- | ---: | ---: | ---: |
| 0A_no_prior_clean_race_laps | 38 | 1.417 | 1.417 |
| 0B_first_clean_lap_of_new_stint_with_race_history | 35 | 0.748 | 1.622 |
| 1_2_early_stint | 140 | 0.297 | 1.633 |
| 3_plus_warm_stint | 721 | 0.228 | 1.674 |

## Replacement forecasts and physical strategy

Singapore 2025 supplies the saved cutoff features for drivers 63 and 4. The field, pit path, shared box, tyre inventory and race-control interventions are declared scenarios. Exact historical field and rejoin geometry remain unsupported. Replacement validation in the historical ledger conditions on the set used. The counterfactual exercise supplies fresh hard, medium and soft sets and a used soft set with three laps.

Standalone requests with numeric integrity: 14/14. Frozen weather prior to cutoff: True.

Paired draws use a common initial continuation and shared innovations for alternatives at the same stop clock key, driver, stint ordinal and horizon. Separate drivers and stint starts have separate streams. The fitted sampler represents within-stint dependence.

Identical policies reproduced exactly: True. Reordering candidates preserved the same draw: True. Unrelated driver's continuation preserved: True. Distinct streams on the two-stop path: 3.
The hard and medium replacements at the same stop used the same replacement draw key.

The hard and soft stops reached the timing marker 0.714 s apart in clean air, 0.000 s apart behind the declared slower car, and 0.714 s apart after its retirement. The slower car's saved clean-lap observations were increased by four seconds for this sensitivity test.
The double stack incurred a 2.904 s box wait.

| Scenario | Strategy | Δ vs continuation (s) | Pit visits |
| --- | --- | ---: | ---: |
| clean-air | hard | 5.973 | 1 |
| clean-air | identical-medium | 5.969 | 1 |
| clean-air | later | 6.001 | 1 |
| clean-air | medium | 5.969 | 1 |
| clean-air | soft | 5.258 | 1 |
| clean-air | two-stop | 12.156 | 2 |
| clean-air | used-soft | 5.585 | 1 |
| two-car | hard | 0.000 | 1 |
| two-car | identical-medium | 0.000 | 1 |
| two-car | later | 0.000 | 1 |
| two-car | medium | 0.000 | 1 |
| two-car | soft | 0.000 | 1 |
| two-car | two-stop | 0.000 | 2 |
| two-car | used-soft | 0.000 | 1 |
| retirement | hard | 1.352 | 1 |
| retirement | identical-medium | 1.349 | 1 |
| retirement | later | 5.214 | 1 |
| retirement | medium | 1.349 | 1 |
| retirement | soft | 0.638 | 1 |
| retirement | two-stop | 7.536 | 2 |
| retirement | used-soft | 0.965 | 1 |
| vsc | medium | -19.541 | 1 |
| vsc | react | -19.541 | 1 |
| sc | medium | -13.780 | 1 |
| sc | react | -13.780 | 1 |
| shared-box | double-stack | 5.073 | 2 |
| shared-box | stagger | -6.190 | 2 |
| wider-gap | hard | 3.839 | 1 |
| wider-gap | medium | 3.836 | 1 |
| original-opponent | hard | 5.973 | 1 |
| original-opponent | medium | 5.969 | 1 |

These paired simulator times are conditional on the declared field. Headway, pit merging and neutralization can erase clean-air differences. The exercise establishes fixture-level behavior. Race-finishing probabilities, joint driver uncertainty and event probabilities remain out of scope.

## Release gates

| Gate | Result |
| --- | --- |
| G0 data provenance | PASS |
| G1 numerical integrity | PASS |
| G2 mean quality | PASS |
| G3 uncertainty quality | FAIL |
| G4 replacement forecasting | PASS |
| G5 strategy integration | PASS |
| G6 reproducibility | PASS |

G3 required observed coverage of at least 40% for the 50% interval and 80% for the 90% interval at H5, H10, H15 and H20.

Independent refit from the recorded feature table repeated 11,702 prediction rows. Exact table equality: True. Fitted byte digest matched: True.

## Reproduction and S6 scope

Run `f1ri stage3-r1 --acquire` to recreate the bounded source cache. Run `f1ri stage3-r1 --output artifacts/runs/stage3_r1_reproduced` against that cache. OpenF1 may revise its response bodies, so compare the source manifest before interpreting numerical differences across pulls. The evidence archive includes the input feature table, predictions, source ledger, release gates and per-file checksums. Extract it and run `f1ri stage3-r1 --verify-run <extracted-folder>` with the versions in the training manifest. The recorded run uses single-threaded BLAS and `PYTHONHASHSEED=42`.

Stage 3 source hashes and frozen S5 metrics were verified. The 2026 regulation transfer remains unmeasured.
The mean-only release supports S6 comparisons within declared race scenarios. Probabilistic S6 needs horizon-aware recalibration on the calibration cohort and a further independent test of cumulative coverage, along with its own event and finishing-outcome evidence.

## Verification

The retained clean-air lap population covers 28 unique drivers across 3 seasons. A second warehouse rebuild reproduced the population, feature table, held-out forecasts, baseline scores and source manifest byte for byte. The full suite passed 348 tests, including the 339 original tests. Ruff passed for source and tests. Ten existing import-order findings remain in the archived notebooks. The rerun S5-C.1 metrics matched the frozen SHA-256.

[Evidence archive](stage3_r1_pace_reconstruction.zip) · [Diagnostic overview](stage3_r1_diagnostics.png) · [Release manifest](stage3_r1_model_release.json)
