# Stage 6 S6-C — Race Intelligence Decision Board

**Release: `RESEARCH_DECISION_BOARD_VALIDATED`**

**Operational status: `LIVE_UNSUPPORTED`**

The decision board opens in the existing Streamlit app. It links one cutoff and scenario to D8R1 mean pace, manual strategy comparisons, conditional optimization, robust strategy, pit commitment, and evidence. The earlier strategy tools remain available from the sidebar.

## What the presets establish

| Preset | Supported use |
| --- | --- |
| Singapore 2025 archived cutoff | D8R1 mean continuation for Russell or Norris at 3, 5, or 10 laps. The cutoff is 2025-10-05 12:52:29.272 UTC. |
| Declared two-car encounter | Same-marker physical comparisons with a declared track-local gap, opponent pace, pit route and replacement sets. |
| Declared GREEN / VSC / SC | Cutoff-known and future-conditioned branches, plus a shared policy matrix across selected declared futures. |
| Frozen SC deployment at +58 s | The S6-B.1 original, adaptive, revision and scheduled traces, with both frozen comparator versions identified. |
| Declared shared pit box | Two-driver policies and shared box contention with combined, lead-driver or weighted scoring. |
| Unsupported full-field reconstruction | Displays available archived features and disables analysis that requires the missing physical field. |

The board classifies individual fields as observed, derived, declared, model forecast, or unavailable. The Singapore archive supports fitted tyres, stint context, weather and timing features. Historical track-local positions, replacement-set inventory, service-box occupancy and race-control state at this cutoff remain unavailable. The simulated field uses declared inputs and the deterministic no-pass baseline. The D8R1 release remains `MEAN_ONLY_VALIDATED` under the 2023–2025 era. G3 remains failed. The board omits calibrated intervals and event probabilities.

## Decision and timing results

The sealed declared neutralization study used a one-lap stop window. It attempted 34 conditional evaluations and 140 robust evaluations, including hindsight-only oracle work. Its minimax choice, `63-L31-soft`, has near-equal hard, medium and used-soft alternatives. The leaders also change across futures. The board displays `FAMILY_SENSITIVE`, `NEAR_EQUAL_ALTERNATIVES` and `NO_UNIQUE_RECOMMENDATION`. Search-budget exhaustion withholds a complete recommendation and preserves the evaluated ranking for inspection.

In frozen `sc-deploy-58`, the lap-31 fixed stop committed **29.967958 s** after the cutoff. The SC activated at **+58 s** and the original reactive policy requested lap 32. GREEN returned at **+150 s**. The original stop crossed irreversible pit entry at **+172.226035 s**, incurring **46.754185 s** of regret. P2 cancelled before entry at the GREEN signal. Its stay-out branch completed without a pit visit and its replan branch committed on lap 33. The P5 scheduled conditional policy committed on lap 31. The displayed regret values name the original 37-policy and expanded 42-policy comparators. The checkpoint gap and commitment ledgers explain the timing without assigning causal meaning to a change in comparator.

## Verification

- All 393 tests passed, including the 380 carried forward. Ruff passed for source and tests.
- All 26 deterministic S6-B.1 files matched the published checksums after an independent run.
- Two independent S6-C study runs matched across the deterministic ledgers and exported analysis. The observed wall-clock timings and their enclosing checksum manifest vary by run.
- The board matched the standalone S6-A conditional score and the complete S6-B robust matrix, including their execution ledgers. Forecast caching preserved scores and traces.
- The Chrome capture exercised preset and candidate selection, conditional-versus-cutoff views, the robust matrix and Pareto view, frozen commitment selections, budget-incomplete withdrawal, and the unsupported historical preset. The evidence bundle contains 24 images of the running app.
- Export reimport verified checksums, pinned source revisions and bytewise reproduction of the canonical result. The export contains the settings, snapshot, source hashes, declared scripts, model inputs, candidates, scores and execution traces.

## Run or reproduce

```powershell
python -m streamlit run src/f1ri/dashboard/app.py
python -m f1ri.cli stage6-s6-c --output artifacts/runs/stage6_s6_c_reproduced
python -m f1ri.cli stage6-s6-c --replay path/to/s6-c-research-analysis.zip
```

Use a new output directory for a study run. The [sealed evidence bundle](stage6_s6_c_evidence.zip) includes the full screenshot set, checksums, live-readiness audit and a sample replay. The [metrics](stage6_s6_c_metrics.json) summarize the validation results.

## Live readiness

OpenF1 ingestion can retrieve timestamped HTTP snapshots, handle retryable failures and retain raw response hashes. Operational use requires separate validation of receipt and event ordering, field freshness and publication delays, corrections and outage recovery. Tire-set inventory, track-local predecessor, race-control effect times and physical pit commitment also need cutoff-valid evidence at race speed. Live ingestion and live recommendation remain disabled in S6-C.
