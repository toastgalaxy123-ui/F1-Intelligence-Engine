# Stage 7 S7-A.1 — Historical pit branch, rejoin and predecessor evidence

**Decision B · Partial geometric improvement**

The Imola pit-lane interior can be distinguished from the sampled racing loop for a limited part of the layout. Two held visits passed the route checks. The pilot recovered two entry route-change windows and two return-to-track windows. It did not establish surveyed boundary positions, an exact geometric merge, or a certified historical predecessor.

## Fit, held visits and geometry

The frozen protocol selected seven cases from the two pilot races and acquired four bounded OpenF1 windows, returning **16,458 location fixes**. Imola car 1's VSC pit visit supplied the fit. Car 4 under SC and car 87 after the reported VSC clear were held. The Singapore fit used car 22 and reserved car 43 for evaluation. Sparse Imola car 22 and route-ambiguous Imola car 23 served as negative or partial-evidence cases. Source identities, request parameters, response hashes, and event-time ranges are preserved.

The Imola fit comprises **74 segments and 369.8 m** of pit-lane interior in the same Cartesian source frame as the frozen loop. The model requires continuous forward motion or a supported stationary lane observation, acceptable residuals, and separation from the competing route. It retains the 650 m guard wherever those checks do not establish the route. The main-loop uncertainty bounds were not reduced.

| Withheld Imola visit | Pit witnesses identified | Racing controls identified | Wrong labels |
| --- | ---: | ---: | ---: |
| Car 4, SC | 84 / 92 | 49 / 51 | 0 |
| Car 87, after reported VSC clear | 171 / 173 | 81 / 87 | 0 |

Witnesses are correlated samples from two visits in one race. Pit labels use the independently archived lane timing within a trimmed interior. Racing controls are concurrent observations screened against the recorded pit visits. These checks support the separability of the observed interior. They do not establish a broad historical false-route rate. The FIA documents establish pit-route topology. Their diagrams provide no shared control points for a metric OpenF1 transformation. [Imola FIA circuit and pit drawings](https://www.fia.com/system/files/decision-document/2025_emilia_romagna_grand_prix_-_race_director_event_notes_-_circuit_map_pit_lane_emergency_exits_quarantine_zone_and_red_zones.pdf) · [Singapore FIA circuit and pit drawings](https://www.fia.com/system/files/decision-document/2025_singapore_grand_prix_-_event_notes_-_circuit_map_pit_lane_emergency_exits_map_and_quarantine_zone.pdf) · [OpenF1 source documentation](https://openf1.org/docs/)

## Route-change windows and rejoin order

| Imola visit | Main loop to pit core, UTC | Pit core to main loop, UTC |
| --- | --- | --- |
| Car 1, VSC | 13:42:44.140–13:43:03.760 | 13:43:23.160–13:43:33.780 |
| Car 4, SC | Unavailable | 14:07:54.641–14:08:04.821 |
| Car 23, VSC | 13:43:18.801–13:43:39.000 | Unavailable |

The windows bound a change between confirmed route observations. The archived lane timing sits in a separate field. The trials retain stationary lane candidates without treating them as confirmed service. Car 43 was the only ahead candidate found among mapped observations across the hypothetical car-1 merge window. Unlocalized and ambiguous cars still prevent a unique merge claim. Removing exit fixes from the held SC visit suppressed the 10.18 s window.

At Imola SC track clear, the uncertainty bands overlapped for **16** circular pairs among the localized cars. Another **137** lap-unwrapped pairs had separated intervals. The separated pairs establish partial race-distance order only. Stationary or missing cars, circular overlap and the lack of a simultaneous independent order reference left **zero certified predecessors**. The certificate records candidate arcs, possible interveners, and exclusions using route evidence or conservative movement bounds.

## Cutoff and sparsity results

Across eleven paired cutoffs with identical observations, the new method added **eight retrospective racing-route assignments** and **one retrospective pit-lane assignment**. It withheld one prior retrospective racing position, Singapore car 63 at the later control, because the available movement estimate was near stationary and the route was unverified.

The causal method requires the fit and both held checks to predate a cutoff in event time. The Imola qualification window ended at **14:08:55.101 UTC**. There were **zero new causal route assignments** at the selected cutoffs. Removing future fixes reproduced each causal field.

At the exact Singapore D8R1 cutoff, cars 63 and 4 both had fixes **0.118 s** old. The Singapore fitting trace failed the frozen continuity and motion criteria. Projected loop candidates remain available for audit, while pit residuals, certified routes, and lap-unwrapped positions for those two cars remain unavailable. The board still reports **17/20 supported positions** at that cutoff.

One-in-eight thinning removed localization at both stress cutoffs. One-in-four thinning retained 18 Imola SC-clear positions after the stationary-motion gate, and none in Singapore. Thinning must not promote stationary records into verified racing motion. Duplicate, conflict, irregular, short-gap, long-gap and delayed-availability controls are in the evidence. Full-input comparisons measure stability against correlated estimates.

## Reproduction and readiness

- **421 tests passed**, including the 406-test baseline. Ruff passed.
- All four pinned S7-A release hashes matched. An independent S7-A run reproduced 52 deterministic outputs and its checksum manifest. An independent S6-C run matched 20 deterministic outputs, and the sealed analysis reproduced with the pinned D8R1 model. Simulator execution code was untouched.
- Two S7-A.1 runs matched all 54 deterministic outputs and their checksum manifest. The evidence bundle contains 26 diagnostic figures and six captures of the running dashboard.
- Open **Race State → Pit routes and predecessor evidence** to compare strict and revised assignments, inspect the certificate and view the retrospective pit windows. **Evidence & Validation** exposes the geometry and readiness gates.

```powershell
python -m f1ri.cli stage6-s6-c --output artifacts/runs/s7a1_s6c_replay
python -m f1ri.cli stage7-s7-a --output artifacts/runs/s7a1_s7a_replay --s6-c-run artifacts/runs/s7a1_s6c_replay
python -m f1ri.cli stage7-s7-a1 --output artifacts/runs/s7a1_replay --s7-a-run artifacts/runs/s7a1_s7a_replay
```

Historical strategy execution remains disabled. The next physical evidence gap is an independently registered entry or merge boundary with simultaneous neighboring cars. Publication, receipt and freshness remain unverified. Live operation remains unsupported.

[Evidence bundle](stage7_s7_a1_evidence.zip) · [Frozen new inputs](stage7_s7_a1_inputs.zip) · [Metrics](stage7_s7_a1_metrics.json)
