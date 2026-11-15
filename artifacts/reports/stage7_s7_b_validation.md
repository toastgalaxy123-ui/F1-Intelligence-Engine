# Stage 7 S7-B — Source arrival, receipt-time availability, freshness and outage replay

**Decision B · Receipt capture and replay validated for research. Empirical racing availability remains incomplete.**

Operational status remains `LIVE_UNSUPPORTED`. Singapore and Imola 2025 stay event-time-only at their original cutoffs. The study recorded genuine local receipts when it re-queried the Singapore archive. It recovered neither the provider publication clocks nor the original race-time receipts. The official calendar had no race during this bounded collection window. [Formula 1 calendar](https://www.formula1.com/en/racing/2026)

## What the collector observed

The collector made ten rate-limited requests across five endpoints in **19.04 seconds** on November 15, 2026. All returned HTTP 200, with no retries. Monotonic request durations ranged from **0.128 to 0.997 seconds**, with a median of **0.387 seconds**. The journal retains all ten receipt envelopes and five distinct raw response bodies. The clock audit found no inconsistent attempts. Host synchronization was unverified.

| Endpoint | Successful responses | First-seen record versions |
| --- | ---: | ---: |
| Session metadata | 2 | 1 |
| Location | 2 | 480 |
| Competitive position | 2 | 454 |
| Laps | 2 | 1,229 |
| Race control | 2 | 121 |
| **Total** | **10** | **2,285** |

The second pass redelivered 2,285 identical versions. Those versions retained their first-seen timestamps. The capture observed zero changed versions and zero live race windows. The monotonic request times are HTTP transport measurements for this archive query. Racing event-to-first-seen age, publication latency, live driver freshness and operational availability remain unmeasured. Query completeness was unverified, so the observed capture supplies no confirmed absence bounds. [OpenF1 endpoint documentation](https://openf1.org/docs/)

## Clock, version and availability contract

- The envelope keeps source event time, original timestamp, request start, first body byte, response completion, processing and virtual decision clocks distinct. A version becomes usable only after its response and processing are complete. Monotonic timing stays within its collection process epoch. A detected wall-clock inconsistency quarantines the remainder of that epoch.
- Stable keys use endpoint semantics. A per-driver lap can retain its identity across a corrected start timestamp. Timestamp-keyed feeds preserve distinct keys when the source supplies no stable link between a changed time or driver and an earlier observation. Conflicting payloads at one key are preserved and withheld.
- Older requests may complete later. They cannot supersede a version from a newer request. Omission is recorded without inferring deletion. A return to an earlier version after a correction is withheld as unresolved.
- The append-only journal records an attempt before sending it. It publishes the raw body and matching receipt as immutable evidence. An interrupted attempt without a committed receipt stays unavailable and still counts toward the request budget. Resumption keeps the original plan and cannot replace prior evidence.
- A complete controlled query can establish an appearance window. The lower bound uses the earlier successful check's request start. The upper bound uses the first observed response completion. Failed polls never establish absence. Live publication remains unknown.

## Deterministic controls

All 18 controlled cases passed. They cover delayed and out-of-order responses, duplicates, corrections, missing polls, rate limits, server and permanent errors, short and long outages, partial driver coverage, delayed control and location, late backfill, clock anomalies, interruption, recovery and unknown publication timing. The correction example retained **94.0 s** before receipt and changed to **94.5 s** afterward. The interruption test resumed with two committed responses, retained one orphaned attempt, and reproduced the earlier replay bytes.

The existing deterministic race retained the same physical Safety Car deployment at race clock **156 s**.

| Injected receipt delay | Policy decision | Reactive pit stop |
| --- | ---: | --- |
| 0 s | 156 s | Lap 2 |
| 40 s | 196 s | Lap 3 |
| 60 s | No trigger | None |

These were declared receipt delays applied to the policy review clock. They do not estimate real race-control communication delays. The synthetic field example supplied only receipt-visible observations to the existing strict S7-A reconstruction. Stale location or missing pit support withheld the field. A passing receipt filter still left only one supported physical position and zero certified predecessors in that fixture.

## Reproduce and inspect

**Source Availability & Replay** in the Decision Board separates the frozen historical cutoffs, real archive collector receipts and synthetic delivery cases. At the exact Singapore event cutoff, moving the collector's 2026 receipt slider advances from zero to **1,174** visible versions. The source event cutoff stays fixed. Historical strategy execution remains disabled.

```powershell
python -m f1ri.cli stage7-s7-b study --output artifacts/runs/s7b_reproduced
python -m f1ri.cli stage7-s7-b verify --export artifacts/runs/s7b_reproduced/diagnostics/availability_replay.zip
python -m f1ri.cli stage7-s7-b capture --help
python -m f1ri.cli stage7-s7-b replay --help
```

All **437 tests** passed, including the 421-test baseline. Ruff passed. S6-C, S7-A and S7-A.1 independently reproduced 20, 52 and 54 frozen outputs, respectively. The sealed S6-C experiment reproduced with the pinned D8R1 mean model. Two S7-B replays matched all **52 generated files** and the checksum manifest. The evidence package contains 26 figures, four dashboard captures and a byte-verifiable replay export.

[Validation evidence](stage7_s7_b_evidence.zip) · [Frozen collector responses](stage7_s7_b_capture.zip) · [Metrics](stage7_s7_b_metrics.json)
