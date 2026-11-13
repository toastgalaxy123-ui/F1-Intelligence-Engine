# Stage 5 S5-A — Deterministic race-control validation

**Decision — Path B.** The declared mechanics pass the synthetic and archived-status checks. A narrow stabilization increment should bind pit diversion to the simulated entry boundary, then retime the S2 itinerary and box queue. The Imola replay also identifies a concrete restart limitation. The field-wide line gate delayed simulated release by 132.663 seconds relative to archived track clear. The next validation should use supported physical snapshots and assess release per driver.

[Specification](../../docs/STAGE5_S5_A_NEUTRALIZATION.md) · [Diagnostics A–P](stage5_s5_a_neutralization_mechanics.png) · [Metrics](stage5_s5_a_neutralization_mechanics_metrics.json) · [Run ledgers](stage5_s5_a_neutralization_mechanics_ledgers.zip)

## Architecture and execution

S0 already owns the race clock, checkpoint commits, pit-route transitions, and classification. S3 owns the circular following relation. S4-A adds a dynamic physical chain, pass proposals and generation invalidation. S5-A extends that field engine with a race-control policy and the same scheduler. All-green calls delegate to the original path. The movement input is a declared per-car clean pace. Existing Pace V2 and D8 outputs retain their role as latent pace inputs, with no new random draws or model fits.

The rule profile uses the FIA 2025 Issue 5 reference. It records the source revision and supported procedures. Same-tick race-control commands have priority over car events. Elapsed movement up to a change is retained, and the future checkpoint schedule is regenerated. Local yellows retain green-path movement and conservatively veto ordinary passes. The three equal-distance checkpoint segments support a declared interpolation and do not encode FIA marshal-sector timing. [FIA regulation archive](https://www.fia.com/regulation/category/110) · [2025 sporting regulations Issue 5](https://www.fia.com/system/files/documents/fia_2025_formula_1_sporting_regulations_-_issue_5_-_2025-04-30.pdf)

## Synthetic validation

All 32 synthetic scenarios and six historical windows passed the 11 recorded invariant checks. The fixtures cover deployment one microsecond before, at, and one microsecond after a checkpoint, VSC ending, Safety Car collection, lapped cars, pit integration, RED clocks, escalation, retirement, and finish under caution.

| Case | Measured result |
| --- | --- |
| VSC | The gap remained constant through active and ending phases. The crossing at the exact deployment boundary committed once at 10.000000 s. |
| Three-car Safety Car | Deployment at 5 s. The train criterion was first observed at 10 s, with two 12 m follower gaps and 76 m total compression from the initial state. |
| SC release | A green request at 62 s deferred until 72.400001 s in the three-car fixture, after the active field crossed the line following withdrawal. |
| Passing | The pending pass canceled at deployment. A pass completed at 7.500 s stayed committed through the later intervention. A separate test exercised a rejected caution attempt and a successful post-release attempt. |
| RED | The hold fixture stopped all racing progress for 55 s. The resumption fixture preserved 30 s of suspended time and released the rolling field at 101.200001 s. |
| Finish | Four caution and restart fixtures used the existing finish transition and classified cars 1, 3, and 2 in that order. Car 2 finished one lap down. |
| Unsupported rules | Unlapping, standing restart, stationary RED service, coarse rejoin, and a missed declared entry remain explicit unsupported cases. |

RED elapsed time uses the simulator wall clock. Active elapsed time subtracts suspension. The declared stationary hold preserves checkpoint, fractional progress and classification until the rolling resumption. A release interrupted by a new RED is superseded. Official session-limit and restart reordering procedures remain outside this mechanical scope.

## Pit accounting

| State | Elapsed entry to merge | Stationary service | Stay-out to the same marker from entry | Strategic loss |
| --- | ---: | ---: | ---: | ---: |
| GREEN | 57.000 s | 3.000 s | 31.667 s | 25.333 s |
| VSC | 57.000 s | 3.000 s | 48.556 s | 8.444 s |
| SC collection | 57.000 s | 3.000 s | 31.667 s | 25.333 s |

The 57 seconds include 26 seconds of lane motion and 28 seconds of route transitions. The SC stay-out car in this fixture was already slower than the configured pacing ceiling, so its strategic loss matched the GREEN control. The shared-box fixture preserved both three-second services and a two-second wait for car 4. The comparison uses the same field, intervention and checkpoint in each stay-out run. Across fixtures a resolved S2 route is usable when its declared entry still fits the simulated checkpoint sequence. Shifted entry, box reevaluation and retimed merge are the remaining Path B work.

## Approved archive replay

Singapore's two archived caution windows produced zero global neutralizations. Imola VSC deployed at 13:42:34 UTC, entered ending at 13:46:03 and released at 13:46:14. The recorded deployment and release clocks were applied exactly. The SC deployment at 14:06:27 was also applied exactly. The archived in-this-lap message arrived at 14:20:09 and track clear at 14:21:42. The line-gated simulator release occurred at 14:23:54.663121 UTC. An archived instruction for a lapped car was retained as unsupported.

Imola's declared field uses the active on-track set and completed laps from the timing replay, plus synthetic within-sector distances and pace. The independent line comparison has a median absolute error of 28.380 seconds over 63 VSC-window matches and 283.580 seconds over 108 SC-window matches. The archived pit and retirement events are comparison evidence. The replay establishes timing ingestion and state execution on the declared field. Historical physical behavior, exact FIA delta compliance, and predictive restart pace remain unvalidated. No OpenF1 calls were made.

## Reproduction

The baseline revision was `46be33a1fd80b9c5f1a7c9addb643d5ee064a232`. The 269-test baseline reproduced. The final suite passed 283 tests with the existing 14 warnings. The 14 focused tests and Ruff passed. The S4-A replay reproduced all 12 generated artifacts byte for byte with S1, S2, S3 and T1 parity. The study verified S4-B, S3-C4 and D8. The pasted S4-B.1 hash differs from the committed artifact. The committed report and both frozen runs agree on `438026418c126c0002999e4f2d038e4ef383da4f9cd230de6b5a8e9fb1690792`, which the experiment uses.

Two S5-A runs reproduced 20 generated files byte for byte. The metrics SHA-256 is `c345b7e78f96168c7b72f367362bece02534b675942ba44861d759a4b5cfe54e`. The archive contains the rule profile, synthetic and historical inputs, trace and progress ledgers, source hashes, invariant checks, and diagnostics.

```powershell
$env:PYTHONPATH = "$PWD/src"
$py = 'C:/Projects/f1-race-intelligence/.venv/Scripts/python.exe'
$wh = 'C:/Projects/f1-race-intelligence/data/warehouse/f1ri.duckdb'
& $py -m f1ri.cli stage5-s4-a --warehouse $wh --stage3-root C:/Projects/f1-race-intelligence --s1 artifacts/runs/stage5_s4_a_s1_replay --s2 artifacts/runs/stage5_s4_a_s2_replay --singapore artifacts/runs/stage5_s4_a_singapore_replay --transition artifacts/runs/stage5_s3_c_forensics_transition --output artifacts/runs/stage5_s5_a_s4_regression_v1
& $py -m f1ri.cli stage5-s5-a --warehouse C:/Projects/f1-race-intelligence/data/warehouse/f1ri.duckdb --stage3-root C:/Projects/f1-race-intelligence --s4-a-replay artifacts/runs/stage5_s5_a_s4_regression_v1 --output artifacts/runs/stage5_s5_a_neutralization_mechanics_release_v3
& $py -m f1ri.cli stage5-s5-a --warehouse C:/Projects/f1-race-intelligence/data/warehouse/f1ri.duckdb --stage3-root C:/Projects/f1-race-intelligence --s4-a-replay artifacts/runs/stage5_s5_a_s4_regression_v1 --output artifacts/runs/stage5_s5_a_neutralization_mechanics_repeat_v3
& $py -m pytest -q --disable-warnings
```
