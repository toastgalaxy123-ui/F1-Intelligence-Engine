# Stage 5 S5-C.1 model strategy and constraint audit

## Reproduce

From the project root in the installed project environment:

```powershell
python -m f1ri.cli stage5-s5-c --output artifacts/runs/s5_c_baseline_check
python -m f1ri.cli stage5-s5-c1 --frozen-s5-c artifacts/runs/s5_c_baseline_check --output artifacts/runs/s5_c1_audit
```

Use a fresh output directory on each run. `--stage3-root` specifies a local original bundle and `--backup` optionally inventories a local project ZIP. The recovery audit records its inputs and pins the model hash before the original loader can read model bytes. The historical warehouse and previous ignored run dependencies may affect which availability rows can be reproduced. To reproduce the released audit on the original machine, include the same local project backup ZIP.

## Read the results

The Streamlit dashboard's **Pace and constraints** tab displays committed clean and effective traversals and the time added by binding predecessor headway. Headway is measured after accounting for the strongest other bound. It cannot be added to a separate estimate of SC or tyre effects to reconstruct a full counterfactual. The study retains pit service and physical rejoin clocks, common checkpoint validity, timing classification, and each teammate's delta.

The original S1 saved trajectories are available through **FROZEN_PACE_CONTINUATION** where installed. Any uncertainty display covers pace only. The scenario and trajectory archive do not identify deployment or overtaking probabilities.

## Use the verified model boundary

With a locally recovered bundle, the existing dashboard verifies the files and reference cases before opening the forecast controls. For programmatic access, pass cutoff records to `StrategyForecastAdapter.from_verified_files` and supply that adapter to `compare_strategies`. The CLI accepts a versioned scenario with `--scenario`, the same feature records with `--forecast-features`, and the bundle location with `--stage3-root`. Omit `--draw-index` to use the model mean. A draw index selects a single matched path.

Each feature-record object contains a `request` matching `StintForecastRequest`, a lowercase `source_sha256`, and `evidence_as_of` with UTC timestamps. Identity, timing and tyres are required. Include clocks for any supplied weather, clean-history, prior stint or grid/qualifying inputs. The caller is responsible for building those records from a trustworthy point-in-time prefix. A matching timestamp does not by itself prove provenance. Reactive replacement uses the actual fitted set and stint lap while keeping those initial observations fixed.

See the [validation report](../artifacts/reports/stage5_s5_c1_model_strategy_integration_report.md) for the available asset and cohort limits.
