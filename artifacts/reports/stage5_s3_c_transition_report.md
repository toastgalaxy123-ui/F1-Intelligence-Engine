# Stage 5.3-C clean-to-traffic transition pilot

## Cutoff census

| Race | Lap-start cutoffs | Fewer than three completed laps on stint | Enough completed laps, fewer than three clean | Three or more clean | D8 supported | D8 supported with approach | Approach with five frozen controls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Austria `9955` | 633 | 56 | 265 | 312 | 293 | 6 | 0 |
| Hungary `9928` | 780 | 69 | 360 | 351 | 280 | 9 | 3 |

The census audited recorded cutoffs on laps 8–46. The 15 prospective
cutoffs span five Austria and six Hungary follower stints. Cached
physical evidence identifies three Austria and four Hungary
driver–leader–stint groups among the candidates. Coarse intervals
provide acquisition priority for candidates with limited physical
evidence. The ledger retains sample clocks, uncertainty bounds, known
opponent lap relationship, estimated closing rate and relative D8 pace
where available. It also records earlier Stage 1 exclusions, evidence
clocks and whether the cached physical reconstruction agrees with the
Stage 1 clean-air classification.

The frozen Hungary audit still contains 86 prior-lap records with Stage 1
`TRAFFIC` exclusions across eight earlier following cutoffs. The physical
history audit preserves an insufficient-evidence category. The car 5,
lap 29, sector 2 marker remains unresolved. Its 1.039 s bracket exceeds
the 1.0 s limit. Diagnostic interpolation is about −0.068 s from the
archived 13:43:05.547 UTC marker.

The acquisition lock was created before requesting new telemetry and
reproduced byte for byte. Both races used 260 s blocks starting 35 s
before the cutoff, with an upper limit of 1,000 s of new field coverage
and 24 endpoint requests per race. Request duration counts each new time
slice once across its paired `location` and `car_data` calls and includes
boundary overlap.

| Race | Transition blocks | Earlier-control blocks | Seeded random blocks | New field seconds | Endpoint requests |
| --- | ---: | ---: | ---: | ---: | ---: |
| Austria | 2 | 2 | 1 | 725 | 14 |
| Hungary | 3 | 2 | 1 | 309 | 8 |

[Census summary](stage5_s3_c_transition_census.json) · [Complete candidate ledger](stage5_s3_c_transition_candidate_ledger.zip) · [Frozen acquisition lock](stage5_s3_c_transition_lock.json)

## Pilot results

| Race and arm | Complete-field samples | Full laps | Physically screened | Following | D8 following | Following proxies | Supported clean controls | Clean-control proxies | Physical encounter groups | Timing resolved |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Austria transition | 995/1,038 | 85 | 42 | 3 | 1 | 0 | 5 | 1 | 2 | 8/8 |
| Austria earlier control | 988/1,038 | 80 | 28 | 0 | 0 | 0 | 7 | 0 | 0 | 8/8 |
| Austria random green | 463/519 | 44 | 7 | 1 | 0 | 0 | 1 | 0 | 1 | 0 scheduled |
| Hungary transition | 1,515/1,557 | 127 | 47 | 4 | 0 | 0 | 4 | 0 | 3 | 12/12 |
| Hungary earlier control | 1,030/1,038 | 81 | 28 | 3 | 0 | 0 | 7 | 2 | 2 | 4/4 |
| Hungary random green | 499/519 | 40 | 6 | 2 | 0 | 0 | 0 | 0 | 1 | 0 scheduled |

Both races passed the frozen timing gate with 16/16 markers. Austria
resolved 195 of 196 eligible control-line predecessor checks and matched
195. Hungary resolved 261 of 264 and matched 259. Both races reproduced
their history-study geometry.

All five transition anchors had a full lap and D8 support at selection.
Four initialized a supported cutoff neighborhood. One passed the full
physical screen. None had a verified close encounter at cutoff and none
converted. Across all arms, the 11 anchors produced zero following
proxies. Their first decisive outcomes were seven physical-support
failures, three laps without a continuing cutoff encounter and one pit
or stint transition. High aggregate field coverage can coexist with
insufficient support across a particular driver's target lap.

The transition windows yielded seven physical following laps in 212
full laps. Random windows yielded three in 84. The earlier generic
close arm yielded one in 230. These are descriptive rates from small,
differently selected windows. The targeted windows contained the only
D8-supported following lap, an incidental observation rather than a
selected anchor. All arms yielded zero fully eligible targets. The
pilot therefore provides no evidence for the preregistered hypothesis
of improved final-target yield.

The sole supported following lap was Austria car 6, lap 47, following
car 63. Frozen D8 predicted 70.391275 s and the observed lap was 70.412 s,
for a signed residual of +0.020725 s. Twenty distinct development laps
had completed and become available before the cutoff. Twelve matched
compound and four survived tire-age and progress filters. The four
controls leave the observational proxy null. The other 12 following laps
failed the frozen three-clean-lap history rule. Across Austria's four
following laps, 13 fragments and four cutoff episodes reduced to three
driver–leader–stint groups across three drivers. Hungary's nine laps,
46 fragments and nine cutoff episodes reduced to five groups across
five drivers. Zero groups in either race qualified for readiness.

[Diagnostic figure](stage5_s3_c_transition.png) · [Race and arm metrics](stage5_s3_c_transition_metrics.json) · [Acquisition manifest](stage5_s3_c_transition_acquisition_manifest.json) · [Anchor conversion](stage5_s3_c_transition_anchors.json) · [Target eligibility](stage5_s3_c_transition_target_eligibility.json) · [Control attrition](stage5_s3_c_transition_control_attrition.json)

## Readiness decision

Austria and Hungary both remain ineligible. The protocol requires at
least five following proxies, five clean-control proxies, five independent
following groups and a passed timing gate in each of two development
races. This pilot produced zero following proxies and one Austria and two
Hungary clean-control proxies. The cutoff census found only 15 joint
history, D8 and approach candidates, with five frozen controls available
for three. Under the sampled population and frozen requirements, the
candidate pool is sparse. Coverage outside that population remains unknown.

`T1_DETERMINISTIC_NO_PASS` remains operational. Traffic V1 remains
`RESEARCH_ONLY`, empirical fitting remains `GATED` and the T0–T3 comparison
is pending. Imola retained its reconstruction/reference role and Monza
retained its fixed validation evidence. Austin and the four reserved
races remained sealed.

The next bounded study should replay the cached approach windows to
measure the time from the first approach signal to a supported close
cutoff, and audit why the per-lap route and support mask falls below
the frozen threshold. Use this to preregister a more specific cutoff
selector. In parallel, target earlier controls for Austria car 6,
lap 47 using the frozen compound, age, progress and chronology rules.
This addresses the two observed failures with a small additional budget
before considering a separate pace-reference research path.

## Reproduction

Run from the project root with the frozen telemetry cache, warehouse and
Stage 3 artifacts available. The acquisition command replays the cache.
The original run appended `--ingest` to that command for the locked
missing slices.

```powershell
$env:PYTHONPATH = "$PWD/src"
$py = 'C:/Projects/f1-race-intelligence/.venv/Scripts/python.exe'
$wh = 'C:/Projects/f1-race-intelligence/data/warehouse/f1ri.duckdb'
$d8 = 'C:/Projects/f1-race-intelligence'
$baseline = 'artifacts/runs/stage5_s3_c_transition_repro_baseline'
$history = 'artifacts/runs/stage5_s3_c_transition_repro_history'
$census = 'artifacts/runs/stage5_s3_c_transition_repro_census'
$plan = 'config/experiments/stage5_s3_c_transition_pilot.yaml'
$lock = "$census/lock.json"
& $py -m f1ri.cli stage5-s3 --warehouse $wh --output artifacts/runs/stage5_s3_transition_repro_singapore
& $py -m f1ri.cli stage5-s3-continue --warehouse $wh --stage3-root $d8 --coverage-pilot config/experiments/stage5_s3_c_coverage_pilot.yaml --output $baseline
& $py -m f1ri.cli stage5-s3-continue --warehouse $wh --stage3-root $d8 --history-pilot config/experiments/stage5_s3_c_history_pilot.yaml --history-baseline $baseline --output $history
& $py -m f1ri.cli stage5-s3-continue --warehouse $wh --stage3-root $d8 --transition-census --transition-history $history --output $census
& $py -m f1ri.cli stage5-s3-continue --warehouse $wh --history-pilot $plan --transition-history $history --freeze-transition --transition-lock $lock
& $py -m f1ri.cli stage5-s3-continue --warehouse $wh --stage3-root $d8 --history-pilot $plan --history-baseline $baseline --transition-history $history --transition-lock $lock --output artifacts/runs/stage5_s3_c_transition_repro
& $py -m pytest -q
```

The Singapore, original continuation, Hungary coverage extension and
Austria–Hungary history metrics reproduced byte for byte from input commit
`ce0771c692d22ec104d12cb88b57654f13a7337a`. The history metrics SHA-256
is `7ff964619c54c6987eab01f82db18aad1622d834ccaeda63fc3268e76c5c4010`.
The warehouse SHA-256 is `9404346d9aef1ef44ee9cea7e36c3f66475dd1d9495a62aed3e0c0e35a0e53d0`.
The acquisition lock SHA-256 is `145239e8e4cf85b75a05c6ab1a446e7783c8d7c99688179d730c307a878d5f8c`.
The frozen D8 SHA-256 is `31734c2244b3bdbd555c1407fe62207b3edf0eaddafe85e722b2cc936c75923c`.
Its training cutoff is `2024-10-20 20:38:45.387 UTC` and its
selection/calibration cutoff is `2025-06-01 14:36:47.725 UTC`. Both
development races are in its original test split.

The focused checks passed 43 tests. The full suite passed 175 tests
with 14 existing NumPy deprecation warnings in the pit-neutralization
tests.
