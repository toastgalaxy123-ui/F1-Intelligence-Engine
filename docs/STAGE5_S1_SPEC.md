# Stage 5.1 — Pace V2 integration

S1 advances an observed field under a dry, green-flag continuation of each
driver's current stint. Free-running timing estimates clean-air potential.
No-pass timing tests delay accounting on a synchronized shared route with
declared order. S1 supplies pace distributions under the scenario. Race
outcome probabilities require future pit, traffic, passing, incident and
race-control models.

## Frozen model and chronology

Load the saved Stage 3 D8 artifact and verify its digest, source/configuration
provenance, training split, calibration split and compound mapping. Use the
latest calibration data as well as the model's training cutoff to gate
historical inference. Imola 2025 belongs to Stage 3 calibration and may only
be used for retrospective integration parity with this artifact. Reproducing
a saved prediction at the same input and seed is a mechanical check. S1
requires the existing artifact and does not fit a replacement.

## Snapshot boundary

Use one timezone-aware UTC cutoff for the field. Observe each car's latest
known crossing independently. A completed lap is admitted only after the
following lap's dated start confirms the crossing and the timed result is
available. Mask durations, sector times and pit-out flags for in-progress
rows. Rebuild clean-air history on truncated source observations. Use dated
pit reports, track control, intervals, positions and weather as of cutoff.
The completed race result and future stint ends do not enter the snapshot.
An observed pit-out lap start may establish a new tire state, subject to
pit timing evidence. Flag drivers with insufficient progress or tire evidence.

Use elapsed time since the last observed crossing for an asynchronous field.
Until a dated intermediate timing feed is supported, current-sector position
is unknown. Allocate the sampled lap's remaining time across its remaining
checkpoints with an explicitly approximate progress rule. A physically
constrained no-pass run requires synchronized checkpoints and a declared order.

## Trajectories and scheduling

Use the Stage 3 request and forecast contracts. A single request draws a
complete trajectory per realization. Keep the model's means, intervals,
support flags, compound/age progression and within-driver correlation.
Use versioned driver-specific seeds from session, cutoff, driver, stint and
master seed. The seed and batch size identify the reproducible ensemble.
D8 models no shared stochastic driver-to-driver shock for this integration.
For an identical seed, request and sample count, adapter values must match
standalone D8. Record clean-air potential, partial-lap elapsed time,
quantized checkpoint durations and following delay separately.

## Validation gates

- Compare to frozen standalone Stage 3 examples and saved forecast outputs.
- Mutate future laps, weather, pits, track states, results and stint endpoints
  and verify predictor snapshots stay fixed. Enforce the training and
  calibration cutoff and regulation era on historical requests.
- Verify timing conservation, tire ages, deterministic replay, stochastic
  seed reproduction and field order independence. Preserve S0 regression tests.
- Evaluate horizons 1, 3, 5, 10, 15 and 20 where supported. Join future
  outcomes after prediction and report endpoint and complete-window eligibility
  separately. Use race-weighted errors, interval coverage and CRPS.
- Distinguish Stage 3 parity fixtures, reused chronological test races and
  any genuinely new holdout. Report 2026 separately and require an era-matched
  model for forecasting. Preserve Stage 3's new-stint bias and long-horizon
  calibration limitations in the validation report.
