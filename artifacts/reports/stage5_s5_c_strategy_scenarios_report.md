# F1 Race Intelligence Engine — Stage 5 S5-C
## Integrated deterministic strategy evaluation

**Path A for bounded deterministic comparisons.** The local dashboard, CLI, and Python interface run alternative tyre and pit policies against a shared starting field and a fixed race-control schedule. **Full-model strategy evaluation remains unsupported.** The fitted D8 bundle and candidate-specific paired replacement trajectories were unavailable.

## Paired outcomes

The objective is minimum time to the same racing checkpoint, summed over target drivers for a team comparison. Negative values favor the candidate. All times below come from declared scenarios.

| Case | Candidate vs. continue | Time difference |
| --- | --- | ---: |
| GREEN | Pit at the next opportunity for medium | +11.433 s |
| VSC | React when VSC is committed | +10.607 s |
| SC | React when SC is committed | +7.327 s |
| SC | Pit after the declared restart review | +8.553 s |
| Shared team box | Stop both teammates at the next opportunity | −14.882 s combined |
| Higher degradation | Extend one lap, then fit medium | −58.972 s |
| RED | Planned medium stop, delayed by suspension | +11.433 s |

At the GREEN marker the medium, hard, used soft, early, late, and two-stop branches reached the same effective clock. Their pit and tyre timelines differed. In that fixture, the post-merge predecessor constrained the tested car through the target. In the separate scripted-pass comparison, the identical pit strategy tied continue in the no-pass case and trailed it by 33.250 s when the authorized pass was enabled. The authorized pass was part of each case's external schedule.

The unqueued green, VSC, and SC stops each took 57.000 s from physical entry to merge. The shared-box follower waited 5.173 s after arriving. The two staggered variants had no service wait. The RED case suspended progress for 28.000 s and committed the stop after rolling resumption. An attempted RED deployment during an active pit itinerary and an unresolved exact-tie merge are covered by explicit unsupported regression cases.

## Sensitivity and attribution

Across ten declared SC and pit-route variants, the reactive stop ranged from **39.642 s faster to 11.433 s slower** than continue. In the tyre-degradation sensitivity, the medium stop ranged from **92.567 s faster to 11.433 s slower**. These are ranges across chosen inputs. The report uses no deployment weights or statistical confidence intervals.

Timing differences come from matched racing markers. The ledgers also record each stop's actual route elapsed, service, wait, and clock gap against the reference at the merge marker where a comparable stay-out crossing exists. These components interact with the tyre trajectory and predecessor order. Adding a fixed pit bonus would double count movement already represented by the comparison.

## Pace, field and rules support

| Execution mode | Release status |
| --- | --- |
| DECLARED_SYNTHETIC | Interpretable dry compounds, used sets, age-indexed pace curves, and S2 routes |
| FROZEN_PACE_REPLAY | Verified saved Singapore continuation sample on a declared two-car field |
| FULL_MODEL | Unsupported. The fitted D8 pickle and Stage 3 run metadata are missing. Candidate-specific paired replacement requests are outside this release. |
| UNSUPPORTED | Missing or unresolved prerequisites remain visible and cannot produce a ranked delta. |

Each branch receives the same hashed initial field, intervention calendar, opponent policies, and pace inputs. Point-in-time policies see committed signals and current race state. The saved continuation sample preserves the existing trajectory draws. Branches do not resample, so candidate order and the addition of another policy cannot change their underlying pace stream. The saved draw establishes pairing parity for continuation. The study does not establish matched sampling for new tyre forecasts.

Timing rank and circular predecessor order are available separately. Short or intermediate windows expose a horizon order. A finish order requires an actual simulated race finish and a supported branch. The optional dry non-Monaco finish check covers the two-specification rule with a declared mandatory race allocation. [2025 FIA Sporting Regulations, Issue 5](https://www.fia.com/system/files/documents/fia_2025_formula_1_sporting_regulations_-_issue_5_-_2025-04-30.pdf)

## Verification and readiness

- The 311-test baseline reproduced. The complete suite passed all **329 tests** with 104 warnings in the available environment. Ruff passed.
- **10 scenario families, 34 branches, 12 sensitivity cases**, and zero recorded invariant failures in the family study. Two independent runs produced **13 byte-identical generated files**.
- The S5-A.1 replay regenerated the frozen metrics hash, including the 160–217 s GREEN pit fixture and approved Singapore and Imola regressions. The frozen S5-B and earlier available metrics hashes matched. The D8 hash was verified against its manifest.
- The Streamlit smoke test ran the declared and saved-pace comparisons, rendered results, exposed scenario and comparison downloads, and displayed the full-model support gate.
- S5-C validates engineering behavior under the chosen scenarios. S5-B's historical physical validation remains partial, with only one VSC and one SC in an independent global race. Queue formation, thermal response, real-world pit advantage, and deployment or overtaking probabilities remain uncalibrated.

The metrics SHA-256 is `669312321029a882336ffdee8f39cb7d0ddb78eeea10df22febed1d2ae5795c1`.

[Local interface and reproduction guide](../../docs/STAGE5_S5_C_STRATEGY.md) · [Diagnostic figure](stage5_s5_c_strategy_scenarios.png) · [Ledgers and checksums](stage5_s5_c_strategy_scenarios_ledgers.zip)
