# Stage 5.3-C encounter conversion and target recovery

## Decision

**Path C.** The cached replay recovered one eligible observational
following target in Austria. Hungary still has zero. The full-lap target
requires a supported encounter at the lap boundary, enough physical
support through the lap, a frozen D8 forecast and five chronological
controls. The observed episodes seldom satisfy those conditions together.
Keep `T1_DETERMINISTIC_NO_PASS` operational and Traffic V1
`RESEARCH_ONLY`. Empirical fitting and the T0–T3 comparison remain gated.

The next research increment should preregister a sector-level or
short-horizon traversal-time target using the same development races.
Use cached data to measure physical support at the new horizon and
account for repeated episodes by driver, leader and stint. The frozen
lap-level benchmark remains available for comparison. Austin and the
reserved outcomes stayed sealed.

## Approach and lap-boundary alignment

| Transition anchor | Signal to supported close | Close to next lap line | Longest strict close run | Frozen screen |
| --- | ---: | ---: | ---: | --- |
| Austria car 18 lap 40 | 1.0 s | 70.3 s | 21.5 s | Intermediate |
| Austria car 5 lap 46 | No supported close in window | — | 0 s | Unsupported at cutoff |
| Hungary car 16 lap 39 | 1.0 s | 80.9 s | 1.0 s | Physical coverage below threshold |
| Hungary car 27 lap 14 | 0.5 s | 82.0 s | 2.5 s | Physical coverage below threshold |
| Hungary car 55 lap 22 | 1.0 s | 81.5 s | 2.5 s | Physical coverage below threshold |

Four approaches were followed by a supported close sample after the
selected lap start. None of the corresponding anchor windows contained
an episode for that driver crossing the next line. The timing offsets
are retrospective measurements from the recorded signal clock.
The selector never uses those later samples to change the state at
the original cutoff. The coarse Austria car 5 approach was right-censored
at the end of its observation window.

| Race | Frozen episode fragments | Starts within 5 s of next line | Crossed next line | Spanned a complete subsequent lap |
| --- | ---: | ---: | ---: | ---: |
| Austria | 531 | 15 | 30 | 1 |
| Hungary | 1,221 | 79 | 99 | 0 |

This is a retrospective episode-duration diagnostic. The frozen target
requires a continuing cutoff encounter and at least 90% usable physical
samples. It can admit a lap whose following episode ends before the
finish. Localization interruptions ended 352 Austria and 938 Hungary
fragments. The frozen gap-open rule ended 165 and 236, respectively.
Its upper-bound exit rule can respond to widening uncertainty, so an
exit alone does not identify physical separation. The episode and sample
ledgers retain the separate evidence. Fragments remain grouped under
`driver-leader-stint-v1` for readiness.

## Driver-specific support

| Anchor | Complete field on target lap | Usable target samples | Longest gap | First exclusion and main contributor |
| --- | ---: | ---: | ---: | --- |
| Austria car 5 lap 46 | 96.5% | 95.2% | 2.0 s | Unsupported cutoff despite sufficient lap coverage |
| Austria car 6 lap 16 | 94.5% | 52.6% | 10.0 s | Circular order and nearest-neighbor uncertainty |
| Hungary car 16 lap 39 | 96.9% | 84.5% | 5.5 s | Route masking, unresolved order and incomplete samples |
| Hungary car 27 lap 14 | 96.4% | 85.0% | 7.0 s | Nearest-neighbor overlap and route masking |
| Hungary car 55 lap 22 | 97.0% | 35.9% | 46.0 s | Nearest-neighbor overlap across much of the lap |
| Hungary car 14 lap 8 | 99.4% | 64.1% | 5.0 s | Circular order and nearest-neighbor uncertainty |
| Hungary car 5 lap 19 | 94.6% | 88.5% | 3.5 s | Route masking and incomplete samples |

The reconstruction reproduced the frozen physical mask at each failed
lap. A complete field can still have overlapping position bounds that
leave the identity of the nearest car uncertain. The ledger includes
the per-car position source and age, missing required cars, route states,
gap bounds and loop-origin flags. A loop-origin flag identifies a
crossing in the gap calculation and carries no attribution of error.
Route masking with an unidentified neighbor is recorded separately
from a known opponent in the pit lane.

The diagnostic 85% threshold would pass the coverage component for
one of the seven rejected anchors with supported cutoff initialization.
The 80% threshold would pass that component for three. Neither threshold
creates a qualifying close encounter in those cases. The frozen
threshold remains 90%.

## Austria car 6, lap 47

The frozen pipeline reproduced the four matches `9955/16/39`,
`9955/4/34`, `9955/4/35` and `9955/4/36`. The cached replay added
`9955/31/43` using the frozen geometry and full clean-control screen.
The original signed residual was +0.020725 s. The median of the five
earlier control residuals was −0.190250 s, yielding a signed
observational proxy of **+0.210975 s**. The estimate is an association
conditional on the frozen pace reference and matched controls.

| Filter | Frozen | With recovered control |
| --- | ---: | ---: |
| Supported clean controls | 48 | 49 |
| Development | 44 | 45 |
| Distinct lap | 44 | 45 |
| Completed before cutoff | 20 | 21 |
| Evidence available before cutoff | 20 | 21 |
| Compound | 12 | 13 |
| Tire age | 4 | 5 |
| Race progress | 4 | 5 |

The recovery ledger covers all archived Austria laps, including
chronology, tire and progress exclusions. The cached replays yielded
one new clean control, 27 intermediate laps and 18 unsupported laps.
Car 31's lap 43 completed at 14:11:12.074 UTC. Its evidence was
available by 14:12:22.833 UTC, before the target cutoff at
14:14:53.993 UTC. The lap used hard tires at age 23, compared with
age 18 for the target, and had 96.9% usable physical coverage.
The cached match met the stop condition, so the frozen request list is
empty. No new telemetry was acquired.

## Feasibility under the frozen contract

| Race | Census | Three clean laps | D8 supported | Supported physical approach | D8 with five available controls | A, frozen valid laps and groups | B, physical coverage lap and group upper bounds | C, control acquisition lap and group upper bounds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Austria | 633 | 312 | 293 | 3 | 47 | 0 / 0 | 6 / 6 | 1 / 1 |
| Hungary | 780 | 351 | 280 | 4 | 113 | 0 / 0 | 63 / 60 | 0 / 0 |

A counts the original transition-pilot targets. The recovered Austria
lap raises the observed count to one lap and one independent group.
B holds the original control pool fixed and excludes cutoffs with
known immutable outcome failures or resolved non-following outcomes.
The census covers laps 8–46. It excludes the separately audited
Austria lap 47, which is included in C through the observed following
ledger. B assumes the best possible missing outcome and gives unresolved
opponents the most favorable grouping. Only three of Austria's six
unresolved candidates and seven of Hungary's 63 have a known supported
close opponent in the cached cutoff evidence. Forty-four of the Hungary
possibilities have no observed lap screen in either acquisition ledger.
C keeps observed physical following and D8 support fixed. Its entire
potential was realized by the cached control recovery.

The 6 and 63 are ceilings, with zero additional verified targets from
physical recovery. The cutoff-first selector found 19 supported close
starts in Austria and 11 in Hungary under the new ranking, with zero
frozen following outcomes at those starts in the transition replay.
The ranking is a retrospective diagnostic of a point-in-time rule.
Its acquisition yield has yet to be tested. A broad download would
mostly test unknown outcomes and is unwarranted on this evidence.

[Diagnostic figure](stage5_s3_c_encounter_diagnostics.png) · [Metrics and bounds](stage5_s3_c_encounter_metrics.json) · [Detailed ledgers](stage5_s3_c_encounter_ledgers.zip) · [Recovery lock](stage5_s3_c_encounter_recovery_lock.json) · [Frozen selector specification](../../config/experiments/stage5_s3_c_encounter_pilot.yaml)

## Reproduction

Run from the project root with the existing warehouse, D8 artifact and
cached raw telemetry. The baseline Git revision was
`a33907a4ed21271fc15eb02cec332575a666ae56`.

```powershell
$env:PYTHONPATH = "$PWD/src"
$py = 'C:/Projects/f1-race-intelligence/.venv/Scripts/python.exe'
$wh = 'C:/Projects/f1-race-intelligence/data/warehouse/f1ri.duckdb'
$d8 = 'C:/Projects/f1-race-intelligence'
$base = 'artifacts/runs/stage5_s3_c_encounter_repro_baseline'
$hist = 'artifacts/runs/stage5_s3_c_encounter_repro_history'
$census = 'artifacts/runs/stage5_s3_c_transition_census_v1'
$transition = 'artifacts/runs/stage5_s3_c_encounter_repro_transition'
& $py -m f1ri.cli stage5-s3 --warehouse $wh --output artifacts/runs/stage5_s3_encounter_repro_singapore
& $py -m f1ri.cli stage5-s3-continue --warehouse $wh --stage3-root $d8 --coverage-pilot config/experiments/stage5_s3_c_coverage_pilot.yaml --output $base
& $py -m f1ri.cli stage5-s3-continue --warehouse $wh --stage3-root $d8 --history-pilot config/experiments/stage5_s3_c_history_pilot.yaml --history-baseline $base --output $hist
& $py -m f1ri.cli stage5-s3-continue --warehouse $wh --stage3-root $d8 --history-pilot config/experiments/stage5_s3_c_transition_pilot.yaml --history-baseline $base --transition-history $hist --transition-lock "$census/lock.json" --output $transition
& $py -m f1ri.cli stage5-s3-encounters --warehouse $wh --stage3-root $d8 --transition $transition --history $hist --baseline $base --census $census --output artifacts/runs/stage5_s3_c_encounter_repro
& $py -m pytest -q tests/test_traffic_encounter.py tests/test_traffic_transition.py tests/test_traffic_history_study.py tests/test_traffic_coverage.py tests/test_traffic_continuation.py tests/test_track_proximity.py
& $py -m pytest -q
```

All five frozen metrics reproduced byte for byte. The transition
metrics SHA-256 is
`3553231894979528f28db4173a03b2c9fbfbd3c26ef67fbc3c721d88f0649db9`.
The warehouse SHA-256 is
`9404346d9aef1ef44ee9cea7e36c3f66475dd1d9495a62aed3e0c0e35a0e53d0`.
D8 reproduced
`31734c2244b3bdbd555c1407fe62207b3edf0eaddafe85e722b2cc936c75923c`.
Its training and calibration cutoffs remain 2024-10-20 and 2025-06-01.
The source hashes, plan, cutoff census and acquisition lock are recorded
with the metrics and detailed ledgers. The encounter metrics SHA-256 is
`ed93d599b55882da255c7e37aac1463e38daa7e2fa891ab109762dc08534d1d5`.
Two cache-only encounter runs reproduced all output files byte for byte.

The change adds the encounter CLI, the support and conversion audit,
the earlier-control recovery workflow, diagnostic plots and focused
tests. The frozen simulator, label, matching and model implementations
remain unchanged.

The focused checks passed 55 tests. The full suite passed 187 with 14
existing NumPy deprecation warnings from the pit-neutralization tests.
