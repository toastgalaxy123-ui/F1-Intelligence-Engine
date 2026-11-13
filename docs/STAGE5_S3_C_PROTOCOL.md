# Stage 5.3-C research protocol

The continuation tests whether independently supported physical gaps and
relative clean-air pace improve one-lap elapsed-time forecasts over frozen
Pace V2 and deterministic following. The Singapore pilot is frozen at
`e3723383de1860326a15300756f9432aac5e62f9`. Its 148 tests, physical outputs,
source hashes and figures are indexed by the baseline manifest.
[Baseline manifest](../artifacts/reports/stage5_s3_c_pilot_lock.json)

## Race cohorts and availability

The initial reconstruction batch is Imola, Hungary and Monza. Development
uses Austria and Hungary. Validation uses Monza and Austin. Mexico City,
São Paulo, Las Vegas and Qatar are reserved in their entirety until
selection is locked. Race dates in the configuration are UTC. Imola
contributed to D8 calibration, and Singapore, Zandvoort and Baku have
already informed engineering. They are reconstruction or audit cohorts.
2026 remains a separate transfer study. The assignments put all development
races before validation and all validation races before final testing.
[Frozen configuration](../config/experiments/stage5_s3_c_protocol.yaml) ·
[2025 race calendar](https://www.formula1.com/en/racing/2025)

Acquire eight nonoverlapping 160-second green windows per initial race using
archived lap-start timing to seed two close windows and two clean-air
windows. Select four green windows by a seeded draw without a gap screen.
The Stage 1 seeds describe selection and remain outside prediction features.
Retrieve both location and speed for the entire declared field. Preserve
raw timestamps, hashes and rejected samples. Geometry comes from an early
green reference lap. It becomes available after the last required reference
fix and is kept fixed for later observations.

The first Monza reference failed the existing 100 m continuity check. The
retry list uses subsequent laps from the same driver and accepts the first
reference that passes the original checks. The sampling plan stays anchored
to the original selection clock. Any window before the accepted geometry
became available is excluded.

Monza car 27 is marked off track only after a conservative 13:06 UTC cutoff.
The formation-lap broadcast documents the return to the pits before the
race start. The declared cutoff is an availability bound, with no claim
about the exact radio timestamp. Other stale lap records remain unknown.
[Formation-lap broadcast](https://www.formula1.com/en/video/2025-italian-grand-prix-hulkenberg-forced-to-retire-on-the-formation-lap.1842611125141941376)

Point-in-time extrapolation can use a car speed observation no older than
0.45 s when the recent position pair cannot support velocity. The estimate
still requires a valid location fix. Its uncertainty grows faster and the
speed observation timestamp is retained for audit.

Independent checks compare sector and control-line timing, and previous
racing-route passage identity and gap. Removing location fixes is a
sensitivity test using shared source coordinates. Coverage, availability,
and timing error are reported by circuit. Gap bounds remain heuristic
until independent interval coverage is measured. The 90% complete-field
coverage target is an engineering diagnostic. Disputed timing and
unresolved route positions are ineligible for labels.

## Exposures, controls and target

Close following starts when the physical gap upper bound is at most 120 m.
Two successive samples above 155 m end it. Missing or uncertain neighbors,
changes of neighbor or lap relationship, pit or flag changes, and a
verified pass also end exposure. Unresolved changes are recorded as such.
A clean-air control has a physical gap lower bound of at least 225 m.
Require supported green observations for at least 90% of a target lap.
Control laps require clean air for at least 90% of their observed samples.
A following lap requires the supported cutoff encounter to continue
into the lap. A later unrelated episode does not label that cutoff.
Cutoff features use the archive prefix only. Elapsed time, episode duration,
pass events, and within-lap exposure summaries are joined as outcomes or
research annotations after the forecast. An exposure observed after the
cutoff is excluded from predictive features.

The clean reference requires a frozen D8 artifact whose training and
calibration end before the cutoff and exclude the target race, plus three
previous clean laps on the same stint. Research fold artifacts, when
needed, have distinct provenance. Keep the signed elapsed-time residual
and subtract the median residual from at least five earlier completed
development controls. Match compound, tire age within five laps and race
progress within 20 percentage points. Earlier controls in a development
race can support later targets in that race. Validation and reserved
outcomes cannot supply the correction. The adjusted residual is an
observational proxy.

## Model selection

Compare T0 frozen Pace V2, T1 checkpoint no-pass following, T2 a constant
delay, and T3 a gap-binned delay on the same supported laps. Assess T4–T7
after the simpler models and support gates have been evaluated. Assess
lap-time MAE first and report RMSE, bias, support, per-race metrics and
episode counts. Use whole-race paired bootstrap intervals and report CRPS
and coverage for distributional forecasts. Require two eligible training
races, two validation races and three reserved test races. A training
race requires at least five independent following laps and five control
laps. A selected empirical model must improve race-weighted MAE over the
best simpler baseline by at least 0.05 s, with a paired interval favoring
improvement and at least 70% common support. Freeze selection before
opening the reserved races. Retain T1 if the evidence is insufficient.

Use `ENGINEERING_VERIFIED` for known-result mechanics,
`RECONSTRUCTION_VALIDATED` for checked physical evidence,
`STATISTICALLY_VALIDATED` for reserved forecasting results and
`RESEARCH_ONLY` for experimental outputs. Statistical approval and
dynamic simulator approval have separate gates. A lap-level model needs
independent traversal-level validation before checkpoint integration.

## Paired coverage replay and Hungary extension

The coverage command adds three comparisons to the frozen point-in-time
replay. One disables archived speed fallback. One interpolates using the
same cutoff-visible route mask. The original retrospective replay keeps its
archived pit mask and is reported separately. All modes share the sample
clock, roster and geometry. Diagnostics keep the evidence-backed off-track
rows and every unresolved driver. The sector for a loss is the sector of
its latest projected observation when available. The first and last second
of a window are reported separately from its interior.

The development pilot is frozen to Hungary. Within each stratum in priority
order, it uses the earliest original anchor for which 300 seconds before
and 20 seconds after fit inside a high-confidence green interval after
geometry availability. Each block is disjoint from previously selected
blocks. This produces one close, one clean and one random 480-second block.
The original draws remain intact. Existing bounded slices are reused wherever
they overlap a block. New queries fill the uncovered intervals with one
second of overlap at internal joins. Merging deduplicates the raw driver
and timestamp keys and excludes conflicting coordinates or speeds.
The longer blocks still use the existing four-driver timing screen. The
final block supplies any outstanding check slots, keeping failed marker
checks in the denominator.
[Frozen acquisition budget](../config/experiments/stage5_s3_c_coverage_pilot.yaml)

The audit keeps all sampled lap-start cutoffs in a separate table and
records full-window eligibility and censoring on the outcome ledger.
The matched-control clock is bounded by elapsed completion, next timing
lines and relevant archive evidence for overlapping laps and pit reports.
Pre-cutoff same-stint history comes from the existing D8 request builder.
An earlier completed development control must have its audit evidence
available before a target's cutoff. The extension uses one record per
session, driver and lap. Within the blocks it uses the extension replay,
and it may use earlier original controls outside the blocks. These clocks
use the observation-time proxy for source availability.

Report fragments and sampled segments separately from unique following
laps, drivers, stints and driver–leader–stint groups. One group may
contribute several laps or fragments, so group counts are concentration
diagnostics. Imola stays reconstruction-only. Hungary was part of the
reported Stage 3 test cohort, with D8 selection and calibration on the
earlier validation cohort. Monza is an inspected validation race. This
increment leaves model fitting and the reserved races closed.

## Austria and Hungary history pilot

The follow-up pilot uses two 2025 development races in chronological
order. Stage 3 lists both as test races. The frozen Pace V2 training and
validation input sets exclude them. Keep Imola reconstruction-only and use
the frozen Monza validation results. Austin and the reserved cohorts stay
sealed for this increment.

The one-lap cutoff population uses lap starts from laps 8 through 46.
The close arm ranks prospective opportunities by a seeded hash within
early and middle phases. It inspects up to 36 close candidates per phase
and race. A fresh positive archived interval at or below 1.4 s supplies
the close signal. The cutoff-visible D8 request must contain at least
three earlier clean laps on the same stint. Select early controls first,
using fresh intervals at or above 3.0 s and the earliest disjoint anchors
in laps 8–24. The random comparison ranks the eligible green cutoff grid
without a gap or history screen. Stage 1 signals and selection intervals
are absent from the prediction features.

Take at most two control blocks, two history-qualified blocks and one
random block per race. Each block covers 320 seconds, starts 20 seconds
before the anchor's whole-second clock and must lie in one high-confidence
green interval after geometry availability. The source cache reuses
verified baseline slices and fills gaps in at most 180-second requests,
with one-second seam overlap. The run stops after the frozen block and
request limits. It does not expand in response to target yield. The
Hungary layout is frozen. Austria uses Norris lap 5 on the FIA 4,326 m
centreline reference. Cutoff-dated broadcast evidence excludes the
documented Austria withdrawals only after the configured clocks.
[Frozen history plan](../config/experiments/stage5_s3_c_history_pilot.yaml)

The previous same-stint history audit records both the elapsed completion
and the next timing line from the frozen Stage 1 prefix. Its physical
cross-check uses only already acquired prior-lap telemetry and never
changes the D8 clean-history definition. It reports abstentions when
full-window or simultaneous-field evidence is insufficient. Each
supported residual records the control count after the development,
distinct lap, completion, evidence time, compound, tire-age and progress
filters. The extension takes precedence for shared lap keys. The
retrospective control proxy remains observational.

The candidate readiness audit counts at most one independent encounter
per race, follower, cutoff leader and follower stint. It counts the
cutoff encounter only, so multiple later fragments or repeated following
laps cannot add independent support for the same group. This is a
conservative counting rule. It leaves the frozen exposure construction
and the historical replay available. The four-driver timing gate and
the existing race, following and control minimums apply.
