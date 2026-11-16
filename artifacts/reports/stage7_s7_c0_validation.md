# Stage 7 S7-C0 — Shadow-mode preflight

**Decision B · Shadow infrastructure validated with empirical limitations.**

The research pipeline formed and sealed 28 snapshots. All withheld action. The bounded OpenF1 rehearsal retrieved archived Singapore 2025 data, so contemporary racing freshness, provider publication latency and original race-time receipt remain unmeasured. The Decision Board remains `LIVE_UNSUPPORTED`.

## Polling feasibility and rehearsal

The preregistered collector permits one outstanding request, starts at least 2.1 seconds apart and caps the rehearsal at 12 attempts or 45 seconds. It observed a minimum start spacing of 2.100111 seconds. All 12 requests returned HTTP 200. The observation registry retained 2,285 distinct versions, with repeated payloads preserving their first-seen lineage.

| Endpoint | Freshness target | S7-B usable gap | S4 largest usable gap | S4 successes |
| --- | ---: | ---: | ---: | ---: |
| Location | 2 s | 10.565 s | 8.405 s | 6 |
| Position | 15 s | 10.483 s | 14.652 s | 2 |
| Race control | 30 s | 10.505 s | 8.520 s | 2 |
| Laps | 150 s | 10.564 s | Single response | 1 |
| Sessions | Startup metadata | 10.113 s | Single response | 1 |

The archive probes measure usable response timing. The location threshold alone requires at least 30 polls/min for a continuous guarantee, versus a sustained ceiling of 28.57/min. Even with location as the sole endpoint and ideal delivery, the theoretical duty-cycle ceiling is 95.24%. The four thresholds together require 36.4 polls/min. Hard request spacing, the attempt cap, global HTTP 429 cooldown and endpoint failure cooldown take precedence over the soft starvation guards.

Each policy ran through the same 61 virtual checkpoints and fault rules. S4 raised nominal location freshness from 6/61 (S0) to 21/61 and reduced withheld checkpoints from 56 to 43. In the fault fixture it reached 14/61 fresh location checkpoints and withheld 48/61. The longest faulted S4 location gap was 28.505 s, compared with 24.305 s for S0. These controlled results include delayed availability, partial driver samples, throttling, failure and recovery. They are properties of the fixture.

The calendar showed no active race in the rehearsal window. A post-rehearsal Windows clock audit found an unsynchronized local CMOS clock (leap indicator 3, stratum 0), with no verified offset. Monotonic durations are usable within the recorded process epoch. Event and receipt dates in the archive cannot establish race-time latency. [OpenF1 endpoints](https://openf1.org/docs/) · [Formula 1 calendar](https://www.formula1.com/en/racing/2026)

## Lap 31 and the original D8R1 request

At Singapore session 9896, driver 63 lap 31 opened at **2025-10-05 12:52:29.272 UTC**. The archived 95.912-second lap implies a finish at **12:54:05.184 UTC** and the next contiguous timing line was recorded at **12:54:05.286 UTC**. The lap-start view exposes identity and the start anchor. It withholds duration, sector results, segment arrays and pit-out information. The later archive view exposes supported completed values. Strict receipt replay additionally requires the usable record version and completion witness by the receipt cutoff. Without an independent sector witness, the lap completion boundary is the conservative fallback.

The Stage 3-R audit reproduced the pinned driver-63 request from the warehouse feature builder. Its current HARD/C3 stint history included clean laps 27 and 29. The warehouse query masks outcome fields until both the implied end and a contiguous next timing line are within the cutoff. A mutation changed lap 31’s completed time, sectors and pit-out value. The original request and serialized field snapshot were unchanged. The later maturity control exposed the changed values after an eligible boundary. Result `NO_LEAKAGE_FOUND_IN_AUDITED_PATH` applies to this request. D8R1 retains `MEAN_ONLY_VALIDATED`, its published model hash, and its 2023–2025 context. The 2026 control returned `MODEL_ERA_UNSUPPORTED`. [Pinned S1 snapshot](../runs/stage5_s1_pace/snapshot.json)

All 12 temporal controls passed, including delayed sector delivery, a corrected lap time, late race control and position backfill. A correction advances the version available at later cutoffs and preserves earlier seals.

## Decisions, physical support and race control

The sealed checkpoint binds the receipt prefix, field-level clocks and maturity, source and driver coverage, physical gates, model context and candidate status. The controlled receipt-qualified field localized one car and certified zero physical predecessors. Pit geometry and tire inventory remained unsupported for strategy. The observed archive prefixes also retained clock and original-race receipt limits. A separate later evaluation confirmed the earlier seal while additional versions became available.

The controlled Safety Car experiment kept physical deployment fixed at race clock 156 s. Receipt delays of 0, 40 and 60 s led to a lap-2 stop, a lap-3 stop and no stop respectively. Moving the receipt across the physical pit-entry deadline by one microsecond on either side switched the committed lap from 2 to 3. GREEN and its later correction became visible at their usable receipt cutoffs. The asymmetric position and race-control controls gated each feed independently.

## Reproduce and inspect

Open **Shadow-Mode Readiness** in the Decision Board to view polling feasibility, lap maturity, sealed checkpoints and race-control cases. The evidence archive includes the structured ledgers, 26 diagnostic figures, four browser captures and the replay export. Real archive receipts are also published in their own checksummed bundle. [Study evidence](stage7_s7_c0_evidence.zip) · [Receipt archive](stage7_s7_c0_rehearsal.zip)

```powershell
python -m f1ri.cli stage7-s7-c0 study --output artifacts/runs/stage7_s7_c0_reproduced
python -m f1ri.cli stage7-s7-c0 verify --export artifacts/runs/stage7_s7_c0_reproduced/diagnostics/shadow_replay.zip
python -m f1ri.cli stage7-s7-c0 capture --output artifacts/runs/stage7_s7_c0_rehearsal_new
```

The capture command defaults to the bounded archive sampler. A manually initiated attempt in a permitted session takes `--session`, `--window-start`, `--window-end` and `--mode FIRST_SEEN_OBSERVED`. An observed receipt envelope records the collector clock. Certification of a contemporary racing window still depends on clock and session evidence.

Two independent S7-C0 runs and a clean checkout matched all 58 checksummed study files. Export reimport independently reproduced the 57 outputs described by the replay manifest. S7-B matched 52 deterministic outputs and its original manifest. The earlier S6-C, S7-A and S7-A.1 runs matched 126 frozen outputs in total. The source, model and earlier published evidence pins matched. Ruff and all 445 tests passed.

## Requirements for contemporary shadow evidence

A bounded permitted racing window needs a verified synchronized clock and a transport that can satisfy the location freshness target with measured driver coverage. Physical predecessor and pit support, tire inventory and an independently validated model for the applicable era are separate decision gates. Live strategy remains unavailable.
