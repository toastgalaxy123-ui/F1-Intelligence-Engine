# F1 Race Intelligence Engine — Stage 5 S5-A.1

## Decision

**Path A for the validated mechanics.** Proceed to S5-B historical reconstruction and validation. The simulator now retimes pit entry from physical progress and releases global Safety Car control independently of each driver's passing permission. The Imola test validates the archived control-state transition. Its physical restart remains supported only by a declared simulation field.

## Pit entry and shared-box execution

A pit decision identifies a driver, intended lap, replacement tyres, validity window and a versioned branch coordinate. The car stays on the racing route until it reaches that coordinate. A race-control change before entry invalidates the old prediction and recomputes the remaining approach. Once entry commits, the S2 lane itinerary starts from the committed entry clock. The actual arrival at the team's box determines service order, and any queue wait shifts the remaining itinerary and merge.

| Fixture | Entry (s) | Shift from GREEN (s) | Merge (s) | Pit elapsed (s) | Strategic loss (s) |
| --- | ---: | ---: | ---: | ---: | ---: |
| GREEN | 160.000 | 0.000 | 217.000 | 57.000 | 25.333 |
| VSC active on approach | 162.133 | +2.133 | 219.133 | 57.000 | 8.444 |
| VSC clears on approach | 160.696 | +0.696 | 217.696 | 57.000 | 25.333 |
| SC active on approach | 163.500 | +3.500 | 220.500 | 57.000 | −2.375 |
| RED suspends approach | 188.000 | +28.000 | 245.000 | 57.000 | 25.333 |

The unqueued traces account for 26 seconds of lane travel, 28 of route transitions and 3 of service. Strategic loss compares arrival at the common merge marker against a matched stay-out simulation under the same controls. The negative SC result belongs to the declared synthetic pace and geometry. It gives no estimate of a historical pit advantage.

The shared-box fixtures reversed service order from 63 → 4 to 4 → 63 under changed approach conditions. The second arrival waited 5.800 s in the first run and 2.587 s in the reversed run. A control change at the first service start preserved the 27.000 s start and held the second car until 33.000 s. The boundary tie tests covered control at entry and one microsecond on either side. Late, expired, cancelled and unsupported decisions did not commit a route change.

## Safety Car restart

The versioned restart policy applies a valid global GREEN signal at its own timestamp. Each active racing driver retains a passing restriction until that driver reaches the applicable line. A car in the pit lane remains restricted through its rejoin and until its next racing-line crossing. The synthetic cases covered lapped cars, a control and crossing at the same tick, an interrupted restart, a finish, and a scripted pass rejected before release and allowed after it.

The FIA's 2025 Sporting Regulations distinguish the green signal from the first-line crossing requirement for each driver in Articles 55.15 and 55.8. The simulator uses the timing line as its supported racing-line marker and applies a conservative policy for pit-lane cars. Article 55.8 also specifies exceptions beyond ordinary scripted passes. [2025 FIA Sporting Regulations, Issue 5](https://www.fia.com/system/files/documents/fia_2025_formula_1_sporting_regulations_-_issue_5_-_2025-04-30.pdf)

### Imola 2025 · Session 9987

| Archived evidence | UTC | Replay treatment |
| --- | --- | --- |
| SAFETY CAR DEPLOYED | 14:06:27 | SC deployment |
| SAFETY CAR IN THIS LAP | 14:20:09 | Withdrawal announced |
| TRACK CLEAR | 14:21:42 | Global GREEN and the declared withdrawal proxy |

The frozen S5-A gate waited for the last required simulated line crossing and committed GREEN at 14:23:54.663 UTC, 132.663 seconds after the archived clear. S5-A.1 applied GREEN at 14:21:42 UTC. The 18 simulated drivers subsequently met their individual line requirements between 79.924 and 83.830 seconds after GREEN. The archive supplies no separate observation of physical Safety Car withdrawal. The in-this-lap message and archived clear remain distinct events.

Candidate B changed the release gate while holding the declared initial spacing, lap counts, pace limits, collection policy and route-evidence policy fixed. The median absolute timing error on the 108 common lap markers was 283.580 s in both versions. On each version's full matched set it was 283.580 s in S5-A and 284.941 s in S5-A.1. Initial spacing, clean pace, SC collection and unsupported unlapping remain distinct explanatory factors. The data do not identify their individual contributions.

Candidate C could not be initialized. The existing point-in-time cache provided 0 eligible racing positions for the 18-driver archived window roster at deployment, announcement, withdrawal proxy, global clear, and the audited next-line cutoffs. The source manifest records both the roster and the localization inputs. No positions were backfilled from later samples. The Imola VSC and local caution fixtures reproduced their frozen results, as did the Singapore 2025 local yellow and double-yellow fixtures.

## Verification and frozen artifacts

- The original 283 tests passed at the frozen revision. The final suite passed all 300 tests, including 17 focused cases. Ruff passed. The replay covered 31 synthetic scenarios and six approved historical windows with zero invariant failures.
- Two independent study commands produced 19 byte-identical generated files, including Parquet ledgers and the diagnostic figure. The metrics SHA-256 is `1d77585f99578084160eb47332caa988ed6934aed508459dbc5753e0acc655d3`.
- The frozen S5-A metrics SHA-256 is `c345b7e78f96168c7b72f367362bece02534b675942ba44861d759a4b5cfe54e`. All 20 generated S5-A files matched the tracked release archive. The parity ledger records the 12 reproduced S4-A files and the verified S4-B.1, S4-B, S4-A and C4 metrics hashes. The S4-B.1 hash used here is the hash of the committed release artifact.
- The original Stage 3 runtime and warehouse were available for the frozen replay and an initial revised historical comparison. They became unavailable before final packaging. The final pair of runs used the hash-verified S5-A archive and the existing Imola cache, with the D8 hash carried forward from the verified S5-A replay. No additional OpenF1 requests or model fits ran.

The S5-A fixed itinerary remains available for benchmark replay. Dynamic execution currently requires a declared branch in the final racing segment and a resolved S2 route. RED while a car is already on a pit route, an exact simultaneous merge with ambiguous order, and regulatory unlapping remain outside this release's validated mechanics. S5-B can build on the control-state reconstruction while treating historical physical initialization as an open evidence requirement.

[Diagnostics](stage5_s5_a1_pit_restart_stabilization.png) · [Metrics](stage5_s5_a1_pit_restart_stabilization_metrics.json) · [Ledgers and checksums](stage5_s5_a1_pit_restart_stabilization_ledgers.zip)
