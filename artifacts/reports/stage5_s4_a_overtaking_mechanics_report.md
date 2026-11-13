# Stage 5 S4-A — Deterministic overtaking validation

S4-A executes explicitly scripted passes at shared racing checkpoints. The mechanics passed the synthetic engineering gates. The archive contained three supported pair reversals across four audited windows. The constant-speed replay reproduced one reversal at a shared checkpoint and rejected the other two. The operational following policy remains `T1_DETERMINISTIC_NO_PASS`, and Traffic V1 remains research-only.

**Decision — Path A.** Proceed to S4-B historical opportunity reconstruction and event labeling at the resolution the source supports. Retain observation brackets, route and flag checks, lap relation, and field coverage with each label. The current archive comparison leaves exact maneuver timing and predictive accuracy unvalidated.

[Specification](../../docs/STAGE5_S4_A_OVERTAKING.md) · [Diagnostic figure](stage5_s4_a_overtaking_mechanics.png) · [Metrics](stage5_s4_a_overtaking_mechanics_metrics.json) · [Run ledgers](stage5_s4_a_overtaking_mechanics_ledgers.zip)

## Implementation

S0 already commits checkpoint crossings and computes timing order. S3 establishes physical neighbors on a wrapped circuit and constrains crossing times with T1 headway. S2 owns the pit itinerary, lane and service clocks, and common merge. S4-A reuses those contracts. An immutable proposal identifies the cars, realization, layout, checkpoint, and allowed time window. A future proposal generator can supply the same inputs.

The engine checks adjacency, route, lap relation, GREEN status, shared checkpoint, remaining clean traversal, and headway. A valid proposal authorizes one pending boundary crossing. On commit the engine records the attacker's crossing, updates the physical predecessor chain, invalidates obsolete scheduler generations, and reschedules future crossings. Committed checkpoints and accumulated delay stay in the ledger. Timing order follows the committed checkpoint history. The ledger records boundary order, timing order, neighbors, and outcome separately.

The scope is a declared checkpoint window with three equal-distance segments per lap, constant clean pace per car, and one pending pass in the field. A declared clean traversal and a 105 m/s scenario ceiling bound the move. Within-sector trajectories and lateral clearance are unresolved. Finish-line passes, FIA exceptions under neutralization, and joint pit replanning are outside this increment. Resolved S2 merges can rejoin the racing chain. Coarse routes provide timing accounting and leave merge geometry unresolved.

## Synthetic results

Twenty scenarios exercised 21 attempts. Ten completed, eight were rejected, one expired, one was superseded by pit entry, and one lacked position support.

| Fixture | Measured behavior |
| --- | --- |
| Same-lap pass | Car 2 crossed checkpoint 10 at 7.500 s. Car 1 crossed it at 10.000 s. Timing order changed from 1–2 to 2–1. |
| Three-car queue | The local order changed from 1–2–3 to 2–1–3. In the successive-pass fixture, car 3 then passed car 1 at 8.889 s. |
| Committed following delay | Car 2 incurred 2.600 s of delay at checkpoint 10. It completed the pass at checkpoint 11 at 25.100 s with zero new delay for that traversal. |
| Lapping and un-lapping | Both exchanged physical order with timing ranks preserved. |
| S2 route and rejoin | Car 63 left the racing chain at 160.000 s. Car 4 gained timing rank during the pit cycle. Car 63 rejoined at 217.000 s, then passed car 4 at 237.000 s. The same-time pit-entry conflict superseded a pending pass. |
| Track status and proposal conflicts | UNKNOWN, YELLOW, DOUBLE_YELLOW, VSC, SC, and RED rejected ordinary passes. The regression suite also covers cancellation during a pending pass and checkpoint suspension during RED. Equal-time scripts resolve deterministically. |
| No script | The two-car case and frozen S3 circular fixture reproduced the original T1 run and traversal records exactly. |

All eight diagnostics passed for the 20 synthetic scenarios and three executed archive replays. They cover event clock order, increasing checkpoint and crossing time, field identity, unique crossings, unique pass commits, headway, authoritative crossing and timing order, and traversal accounting. A scenario without precision crossings contributes no numerical samples to the crossing checks. Existing S2 resolved and coarse fixtures retained 20.000 s losses. The shared-box fixture retained 20.000 s and 23.000 s losses.

## Approved archive audit

The audit used cached 2025 Singapore, Austria, and Hungary data. It screened adjacent-pair reversals for resolved racing routes, non-overlapping position uncertainty, a high-confidence GREEN interval, and pit overlap. The ledger preserves endpoint observations, field coverage, the next available lap timing and classification evidence, and the scripted outcome. The two-car simulation uses point-in-time estimates at the first endpoint and treats their centers and local speeds as declared scenario inputs.

| Session and pair | Archived evidence | Scripted result |
| --- | --- | --- |
| Singapore 9896, 63 over 18 | Lapping bracket from 13:10:33 to 13:10:35 UTC, subset field coverage | Rejected for insufficient clean pace advantage at the shared checkpoint. |
| Austria 9955, 4 over 23 | No supported reversal bracket in the configured window | No script executed. |
| Hungary 9928, 1 over 22 | Same-lap bracket from 13:30:56 to 13:31:00 UTC, full-field endpoints. The next available ranks were 15 and 16. | Rejected for insufficient clean pace advantage at the shared checkpoint. |
| Hungary 9928, 63 over 43 | Lapping bracket from 13:56:25.500 to 13:56:27.500 UTC, subset field coverage. The signed gap changed from +22.26 m to −18.38 m. | Reproduced the direction at checkpoint 117, 15.278 s from the cutoff. Timing order stayed fixed. |

The successful replay committed 13.278 s after the end of the observed bracket. The checkpoint quantization, two-car reduction, and constant-speed assumption limit this comparison. S4-B can use the observed brackets and their support levels as event evidence. A finer within-sector execution model would need separate timing validation. The audit made zero OpenF1 requests.

## Regression and provenance

The baseline revision was `a96ba1899de69c379e572c732bbb4a22b324dba5`. Before implementation the full suite passed 210 tests with 14 warnings. The final suite passed 229 tests with the same warning count, and the focused simulator suite passed 56 tests. Ruff and the staged whitespace check passed.

The S1, S2, and Singapore S3 metric files reproduced byte for byte. S1 covered six Pace V2 parity cases and 28 field cutoffs. S2 reproduced frozen D8 fresh and used replacement parity. The locked P1 artifact was unavailable in both the frozen S2 run and this replay. C3, C4, R4, D8, and the frozen Austria/Hungary transition metrics matched their expected hashes. The Austria H0 proxy remained +0.2109747886 s. The input manifest records the complete hashes for the warehouse, configuration, frozen artifacts, position reconstructions, geometry, and cached payloads.

The release and repeat runs generated byte-identical metrics, traces, and figures. The metrics SHA-256 is `9d99d3815a65b2d43722bdd2c2ac16316a689ea82392a529ce8afec1d130c5ec`. The versioned ledgers include configuration, scenario states, pass scripts, event and crossing traces, physical and timing order, pit records, historical evidence, diagnostics, and file checksums.

## Reproduction

Run from the repository root with the original Stage 3 bundle and the existing local warehouse and telemetry caches. The Austria and Hungary transition input is the previously generated S3-C forensics run.

```powershell
$env:PYTHONPATH = "$PWD/src"
$py = 'C:/Projects/f1-race-intelligence/.venv/Scripts/python.exe'
$stage3 = 'C:/Projects/f1-race-intelligence'
$wh = "$stage3/data/warehouse/f1ri.duckdb"
$localWh = 'data/warehouse/f1ri.duckdb'
$s1 = 'artifacts/runs/stage5_s4_a_s1_replay'
$s2 = 'artifacts/runs/stage5_s4_a_s2_replay'
$singapore = 'artifacts/runs/stage5_s4_a_singapore_replay'
$transition = 'artifacts/runs/stage5_s3_c_forensics_transition'

& $py -m f1ri.cli stage5-s1 --stage3-root $stage3 --holdout-warehouse $localWh --output $s1
& $py -m f1ri.cli stage5-s2 --stage3-root $stage3 --local-warehouse $localWh --output $s2
& $py -m f1ri.cli stage5-s3 --warehouse $wh --audit-warehouse $localWh --output $singapore
& $py -m f1ri.cli stage5-s4-a --warehouse $wh --stage3-root $stage3 --s1 $s1 --s2 $s2 --singapore $singapore --transition $transition --output artifacts/runs/stage5_s4_a_overtaking_mechanics_release_v4
& $py -m f1ri.cli stage5-s4-a --warehouse $wh --stage3-root $stage3 --s1 $s1 --s2 $s2 --singapore $singapore --transition $transition --output artifacts/runs/stage5_s4_a_overtaking_mechanics_repeat_v4
& $py -m pytest -q --disable-warnings
```
