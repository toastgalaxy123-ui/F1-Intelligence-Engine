# Stage 7 S7-A — Historical multi-car field reconstruction

**Decision B · Partial historical field support**

**Operational status · Live remains unsupported**

The study reconstructed track-local and lap-unwrapped progress at ten cutoffs across Singapore and Imola 2025. It located 17 of 20 cars at the exact Singapore forecast cutoff. Six cars had a distinguishable nearest-ahead candidate among the supported positions. The missing routes prevent treating those candidates as verified physical predecessors.

## Source and clock contract

S7-A reused the frozen S5-B Imola telemetry and timing excerpts and the Stage 3-R Singapore timing archive. Four bounded OpenF1 requests returned 8,518 fixes. They covered a Singapore reference lap completed before the forecast cutoff, the Singapore field around that cutoff, Imola VSC track clear, and the Imola VSC pit visit for car 1. The request and response manifests preserve parameters, coverage, retrieval time and response hashes. Imola had 840 duplicate same-time coordinates across overlapping responses with no coordinate conflicts detected. The study quarantines conflicting revisions if present.

The service indexes observations by driver and UTC event time. Causal mapping uses at most four seconds of prior fixes and requires a last fix no more than 0.9 s old. Retrospective interpolation can use a subsequent fix inside a validated bracket. Source timing precision and raw timestamp strings are retained. Historical publication, receipt and reaction clocks are unverified. OpenF1 describes the source location as an approximate Cartesian position and lap-start timestamps as approximate. [OpenF1 endpoint documentation](https://openf1.org/docs/)

## Track-local and lap-unwrapped progress

The sampled line uses a historical racing lap and timing-line anchors, scaled to the documented layout length. Singapore's earlier reference closed with a 12.29 m difference at its start and end, and Imola's frozen reference closed with a 4.45 m difference. These are closure diagnostics. The method rejects implausible direction and motion, conflicting fixes, stale positions, and uncertain routing. It uses a 650 m guard on each side of the line to withhold racing-route classification where the pit branch has no independent geometry. The uncertainty bounds are heuristic and do not carry a probability.

| Event-time cutoff | Localized / roster | Lap-unwrapped | Resolved ahead within mapped subset |
| --- | ---: | ---: | ---: |
| Singapore archived forecast cutoff | 17 / 20 | 17 | 6 |
| Singapore sparse negative control | 0 / 20 | 0 | 0 |
| Singapore later control | 16 / 20 | 16 | 9 |
| Imola VSC deployment | 14 / 20 | 14 | 8 |
| Imola VSC running | 18 / 20 | 18 | 11 |
| Imola VSC track clear | 8 / 20 | 8 | 8 |
| Imola SC deployment | 15 / 20 | 15 | 13 |
| Imola SC running | 12 / 20 | 12 | 11 |
| Imola SC in this lap | 5 / 20 | 5 | 1 |
| Imola SC track clear | 18 / 20 | 18 | 0 |

For example, at Singapore's cutoff, car 81 was classified fourth. Its next distinguishable mapped candidate was car 10, on a different race lap, with an estimated circular separation of 2,790 m. The wide separation and unlocalized cars preclude an immediate predecessor claim. At Imola SC track clear, 18 cars could be placed, but none had a distinguishable nearest-ahead candidate within the mapped subset. Fresh stationary samples were withheld from racing order when route evidence could not distinguish a stopped, off-route or stale-repeated trace. The study retains reported SC and VSC chronology and leaves queue formation, physical withdrawal and restart dynamics unvalidated.

At the archived Singapore cutoff, cars 63 and 4 lie inside the unresolved pit corridor. The board can display the matching archived D8R1 cutoff context, but neither driver received a supported physical position at that instant. Historical strategy execution remains disabled. D8R1 stays mean-only, and the S6-C declared scenarios remain available.

## Independent checks and negative controls

The study compared telemetry-projected line passages with lap timing anchors withheld from each projection. Singapore had **7** matched passages, with median absolute clock difference **0.047 s**. Imola had **40**, with median difference **0.070 s**. All matched within 1.25 s. The two streams use different observations but share one upstream archive. These results check the sampled line origin and timing alignment. Exact position, gap, predecessor and geometric-merge truth were unavailable.

Causal and retrospective progress agreed within a median of **0.64 m** over 33 overlapping Singapore driver-cutoffs and **1.16 m** over 90 overlapping Imola driver-cutoffs. Both estimates use the same telemetry. Retaining every eighth fix removed localization at all ten cutoffs under the frozen recency and velocity rules. A future-telemetry mutation regression changed the retrospective estimate while preserving the causal result. There are two pilot races and two neutralization episodes, both in Imola.

## Pit and rejoin evidence

For Imola car 1 under VSC, the earlier loop-compatible fix was at **13:42:44.420 UTC**. The archived lane entry and exit timings were **13:42:58.463** and **13:43:28.454**. The new telemetry shows a stationary candidate from **13:43:08.480** to **13:43:10.981**, and a later compatible loop fix at **13:43:33.780**. Car 43 was the nearest distinguishable ahead candidate in the supported subset at that later fix. The geometric entry, service confirmation, physical merge instant and merge ordering remain unavailable. The lane ledgers retain 25 Imola visits and preserve lane timing separately from geometric events.

## Verification and reproduction

- 393 tests passed before the change and 406 passed afterward. Ruff passed for source and tests.
- The full S6-C rerun matched all 20 frozen deterministic outputs and reproduced its sealed analysis export. The D8R1 hash and frozen S5-A, S5-B and S6-C releases matched.
- Two S7-A runs produced 52 byte-identical generated files and the same checksum manifest. The sealed release adds the report and eight captures from the running Streamlit app. It also includes 26 diagnostic figures and the machine-readable ledgers.
- Open the **Race State** tab in the existing Decision Board. Choose the historical race, cutoff and reconstruction mode. **Evidence & Validation** exposes the source hashes, observation windows and readiness gates.

```powershell
python -m f1ri.cli stage6-s6-c --output artifacts/runs/stage7_s6c_reproduced
python -m f1ri.cli stage7-s7-a --output artifacts/runs/stage7_s7a_reproduced --s6-c-run artifacts/runs/stage7_s6c_reproduced
python -m streamlit run src/f1ri/dashboard/app.py
```

Receipt-qualified reconstruction, freshness, source revision delivery and outage recovery remain separate validation gates. The largest physical evidence gap is a validated pit-branch and merge reference with simultaneous neighborhood observations.

[Evidence bundle](stage7_s7_a_evidence.zip) · [Frozen inputs](stage7_s7_a_inputs.zip) · [Metrics](stage7_s7_a_metrics.json)
