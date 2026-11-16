# Stage 8 S8-A — Web research application foundation

**Decision A · `WEB_RESEARCH_APPLICATION_FOUNDATION_VALIDATED`.** The private Next.js workspace and FastAPI service ran locally with seven research presets. The UI exposes D8R1 mean pace, legal strategy comparisons, conditional and robust analysis, frozen Safety Car timing, and source evidence. The existing Python services supplied the results. Public access requires a separate access-control layer. Live strategy remains `LIVE_UNSUPPORTED`.

## Supported and withheld workflows

The archived Singapore 2025 cutoff exposes observed context and D8R1 continuation pace. The S7-A event-time field contains 17 supported mapped positions and zero certified predecessors. Historical physical strategy and tire-set inventory remain unavailable. The declared two-car preset supplies a separate physical state and a declared legal tire inventory. Chrome ran a stay-out, hard, medium and soft comparison at the same marker. All four tied exactly under the following constraint, so the workspace withheld a unique recommendation. The export re-executed against the same frozen inputs and returned the identical result checksum.

The small conditional budget preserved `BOUNDED_SEARCH_INCOMPLETE`. The larger conditional search exhausted the legal space. GREEN, VSC and SC robust analysis completed with the larger budget and exposed family sensitivity and near-equal alternatives. Perfect-information oracle results appeared in their own retrospective view. The shared pit-box preset exercised the matched two-car simulator.

The timing view reproduced the original `sc-deploy-58` comparator using the frozen S6-B.1 research and its original library. The reactive policy selected a lap-32 stop when SC was declared, entered after GREEN resumed and incurred **46.754185 s** of regret. The UI exposes the decision, pit-entry, service, exit and rejoin clocks and offers the scheduled conditional policy as a separate trace.

D8R1 retained the published model hash and `MEAN_ONLY_VALIDATED` for 2023–2025. G3 remains failed. The 2026 support control returned `MODEL_ERA_UNSUPPORTED`. The S7-C0 view retained the 28 withheld shadow decisions and the archived receipt limitations.

## Verification

- The original 445 Python tests passed before development. The final suite passed 454 tests, including nine API contract, exact-parity, export and resource-limit tests. Ruff, frontend lint and TypeScript checks passed. The production Next.js build passed.
- Three automated Chrome workflows covered the archive and declared paths, mobile navigation, actual analysis submission, timing interaction, stale-result invalidation, and recovery from a failed network request. The evidence bundle contains 26 screenshots of the running app. The network failure test intentionally interrupted one browser request.
- The HTTP verification compared canonical forecasts, policy rows, pit ledgers, conditional search results, regret and timing with direct engine outputs. It tested export checksum verification and the historical refusal against the running API.
- Fresh reruns matched 25 S6-B.1, 20 S6-C, 52 S7-A, 54 S7-A.1, 52 S7-B, and 58 S7-C0 deterministic files. The prior S7-C0 baseline contained inherited verification flags. The S8-A ledger records the hashes independently checked in these new runs. The model and simulator paths and the five pinned S7-C0 release artifacts matched the baseline.

## Execution profile

The cold run includes Python worker startup. An identical cached manual submission returned in **3.9 ms**. The bundle also records preset and snapshot loading and the browser workflow timings.

| Workflow | End-to-end | Worker CPU | Sampled peak RSS |
| --- | ---: | ---: | ---: |
| D8R1 mean forecast | 2.67 s | 2.05 s | 201 MiB |
| Manual strategy comparison | 2.67 s | 2.17 s | 214 MiB |
| Conditional search, budget 4 | 2.67 s | 1.92 s | 213 MiB |
| Conditional search, budget 128 | 2.80 s | 2.28 s | 213 MiB |
| Robust search, budget 240 | 3.44 s | 2.73 s | 222 MiB |
| Frozen timing reproduction | 7.63 s | 7.00 s | 352 MiB |

One isolated worker executes at a time. The API bounds pending work, wall time, sampled resident memory, result size, and retained cache size. Browser context changes discard stale results. Explicit cancellation requests stop queued or active work. A disconnected browser leaves work subject to the same worker deadline.

## Use the workspace

Start both loopback services with `python scripts/run_local_web.py` after installing the pinned dependencies and building the frontend. For setup, development, browser testing and remote-access requirements, follow the local guide. [Local startup](../../docs/web/local.md) · [Architecture](../../docs/web/architecture.md) · [Evidence bundle](stage8_s8_a_evidence.zip)

The next product stage is S8-B strategy workspace refinement. Historical predecessor and pit-merge certification, empirical SC/VSC calibration, 2026 model transfer, and operational race probabilities remain open research requirements.
