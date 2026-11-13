# Stage 5.3-C3 latent clean-air sector pace

## Decision

**Path B.** Online stint adjustment improved held-out sector-time
accuracy over the static hierarchical model in both development races.
The primary reference still lacks independent clean validation and
calibration support. The study found no eligible H1 traffic proxies.
Keep T1 operational and Traffic V1 research-only.

The next increment should preregister a bounded clean-reference
validation expansion. Prioritize distinct clean stints, especially
early and cold-start contexts, with official timing and physical
confirmation. Include a separately approved repeat-layout cohort
before claiming race-to-race transfer. The present evidence supports
neither new traffic telemetry acquisition nor empirical traffic fitting.

## Evidence census

| Race | Sector candidates | Tier A | A drivers / stints | Tier B | B drivers / stints | Tier C | Excluded |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Austria | 3,384 | 120 | 11 / 15 | 850 | 15 / 36 | 1,649 | 765 |
| Hungary | 4,107 | 131 | 10 / 13 | 1,105 | 20 / 42 | 2,241 | 630 |

Tier A counts by sector were 45, 13 and 62 in Austria and 50, 41 and
40 in Hungary. Tier B uses reconciled official times and Stage 1
clean-air evidence with no cached physical contradiction. The study
kept all Tier B observations out of primary evaluation and online
stint updates. Its separate training sensitivity gave them weight 0.25.
The evidence ledger preserves timing, physical quality, availability,
tire and weather clocks, and exclusion reasons.

Austria and Hungary each represent one race weekend and one layout.
Rotating held-out driver folds gives independent driver evaluation
within each development race. The separate calibration fold reduces
the global training pool for each prediction. The chronological
within-race replay also excludes the focal driver from global fitting,
while both evaluations allow its earlier clean sectors for online
adaptation. Neither tests transfer to an unseen weekend or circuit.
Austin and the reserved races remained sealed.

## Held-out clean-sector predictions

| Race and reference | Predictions | Stints | Support | MAE | Bias | P90 absolute error |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Austria R4-A | 38 | 8 | 31.7% | 0.213 s | −0.024 s | 0.428 s |
| Austria R4-B | 38 | 8 | 31.7% | 0.171 s | −0.010 s | 0.359 s |
| Austria R4-B with Tier B training | 105 | 13 | 87.5% | 0.176 s | −0.088 s | 0.348 s |
| Hungary R4-A | 29 | 8 | 22.1% | 0.172 s | +0.000 s | 0.384 s |
| Hungary R4-B | 29 | 8 | 22.1% | 0.151 s | +0.007 s | 0.384 s |
| Hungary R4-B with Tier B training | 109 | 11 | 83.2% | 0.197 s | −0.143 s | 0.431 s |

R4-B's chronological within-race MAEs were 0.161 s across 62
predictions and 0.145 s across 41. In the held-out comparison, the
zero-history MAEs were 0.192 s across ten Austria sectors and 0.179 s
across 18 Hungary sectors. With three or more prior clean sectors,
the MAEs were 0.156 s across 19 and 0.136 s across five, respectively.
Those history groups represent different observation populations.
The static and adapted rows in the table compare the same observations.

| Held-out common support | Rows / stints | R0 | R1 | R2 | R3 | R4-A | R4-B |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Austria | 6 / 3 | 0.219 | 0.077 | 0.052 | 0.181 | 0.178 | 0.136 |
| Hungary | 3 / 1 | 0.043 | 0.049 | 0.061 | 0.069 | 0.206 | 0.138 |

MAE is in seconds. R4-B did not beat the best existing reference on
common support. That comparison covers only nine sectors. R2's
conditional MAEs were 0.072 s and 0.074 s on its full supported
populations, with 16 and 27 predictions across five and four stints.
The metrics include RMSE, support and separate error distributions
for all references.

On the shared R4-B subset, Tier B training changed MAE from 0.171 to
0.168 s in Austria and from 0.151 to 0.180 s in Hungary. The broader
training evidence improved coverage enough to clear the point
gate in both races. Its change in signed bias and lack of calibration
keep it an exploratory sensitivity.

## Uncertainty and following readiness

R4-B's held-out residual standard deviations were 0.212 s and 0.219 s.
The median absolute errors were 0.128 s and 0.095 s. The 90th-percentile
errors of 0.359 s and 0.384 s exceeded the 0.30 s planning effect.
C2's official-sector timing disagreement at the same percentile was
0.156 s and 0.124 s. The measured timing and reference errors may be
dependent, and the residual already incorporates the official elapsed
measurement. The study leaves their combined quantile unestimated.

No held-out prediction had enough earlier independent calibration
stints for a 50% or 90% interval. Empirical interval coverage, interval
precision relative to 0.30 s and CRPS are therefore unavailable.
The model retains a raw predictive scale that grows with sparse
history. The time-evolving R4-C variant was deferred because the
simpler model still lacked sufficient validation support.

| Race | Frozen H1 following | Physical groups | Exploratory R4-B predictions | Three earlier controls | Eligible proxies |
| --- | ---: | ---: | ---: | ---: | ---: |
| Austria | 70 | 28 | 21 | 0 | 0 |
| Hungary | 58 | 17 | 8 | 0 | 0 |

The H1 readiness ledger retains every frozen following segment.
The recovered H0 Austria car 6, lap 47 proxy remains
+0.2109747886 s. No Traffic V1 model was fitted.

[Diagnostics](stage5_s3_c3_latent_sector_pace.png) · [Metrics](stage5_s3_c3_latent_sector_pace_metrics.json) · [Evidence, splits and model snapshots](stage5_s3_c3_latent_sector_pace_ledgers.zip) · [Research protocol](../../docs/STAGE5_S3_C3_LATENT_SECTOR_PACE.md)

## Reproduction

The input Git revision is `50763cda40e6be8cd57cd7ce08a1d1f0a06abd34`.
C2 reproduced byte for byte, including the candidate and support
ledgers, with metrics SHA-256
`f183d066d367df860baf665ac8c47562ff2f99ff3fe9f77fdf5c278683641461`.
The run verified 82 cached raw payloads and the frozen D8 hash
`31734c2244b3bdbd555c1407fe62207b3edf0eaddafe85e722b2cc936c75923c`.
The versioned R4 prefix snapshots have SHA-256
`6f10cbd13986eaa812182e7142fabcedc8323f12f0bef5f7fce6b6d710b6ad50`.
Two C3 runs produced identical metrics, ledgers, snapshots and figures.
The C3 metrics SHA-256 is
`8db99cd71b51b93c3165e4fbe5892166f0f9b746f44b917ecc8c993d40e16d97`.

```powershell
$env:PYTHONPATH = "$PWD/src"
$py = 'C:/Projects/f1-race-intelligence/.venv/Scripts/python.exe'
$wh = 'C:/Projects/f1-race-intelligence/data/warehouse/f1ri.duckdb'
$d8 = 'C:/Projects/f1-race-intelligence'
$c2 = 'artifacts/runs/stage5_s3_c3_reproduce_c2'
& $py -m f1ri.cli stage5-s3-short-horizon --warehouse $wh --stage3-root $d8 --transition artifacts/runs/stage5_s3_c_forensics_transition --history artifacts/runs/stage5_s3_c_forensics_history --census artifacts/runs/stage5_s3_c_transition_census_v1 --encounter artifacts/runs/stage5_s3_c2_frozen_replay --output $c2
& $py -m f1ri.cli stage5-s3-sector-reference --warehouse $wh --stage3-root $d8 --c2 $c2 --output artifacts/runs/stage5_s3_c3_reproduce
& $py -m pytest -q tests/test_latent_sector_pace.py tests/test_traffic_short_horizon.py tests/test_traffic_encounter.py tests/test_traffic_transition.py tests/test_traffic_history_study.py tests/test_traffic_coverage.py tests/test_traffic_continuation.py tests/test_track_proximity.py
& $py -m pytest -q --disable-warnings
```

The baseline suite passed 197 tests. The focused suite passed 76.
The final suite passed 208 tests with 14 existing NumPy deprecation
warnings. The C3 changes add an evidence builder, the R4 model,
chronological backtesting and diagnostics, a CLI entry point and a
versioned plan.
