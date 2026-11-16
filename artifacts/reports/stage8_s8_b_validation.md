# Stage 8 S8-B — Interactive strategy workspace

**Decision A · `INTERACTIVE_RESEARCH_WORKSPACE_VALIDATED`.** The localhost workspace supports recorded-checkpoint comparison, policy drilldown, robust scenario inspection, paired team strategies and saved investigations. The existing model, simulator and optimizer still supply the numerical results. Authenticated remote use is an S8-C release gate.

## Strategy comparison

The frozen declared two-car case reproduced stay-out, HARD, MEDIUM and SOFT at the same effective marker time. The clean-air estimates differed. Following constrained the simulated result and the interface withheld a unique recommendation. The reference selector changes only the displayed comparison. A trace is linked to the same analysis and result hashes, and the comparison uses shared recorded checkpoints with no interpolated car movement. Pit, service, following and race-control measures are presented individually.

The conditional controls distinguished `EXHAUSTED_LEGAL_SPACE` from `BOUNDED_SEARCH_INCOMPLETE`. The robust heatmap shows observed model outputs under declared GREEN, VSC and SC scripts, a fixed executable policy, evaluated worst cases, and comparable Pareto alternatives. The separate oracle view is retrospective. The frozen Safety Car timing case reproduced **46.754185 s** of regret. The reactive and scheduled conditional traces show their actual decisions and pit events.

## Shared pit box and saved workspaces

| Paired alternative | Actual service order | Driver 63 waiting | Combined elapsed time |
| --- | --- | ---: | ---: |
| Driver 4 HARD lap 30, driver 63 HARD lap 31 | 4 then 63 | 2.903848 s | 867.561648 s |
| Driver 4 HARD lap 30, driver 63 SOFT lap 32 | 4 then 63 | 0 s | 856.744393 s |

Chrome executed both alternatives and the stay-out reference. API rows, driver times, pit visits and trace observations matched the direct engine. The default team objective is combined elapsed time at matched driver markers.

The workspace shelf stores versioned input documents and optional result receipts in the current browser origin. A saved shared-box workspace survived reload and reproduced the same result checksum. The import checks rejected wrong model and source hashes. The validated browser workflow also covered duplicate, export, import and explicit delete. Draft edits hide incompatible results, and earlier results remain labeled with their own inputs.

## Manifest reconciliation

S8-A used `checksum_manifest: false` where the expected deterministic list omitted the manifest itself. Comparing the full manifests reveals two different packaging scopes.

| Release | Published entries | Rerun entries | Required output bytes | Full manifest bytes |
| --- | ---: | ---: | --- | --- |
| S6-C | 47 | 21 | 20/20 verified | Different |
| S7-C0 | 64 | 58 | 58/58 verified | Different |

The shared paths keep the same order. S6-C publishes 24 screenshots, its UI observations and validation report as packaging additions. It also augments the published metrics and regression JSON, with the common fields unchanged. The only changed shared performance field is observed wall time. S7-C0 adds four screenshots, UI observations and its report. All shared S7-C0 member hashes match. The ledger records `OUTPUT_BYTES_VERIFIED` and `SEMANTIC_PARITY_VERIFIED` for both releases. The original evidence remains pinned.

## Validation and readiness

The 454-test baseline passed before implementation. The final suite passed 458 Python tests and two frontend unit tests. Ruff, TypeScript, ESLint and the Next.js production build passed. Eight Chrome workflows exercised the prior S8-A paths and the S8-B desktop, mobile, keyboard, job lifecycle and persistence controls. The evidence archive contains 30 screenshots. All eight S8-A API result checksums reproduced, and fresh reruns of the earlier S6 and S7 studies matched the required deterministic bytes.

The worker stayed serialized with four outstanding jobs, a 35-second wall deadline and a sampled 720 MiB memory ceiling. Two clients posting identical inputs shared an analysis ID. Queued and active cancellations produced terminal `CANCELED` states in 1.6 ms and 28.4 ms in the measured run. The uncached paired comparison took 2.55 s. An identical cached manual request took 3.8 ms. Chrome selected and displayed two trace comparisons in 78 ms, opened a matrix cell in 51 ms, saved the shared-box workspace in 79 ms and reopened its validated inputs in 110 ms. These are observations on the validation host.

D8R1 stays mean-only and the 2026 transfer remains unsupported. Historical Singapore strategy, exact historical pit merges and certified predecessors remain unavailable. Live strategy remains `LIVE_UNSUPPORTED`. The hosted beta plan covers authentication, TLS, private worker access, storage, quotas, licensing checks and rollback. [Workspace guide](../../docs/web/strategy-workspace.md) · [Private-beta plan](../../docs/web/private-beta.md) · [Evidence](stage8_s8_b_evidence.zip)
