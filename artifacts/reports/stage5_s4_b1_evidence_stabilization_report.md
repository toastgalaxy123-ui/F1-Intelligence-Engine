# Stage 5 S4-B.1 — Historical overtaking evidence stabilization

**Decision — Path C.** The five frozen competitive brackets now resolve to three Tier B and two Tier C cases. Austria and Hungary contribute one Tier B event each. No event meets Tier A. The two candidate telemetry gaps contain timing-line screening candidates and almost no physical evidence, and both are in existing development races. Defer empirical overtaking and advance to Stage 5 S5 race-control and neutralization mechanics.

[Protocol](../../docs/STAGE5_S4_B1_EVIDENCE_STABILIZATION.md) · [Event timelines](stage5_s4_b1_evidence_stabilization_timelines.png) · [Diagnostics](stage5_s4_b1_evidence_stabilization.png) · [Metrics](stage5_s4_b1_evidence_stabilization_metrics.json) · [Evidence ledgers](stage5_s4_b1_evidence_stabilization_ledgers.zip)

## Competitive evidence

| Race and pair | Archived UTC bracket | S4-B.1 decision |
| --- | --- | --- |
| Hungary 1→4 | 13:20:21–13:20:22 | Tier C. Target already passed the sector marker in the timing feed while the physical reconstruction still placed him behind it. |
| Hungary 14→4 | 13:20:18.500–13:20:19.500 | Tier C. The same stationary target location and timing conflict affect this bracket. |
| Hungary 1→22 | 13:30:56–13:31:00 | Tier B. Shared markers and lap chart support the direction. The 4-second interval exceeds the Tier A width gate and the target subsequently pitted. |
| Austria 30→10 | 14:05:16.500–14:05:20.500 | Tier B. Shared markers and lap chart support the direction. The interval is 4 seconds and the target's lap-38 pit cycle limits attribution from line order. |
| Imola 10→18 | 13:23:23.500–13:23:26 | Tier B, reconstruction only. Shared markers and lap chart support the direction. The interval is 2.5 seconds and the target had just pitted. |

For both Hungary lap-13 cases, all 13 target location records in the local raw timeline have identical coordinates. The FIA chart keeps Norris ahead of both attackers at the surrounding lines. Line order alone leaves room for a transient exchange. The repeated coordinates and independent sector-timing disagreement make these two physical reversals ambiguous. The three corroborated brackets exceed four native grid periods. The attainable Tier A count for the five existing events and these sources is zero. The official charts and warehouse timing may share an upstream timing feed. The projected gap and reconstructed neighbors share the location feed. No exhaustive independent annotations were available for precision or recall. [Hungary FIA lap chart](https://www.fia.com/sites/default/files/2025_14_hun_f1_r0_timing_racelapchart_v01.pdf) · [Austria FIA lap chart](https://www.fia.com/sites/default/files/2025_11_aut_f1_r0_timing_racelapchart_v01.pdf) · [Imola FIA lap chart](https://www.fia.com/sites/default/files/2025_07_ita_f1_r0_timing_racelapchart_v01.pdf)

Published pit summaries corroborate the nearby pit cycles. Their clock convention differs from the warehouse pit records, so the dossier keeps the published lap and the archived UTC evidence distinct. [Hungary pit summary](https://www.formula1.com/en/results/2025/races/1266/hungary/pit-stop-summary) · [Austria pit summary](https://www.formula1.com/en/results/2025/races/1264/austria/pit-stop-summary) · [Imola pit summary](https://www.formula1.com/en/results/2025/races/1260/emilia-romagna/pit-stop-summary)

## Localization and observable follow-up

The 10-second ledger explains all 50,707 localization-censored cutoffs. The frozen stopping ticks show 39,581 interpolation-support failures, 9,999 ambiguous-neighbor failures, 1,067 lap-alignment failures and 60 projection failures. These causes can co-occur on one cutoff. Archived coverage diagnostics explicitly identify 38,971 unusable interpolation brackets. The native archive often regains support after the first loss. The same pair is resolved again within the horizon for 46,854 cutoffs, with a median strict supported prefix of 2.5 seconds. No alternate cached window supported the first failed tick under the same pair and geometry rules.

Localization censoring differed by race and cutoff conditions. It affected 77.1% of Austria cutoffs and 57.0% of Hungary cutoffs. The two largest windows supplied 6,791 localization stops, about 13.4% of the total. Median starting gap was 55.9 m for censored cutoffs and 61.7 m for observed non-passes. Median initial closing rate was −2.64 m/s and −1.03 m/s respectively, where positive denotes closing. Censored cutoffs also tended to occur earlier in the race and in denser fields. These differences show selection by observable conditions. Dependence on the unseen pass outcome remains unidentified. Tire data are retained as retrospective stint context because their receipt times are unknown.

The event-driven ledger records the support prefix for all 77,630 cutoffs. It preserves termination bounds, canonical pass IDs, and competing events. A later recovery leaves the first loss in place. There are 65,512 cutoffs with positive supported duration and 2,498 with the full 30 seconds. The union of supported time across unordered competitive pairs in the two development races is 35,407.5 pair-seconds. The fixed 5-, 10-, 20-, 30-second and checkpoint results remain available alongside the event-driven ledger. In the reassessed fixed population, five 10-second positives referencing the two ambiguous Hungary brackets move to `AMBIGUOUS_OUTCOME`. Positive cutoffs change from 44 to 39 and distinct referenced events from 16 to 14. The 18,055 fully observed non-passes and all frozen bracket endpoints are unchanged.

## Independence and S4-C feasibility

The same-lap population contains 73,238 cutoffs, 725 frozen encounter groups, 488 directed driver–opponent–stint groups, 258 unordered race-pair groups and 12,283 consecutive selected-sample components. The latter count describes fragmentation. The nested 40, 80 and 120 m screens select 3,137, 43,097 and 70,408 same-lap cutoffs. All include censored and non-pass observations.

Across Austria and Hungary, 502 competitive encounter groups include 244 with an observed 10-second non-pass and 489 with at least one unresolved cutoff. These categories overlap. Only 16,796 of 56,959 development same-lap cutoffs have the core gap, closing, relative-pace and weather inputs. Counting each development encounter once, one has a reassessed 10-second positive and 13 have only observed non-passes. Assigning the 488 unresolved groups either way gives a 0.2%–97.4% sensitivity range. This range is an arithmetic bound for the selected archive.

Two Tier B development events give an optimistic independent-count relative standard error of 71% under a Poisson count approximation. Race dependence can further reduce precision. Holding one race out leaves one development race for fitting. The current cohort cannot validate calibration-in-the-large, horizon Brier scores or interval-censored event-time scores on independent races.

## Gap census and acquisition decision

| Development gap | Cached physical evidence | Timing-only screen |
| --- | --- | --- |
| Austria 13:41:30–13:47:30 UTC | First second only. 80 location rows and 100 car-data rows. No supported reconstructed span. | 36 GREEN timing-line proximity rows across 16 directed pairs. Five overlapping pit intervals. |
| Hungary 14:00:06–14:13:25 UTC | First second only. 100 location rows and 80 car-data rows. No supported reconstructed span. | 58 GREEN timing-line proximity rows across 29 directed pairs. Five overlapping pit intervals. |

The line screen compares same-lap crossing timestamps within two seconds. It provides a candidate list with no verified new physical encounters or passes. Additional telemetry could improve within-race coverage. The gap census gives no defensible independent-event yield and neither window adds a development race. The bounded decision was zero OpenF1 requests. Evaluation B recovered zero complete alternate-window outcomes and zero new canonical pass events.

## Verification and reproduction

The input revision was `90bf53dd15ddc63ebb287b5b3044a06dc29d2767`. The 254-test baseline reproduced, and the final suite passed 269 tests with the existing 14 warnings. The focused suite passed 59 tests and Ruff passed. All 18 frozen S4-B generated files reproduced byte for byte. S4-A, S3-C4 and D8 matched their frozen hashes. The original nine directionally completed, two rejected and nineteen unsupported or uncertain S4-A comparison scripts remain in the frozen record. The operational following policy remains `T1_DETERMINISTIC_NO_PASS`, and Traffic V1 remains research-only.

The independent S4-B.1 runs produced 18 byte-identical generated files. The metrics SHA-256 is `438026418c126c0002999e4f2d038e4ef383da4f9cd230de6b5a8e9fb1690792`. The ledger archive includes the experiment protocol, source manifest and checksums.

Reproduce S4-B using its validation report and set its output to `artifacts/runs/stage5_s4_b1_frozen_replay_v1`. Run S4-B.1 from the repository root with the original warehouse, caches and frozen S4-B directory.

```powershell
$env:PYTHONPATH = "$PWD/src"
$py = 'C:/Projects/f1-race-intelligence/.venv/Scripts/python.exe'
& $py -m f1ri.cli stage5-s4-b1 --warehouse C:/Projects/f1-race-intelligence/data/warehouse/f1ri.duckdb --stage3-root C:/Projects/f1-race-intelligence --frozen artifacts/runs/stage5_s4_b_historical_overtakes_release_v5 --replay artifacts/runs/stage5_s4_b1_frozen_replay_v1 --output artifacts/runs/stage5_s4_b1_evidence_stabilization_release_v4
& $py -m f1ri.cli stage5-s4-b1 --warehouse C:/Projects/f1-race-intelligence/data/warehouse/f1ri.duckdb --stage3-root C:/Projects/f1-race-intelligence --frozen artifacts/runs/stage5_s4_b_historical_overtakes_release_v5 --replay artifacts/runs/stage5_s4_b1_frozen_replay_v1 --output artifacts/runs/stage5_s4_b1_evidence_stabilization_repeat_v4
& $py -m pytest -q --disable-warnings
```
