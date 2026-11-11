# Stage 2 Specification — Validated Race Pace Modeling

## 1. Prediction Objective & Mathematical Decomposition

Stage 2 estimates a driver's underlying **clean-air green-flag lap-time distribution** for a target lap $l$ in race $r$, conditional on continuing the current tire stint under normal green-flag racing conditions:

$$T_{d,r,l} = \mu_{d,r} + F_r(l) + D(c, a) + W(x) + \epsilon_{d,r,l}$$

Where:
- $T_{d,r,l}$: Observed lap duration (seconds) of driver $d$ in race $r$ on lap $l$.
- $\mu_{d,r}$: Latent driver/car/circuit baseline pace for race $r$, combining a historical pre-race prior with online shrinkage from already-completed clean laps in race $r$.
- $F_r(l)$: Race-progression effect $p_l = l / N_r$ (capturing fuel burn and track evolution, where $N_r$ is the scheduled race distance known prior to the lap).
- $D(c, a)$: Tire compound $c$ and tire age $a$ (`tyre_age_at_lap_start`) effect.
- $W(x)$: Point-in-time weather/contextual adjustments (`last_known_track_temp_c`).
- $\epsilon_{d,r,l}$: Residual lap-to-lap variability, calibrated into a predictive distribution (`std_seconds`, 50% and 90% prediction intervals).

### Key Decisions Locked In
1. **Primary prediction target**: Next-lap (`horizon = 1`) clean-air green-flag lap time (`observed_target_lap_time_s` when `target_is_clean_green = TRUE`), with secondary evaluation at `horizon = 3` and `horizon = 5` conditional on continuing the current stint.
2. **Prediction cutoff (`forecast_as_of_utc`)**: Immediately upon completion of lap $l - h$ (`lap_end_estimate_utc` of the cutoff lap). Zero observations after `forecast_as_of_utc` may enter feature computation or online pace adaptation.
3. **Evaluation eligibility separation**: Predictions are generated for all continued-stint forecast opportunities at the cutoff; whether the target lap subsequently remained clean and green (`target_is_clean_green`) is used strictly to filter clean-air accuracy evaluation while preserving all predictions in the backtest log.
4. **Chronological race-level splits**:
   - **Training (`train`)**: 2023–2024 seasons (`7953` Bahrain 2023, `9102` Spain 2023, `9157` Monza 2023, `9165` Singapore 2023, `9472` Bahrain 2024, `9515` Imola 2024, `9590` Monza 2024, `9606` Singapore 2024).
   - **Validation (`val`)**: First half of 2025 (`10014` Bahrain 2025, `9987` Imola 2025 [diagnostic/inspected]).
   - **Held-Out Test (`test`)**: Second half of 2025 (`9912` Monza 2025 [untouched], `9896` Singapore 2025 [diagnostic/inspected; reported both pooled and separately from untouched test]).
   - **Regulation Transfer (`transfer_2026`)**: Completed 2026 races (`11253` Suzuka 2026, `11361` Monza 2026, `11388` Singapore 2026).
5. **Primary selection metric**: Race-weighted next-lap MAE on validation races (preventing high-clean-lap races from dominating model selection), accompanied by lap-weighted MAE, RMSE, mean error (bias), 50%/90% interval coverage, mean interval width, and Gaussian CRPS.

## 2. Controlled Experiment Matrix (`E0`–`E10`)

| ID | Model | Description |
| :--- | :--- | :--- |
| **E0** | `HistoricalCircuitPriorBaseline` | Pre-race historical circuit + team/driver reference pace (no in-race laps). |
| **E1** | `RecentCleanLapMedianBaseline` | Median of up to last 3 clean green laps in the race (`forecast_as_of_utc`), falling back to E0. |
| **E2** | `CompoundRecentMedianBaseline` | Median of up to last 3 clean green laps on the **current stint/compound**, falling back to E1/E0. |
| **E3** | `RaceProgressDriftBaseline` | E2 adjusted by historical linear race-progress slope $\beta_{\text{progress}} \Delta p$. |
| **E4** | `AdditivePaceModel (Base)` | Ridge-regularized additive regression: circuit, team/driver, compound, and race-progress effects. |
| **E5** | `AdditivePaceModel (+Age)` | E4 + compound-specific linear/quadratic tire-age degradation slopes $D(c, a)$. |
| **E6** | `AdditivePaceModel (+Temp)` | E5 + point-in-time track temperature (`last_known_track_temp_c`). |
| **E7** | `PaceModelV1 (Adaptive)` | Best of E5/E6 + empirical-Bayes partial-pooling online adaptation of $\mu_{d,r}$ from prior clean laps in the current race/stint. |
| **E8** | `PaceModelV1 (Calibrated)` | E7 + out-of-sample residual scale calibration conditioned on sample count and tire age (`50%` & `90%` intervals). |
| **E9** | `Frozen 2023-2025 -> 2026` | Direct transfer of frozen 2023–2025 Pace V1 to 2026 races without retraining. |
| **E10** | `Progressive 2026 Adaptation` | Rolling chronological updating across 2026 races with era-specific adaptation. |
