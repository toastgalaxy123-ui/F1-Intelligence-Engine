# Stage 5 S5-B historical neutralization validation

The study reads the tracked S5-A and S5-A.1 ledgers and verifies them against their recorded digests. It also reads the approved OpenF1 caches and the S3 Imola geometry. The warehouse is audited read-only. The experiment uses archive event clocks with unknown historical receipt times.

## Reproduce

Use an installed project environment, the prior S3 telemetry caches and the Stage 5.3 Imola localization data at `artifacts/runs/stage5_s3_c_forensics_baseline/9987`. The ledger archive records the hashes for those inputs and includes the six bounded responses under `source_snapshots/`. Restore those responses into `data/telemetry/s5_b/9987` for byte-level verification. If they are absent, the acquisition script can issue the six frozen logical queries. The response bytes from a later retrieval may reflect archive revisions.

```powershell
python scripts/acquire_s5_b_neutralization.py
python -m f1ri.cli stage5-s5-b --output artifacts/runs/stage5_s5_b_historical_neutralization_validation_v1
```

Choose a fresh output directory for a repeat. Use `--imola` and `--warehouse` if the local input paths differ. The validation command reads the current inputs and makes no network requests. See the [frozen protocol](../config/experiments/stage5_s5_b_historical_neutralization_validation.yaml) and [release report](../artifacts/reports/stage5_s5_b_historical_neutralization_validation_report.md).
