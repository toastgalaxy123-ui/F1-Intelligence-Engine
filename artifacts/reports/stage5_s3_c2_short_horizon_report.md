# Stage 5.3-C2 short-horizon traffic target feasibility

## Decision

**Path C. The existing cached data supports a defensible physical
traversal label, but it does not yet support a statistically useful
short-horizon traffic proxy.** Shorter intervals yielded substantially
more independent following groups than the frozen lap target. None of
the four clean-air references passed the held-out sample and accuracy
gate. No sector or checkpoint proxy qualified.

The next study should preregister a latent clean-pace reference and a
broader independent clean validation sample. Start with official
sectors, where the archive supplies an independent timing comparison.
Defer additional traffic telemetry until reference accuracy and support
are established. This increment used zero new requests. T1 remains
operational, Traffic V1 remains research-only, and model fitting and
the T0–T3 comparison stay gated.

## Target construction and timing

H0 preserves the frozen complete-lap protocol and the recovered Austria
car 6, lap 47 proxy of **+0.210975 s**. H1 uses official sector elapsed
times and geometry-projected passage checks. H2 uses four fixed-distance
quarters. It waits for the right-hand fix to become available before
taking a prediction snapshot. The time already elapsed since the
interpolated start remains visible in the timing ledger. H3 measures
physical support and observed progress over fixed time intervals.

The support mask requires 90% usable samples and no gap longer than
two seconds. Following labels also require the same close leader for
the first two seconds and censor observed leader changes. Pit, pass
and weather exclusions conservatively apply to the whole lap. The
candidate ledger contains cutoff inputs. Subsequent support, exposure
and crossing evidence are stored separately.

| Race | Horizon | Physical following segments | Driver–leader–stint groups | Eligible proxies |
| --- | --- | ---: | ---: | ---: |
| Austria | H0 lap | 4 | 3 | 1 |
| Austria | H1 sector | 70 | 28 | 0 |
| Austria | H2 distance | 89 | 33 | 0 |
| Hungary | H0 lap | 9 | 5 | 0 |
| Hungary | H1 sector | 58 | 17 | 0 |
| Hungary | H2 distance | 139 | 35 | 0 |

The H1 90th-percentile absolute timing disagreement was 0.156 s in
Austria and 0.124 s in Hungary, below the 0.25 s research threshold.
H2 disjoint-fix repeatability was 0.133 s and 0.141 s respectively.
Both H2 subsequences share the receiver and geometry. Systematic
timing error therefore remains unmeasured for interior checkpoints.
H0 counts come from the frozen transition windows. H1–H3 also use
the earlier history windows. The yield comparison includes this
change in sampling coverage. H2 starts require an identifiable
crossing, so its funnel is conditional on that initial evidence.

| Race | 5 s physical support | 10 s | 20 s | 30 s | 5 s following segments / groups | 30 s segments / groups |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Austria | 61.0% | 53.4% | 44.3% | 37.7% | 705 / 68 | 153 / 40 |
| Hungary | 57.2% | 49.9% | 40.9% | 35.2% | 1,275 / 59 | 388 / 42 |

Support fractions use completed opportunities as denominators. H3
counts are diagnostic only. At ten seconds, 194 Austria and 424 Hungary
following segments overlapped an earlier segment in their conservative
group. Episode fragments remain traceable without contributing extra
independent groups.

## Chronological clean-air validation

Training used only earlier observations available before each cutoff.
Global references excluded a seeded held-out driver group. R2 could
adapt from earlier clean traversals on that driver's own stint. A
control had to come from an earlier distinct driver–stint and match
the segment and context. Clean calibration errors were chronological.
The ledgers retain signed residuals, D8 uncertainty propagation for
R1, training provenance and 50% and 90% interval coverage where
available.

| Race / horizon | R0 MAE / n | R1 MAE / n | R2 MAE / n | R3 MAE / n | Shared validation rows |
| --- | ---: | ---: | ---: | ---: | ---: |
| Austria sector | 0.271 / 24 | 0.094 / 17 | 0.089 / 5 | 0.227 / 24 | 2 |
| Austria distance | 0.193 / 30 | 0.103 / 19 | 0.118 / 7 | 0.170 / 30 | 4 |
| Hungary sector | 0.467 / 1 | — / 0 | 0.074 / 26 | 0.467 / 1 | 0 |
| Hungary distance | 0.202 / 12 | 0.143 / 1 | 0.076 / 39 | 0.176 / 12 | 0 |

MAE is in seconds. The best-covered Austria distance references had
seven independent clean stints against a minimum of ten. Hungary R2
had four sector and five distance validation stints. The low
conditional R2 errors are preliminary and come from drivers with
enough prior clean history. The common sample was too small to rank
the four references. R0 and R3 also showed 90th-percentile absolute
errors around or above the 0.30 s planning effect in several strata.
Residual comparisons against gap, relative pace and exposure remain
empty because no proxy passed the validation gate.
At least one exploratory reference produced a chronological prediction
for 82 distinct following traversals. Seven had three matched earlier
control stints under at least one reference, all in Austria. The clean
validation gate kept all seven proxies null.

The two development races establish that the physical construction
works across their cached layouts. Held-out drivers test within-race
generalization. They leave performance on unseen races and layouts
unmeasured. Archive sample times stand in for actual feed receipt.
Austin and the reserved outcomes remained sealed.
R1 was eligible only with a frozen supported D8 request and earlier
clean traversal shares. R0, R2 and R3 use their own segment eligibility
rules. The study never used R1 or another reference to promote a
segment after its clean-validation gate failed.

[Sixteen diagnostic panels](stage5_s3_c2_short_horizon.png) · [Metrics and funnels](stage5_s3_c2_short_horizon_metrics.json) · [Parquet ledgers and manifests](stage5_s3_c2_short_horizon_ledgers.zip) · [Research specification](../../docs/STAGE5_S3_C2_SHORT_HORIZON.md)

## Reproduction

The baseline commit was `1495f321b693881d917acf3c9d8f140fd959a07a`.
The frozen encounter metrics reproduced with SHA-256
`ed93d599b55882da255c7e37aac1463e38daa7e2fa891ab109762dc08534d1d5`.
The exact D8 bundle hash was
`31734c2244b3bdbd555c1407fe62207b3edf0eaddafe85e722b2cc936c75923c`.
The data and geometry manifests record the warehouse, raw payload and
frozen geometry hashes.
The Singapore, original continuation, Hungary coverage extension,
history, transition and encounter metrics matched their frozen artifacts.
Two C2 cache replays produced byte-identical ledgers, manifests, metrics
and diagnostic figures. The C2 metrics SHA-256 is
`f183d066d367df860baf665ac8c47562ff2f99ff3fe9f77fdf5c278683641461`.

```powershell
$env:PYTHONPATH = "$PWD/src"
$py = 'C:/Projects/f1-race-intelligence/.venv/Scripts/python.exe'
$wh = 'C:/Projects/f1-race-intelligence/data/warehouse/f1ri.duckdb'
$d8 = 'C:/Projects/f1-race-intelligence'
$base = 'artifacts/runs/stage5_s3_c_forensics_baseline'
$hist = 'artifacts/runs/stage5_s3_c_forensics_history'
$transition = 'artifacts/runs/stage5_s3_c_forensics_transition'
$census = 'artifacts/runs/stage5_s3_c_transition_census_v1'
$encounter = 'artifacts/runs/stage5_s3_c2_frozen_replay'
& $py -m f1ri.cli stage5-s3-encounters --warehouse $wh --stage3-root $d8 --transition $transition --history $hist --baseline $base --census $census --output $encounter
& $py -m f1ri.cli stage5-s3-short-horizon --warehouse $wh --stage3-root $d8 --transition $transition --history $hist --census $census --encounter $encounter --output artifacts/runs/stage5_s3_c2_short_horizon_repro
& $py -m pytest -q tests/test_traffic_short_horizon.py tests/test_traffic_encounter.py tests/test_traffic_transition.py tests/test_traffic_history_study.py tests/test_traffic_coverage.py tests/test_traffic_continuation.py tests/test_track_proximity.py
& $py -m pytest -q
```

The change adds the C2 CLI, traversal screening, reference validation,
diagnostic figures, manifests and focused tests. The shared forward
crossing helper now exposes its bracketing fixes while preserving the
existing crossing-only output.

The focused suite passed 65 tests. The full suite passed 197 tests
with 14 existing NumPy deprecation warnings from pit-neutralization.
