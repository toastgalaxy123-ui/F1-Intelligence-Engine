# Stage 5 S5-A.1 pit entry and Safety Car restart

Pass `pit_decisions` to `simulate_neutralization` for progress-triggered S2 pit execution. Each `PitDecision` carries the resolved S2 plan and the independent pit-entry progress on the declared physical layout. The target lap comes from the S2 request. The optional reference entry clock is for comparison. The decision time and expiry define when the branch may be taken, and the optional cancellation timestamp applies before entry. Race-control commands at the entry clock run before entry. A committed stop reserves service on actual box arrival, with the scheduler's stable driver order resolving arrival ties.

Pass `individual_restart=True` to enable global signal timing with per-driver pass restrictions. The declared withdrawal establishes each on-track driver's next applicable line. For a restricted driver in the pit lane, the line is assigned on rejoin. A new SC or RED phase supersedes the previous restart. The original S5-A path is selected when both inputs are omitted.

## Reproduce the cache-only validation

With the tracked S5-A release archive extracted to an input directory and the existing session 9987 localization cache available, run:

```powershell
python -m f1ri.cli stage5-s5-a1 --frozen-inputs `
  --baseline artifacts/runs/stage5_s5_a1_frozen_replay_v1 `
  --imola artifacts/runs/stage5_s3_c_forensics_baseline/9987 `
  --output artifacts/runs/stage5_s5_a1_pit_restart_stabilization_v1
```

Choose a fresh output directory for each run. For source-warehouse validation, omit `--frozen-inputs` and supply `--warehouse`, `--stage3-root` and `--s4-replay` from the frozen S5-A validation. This mode reopens the original warehouse and Stage 3 model bundle. The [S5-A.1 report](../artifacts/reports/stage5_s5_a1_pit_restart_stabilization_report.md) records the evidence and the tested scope.
