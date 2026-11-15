# F1 Race Intelligence Engine — Stage 6 S6-B.1
## Timing boundaries and pit commitment

**`PATH_A_TIMING_ROBUSTNESS_AND_POLICY_SEMANTICS_VALIDATED`**

The 46.754185 s failure reproduced exactly. It is expected behavior for the frozen commitment rule. The policy observed the Safety Car before requesting lap 32, but the stop stayed pending after GREEN. The fixed lap-31 stop entered before deployment. No scheduling, geometry or forecast-support defect was needed to explain the original result.

## Replay at microsecond precision

| Event | Fixed lap 31 (s from cutoff) | Original adaptive (s from cutoff) |
| --- | ---: | ---: |
| Decision | 0.000000 | 58.000000 |
| Entry / irreversible route transition | 29.967958 | 172.226035 |
| Service starts | 51.967958 | 194.226035 |
| Tyre fitted / service completes | 54.967958 | 197.226035 |
| Pit exit | 62.967958 | 205.226035 |
| Rejoin | 86.967958 | 229.226035 |

SC committed at +58.000000 s and GREEN at +150.000000 s. The original entered 22.226035 s after GREEN. Both pit routes took 57.000000 s with no queue wait. They entered from GREEN and the same soft set was fitted. The common checkpoint table tracks where their timing paths separated.

| Matched checkpoint | Adaptive minus fixed (s) |
| ---: | ---: |
| 92 | +0.000000 |
| 93 | -9.585828 |
| 94 | +19.668319 |
| 95 | +40.219120 |
| 96 | +50.668096 |
| 97 | +46.867756 |
| 98 | +46.854102 |
| 99 | +46.840448 |
| 100 | +46.827793 |
| 101 | +46.815140 |
| 102 | +46.802485 |
| 103 | +46.790344 |
| 104 | +46.778203 |
| 105 | +46.766062 |
| 106 | +46.754185 |

## Alternatives and physical commitment

The scheduling layer owns the pit route. An instruction stays revisable while it is pending and the car is strictly before the geometric line. The SC and VSC fixtures verified cancellation one microsecond before entry and preserved entry at equality and one microsecond after. Race control commits before dynamic entry at a shared clock. The geometric guard still refuses revision at equality. The VSC-ending signal leaves the pending instruction in place until GREEN commits.

| Policy | Delta in sc-deploy-58 (s) | Worst regret on expanded stress family (s) |
| --- | ---: | ---: |
| Fixed lap 31 | -41.492159 | 13.518744 |
| P0 original | +5.262026 | 46.754185 |
| P2 replan | +5.979429 | 47.471588 |
| P2 stay out | +0.000000 | 41.492159 |
| P5 scheduled | -40.626300 | 13.518744 |

The stay-out variant discarded the missed stop under GREEN. It avoided the original +5.262026 s loss against the stay-out reference but could not recover the earlier lap-31 opportunity. The one-revision demonstration canceled lap 32 and later executed a new lap-33 instruction. Replanning incurred more regret in this case. The original and active-at-decision control use the same live trigger and provide an intentional behavior check.

## Declared family and boundary coverage

| Family | Winners or tied policies | Supported / expected cells |
| --- | --- | ---: |
| A-primary | adaptive-63-hard-or-soft, adaptive-63-medium-or-soft, adaptive-63-used-soft-or-soft, p1-active-at-decision, p2-green-replan, p2-green-stay-out, p4-early-window | 252/252 |
| B-stress | 63-L31-hard, 63-L31-medium, 63-L31-soft, 63-L31-used-soft, p5-scheduled-conditional | 462/462 |
| D-gap | after-sc-63-hard, after-sc-63-medium, after-sc-63-soft, after-sc-63-used-soft, reference, sc-63-hard, sc-63-medium, sc-63-soft, sc-63-used-soft, vsc-63-hard, vsc-63-medium, vsc-63-soft, vsc-63-used-soft | 84/84 |
| D-opponent | after-sc-63-hard, after-sc-63-medium, after-sc-63-soft, after-sc-63-used-soft, reference, sc-63-hard, sc-63-medium, sc-63-soft, sc-63-used-soft, vsc-63-hard, vsc-63-medium, vsc-63-soft, vsc-63-used-soft | 168/168 |
| D-shared-box | 4-L31-soft__63-L31-soft | 184/184 |
| D-clean-air | adaptive-63-soft-or-hard, adaptive-63-soft-or-medium, adaptive-63-soft-or-used-soft | 252/252 |
| D-wider-gap | sc-63-soft | 252/252 |
| D-original-opponent | adaptive-63-soft-or-hard, adaptive-63-soft-or-medium, adaptive-63-soft-or-used-soft | 252/252 |
| E-wider-gap-stress | reference, vsc-63-hard, vsc-63-medium, vsc-63-soft, vsc-63-used-soft | 126/126 |
| C-duration | 63-L31-soft | 336/336 |
| H3 | 63-L31-soft | 84/84 |
| H5 | 63-L31-soft | 84/84 |
| H10 | 63-L31-soft | 84/84 |
| C-sc-boundary | 63-L31-soft | 420/420 |
| C-vsc-boundary | 63-L31-hard, 63-L31-medium, 63-L31-soft, 63-L31-used-soft, p5-scheduled-conditional | 420/420 |
| C-exact-event-order | 63-L31-soft, p5-scheduled-conditional | 462/462 |

The original 37-policy comparator was replayed separately for the primary and stress families. Their expanded 42-policy comparator admits the five new variants. Its oracle also includes the S6-A schedule-informed review clocks. Other families record their own candidate counts. Regret is calculated within each version. Narrowed intervals bracket changes observed at tested endpoints and do not establish continuity or exhaustive timing coverage.

| Flag | Refined deployment bracket (s) | Width (s) |
| --- | --- | ---: |
| SC | 13.625000 to 16.500000 | 2.875000 |
| SC | 28.000000 to 31.750000 | 3.750000 |
| VSC | 13.625000 to 16.500000 | 2.875000 |
| VSC | 28.000000 to 31.750000 | 3.750000 |

## Recommendation support

`ROBUST_WITHIN_DECLARED_FAMILY`, `FAMILY_SENSITIVE`, `BOUNDARY_SENSITIVE`, `NEAR_EQUAL_ALTERNATIVES`, `CONSTRAINT_DOMINATED_IDENTIFIABILITY`, `NO_UNIQUE_RECOMMENDATION`.

The primary following fixture can tie materially different tyre actions. The clean-air, gap and opponent controls preserve that distinction. Horizons have separate common markers and regret benchmarks. Only two archived, cutoff-valid model contexts support the declared multi-car field. No independent pit-closure or historical announcement-latency model was used.

S6-C can use the timing-risk contract and show recommendation scope, while withholding a unique action where the family or support diagnostics require it. This stage keeps the D8R1 mean-only release and assigns no neutralization probabilities.

`f1ri stage6-s6-b1 --output artifacts/runs/stage6_s6_b1_reproduced` recreates the study.

## Release verification

All 380 tests passed, including the 373-test baseline. Ruff passed for source and tests. The two independent S6-B.1 runs matched all 26 files. The post-change S6-B replay matched all 29 frozen deterministic files. The completed matrix ran 4,546 evaluations across 16 families with zero physical invariant failures. The Streamlit Timing risk panel displayed the stress replay and withheld a unique recommendation.

[Evidence archive](stage6_s6_b1_ledgers.zip) · [Diagnostic overview](stage6_s6_b1_diagnostics.png) · [Final metrics](stage6_s6_b1_metrics.json)
