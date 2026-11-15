# F1 Race Intelligence Engine — Stage 6 S6-B
## Robust strategy under declared futures

**`PATH_A_ROBUST_OPTIMIZATION_VALIDATED`**

The study evaluated 2,652 branches across 15 families. Every policy in a family runs from the same decision cutoff and targets the same common marker. The initial-gap family declares alternative physical positions explicitly. The primary family uses the Singapore 2025 saved model context and a declared two-car field with a slower predecessor.

## Primary minimax-regret policy

3 policies tied at a worst-case regret of +0.000 s and a worst reference-relative loss of +0.000 s. The GREEN fallback tyre changes the action while following equalizes the marker time. Worst reference-relative cases · green.
- **adaptive-hard-or-soft** · Before the decision deadline, take soft at the next valid entry on laps 31–33 if VSC or SC is active and deployment has been committed. If GREEN continues until lap 33 is the next pit opportunity, take hard.
- **adaptive-medium-or-soft** · Before the decision deadline, take soft at the next valid entry on laps 31–33 if VSC or SC is active and deployment has been committed. If GREEN continues until lap 33 is the next pit opportunity, take medium.
- **adaptive-used-soft-or-soft** · Before the decision deadline, take soft at the next valid entry on laps 31–33 if VSC or SC is active and deployment has been committed. If GREEN continues until lap 33 is the next pit opportunity, take used-soft.

The adaptive policy can react to committed VSC or Safety Car deployment until 120 seconds after cutoff, within its declared pit window. Inactive triggers and fallback decisions are visible in the ledger. Exact ties retain different actions where applicable.

| Future | Declared assumption | Realized stop | Δ vs stay out (s) | Best-realized regret (s) | Oracle regret (s) |
| --- | --- | --- | ---: | ---: | ---: |
| green | Continue under GREEN | L33 hard | +0.000 | +0.000 | +0.000 |
| early-vsc | VSC deployment +5 s and green +80 s | L31 soft | -23.039 | +0.000 | +0.000 |
| late-vsc | VSC deployment +89 s and green +164 s | L32 soft | -13.519 | +0.000 | +0.000 |
| early-sc | SC deployment +5 s, green +97 s | L31 soft | -14.721 | +0.000 | +0.000 |
| longer-sc | Same SC deployment, green delayed 35 s | L31 soft | -32.718 | +0.000 | +0.000 |
| restart-pace | Identical SC clocks and 10% higher declared restart speed | L31 soft | -13.952 | +0.000 | +0.000 |

Near-equal alternatives under the prespecified 1.44 s margin · adaptive-hard-or-medium, adaptive-hard-or-used-soft, adaptive-medium-or-hard, adaptive-medium-or-used-soft, adaptive-soft-or-hard, adaptive-soft-or-medium, adaptive-soft-or-used-soft, adaptive-used-soft-or-hard, adaptive-used-soft-or-medium
The margin is a model-error reporting guideline. Exact minimax arithmetic and scenario-wise dominance use unrounded simulated microseconds.

## Comparator and search scope

Best-realized regret compares against the fastest valid execution from the fixed library in each future. The perfect-information comparator additionally searches S6-A review clocks chosen using the declared future script. Both benchmarks are bounded. The primary library is evaluated exhaustively and every primary oracle search completes.

No scenario probabilities or uncalibrated D8R1 intervals enter these decisions. A separate declared-weight demonstration normalizes supplied weights. Incomplete searches preserve observed deltas and withhold exact minimax rankings and regret.

## Sensitivity and team decisions

| Family | Selected policy or ties | Coverage |
| --- | --- | --- |
| neutralization-timing | L31-hard, L31-medium, L31-soft, L31-used-soft | 407/407 |
| pit-entry-boundary | L31-soft | 111/111 |
| initial-gap | Stay out | 74/74 |
| future-traffic | Stay out | 148/148 |
| separate-clean-field | vsc-soft | 111/111 |
| pace-and-route-sensitivity | Stay out | 333/333 |
| horizon-3 | adaptive-medium-or-soft | 68/68 |
| horizon-5 | adaptive-medium-or-soft | 68/68 |
| horizon-10 | adaptive-medium-or-soft | 68/68 |
| team-combined | 4-L31-soft + L31-soft | 164/164 |
| team-lead | 4-L31-medium + L31-soft, 4-L31-soft + L31-soft | 164/164 |
| team-weighted | 4-L31-soft + L31-soft | 164/164 |
| budget-limited | Withheld | 12/222 |

The timing sweep varies deployment, duration and control pace with the original scheduler. The boundary test brackets the earlier S6-A GREEN entry clock by one microsecond on each side. The wider gap restores a tyre difference suppressed by following in the close-gap case. The scripted pass is an authorized deterministic sensitivity.

H3, H5 and H10 use the same policy definitions on separate common-marker matrices. Planned stops beyond the marker are labeled as unexecuted prefixes. A pending in-window stop that fails to complete remains unsupported. Team results retain each driver's elapsed delta, committed arrival and service order, wait and rejoin in the audit ledger.

## Verification and reproduction

The study recorded 0 physical invariant failures. The single-future run reproduced S6-A marker outcomes and event traces. Reversing the policy and scenario orders and adding policies or cases preserved earlier executions. The cache parity fixture used 18 calls with caching and 22 without, with equal traces.

`f1ri stage6-s6-b --output artifacts/runs/stage6_s6_b_reproduced` runs the declared matrix. The evidence archive includes the protocol, realized decision and pit ledgers, forecast provenance, both regret comparators, dominance, near-equality diagnostics and checksums.

This release is limited to 2025 declared conditions and the D8R1 mean forecast gate. Historical field reconstruction, neutralization pace calibration and probabilistic outcomes remain outside the release. S6-C and the separate Stage 3-R.1 uncertainty study can proceed.

## The choice changes when the timing grid widens

The medium-under-GREEN, soft-under-neutralization policy matched the bounded hindsight benchmark in all 6 primary futures. On the separately declared timing grid, its worst regret rose to 46.754 s when the Safety Car deployed 58 seconds after cutoff. It committed to lap 32 on the announcement and entered after GREEN returned. The fixed lap-31 soft stop entered before deployment. The fixed lap-31 policies tied for lowest worst regret on that grid at 13.519 s. None of these timings carries a scenario probability.

## Release verification

The 25 executed fixture checks passed. B26 matched all 29 files across two separate full runs. The post-change S6-A replay matched all 21 frozen deterministic files. S5-C and S5-C.1 metrics reproduced byte for byte. All 373 tests passed. Ruff passed for source and tests, with ten existing import-order findings in archived notebooks. The dashboard smoke test verified a complete strategy search and withheld the recommendation when a 12-evaluation budget truncated the matrix.

[Evidence archive](stage6_s6_b_ledgers.zip) · [Diagnostic overview](stage6_s6_b_diagnostics.png) · [Final metrics](stage6_s6_b_metrics.json)
