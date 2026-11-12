# Stage 4 — Pit-loss and transition contract

Stage 4 estimates time costs. It does not optimize stops, infer rejoin positions,
simulate Safety Car queues, or predict future neutralizations. The dashboard is
unchanged. This is a research implementation with explicit acceptance gates;
passing the software tests does not establish statistical superiority.

## Frozen dependency and known limitations

PaceModelV2 / DegradationV1 at `7b12014d341842f3ee4c7b34aaa3255deda79b2a`
is the comparative Stage 3 dependency. `config/experiments/stage3_frozen_reference.json`
records hashes of the core sources, configuration and existing validation report.
Stage 3 model artifacts were not included in the checkout; the original Git
revision and tracked report are preserved, rather than claiming missing artifacts
were available. New Stage 4 runs never invoke or overwrite Stage 2/3 runners.

The supplied review identifies a roughly −0.727 s observed-minus-predicted bias
in regime 0B, undercoverage at long horizons, and a required fixed-cohort 20-lap
cumulative diagnostic. These remain stabilization work, not Stage 4 fixes.
Static inspection of the frozen new-stint path found:

- `_estimate_components_at_cutoff` resets stint-specific latent state explicitly.
- It clips estimated old-tire reference age to 8–18 laps and damps the structural
  reset to 0.68, then blends the race anchor with the historical anchor at 0.88/0.12.
- For an old tire 30 laps old, clipping the reference to 18 subtracts less
  degradation from the old observed time, which can carry an overly slow base
  anchor into the new stint. Reset damping can have the same direction of effect.
  These are plausible bias mechanisms, not a validated attribution of the report's
  error. They have not been tuned using the reviewed test races.
- The existing code strips historical compound/progress terms before adding the
  structural tire effects. No change asserting a confirmed double-application bug
  is made here.

For pit labels we use a separate cross-fitted normal-lap nuisance reference,
with explicit measurement uncertainty and independent matched controls. This
avoids silently incorporating the reviewed Pace V2 bias into pit coefficients.

## Definition, timing and ownership

For a verified pit-in lap `l` and pit-out lap `l+1`:

```
net_loss = observed_in_lap + observed_out_lap
           - normal_old_tire_lap(l) - normal_new_tire_lap(l+1)
lane_duration = moving_lane_duration + stationary_duration
stop_strategy_time = normal_lap_trajectory + net_loss
```

All quantities use seconds and timezone-aware UTC timestamps. Tire age at pit
entry is completed age; the in-lap normal reference uses that age minus one.
New-tire age is the explicitly specified initial age for the out-lap reference.
Raw pit lap numbers are reconciled against both stint boundaries and the raw
`is_pit_out_lap` marker, rather than assumed to be a canonical lap index.

OpenF1 `pit_duration` is accepted only as a fallback alias for `lane_duration`.
Stationary service is already inside lane time. It is never added to measured
lane duration. Lane time and net loss are parallel outputs, not additive costs.

Pace V2 owns normal clean-air racing performance. Stage 4 owns incremental
entry/exit and abnormal pit-out transition effects inside the two-lap window.
Do not add a second warm-up cost. The additive model's route and transition
terms are statistical attributions, not independently identified physical phases.

The provider pit timestamp and lap starts are approximate. OpenF1 timestamps
timing-feed PitStop/PitStopSeries/PitLaneTimeCollection reports. The sampled
stationary phase can precede that timestamp. We therefore preserve the report
time separately and use a backwards window `[report - lane_duration, report]`
only as an approximate lane window, with unmeasured feed-latency uncertainty.
It is not a verified legal entry/exit loop. Conflicting chronology remains quality C. Exact pit-out
boundary ownership requires independent route-anchor evidence before quality A.

The provider implementation was checked at
`https://github.com/br-g/openf1/blob/9cf3019aa494be0e26ffec3b34af3bd0939357c2/src/openf1/services/ingestor_livetiming/core/processing/collections/pit.py`.
Targeted speed traces at five circuits independently showed stationary samples
before the pit-record timestamp, preventing an erroneous forward lane window.

The same upstream revision's `services/query_api/query_params.py` reconstructs
`key=value` before parsing comparisons. Consequently `httpx` filter keys are
`date>` and `date<`, with the HTTP separator supplying the equals sign. Using
`date>=` as the key produces a doubled equals sign and an invalid date filter.
The bounded telemetry adapter tests and versions that query convention.

## Tables and provenance

| Table | Grain and meaning |
| --- | --- |
| `silver.pit_lane_reference` | Circuit/layout/effective period with availability and source |
| `gold.pit_events` | Canonical visit, observed components, tire transition and quality |
| `gold.pit_transition_observations` | Event × measurement method, an estimated label |
| `gold.pit_context` | Decision cutoff × explicit tire scenario |
| `gold.pit_training_examples` | Context with separate realized targets and eligibility |

Visit IDs are `session:driver:sequence` under deterministic chronological ordering.
Identical duplicate payloads become one visit with a duplicate count. Conflicting
same-time records remain explicitly ineligible. Rebuilding unchanged snapshots
preserves identities and table counts. Chronologically earlier newly discovered
visits can renumber later sequence IDs; snapshots and their hashes must be fixed
when joining historical labels.

Missing stationary values remain NULL. Invalid durations, missing/contradictory
stint boundaries, absent out markers, impossible chronology, unknown track state,
mixed transitions and possible double stacks remain visible in the audit.
Arrival proximity flags a *possible* shared box delay; it does not diagnose one.

Routine tire changes are inferred from verified tire transitions, never short
durations. Repairs, penalties, drive-throughs, stop-go and other exceptional
causes require independently recorded evidence in
`data/raw/reference/pit_event_annotations.csv`. Classification is retrospective
target metadata, never an unknown future predictor. Merely long service belongs
to an empirical tail and is not a claim of a particular incident cause.

`pit_lane_reference.csv` contains explicitly unverified circuit-season audit
placeholders. Unknown historical speed limits are not replaced with a universal
80/60 km/h assumption. Unverified rows have no availability date and cannot be
used as known forecast features. Populate verified effective periods, source URLs,
availability timestamps and limits before testing speed/layout effects.

## Measurement methods

1. **TWO_LAP_CROSS_FIT** is the scalable green-flag label. A centered ridge
   nuisance model learns compound, tire-age and progression effects using clean
   laps from earlier races only. The target race never fits those parameters.
   At least three clean driver laps completed before pit entry anchor current
   car pace. Reference drift and timing uncertainty are retained in label std.
   A single reference artifact is fitted per target race and its earlier-race
   session list, training end and version are saved on every label.
2. **MATCHED_NON_PITTING_CONTROLS** is a supporting difference-in-differences
   estimate, using uninterrupted simultaneous clean controls within 45 seconds,
   adjusted by target/control pre-stop pace. It does not call the ridge reference.
   Tire choices can still confound it. Disagreement is reported, not blindly
   averaged away or rejected because it is large. Label uncertainty increases
   when independent estimates disagree.
3. **VERIFIED_ROUTE_ANCHORS** accepts externally verified equivalent progress
   boundaries, source hashes and resolution uncertainty. Approximate GPS alone
   is insufficient. This physical-route estimate does not automatically include
   the whole pit-out warm-up window and must not be silently interchanged with
   a two-lap label.

The pipeline does not apply a green normal-lap reference to a VSC/SC transition.
Non-green target labels remain unavailable until a matched neutralized reference
is established. Low error on a reconstructed green label does not prove the true
physical loss has been perfectly identified.

## Prediction modes

`PitLossRequest`, `PitLossDistribution`, `PitSharedScenario` and `PitStatusSegment`
are re-exported from `models.contracts`; existing pace contracts are unchanged.

- **PRE_STOP:** Only information known at the cutoff plus explicitly supplied
  hypothetical tire/service/status inputs. Realized service duration is rejected.
- **IN_PROGRESS:** A known stationary duration requires a timestamp no later than
  cutoff. The full updated distribution and the incremental loss beyond consumed
  stationary time are returned. This does not claim to observe unprovided moving
  phase progress.
- **Post-stop measurement:** Use `validation.pit_measurement`; complete historical
  observations and future realized flag intervals are allowed only here.

An information-limited request uses status known at cutoff and assumes persistence;
it never reads future withdrawal/deployment. A conditional scenario must supply
its future timeline explicitly. Scenario segments are contiguous, non-overlapping
and relative to projected entry over a declared comparable-route window. Stage 4
does not learn a future neutralization hazard.

`build_pit_opportunity` accepts raw pre-cutoff laps, stints, flags, weather,
position and intervals. It needs no future pit event. The proposed new compound
and initial age come from the scenario. Forecast predictors never include actual
future lane/service durations, positions, gaps, flags or tire decisions.
The default benchmark scenario grid is fresh HARD/MEDIUM/SOFT; observed used-tire
stops are retained for component fitting but are not falsely matched to a fresh
tire counterfactual. Public requests support specified used-tire scenarios.

## Models and uncertainty

P0/P1 are global/circuit median-location distributions. P2 adds expected service,
P3 partially pools circuits and era, P4 separates route/service, P5 pools team
service effects, P6 enables the observed delayed-service tail, P7 adds regularized
compound-transition residuals and P8 calibrates predictive scale on validation.
The model returns both expected and median loss: mean-based accuracy can differ
from median-based accuracy for heavy tails.

Service uses a partially pooled empirical ordinary/delayed mixture with threshold
4.5 seconds for the slow-stop calibration diagnostic. Missing service is never
scored as zero. Latent expected service can aid route fitting and is explicitly
separate from observed components. Joint residual variation is estimated from
measured-service labels when service is sampled separately; otherwise service
noise would be counted twice. P8 does not replace simpler models automatically.

Label uncertainty is separated from stop variation, with conservative variance
deconvolution. Shared route-parameter draws can be reused for multiple stops.
Because loss is observed minus normal reference, the shared normal-reference
perturbation has negative sign in pit loss and positive sign in matching pace.
Supplying the same draw allows that nuisance uncertainty to cancel. Matching
cannot be inferred from independently seeded Pace V2 samples; callers must
explicitly supply/certify the common reference scenarios.

N0–N3 expose neutralization requests and conditional correction infrastructure.
Corrections require independently verified route-equivalent slowed on-track
observations with enough events and races. A binary SC/VSC label or whole-lap
slowdown is insufficient. No fabricated universal VSC discount is used. Missing
evidence returns a wide, GREEN-centered **UNSUPPORTED** fallback, and integration
rejects it. Full SC queue/field effects are always outside this contract.

P9 freezes historical fitting for 2026 transfer. P10 adds strictly earlier
completed 2026 races with the same fixed specification. Neither uses the reserved
Stage 4 final-test pit outcomes for transfer fitting. Same-race online fitting is
not enabled without explicit observation-availability metadata.

## Reproduction and validation

From the repository root:

```bash
source .venv/bin/activate
f1ri ingest-batch --config config/experiments/stage4_pit_loss_v1.yaml
f1ri stage4 --config config/experiments/stage4_pit_loss_v1.yaml
pytest -q
ruff check src tests
```

For optional bounded timing verification, run `f1ri pit-telemetry-audit` before
the Stage 4 runner. It saves samples and provenance for two stops at each of
Singapore, Monza, Sakhir, Suzuka and Monaco. No whole-session traces are fetched.

The runner fails before fitting if required snapshots are missing. It never
shrinks the final cohort silently. Normal source ingestion is serial and obeys
provider limits. The Stage 4 batch manifest has its own output directory.

Training is 2023–2024, validation early 2025 and the reserved final test is Mexico,
Brazil and Qatar 2025, absent from the entire Stage 3 cohort. This deliberately
also exercises unseen-circuit transfer. These selections are frozen in YAML
and a timestamped split manifest before any model fitting. Previously inspected
Stage 3 events cannot be relabeled untouched. Separate leave-one-circuit-out
evaluation uses validation races. Subsequent reruns are regression evaluations
of the reserved set, not newly untouched evidence.

Direct lane/stationary timing and reconstructed net-loss metrics are separate.
Reports include MAE, RMSE, bias, race-weighted MAE, 50/90% coverage, widths, CRPS,
slow-stop Brier score and slices with unique event/race counts. Paired comparisons
join identical events and bootstrap races, not independent pit rows. Validation
selects the candidate; final tests never tune its hyperparameters. P8's validation
scores are explicitly pre-calibration.

The run folder retains config, source/data hashes, splits, labels, requests,
predictions, model artifacts, diagnostics A–L, an event reconstruction and a report.
Pickles are trusted local outputs only. Tests cover event chronology, NULL service,
physical recovery, exact time ownership, scenario support, probabilistic validity,
future-observation/training mutations and source preservation.

## Completion gate

Engineering implementation and verified experimental results are separate from
model approval. Do not claim Stage 4 approved while P8 lacks reliable out-of-sample
improvement over circuit baselines, neutralized references remain unvalidated,
timing reconstruction lacks independent checks, or reference data are unsourced.
The current measured outcome and remaining gates are in
`artifacts/reports/stage4_validation.md`. Stage 5 can develop against the stable
contracts while preserving these restrictions; unrestricted strategy optimization
is not justified by software-test success.
