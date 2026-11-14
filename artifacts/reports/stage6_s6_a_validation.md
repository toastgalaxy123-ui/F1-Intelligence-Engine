# F1 Race Intelligence Engine — Stage 6 S6-A
## Conditional strategy optimization

**`PATH_A_CONDITIONAL_OPTIMIZATION_VALIDATED`**

Bounded enumeration executed 856 strategy branches across 27 declared scenarios and sensitivities using frozen PaceV2_D8R1 means. The S5-C scheduler resolved actual entry, service, rejoin, following and neutralization. The Singapore 2025 cutoff supplies model features. The track positions, resources, routes, opponent variations and control schedules come from declared scenarios. A midlap cutoff can require the next lap's forecast to complete the final partial sector.

## Decisions and scenario outcomes

A point-in-time choice uses the events known at the cutoff. The same candidates also run through the full declared script to show what happens if those events occur. Reactive policies see committed announcements as they arrive. Perfect-foresight runs make future scripted intervention clocks available for candidate review and are labeled separately.

| Scenario | Point-in-time best or oracle best | Best in declared script | Best delta vs reference (s) | Search |
| --- | --- | --- | ---: | --- |
| clean-air | stay out, after-sc-hard, after-sc-medium (+10 exact ties) | stay out, after-sc-hard, after-sc-medium (+10 exact ties) | +0.000 | EXHAUSTED_LEGAL_SPACE |
| two-car | stay out, L31-hard, L31-medium (+18 exact ties) | stay out, L31-hard, L31-medium (+18 exact ties) | +0.000 | EXHAUSTED_LEGAL_SPACE |
| retirement | stay out, L31-hard, L31-medium (+18 exact ties) | stay out, after-sc-hard, after-sc-medium (+10 exact ties) | +0.000 | EXHAUSTED_LEGAL_SPACE |
| vsc | stay out, L31-hard, L31-medium (+18 exact ties) | L31-soft, vsc-soft | -23.039 | EXHAUSTED_LEGAL_SPACE |
| sc | stay out, L31-hard, L31-medium (+18 exact ties) | L31-soft, sc-soft | -14.721 | EXHAUSTED_LEGAL_SPACE |
| late-vsc | stay out, after-sc-hard, after-sc-medium (+10 exact ties) | L32-soft, vsc-soft | -0.747 | EXHAUSTED_LEGAL_SPACE |
| shared-box | 4-L30-soft | 4-L30-soft | -11.937 | EXHAUSTED_LEGAL_SPACE |
| two-stop | stay out, after-sc-hard, after-sc-medium (+7 exact ties) | stay out, after-sc-hard, after-sc-medium (+7 exact ties) | +0.000 | EXHAUSTED_LEGAL_SPACE |
| three-lap | stay out, after-sc-hard, after-sc-medium (+10 exact ties) | stay out, after-sc-hard, after-sc-medium (+10 exact ties) | +0.000 | EXHAUSTED_LEGAL_SPACE |
| ten-lap | stay out, after-sc-hard, after-sc-medium (+10 exact ties) | stay out, after-sc-hard, after-sc-medium (+10 exact ties) | +0.000 | EXHAUSTED_LEGAL_SPACE |
| budget-limited | stay out | stay out | +0.000 | BOUNDED_SEARCH_INCOMPLETE |
| vsc-oracle | L31-soft, oracle-000-VSC-soft, vsc-soft | L31-soft, oracle-000-VSC-soft, vsc-soft | -23.039 | EXHAUSTED_LEGAL_SPACE |
| sc-oracle | L31-soft, oracle-000-SC-soft, sc-soft | L31-soft, oracle-000-SC-soft, sc-soft | -14.721 | EXHAUSTED_LEGAL_SPACE |

Inactive reactive policies can tie the reference because no trigger committed inside the pit window. The decision ledger preserves all exact ties and near-equal candidates. Near-equality margins are 0.91, 1.44 and 3.54 seconds per driver at three, five and ten laps, based on held-out mean errors. These are reporting thresholds and carry no significance or probability interpretation.

## Physical and model sensitivities

- clean-air · hard minus soft +0.863 s. Soft receives 0.000 s from following beyond other bounds.
- two-car · hard minus soft +0.000 s. Soft receives 12.572 s from following beyond other bounds.
- retirement · hard minus soft +0.863 s. Soft receives 0.241 s from following beyond other bounds.
- wider-gap · hard minus soft +0.863 s. Soft receives 0.000 s from following beyond other bounds.

The late-VSC fixed stop gained 0.747 s over staying out. That difference falls within the five-lap model-error margin. The decisive timing changes in the early-VSC and SC fixtures are conditional on their declared speeds, pit geometry and control clocks.

The largest observed shared-box wait was 2.904 s. For the soft/soft plans, the double stack cost +3.883 s versus staying out. Staggering changed that to -6.294 s with driver 4 first and -5.975 s with driver 63 first. Team branches report per-driver deltas alongside the combined, lead-driver and weighted objectives.

6/15 declared variations changed the set of first-place candidates. 8 variations reversed the order of at least one comparable candidate pair. The sensitivity ledger records changes in pace level, tyre slope, compound, pit travel, service, gap, opponent behavior, VSC speed and restart pace.

## Verification

| Fixture | Result |
| --- | --- |
| O1 no pit needed | PASS |
| O2 immediate | PASS |
| O3 delayed | PASS |
| O4 compound | PASS |
| O5 exact tie | PASS |
| O6 following | PASS |
| O7 blocker removed | PASS |
| O8 vsc | PASS |
| O9 sc | PASS |
| O10 double stack | PASS |
| O11 two stop | PASS |
| O12 unavailable | PASS |
| O14 budget | PASS |
| O15 small case and order | PASS |
| O17 no pass | PASS |
| cache parity | PASS |
| O13 unsupported horizon | PASS |
| O16 historical field unsupported | PASS |
| O18 unverified model | PASS |

The full physics ledgers recorded 0 invariant failures. The small-space benchmark builds the fixed policies independently, compares marker values and event traces, and also checks reordered subsets. Forecast caching reuses complete typed forecast requests within one search. The cache parity run checks complete path and score equality.

## Reproduce the study

`f1ri stage6-s6-a --output artifacts/runs/stage6_s6_a_reproduced` runs the frozen matrix. Install the repository modeling dependencies and verify the input hashes in `regression_parity.json`. The checksum list covers the report, configuration, ledger tables and charts. Wall-clock measurements and the runtime figure vary with the machine and run.

This readiness decision applies to conditional, bounded 2025 mean optimization. Exact historical field reconstruction, calibrated neutralization pace, 2026 transfer and race-win probabilities remain outside the validated release.

## Release verification

Two runs produced the same hashes for 21 deterministic files, including the candidate tables, race-control and pit event traces, decision ranks and analysis figure. Wall-clock observations and their figure vary by run. Both archived S5-C and S5-C.1 metrics were reproduced byte for byte after correcting the S5-C pit-model inventory to scan only its pit-loss directory. The separate scripted-pass sensitivity committed 25 authorized passes across its candidate branches. All 363 tests passed. Ruff passed for source and tests, with ten existing import-order findings in archived notebooks. The Streamlit smoke test exercised complete GREEN and VSC searches.

The two-stop action set uses a declared minimum of two laps between stops for the same driver. The engine still resolves the route and confirms that each selected stop can finish inside the evaluation window. All rankings apply to the declared candidate set and common marker. S6-B can now test robust decisions across declared conditions. The R1.1 uncertainty study remains separate.

[Evidence archive](stage6_s6_a_ledgers.zip) · [Diagnostic overview](stage6_s6_a_diagnostics.png) · [Final metrics](stage6_s6_a_metrics.json)
