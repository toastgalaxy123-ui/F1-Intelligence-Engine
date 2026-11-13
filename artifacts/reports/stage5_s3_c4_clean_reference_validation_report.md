# Stage 5.3-C4 independent clean-sector validation

## Decision

**Path C. Empirical traffic estimation remains unsupported.** The final
cached expansion improved R4-B coverage in Hungary but did not produce
enough independent validation or calibration stints. Even if the
reference and calibration gates passed, no existing H1 forecast has
the three earlier matched controls it needs. The optimistic recovery
bound reaches one independent encounter group in Austria and zero in
Hungary.

Freeze the empirical findings and prioritize deterministic S4 overtaking
mechanics. Keep `T1_DETERMINISTIC_NO_PASS` operational and Traffic V1
`RESEARCH_ONLY`. This study made zero OpenF1 requests and fitted no
traffic-penalty models.

## Hypothesis A — Independent reference validation

| Race | C3 Tier A sectors / stints | C4 Tier A sectors / stints | C3 R4-B held-out predictions / stints | C4 predictions / stints | C4 support | C4 MAE | C4 P90 absolute error |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Austria | 120 / 15 | 120 / 15 | 38 / 8 | 38 / 8 | 31.7% | 0.171 s | 0.359 s |
| Hungary | 131 / 13 | 172 / 14 | 29 / 8 | 70 / 9 | 40.7% | 0.146 s | 0.311 s |

The expansion replayed the original Hungary continuation and the
coverage extension with the existing H1 reconstruction and frozen
geometry. It added 41 Tier A sectors and one clean stint. Austria's
inputs were unchanged. The Hungary coverage increase also changed
the supported evaluation population. The before-and-after MAEs of
0.151 s and 0.146 s describe their respective populations.

Both races fell below 60% operational support and ten independent
evaluation stints. On common held-out support, the six references
shared six observations in each race. The metrics retain the common
and operational estimates separately.

### Tier B sensitivity

| Race | Tier B weight | Supported predictions / stints | MAE | Mean signed bias |
| --- | ---: | ---: | ---: | ---: |
| Austria | 0.10 | 105 / 13 | 0.173 s | −0.064 s |
| Austria | 0.25 | 105 / 13 | 0.176 s | −0.088 s |
| Austria | 0.50 | 105 / 13 | 0.178 s | −0.107 s |
| Hungary | 0.10 | 144 / 12 | 0.177 s | −0.094 s |
| Hungary | 0.25 | 144 / 12 | 0.178 s | −0.109 s |
| Hungary | 0.50 | 144 / 12 | 0.175 s | −0.115 s |

All validation observations were Tier A. Adding Tier B increased
coverage and made signed bias more negative as its weight increased.
In Hungary, cold-start MAE was about 0.25 s under all three
sensitivities, compared with 0.182 s for Tier A-only R4-B on its own
supported cold-start sample. The ledgers preserve common-support
results and age, sector and race-progress slices. These differences
do not establish traffic contamination in the Stage 1 labels.

### Chronological calibration

The largest available calibration pool at any cutoff contained three
independent stints in each race. U0, U1 and U2 all required eight.
The detailed ledger records fold membership, residual availability,
out-of-sample provenance and overlapping fitting prefixes. Different
folds, residuals arriving after the cutoff and repeated observations
within a stint reduced the pool. No primary prediction received a
valid interval. Empirical 50% and 90% coverage, interval widths and
CRPS are unavailable.

The reference P90 errors were 0.359 s and 0.311 s, compared with the
0.30 s planning effect. Frozen H1 timing disagreements had P90 values
of 0.156 s and 0.124 s. The reference residual already uses the
official elapsed time. Dependence between the timing and reference
errors leaves the joint uncertainty and proportion of sufficiently
precise forecasts unestimated.

## Hypothesis B — H1 chronological controls

| Race | Frozen following sectors / groups | R4-B forecasts / groups | Calibrated forecasts | A, eligible proxies | B, if reference passes | C, optimistic sectors / groups |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Austria | 70 / 28 | 21 / 9 | 0 | 0 | 0 | 11 / 1 |
| Hungary | 58 / 17 | 10 / 7 | 0 | 0 | 0 | 0 / 0 |

Scenario A applies the observed reference, calibration and control
gates. Scenario B assumes a validated and calibrated reference with
the observed controls. Scenario C also assumes eligible Stage 1
sectors can gain independent physical confirmation and supported
out-of-sample predictions, within the same chronological matching
rules and driver folds. It conditions on currently supported
following forecasts. Its 11 possible Austria sectors are all from
one driver, one stint and one conservative encounter group. The
gate requires ten groups and 20% conversion in each race.

The per-target control ledger records sector, compound, tire age,
race progress, temperature, evidence availability and independent
residual filters. For example, the Austria search retained 310
Tier A target/control pairs across all frozen following sectors.
Only 65 pairs had an eligible reference residual, and deduplication
reduced these to 20 target/stint matches. No supported forecast had
three eligible earlier control stints. The candidate manifest records
cached and missing spans for optimistic control seeds. They offer
insufficient independent yield to justify a new acquisition.

## Repeat-layout governance

Austria 2024 is absent from the frozen warehouse. Hungary 2024
(`9566`) has 1,353 lap rows with all three positive sector durations
and 60 dry-tire stint rows with age metadata. It was a D8 training
race, has no cached physical evidence, and its layout and timing-marker
equivalence were not verified. The study inspected availability
metadata and used zero sector outcomes from that session. An
approved 2024-to-2025 transfer could be exploratory. The 2025
development results already informed the protocol. Monza, Austin
and the reserved cohorts retained their frozen roles.

[Diagnostics](stage5_s3_c4_clean_reference_validation.png) · [Metrics and bounds](stage5_s3_c4_clean_reference_validation_metrics.json) · [Ledgers and manifests](stage5_s3_c4_clean_reference_validation_ledgers.zip) · [Frozen protocol](../../docs/STAGE5_S3_C4_CLEAN_REFERENCE_VALIDATION.md)

## Reproduction

The input Git revision was `3ecc4b6f333ee0f2a7111096860787c25ed23aee`.
C3 metrics and original R4 snapshots reproduced with SHA-256
`8db99cd71b51b93c3165e4fbe5892166f0f9b746f44b917ecc8c993d40e16d97`
and `6f10cbd13986eaa812182e7142fabcedc8323f12f0bef5f7fce6b6d710b6ad50`.
The frozen D8 SHA-256 is
`31734c2244b3bdbd555c1407fe62207b3edf0eaddafe85e722b2cc936c75923c`.
The Austria H0 proxy remains +0.2109747886 s.

```powershell
$env:PYTHONPATH = "$PWD/src"
$py = 'C:/Projects/f1-race-intelligence/.venv/Scripts/python.exe'
$wh = 'C:/Projects/f1-race-intelligence/data/warehouse/f1ri.duckdb'
$d8 = 'C:/Projects/f1-race-intelligence'
$c2 = 'artifacts/runs/stage5_s3_c3_c2_replay'
$c3 = 'artifacts/runs/stage5_s3_c4_reproduce_c3'
$base = 'artifacts/runs/stage5_s3_c_forensics_baseline'
$hist = 'artifacts/runs/stage5_s3_c_forensics_history'
$transition = 'artifacts/runs/stage5_s3_c_forensics_transition'
$census = 'artifacts/runs/stage5_s3_c_transition_census_v1'
& $py -m f1ri.cli stage5-s3-sector-reference --warehouse $wh --stage3-root $d8 --c2 $c2 --output $c3
& $py -m f1ri.cli stage5-s3-clean-validation --warehouse $wh --stage3-root $d8 --c3 $c3 --c2 $c2 --transition $transition --history $hist --census $census --baseline $base --output artifacts/runs/stage5_s3_c4_reproduce
& $py -m pytest -q tests/test_sector_validation.py tests/test_latent_sector_pace.py tests/test_traffic_short_horizon.py tests/test_traffic_encounter.py tests/test_traffic_transition.py tests/test_traffic_history_study.py tests/test_traffic_coverage.py tests/test_traffic_continuation.py tests/test_track_proximity.py
& $py -m pytest -q --disable-warnings
```

The focused suite passed 78 tests. The full suite passed 210 tests with
14 existing NumPy deprecation warnings from the pit-neutralization
tests.

Two C4 replays produced byte-identical ledgers, metrics, model
prefixes and figures. The C4 metrics SHA-256 is
`21451ffd5a8734f007e0fdc2ffc8701b342e17ade39c2b1a251e0bffb1df7fff`.
