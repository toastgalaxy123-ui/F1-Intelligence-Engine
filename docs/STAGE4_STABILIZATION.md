# Stage 4.1 protocol

Scope is Stage 4 validation and stabilization. No new race simulator, queue,
overtaking, strategy optimizer or Stage 5 component is implemented. Stage 3
sources/reports and the existing pit accounting contract are hash frozen.

## Visit eligibility

`features/pit_policy.py` defines the common `stage4.1-ordinary-v1` policy.
Compatible reason flags preserve more than one event circumstance. Any RED
overlap excludes ordinary evaluation, including mixed RED/GREEN transitions.
A lane stay above 60 s triggers unresolved-long-stay review. Neither duration
nor a model residual establishes a repair, penalty or mechanical failure.
Explicit source-backed annotations can establish those causes separately.
Ambiguous timing and unresolved possible double stacks are excluded.

An inferred tire change is `ROUTINE_UNVERIFIED`, even when its stint boundary is
reconciled. `ROUTINE_CONFIRMED` needs additional recorded evidence. Screened
unverified visits remain usable, with their grade reported; a confirmed-only
sensitivity exposes the lack of independently confirmed visits.

Direct lane duration already contains stationary service. Missing stationary
duration is missing evidence. Recorded driver BLUE flags in the transition or
multiple pit operations invalidate a single-stop normal-lap counterfactual,
while retaining independently usable measured lane/service observations. GREEN
net-label evaluation also requires a GREEN forecast cutoff. Every model uses
the same eligibility population and identical targets within each comparison.
The raw event records and excluded extremes remain in the audits.

## Historical evidence and targets

The training expansion uses 2023 Las Vegas and late-2024 Mexico, Brazil, Las
Vegas, Qatar and Abu Dhabi. Training ends before the four early-2025 validation
races. Service measurements are audited by source session, circuit and team.

The primary two-lap label remains a past-race ridge normal-lap estimate. Its
supporting check uses a separately fitted robust Huber reference and non-pitting
control pairs within 45 s of the same interval. Both target tire states enter
the reference. Controls and target are compared at equal two-lap progress.
Reference coefficients exclude the target race and use strictly earlier
development races. Local pre-stop anchors use only prior laps. The supporting
method can use other drivers' observed transition laps as a historical label
check, never as pre-stop forecast features.

Both estimators share timing feeds and assumptions; their agreement does not
identify common bias or certify legal pit-entry/exit anchors. Disagreements
are retained with uncertainty. Large residuals do not determine eligibility.

The tire scenario grid retains standard fresh dry-tire scenarios and adds an
explicit realized used/wet tire scenario when needed for historical conditional
evaluation. Supplying the actual tire scenario answers a conditional question;
it does not demonstrate prediction of the team's future tire choice. A missing
age is never treated as a known fresh tire. Dry-only normal-reference labels
are not manufactured for wet transitions.

## Selection and reserved tests

The immutable preregistration records the new test populations before new test
snapshots are fetched. Familiar circuits are Zandvoort and Las Vegas; unseen
circuits are Montreal, Silverstone and Baku. Earlier inspected reserved races
are diagnostic only. The runner verifies fitted circuit membership.

P0/P1/P3 form the simple benchmark pool. P2/P6/P7/P8 remain candidates. Selection
uses race-weighted CRPS on chronological validation folds, with a 10% MAE
tolerance against the best simple benchmark and evidence from at least three
independent service weekends for a tail-capable candidate. Service Brier score,
50/90% coverage and interval widths are reported. Complexity alone grants no
promotion; a simple net-loss fallback has no approved slow-service risk role.

P8's calibration scales are prespecified as 0.5, 0.75, 1, 1.25 and 1.5. Each
validation race uses strictly earlier validation outcomes, with at least two
prior races and ten eligible labels. This rolling-prefix implementation is
stricter in time than a non-temporal leave-one-race-out calibration. The final
scale uses validation only. Calibration changes statistical physical residual
variation while preserving the shared normal-reference error and measured
service/lane distributions. Component reconciliation is statistical, not a
physically measured route decomposition.

Selection locks configuration, source/reference CSVs, development parquet
files and model pickle hashes. The test access log is written before the first
reserved-test ingestion. Once written, it prohibits refitting/reselection for
that study. Both test populations are evaluated separately on the frozen
models. Race bootstrap intervals based on two/three races are descriptive.

## Official circuit references

`f1ri pit-reference-import --metadata source.json` downloads an official FIA PDF
through the environment proxy, verifies its event/year, speed directive and
publication-date evidence, and records its SHA-256 hash. Availability defaults
conservatively to the end of the documented publication day. An exact speed
quote may disambiguate multiple directives; the reviewer must ensure it applies
to the race. Use a verified historical document, with metadata shaped as:

```json
{
  "source_url": "https://www.fia.com/<actual-official-document>.pdf",
  "circuit_short_name": "<OpenF1 circuit name>",
  "document_circuit_aliases": ["<circuit/event name appearing in PDF>"],
  "season": 2024,
  "source_publication_date": "2024-01-01",
  "publication_date_evidence": "<exact printed date text>",
  "speed_limit_quote": "<exact printed pit-lane speed-limit directive>",
  "effective_to_utc": "<end of verified event applicability, with UTC offset>"
}
```

This is an input template, not a verified source. The importer writes a separate
`pit_lane_reference_verified.csv`; original placeholder rows are retained.
Speed verification does not establish lane length, legal timing anchors or
layout identity. A sourced layout version can be supplied separately; none is
invented. FIA access is now active. The post-lock source audit covers 34 race
weekends: 14 have explicit pre-forecast speed statements, three have separately
reviewed speed signs in dated official drawings, and 32 have conservatively
dated pre-forecast layout documents. Unknown speeds remain missing.

The resulting catalog and CSV are stored separately in
`data/raw/reference/stage4_1_fia/`; the frozen study loader does not consume them.
Source PDFs remain in the ignored run directory. Availability is the later of
the printed document date and archive publication date, at end-of-day UTC.
Visual observations identify their exact PDF pages and are distinct from text
directives. A drawing's document hash is not a physical geometry version.
Source chronology is checked against each race's first saved forecast.

Run `python -m f1ri.validation.pit_fia_reference_audit --fetch` to reproduce
collection and characterization. The [FIA reference audit](../artifacts/reports/stage4_1_fia_reference_audit.md)
records source coverage and fixed-model behavior. No model weights,
eligibility, calibration or primary results are changed; forecast improvement
from these features still requires a separately frozen study.

## VSC/SC evidence boundary

A dedicated neutralization audit records constant-state, time-matched two-lap
field references, with control IDs, time bounds, state coverage and dispersion.
At least three controls are required. Mixed phases and insufficient controls
carry explicit failure reasons. Even a matched field reference remains
`UNSUPPORTED`: target-route movement and SC queue effects are unidentified.
There is no generic percentage discount, fabricated route geometry or approved
neutralized strategy comparison. Independent route/reference validation remains
a separate unfinished Stage 4 requirement.

## Artifacts and reproduction

The CLI writes new artifacts under `artifacts/runs/stage4_1_stabilization/` and
new model pickles under `artifacts/models/stage4_1_stabilization/`. Raw snapshots,
models and parquet files remain local/ignored; the human-readable report,
figures, summary manifest, configs and tests are versioned. Existing reviewed
Stage 4 artifacts must be present for the preservation audit. The original
Stage 3 fitted artifacts were absent from the initial checkout, so source/report
hash preservation does not imply their fitted-artifact reproduction.

The reviewed-artifact hashes are versioned in
`config/experiments/stage4_reviewed_artifacts_reference.json`. Restore that
manifest as the run's `baseline_manifest.json` when reproducing from archived
reviewed artifacts; the checker requires those artifacts to exist and match.
It never silently substitutes a newly fitted baseline for the reviewed run.

Primary-cohort attrition left only Baku with usable unseen dry net-loss labels.
Montreal's stint boundaries conflict with its pit-out markers; Silverstone's
wet transitions are outside the dry reference domain. A separately registered
Spa 2025 replication preserves all model/eligibility choices and also yields
no eligible net labels. This failed replication is reported rather than used
to change eligibility or repeatedly seek a favorable test set. Unseen net-loss
race-level uncertainty remains unavailable. Reproduce the supplementary audit
with `python -m f1ri.validation.pit_supplementary --ingest`; its outcome access
and frozen-model hashes are recorded separately from the primary study.
