# F1 Race Intelligence Engine — Stage 5 S5-B
## Historical neutralization reconstruction and simulation validation

**Path B — Partial historical support.** S5-C can use deterministic strategy scenarios with the pace, queue, restart and pit assumptions displayed. The present evidence supports archived control chronology and descriptive timing in Imola. The physical Safety Car train, historical rejoin order and strategic pit counterfactuals remain unvalidated. The independent sample is one VSC and one SC episode from one race and one layout. Singapore supplies two local-caution controls.

## 1. Sources and chronology

The Stage 1 state machine reproduced all six frozen intervention windows from the archived message excerpts. Singapore's yellow and double-yellow windows generated no global control commands. The two Imola track-wide episodes kept the original boundaries.

| Episode | Deployment UTC | Ending or in-this-lap UTC | Track clear UTC | Duration |
| --- | --- | --- | --- | ---: |
| Imola VSC | 13:42:34 | 13:46:03 | 13:46:14 | 220 s |
| Imola SC | 14:06:27 | 14:20:09 | 14:21:42 | 915 s |

Race-control timestamps give reported chronology. Driver receipt and reaction clocks are absent. Track clear is the declared SC withdrawal proxy. The physical withdrawal time is unknown. The archived message authorizing car 87 to unlap at 14:18:03 UTC identifies a procedure the simulation does not reproduce.

The local warehouse contained neither approved session. The original Stage 3 runtime and D8 bytes remained unavailable. The study used the hash-verified S5-A and S5-A.1 release archives, existing S3 geometry and telemetry, and six frozen Imola OpenF1 queries. Two lap-timing queries returned 428 rows. Four short location queries returned 17,192 fixes across the VSC deployment, SC deployment, announcement and clear windows. The raw lap ends agreed exactly with all 271 shared archived S0 lap markers. They derive from the same timing feed. The bounded location feed provides different observations, with archive samples indexed by event time and historical receipt times unknown. Additional cache candidates were inventoried by filename and manifest presence, preserving their original race roles. [OpenF1 source documentation](https://openf1.org/docs/)

## 2. Why the original Imola localization was 0 of 18

The old field location cache had no request between 14:01:33 and 14:22:45 UTC. At SC deployment, announcement and clear, all 18 drivers' latest cached fixes were stale. The complete eligibility ledger records the nearest samples on both sides, the first failed gate, timing evidence, and predecessor support. Later fixes are recorded for diagnosis and stay out of cutoff estimates.

| Cutoff | Existing H0 | With bounded acquisition | Resolved physical predecessors |
| --- | ---: | ---: | ---: |
| SC frozen initialization, 14:06:22 | 0/18 | 18/18 | 17/18 |
| SC deployment, 14:06:27 | 0/18 | 18/18 | 18/18 |
| SC in this lap, 14:20:09 | 0/18 | 11/18 | Full-field order unavailable |
| SC clear, 14:21:42 | 0/18 | 18/18 | 0/18 |
| VSC frozen initialization, 13:42:29 | 0/19 | 13/19 | Full-field order unavailable |
| VSC deployment, 13:42:34 | 0/19 | 19/19 | 16/19 |

At the SC announcement, seven cars lacked a usable recent velocity estimate. At clear, position uncertainty overlaps nearby cars' bounds. The ledger keeps their coordinates and their unresolved order separately. There is no observed queue-formation timestamp. Same-lap crossing spread for 18 drivers fell from 145.301 s on lap 46 to 46.878 s on lap 52. These timing spreads can include lap relationships and pit effects. They do not establish simultaneous following gaps or a physical train.

The support hierarchy records **H0** projected location with heuristic uncertainty, **H1** sector-constrained interval, **H2** lap-line and lap-count support, **H3** classification or active-roster support, and **H4** declared simulation state. The new sectors support retrospective bounds. The archive does not supply the publication clocks needed to use those sector bounds for a cutoff-initialized Replay C.

## 3. Timing and model validation

| Episode and matched population | Replay B median absolute line error | Replay B median absolute lap-duration error |
| --- | ---: | ---: |
| Imola VSC | 28.380 s on 63 markers | 10.992 s on 44 consecutive laps |
| Imola SC | 283.580 s on 108 markers | 74.794 s on 90 consecutive laps |

The four complete, non-pit VSC laps had a median duration of 113.715 s. The five partially neutralized non-pit laps had a median of 102.660 s, and 48 post-clear GREEN laps had a median of 81.153 s. These populations describe observed durations. They differ in drivers, lap phase and session exposure, so these differences do not estimate a causal VSC effect. A clean pre-VSC comparison could not be certified from the frozen message subset. The timing ledger retains sectors and their interval overlap. The simulator's equal-distance checkpoints do not coincide with the archived timing sectors, so direct model-sector error is unavailable.

On the 108 common SC markers, Replay A and B both had a median absolute error of 283.580 s. Mean absolute error fell from 272.117 to 264.168 s. On the full variant-specific populations, the medians were 283.580 s on 108 markers and 284.941 s on 126. Replay B applied global GREEN at the archived clear, correcting the 132.663 s delay. The 18 drivers' earliest recorded lap-line crossings after clear were 21.403 to 28.383 s later. Replay B's simulated passing eligibility arrived 79.924 to 83.830 s after clear. Actual historical passing-permission and physical withdrawal times remain unknown.

Replay D could not run at either original initialization. Car 5's predecessor was unresolved at the SC start, and six VSC drivers lacked eligible fixes at the VSC start. At SC deployment, the ordering bounds were sufficient, but the current simulator accepts only exact scenario coordinates. A separate bounded sensitivity experiment supplied exact scenario points at the centre and both signed endpoints of the projected bounds. All three SC runs retained the same 108 markers and yielded medians of 138.891–139.658 s. These scenarios begin five seconds later than frozen Replay B. The fixed-cutoff endpoint comparison measures sensitivity within those sampled bounds. The improvement versus B also includes the changed initialization clock and cannot identify a causal spacing effect. No VSC sample passed the deployment order gate.

Archived lane-event pairs were available for 15 VSC stops and nine SC stops. Their elapsed times arise from the frozen S0 route reconstruction. The archive supplies no independently matched physical merge, stationary service or stay-out counterfactual for these comparisons. S2 and S5-A.1 retain their synthetic pit validation.

## 4. Reproducibility and S5-C scope

The 300-test baseline and final 311-test suite passed with 104 warnings in the available Python environment. Ruff passed. The release verifies byte hashes for S5-A.1, S5-A, S4-B.1, S4-B, S4-A and S3-C4. The D8 hash was checked against its frozen manifest. All earlier research cohorts and simulator defaults remain unchanged.

Two independent runs produced 25 byte-identical generated files. The metrics SHA-256 is `a49ce8809f8169d8eacfe8f640f4e406b52780a3d37aeaa2b326794772d8b9df`. The release archive contains the checksummed results and the exact six bounded source responses with their retrieval metadata.

The diagnostic figure and ledgers preserve absolute and relative errors, model support, exclusions, and incomplete evidence. The study did not fit deployment probabilities, retune pace, or open sealed outcomes. An S5-C scenario tool may expose the tested deterministic mechanics. Empirical claims about field compression, restart speed or pit advantage require further independent evidence and a simulator that can consume uncertain positions without treating them as exact.

[Diagnostics](stage5_s5_b_historical_neutralization_validation.png) · [Metrics](stage5_s5_b_historical_neutralization_validation_metrics.json) · [Ledgers and checksums](stage5_s5_b_historical_neutralization_validation_ledgers.zip)
