# Stage 5 S4-B — Historical overtaking reconstruction

**Decision — Path C.** The archive supports an interval-censored research dataset. It contains 30 Tier B exchanges and zero Tier A exchanges. Five were same-lap passes, including one from the Imola reconstruction cohort. Only Austria and Hungary contribute development evidence. The current support is insufficient for S4-C pass-completion hazard fitting. S4-A scripted execution and the operational T1 no-pass baseline retain their roles.

[Specification](../../docs/STAGE5_S4_B_HISTORICAL_OVERTAKES.md) · [Diagnostics A–P](stage5_s4_b_historical_overtakes.png) · [Metrics](stage5_s4_b_historical_overtakes_metrics.json) · [Complete ledgers](stage5_s4_b_historical_overtakes_ledgers.zip)

## Source census and event labels

The study evaluated 42 reconstruction windows from eight cache sources. Overlapping windows share event IDs and cutoff IDs. The retrospective window spans covered approximately 81 seconds in Singapore, 2,311 in Austria, 3,144 in Hungary and 1,272 in Imola after merging overlaps. These durations describe cached snapshot spans. Continuous pair support is assessed separately. Singapore retains its frozen pilot role and Imola remains reconstruction-only. The run made zero new OpenF1 requests.

| Result | Count |
| --- | ---: |
| Same-lap racing passes | 5 |
| Lapping passes | 25 |
| Supported unlapping passes | 0 |
| Ambiguous physical reversals | 7 |
| Separately supported pit-induced neighbor changes | 12 |

The 30 supported brackets span 1.0–5.0 seconds with a 2.0-second median. The ledger retains the endpoints, reconstructed gap bounds, native grid period, source observation clocks, staleness and sector. It also records next timing observations, positions and nearby race-control evidence. Three of five same-lap cases have attacker-first order at a shared post-event timing marker. The other two lack that shared corroboration. The position feed was too sparse for an independent two-driver rank comparison around all 30 brackets. The stratified audit contains inspectable examples of available event and outcome classes. Precision and recall remain unestimated because an independently annotated reference population is unavailable.

The S4-A comparison preserved the original Singapore 63→18 lapping bracket of 2 seconds, Hungary 1→22 same-lap bracket of 4 seconds, and Hungary 63→43 lapping bracket of 2 seconds. The configured Austria 4→23 audit still has no supported bracket. The frozen mechanical replay completed one of those three scripts 13.278 seconds beyond the observation bracket. Across the expanded set, nine scripts reproduced direction, two were rejected and 19 had inadequate cutoff support. All nine successful mechanical commits followed their historical brackets by 4.124–26.592 seconds.

The frozen ordering audit confirmed that the two-car physical cycle is anchored at the attacker. Its ordering tuple can be identical across a pass. The before and after checkpoint pairs and committed timing frames show the exchange. The versioned audit exposes both phases and preserves the original artifacts.

## Opportunities and censoring

The 77,630 unique cutoffs represent 836 linked encounters across 312 race-pair groups, four layouts and 20 drivers. At 10 seconds, 44 positive cutoff outcomes reference 16 distinct events. There are 18,055 observed non-pass outcomes across 402 encounters. Localization censored 50,707 cutoffs, pit transitions censored 498, and a resolved neighbor change censored one. A further 6,361 outcomes are ambiguous and 1,964 have incomplete horizons. The archived sampling preselects GREEN windows, so flag and retirement censoring have regression coverage and no measured 10-second instances in this set.

| 10-second population | Cutoffs | Encounter groups | Distinct pass events |
| --- | ---: | ---: | ---: |
| Upper gap ≤40 m | 3,204 | 186 | 4 |
| Upper gap ≤80 m | 44,589 | 419 | 9 |
| Upper gap ≤120 m | 74,380 | 653 | 16 |

The ledger identifies close following, supported approaches, seeded random GREEN controls and lapped encounters. The three nested gap populations are screening definitions. Encounter counts can overlap across thresholds and outcomes. They measure support in a selected archive. Pit censoring and loss of localization can depend on the racing situation.

Cutoff fields use the observation clock and keep future evidence in a separate ledger. Historical receipt times are unavailable. Prior screened lap pace is available for both cars at 30,104 cutoffs. The core predictor set has support at 29,250 cutoffs, about 38%. Tire and stint context is published separately because its availability clock is unknown. The outcome ledger retains checkpoint and time-horizon endpoints, first interruption, uncertainty and event IDs. A non-pass label describes the observable outcome and leaves driver intent unresolved.

## S4-C evidence needs

The development set contains 27 Tier B events, including four same-lap events, across two races. The development 10-second controls cover 285 linked encounters. High-confidence competitive labels and race-level coverage remain the limiting factors. The readiness artifact records race-clustered evaluation, calibration-in-the-large, horizon Brier scores and interval-censored log scores as future design requirements.

The next bounded evidence check would revisit localization loss before adding races. Candidate development gaps include 13:41:30–13:47:30 UTC in Austria and 14:00:06–14:13:25 UTC in Hungary. These are cached reconstruction gaps, not estimates of extra passes. A future acquisition plan should first screen the missing intervals for cutoff-visible encounters, expected independent yield and competing events, then set a request cap and stop rule. No new acquisition budget was exercised.

## Reproduction

The baseline was `371d6c4ccc7557205848f4c55030907b184661b1`. The 229-test baseline reproduced. The final suite passed 254 tests with the same 14 warnings. Ruff passed. The frozen S4-A metrics and inspected replay ledgers reproduced byte for byte, with S3-C4 and D8 hash parity. The S4-A run also verifies frozen S1, S2, S3 and transition inputs.

Two S4-B runs produced 18 byte-identical generated files. The metrics SHA-256 is `09a0512aa29d3c4b2f3e5522e3a9f3a9e4c59d676f2e4d64c6d2bdde3345ee4d`. The ledger audit checked feature clocks on all 77,630 cutoffs and verified all 388,150 outcome references. Every positive outcome links to an eligible pass bracket within its horizon.

The release archive contains both data layers, event and encounter ledgers, source manifests, readiness and audit results, a diagnostic figure and checksums. The S4-B command requires the original local warehouse and approved caches.

```powershell
$env:PYTHONPATH = "$PWD/src"
$py = 'C:/Projects/f1-race-intelligence/.venv/Scripts/python.exe'
& $py -m f1ri.cli stage5-s4-a --warehouse C:/Projects/f1-race-intelligence/data/warehouse/f1ri.duckdb --stage3-root C:/Projects/f1-race-intelligence --s1 artifacts/runs/stage5_s4_a_s1_replay --s2 artifacts/runs/stage5_s4_a_s2_replay --singapore artifacts/runs/stage5_s4_a_singapore_replay --transition artifacts/runs/stage5_s3_c_forensics_transition --output artifacts/runs/stage5_s4_b_s4_a_replay_v1
& $py -m f1ri.cli stage5-s4-b --warehouse C:/Projects/f1-race-intelligence/data/warehouse/f1ri.duckdb --stage3-root C:/Projects/f1-race-intelligence --singapore artifacts/runs/stage5_s4_a_singapore_replay --transition artifacts/runs/stage5_s3_c_forensics_transition --history artifacts/runs/stage5_s3_c_forensics_history --baseline artifacts/runs/stage5_s3_c_forensics_baseline --s4-a-replay artifacts/runs/stage5_s4_b_s4_a_replay_v1 --output artifacts/runs/stage5_s4_b_historical_overtakes_release_v5
& $py -m f1ri.cli stage5-s4-b --warehouse C:/Projects/f1-race-intelligence/data/warehouse/f1ri.duckdb --stage3-root C:/Projects/f1-race-intelligence --singapore artifacts/runs/stage5_s4_a_singapore_replay --transition artifacts/runs/stage5_s3_c_forensics_transition --history artifacts/runs/stage5_s3_c_forensics_history --baseline artifacts/runs/stage5_s3_c_forensics_baseline --s4-a-replay artifacts/runs/stage5_s4_b_s4_a_replay_v1 --output artifacts/runs/stage5_s4_b_historical_overtakes_repeat_v5
& $py -m pytest -q --disable-warnings
```
