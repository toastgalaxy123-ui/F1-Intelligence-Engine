# F1 Race Intelligence Engine — Stage 5 S5-C.1
## Model recovery, paired forecasts and strategy discrimination

**Path C — Original D8 model unavailable.** The deterministic strategy engine can distinguish tyre trajectories and show when physical following suppresses a pace advantage. The saved Singapore draws still support continuation replay. This release produced zero authentic D8 replacement forecasts and no full-model strategy ranking.

## Model recovery and forecast scope

The audit checked the current project, the former Stage 3 directory, reachable Git paths and LFS, research ZIPs, and a local project backup ZIP. It found the pinned model manifest, the Stage 3 source and experiment hashes, the archived numerical reference cases, and the current project's byte-verified Pirelli allocation CSV. The original fitted pickle and the Stage 3 run's configuration, data manifest, run metadata and split file were missing. The original warehouse was also missing. The surviving local warehouse has a different hash. No training or candidate pickle loading took place in the study.

The forecast adapter uses the existing S1 continuation and S2 replacement requests. It validates source clocks, driver and session context, cutoff-known tyre state, the planned lap and forecast horizon. At a committed tyre fit it generates a separate new-stint request using the fitted set and actual target lap. It holds external observations fixed at the experiment cutoff. The service clock is stored separately from the feature cutoff. Branch-local requests carry the previous fitted set through later stops. The authentic path requires the exact verified model bundle and a Stage 3 reference parity check. Tests with a stub exercised new requests, fit timing and stream bookkeeping; no authentic replacement outputs or full-model parity are claimed.

The saved continuation archive supplied 1,000 draws per driver for 19 drivers. The study summarized 1, 3, 5, 10, 15 and 20 lap prefixes and replayed draws 0, 1 and 7. Each identical-policy pair reproduced the same path. The observed lap-1/lap-2 sample correlation for car 63 was 0.669. The first-lap correlation between cars 4 and 63 was −0.008. S1 seeds each driver and stint independently, and D8's sampler models persistent pace and slope terms plus within-stint autoregressive innovations. The adapter uses the same seed for same-driver alternatives at the same start lap, stint ordinal and draw shape. A different stint or start lap has a distinct seed. This preserves the existing sampling policy, which does not estimate cross-driver or cross-stint covariance. The saved continuation summaries are separate from a new fitted-model parity run.

Singapore remains a frozen pilot. Its saved P2P snapshot provides lap, tyre and weather features, but at that cutoff there is no verified exact predecessor field, replacement inventory or physical pit branch and merge for the projected field. Historical strategy replay stayed gated.

## When tyre pace changes the result

The new ledger captures the actual maximum over the clean-air traversal, predecessor headway, scheduler clock floor and neutralization movement bounds. It identifies the binding category and measures how much the headway adds beyond the strongest other floor. Pit-route events, service waiting and scripted passes have separate rows. This attribution is local to the committed schedule. Removing one bound can change later events.

| Declared fixture | Hard minus medium at the common marker | Medium branch following through the marker |
| --- | ---: | ---: |
| GREEN with predecessor | 0.000 s | 10 of 10 racing traversals |
| Clean air | +20.667 s | 0 of 10 |
| Predecessor retires at 290 s | +14.306 s | 2 of 10 |
| Longer horizon with the same predecessor | 0.000 s | 19 of 19 |

In the original GREEN fixture, medium and hard have ten differing clean-air checkpoints. Nine reach virtually the same crossing clock while the headway binds. Medium's measured headway contribution through the marker is 41.100 s. Both branches are 11.433 s behind the stay-out reference. The clean-air and retirement controls confirm that the different tyre curves become effective when the physical constraint is absent. Different opening gaps and opponent pace change which branches remain constrained. The longer horizon preserves the hard-medium tie under continued blocking while the used-soft branch falls further behind.

## Sensitivity and field integration

- Extending the declared SC release sequence by 22 s changed the reactive stop from +7.327 s to −39.642 s against stay-out. The pit decision, entry, merge and terminal predecessor stayed the same. Headway added 42.568 s in the reference reactive branch and zero in the extended case. Both reached the same racing marker.
- Under the heavier declared wear curve, the immediate-medium branch reached the same clock. Stay-out took 104.000 s longer, reversing the paired delta from +11.433 s to −92.567 s.
- Increasing service from three to five seconds inside the fixed lane timetable left merge unchanged. In a separate case that also shifted the downstream route by two seconds, the car actually merged two seconds later. The following constraint then absorbed two seconds less, leaving the final marker unchanged.
- The six-car declared field used the verified 4,909 m Imola loop length, three teams, multiple compounds and ages, one lapped car, shared boxes, VSC and SC. In this fixture, staggering was 23.744 s faster in combined time than stay-out, with separate gains of 10.731 and 13.013 s. This is conditional on the declared field, tyre curves, pit routes and neutralization profile.

## Verification

The study ran 13 fixtures, 68 deterministic branches and 13 sensitivity cases with zero recorded invariant failures. The 329-test baseline and all 339 final tests passed, with 104 warnings in the available environment. Ruff and the Streamlit smoke test passed. The S5-C metrics reproduced byte-for-byte, and the frozen GREEN pit stop still executed at 160–217 s. Two independent S5-C.1 runs produced 16 byte-identical files. Metrics SHA-256 is `500b99c186e144cf727515ad0db3c9f473c182dc45b2cc7ef186f35550e4a823`.

Deterministic strategy analysis can proceed within the tested engineering scope. Model-backed optimisation remains gated on the original D8 bundle and cutoff-qualified replacement inputs. If the original bundle cannot be recovered, a separately authorized Stage 3 reproduction would need the original cohort roles and a new model identity.

[Diagnostic figure](stage5_s5_c1_model_strategy_integration.png) · [Checksummed ledgers](stage5_s5_c1_model_strategy_integration_ledgers.zip) · [Reproduction guide](../../docs/STAGE5_S5_C1_MODEL_STRATEGY.md)
