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
