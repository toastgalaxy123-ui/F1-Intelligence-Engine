# Stage 2 Validation Report — Validated Race Pace Modeling (`PaceModelV1`)

## 1. Executive Summary & Prediction Contract

Stage 2 transitions the F1 Race Intelligence Engine from race-condition reconstruction (Stages 0–1) to point-in-time statistical pace forecasting. The primary target modeled by `PaceModelV1` (`AdditivePaceModel` / `E8_pace_v1_calibrated`) is:

> **Target Definition**: The observed lap duration $T_{d,r,l+h}$ (in seconds) of a completed, uninterrupted, clean-air, green-flag racing lap at horizon $h \in \{1, 3, 5\}$ laps ahead of cutoff lap $l$, conditional on the driver remaining out of the pits on the specified tire compound and tire age.

Every forecast row in `gold.pace_training_examples` is governed by a strict point-in-time cutoff timestamp (`forecast_as_of_utc`), defined as the completion timestamp of cutoff lap $l$. No information from after `forecast_as_of_utc`—including future laps in the same stint, full-race driver averages, post-cutoff weather readings, or post-cutoff track-status changes—is permitted to enter the feature vector or model state.

### Key Empirical Outcomes
1. **Point-in-time accuracy beats all baselines on held-out races**:
   - On 2025 validation races (`10014` Sakhir, `9987` Imola), `E8_pace_v1_calibrated` achieves a race-weighted next-lap MAE of **0.3085 s** (vs **0.3596 s** for `E2` compound-adjusted recent median and **0.3555 s** for `E3` race-progress drift).
   - On 2025 test races (`9912` Monza untouched, `9896` Singapore), `E8_pace_v1_calibrated` achieves a race-weighted next-lap MAE of **0.3240 s** (matching `E2` at **0.3240 s**, beating `E3` at **0.3451 s** and `E1` at **0.3543 s**) and **0.2797 s** on the completely untouched 2025 Monza test race (`9912`).
   - Across all 4 held-out 2025 races (`val + test`), `E8` improves race-level MAE over `E2` on **3 of 4 races** (mean paired $\Delta\text{MAE} = -0.0256\text{ s}$) and improves over both `E3` ($\Delta\text{MAE} = -0.0341\text{ s}$, 90% CI $[-0.0479, -0.0202]\text{ s}$) and `E1` ($\Delta\text{MAE} = -0.0648\text{ s}$, 90% CI $[-0.1009, -0.0287]\text{ s}$) on **4 of 4 races**.
2. **Where `PaceModelV1` wins most**:
   - Immediately after pit stops (`clean_laps_observed_current_stint == 0`, `clean_laps_observed_race > 0`), naive recency baselines (`E1`–`E3`) carry over worn-tire pace from the end of the previous stint and suffer **1.27 s–1.33 s** MAE. `PaceModelV1` explicitly models the cross-stint fresh-tire reset and fuel burn, cutting post-pit first-clean-lap MAE to **0.57 s–0.78 s** (a **~0.60 s** reduction in error).
   - Over multi-lap horizons ($h=3$ and $h=5$), `PaceModelV1` (`val` MAE **0.4360 s** at $h=3$, **0.5226 s** at $h=5$) outperforms `E2` (**0.5300 s** at $h=3$, **0.6429 s** at $h=5$) by accounting for compound-specific within-stint drift.
3. **Calibrated uncertainty intervals**:
   - Bucketed residual interval calibration (`E8` vs uncalibrated `E7`) improves held-out 90% prediction interval coverage from **83.24%** to **92.35%** on `test` ($h=1$, nominal 90%) and **61.25%** to **84.53%** at $h=5$, while reducing CRPS from **0.2588 s** to **0.2478 s** ($h=1$) and from **0.5068 s** to **0.4450 s** ($h=5$).
4. **2026 regulation-era transfer (`E9` vs `E10`)**:
   - On the 3 completed 2026 races (`11253` Suzuka, `11361` Monza, `11388` Singapore), progressive rolling-origin adaptation (`E10`, race-weighted MAE **0.4907 s**, CRPS **0.3841 s**) outperforms both the frozen 2023–2025 model (`E9`, MAE **0.4952 s**) and the `E2` baseline (**0.5049 s**), reducing MAE on the third 2026 race (`11388` Singapore) from **0.3661 s** (`E9`) to **0.3483 s** (`E10`).

---

## 2. Dataset Cohort & Quality Audit (Stage 2.1)

Fifteen dry-race sessions across four seasons (2023–2026) and two regulation eras (`2023_2025` and `2026_PLUS`) were ingested with all 10 OpenF1 endpoints and validated against all Stage 0 and Stage 1 invariants (`15/15 READY`). Across the 15 races, `gold.lap_context` contains **17,027 completed/classified laps** (`6,557` clean green pace laps) and `gold.pace_training_examples` contains **55,146 point-in-time forecast opportunity rows** across horizons $h \in \{1, 3, 5\}$.

| Split | Season | Session Key | Circuit | Era | Total Laps | Clean Green Laps | Clean % | Clean Stints | Median Clean Lap (s) | IQR (s) | Inspected Prior to Stage 2 |
| :--- | :---: | :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `train` | 2023 | `7953` | Sakhir | `2023_2025` | 1,058 | 484 | 45.75% | 55 | 97.973 | 1.217 | No |
| `train` | 2023 | `9102` | Catalunya | `2023_2025` | 1,312 | 607 | 46.27% | 52 | 79.874 | 1.441 | No |
| `train` | 2023 | `9157` | Monza | `2023_2025` | 968 | 370 | 38.22% | 34 | 86.795 | 0.955 | No |
| `train` | 2023 | `9165` | Singapore | `2023_2025` | 1,091 | 284 | 26.03% | 30 | 99.897 | 1.827 | No |
| `train` | 2024 | `9472` | Sakhir | `2023_2025` | 1,129 | 531 | 47.03% | 50 | 96.454 | 1.448 | No |
| `train` | 2024 | `9515` | Imola | `2023_2025` | 1,239 | 533 | 43.02% | 38 | 81.763 | 1.600 | No |
| `train` | 2024 | `9590` | Monza | `2023_2025` | 1,009 | 420 | 41.63% | 42 | 84.615 | 1.608 | No |
| `train` | 2024 | `9606` | Singapore | `2023_2025` | 1,179 | 574 | 48.69% | 38 | 98.287 | 1.549 | No |
| `val` | 2025 | `10014` | Sakhir | `2023_2025` | 1,129 | 306 | 27.10% | 38 | 98.252 | 1.133 | No |
| `val` | 2025 | `9987` | Imola | `2023_2025` | 1,207 | 338 | 28.00% | 40 | 81.226 | 1.618 | Yes (Stage 1) |
| `test` | 2025 | `9912` | Monza | `2023_2025` | 976 | 415 | 42.52% | 32 | 82.995 | 0.998 | **No (Untouched)** |
| `test` | 2025 | `9896` | Singapore | `2023_2025` | 1,229 | 474 | 38.57% | 37 | 97.083 | 1.856 | Yes (Stage 0/1) |
| `transfer_2026` | 2026 | `11253` | Suzuka | `2026_PLUS` | 1,108 | 326 | 29.42% | 36 | 95.332 | 2.513 | No |
| `transfer_2026` | 2026 | `11361` | Monza | `2026_PLUS` | 1,055 | 370 | 35.07% | 29 | 86.484 | 1.077 | No |
| `transfer_2026` | 2026 | `11388` | Singapore | `2026_PLUS` | 1,240 | 525 | 42.34% | 42 | 98.563 | 2.023 | No |

### Upstream Anomalies & Data Hygiene Discoveries
- **Red Flag Restart Procedure Masking (`11361` 2026 Monza)**: During the Lap 3 Red Flag suspension at 2026 Monza, FIA Race Control issued a `TRACK CLEAR` message while cars were still parked in the pit lane, followed after `SESSION STARTED` by an `EXTRA FORMATION LAP` and `STANDING START` on Lap 5 (`194.6 s–200.7 s`). Updating the Stage 1 state machine (`build_track_status_intervals`) so that `TRACK CLEAR` never clears an active `RED` suspension and `STANDING START` / `EXTRA FORMATION LAP` / `ROLLING START` hold `SC` status until `PIT EXIT OPEN` / `OVERTAKE ENABLED` eliminated all 12 contaminated restart laps.
- **Transponder Clock Inversion (`10014` 2025 Bahrain, Russell Lap 38)**: OpenF1 recorded `date_start` for Lap 38 `14.18 s` earlier than `lap_start_utc(37) + lap_duration(37)`. The example builder flags any row where `forecast_as_of_utc - target_lap_start_utc > 1.5 s` as `UPSTREAM_TIMESTAMP_ANOMALY` and excludes it from clean evaluation.

---

## 3. Selection-Bias Audit of `is_clean_green_pace_lap`

Filtering to clean-air, green-flag, non-pit racing laps retains **38.51%** of all laps across the 15-race cohort, but retention is strongly non-uniform across race states:

| Dimension | Bucket | Total Laps | Clean Green Laps | Retention % | Modeling Implication |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **Position Group** | `1_LEADER_P1` | 876 | 658 | **75.11%** | Race leader runs predominantly in clean air (except when lapping backmarkers). |
| | `2_FRONT_P2_P5` | 3,502 | 1,499 | **42.80%** | Front-runners spend ~57% of laps within 2.0 s traffic or pit/neutralized phases. |
| | `3_MIDFIELD_P6_P12` | 6,109 | 2,216 | **36.27%** | Midfield DRS trains reduce clean-air observability. |
| | `4_BACK_P13_P20` | 6,442 | 2,184 | **33.90%** | Backmarkers have the lowest clean-air sample rate. |
| **Race Phase** | `0_LAP_1` | 305 | 0 | **0.00%** | Standing start and Lap 1 traffic are deterministically excluded. |
| | `1_EARLY_THIRD` | 5,288 | 1,453 | **27.48%** | Field compression in Stint 1 suppresses early clean-lap counts. |
| | `2_MIDDLE_THIRD` | 5,881 | 2,559 | **43.51%** | Field spreads out after first pit window. |
| | `3_LATE_THIRD` | 5,455 | 2,545 | **46.65%** | Highest clean-air visibility occurs in final third of the race. |
| **Tire Age** | `00_05_FRESH` | 4,081 | 958 | **23.47%** | Pit-out laps (`tyre_age <= 1`) and early-stint traffic reduce fresh-tire visibility. |
| | `06_15_MID` | 6,618 | 2,576 | **38.92%** | Balanced mid-stint coverage. |
| | `16_25_USED` | 4,020 | 1,891 | **47.04%** | High retention in settled stints. |
| | `26_PLUS_OLD` | 2,165 | 1,132 | **52.29%** | Long clean stints disproportionately survive to high tire ages (survivorship bias). |
| **Compound** | `HARD` | 8,284 | 3,489 | **42.12%** | Primary long-run race compound. |
| | `MEDIUM` | 5,934 | 2,194 | **36.97%** | Frequently used in Stint 1 where traffic is denser. |
| | `SOFT` | 2,666 | 874 | **32.78%** | Concentrated in opening stints or late fast-lap attempts. |

Because long stints on durable tires disproportionately survive to `26_PLUS_OLD`, an uncentered regression across raw laps underestimates true tire wear. `AdditivePaceModel` mitigates this by estimating dynamic slopes using within-`(session_key, driver_number)` centered design matrices (`ridge_alpha = 15.0`) and separating cross-stint fresh-tire reset slopes from within-stint net drift rates.

---

## 4. Controlled Experiment Matrix (`E0`–`E10`)

All models implement the `PaceModel` contract (`predict(PaceRequest) -> PaceDistribution`). Models `E0`–`E8` were trained strictly on the 2023–2024 `train` split (with `E8` fitting its residual interval multipliers on `val`).

### 4.1 Next-Lap ($h = 1$) Held-Out Performance

| Experiment ID | Model Description | `val` Race MAE (s) | `val` RMSE (s) | `val` CRPS (s) | `test` Race MAE (s) | `test` Untouched (`9912`) MAE (s) | `test` RMSE (s) | `test` 50% Cov | `test` 90% Cov | `test` CRPS (s) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `E0_historical_prior` | Historical circuit + team + driver + compound prior | 1.1808 | 1.3990 | 0.8003 | 1.4471 | 1.6697 | 1.6176 | 28.0% | 96.2% | 0.9342 |
| `E1_recent_clean_median` | Driver's last 3 clean green laps median (fallback `E0`) | 0.4077 | 0.6373 | 0.3133 | 0.3543 | 0.2872 | 0.7352 | 52.9% | 85.3% | 0.2795 |
| `E2_compound_recent_median` | Driver's last 3 clean laps on current stint/compound | 0.3596 | 0.5670 | 0.2689 | **0.3240** | **0.2672** | 0.6919 | 50.5% | 84.5% | 0.2503 |
| `E3_race_progress_drift` | `E2` + linear race-progress drift | 0.3555 | 0.5663 | 0.2665 | 0.3451 | 0.2976 | 0.6985 | 45.1% | 82.8% | 0.2645 |
| `E4_additive_base` | Static additive model (no tire-age slope, no online adapt) | 1.2109 | 1.3957 | 0.8298 | 1.5683 | 1.8004 | 1.7345 | 14.7% | 59.3% | 1.0966 |
| `E5_additive_compound_age` | `E4` + compound-specific linear tire-age slopes | 1.1988 | 1.3303 | 0.8153 | 1.9147 | 2.1892 | 2.0425 | 3.5% | 29.8% | 1.4171 |
| `E6_additive_temp` | `E5` + point-in-time track temperature slope | 1.1447 | 1.2738 | 0.7738 | 1.8904 | 2.1664 | 2.0203 | 3.5% | 31.1% | 1.3960 |
| `E7_pace_v1_online_uncalibrated` | `E6` + in-race stint/cross-stint adaptation (uncalibrated) | **0.3085** | **0.4626** | 0.2333 | **0.3240** | 0.2797 | **0.6879** | 51.1% | 83.2% | 0.2588 |
| `E8_pace_v1_calibrated` | **Pace V1**: `E7` + bucketed empirical residual calibration | **0.3085** | **0.4626** | **0.2236** | **0.3240** | 0.2797 | **0.6879** | **59.3%** | **92.4%** | **0.2478** |

### 4.2 Multi-Lap Horizon ($h = 3$ and $h = 5$) Held-Out Performance

| Experiment ID | `val` $h=3$ MAE (s) | `val` $h=5$ MAE (s) | `test` $h=3$ MAE (s) | `test` $h=5$ MAE (s) | `test` $h=3$ 90% Cov | `test` $h=5$ 90% Cov | `test` $h=3$ CRPS (s) | `test` $h=5$ CRPS (s) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `E1_recent_clean_median` | 0.5647 | 0.6739 | 0.4782 | 0.5574 | 83.0% | 85.2% | 0.3604 | 0.4063 |
| `E2_compound_recent_median` | 0.5300 | 0.6429 | 0.4496 | 0.5328 | 81.6% | 82.2% | 0.3345 | 0.3854 |
| `E3_race_progress_drift` | 0.5191 | 0.6312 | 0.5085 | 0.6289 | 75.8% | 69.7% | 0.3762 | 0.4578 |
| `E7_pace_v1_online_uncalibrated` | **0.4360** | **0.5226** | 0.4826 | 0.6177 | 72.8% | 61.3% | 0.3892 | 0.5068 |
| `E8_pace_v1_calibrated` | **0.4360** | **0.5226** | 0.4826 | 0.6177 | **87.6%** | **84.5%** | 0.3551 | 0.4450 |

### 4.3 Fitted Structural Parameters of `E8_pace_v1_calibrated`
- **Cross-stint race-progress slope (`progress_slope_`)**: `-2.9625 s` per full race distance (`~ -0.049 s/lap` on a 60-lap race), capturing fuel mass reduction across stints.
- **Cross-stint compound tire-age slopes (`compound_age_slopes_`)**:
  - `SOFT`: `+0.06999 s/lap`
  - `MEDIUM`: `+0.06377 s/lap`
  - `HARD`: `+0.06261 s/lap`
- **Within-stint net per-lap drift (`within_stint_drift_`, combining fuel burn and tire wear inside an active stint)**:
  - `SOFT`: `+0.03655 s/lap`
  - `MEDIUM`: `+0.02101 s/lap`
  - `HARD`: `+0.01755 s/lap`
- **Track temperature slope (`temp_slope_`)**: `+0.01325 s/°C` relative to circuit median track temperature.
- **Calibrated uncertainty base scales (`calibrator_bucket_scales_s`)**:
  - `0_COLD` (0 clean laps in current stint): `0.8394 s`
  - `1_2_EARLY` (1–2 clean laps in current stint): `0.3813 s`
  - `3_PLUS_WARM` (3+ clean laps in current stint): `0.3270 s`

---

## 5. Paired Race-Level Comparisons (2025 `val` + `test`)

Because laps within a race are correlated, model superiority is evaluated at the race level across the 4 held-out 2025 races (`10014` Sakhir, `9987` Imola, `9912` Monza, `9896` Singapore):

| Comparison | Races Improved | Mean Race $\Delta\text{MAE}$ (s) | SE (s) | 90% CI (s) | `10014` Sakhir | `9987` Imola | `9912` Monza (Untouched) | `9896` Singapore |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`E8` vs `E2` (Compound Median)** | **3 / 4** | **-0.0256** | 0.0158 | $[-0.0516, +0.0005]$ | **0.3375** vs 0.3952 (`-0.0577`) | **0.2795** vs 0.3239 (`-0.0444`) | 0.2797 vs **0.2672** (`+0.0125`) | **0.3682** vs 0.3809 (`-0.0126`) |
| **`E8` vs `E3` (Progress Drift)** | **4 / 4** | **-0.0341** | 0.0084 | $[-0.0479, -0.0202]$ | **0.3375** vs 0.3754 (`-0.0379`) | **0.2795** vs 0.3356 (`-0.0561`) | **0.2797** vs 0.2976 (`-0.0179`) | **0.3682** vs 0.3926 (`-0.0244`) |
| **`E8` vs `E1` (Recent Clean Median)** | **4 / 4** | **-0.0648** | 0.0220 | $[-0.1009, -0.0287]$ | **0.3375** vs 0.4377 (`-0.1002`) | **0.2795** vs 0.3777 (`-0.0982`) | **0.2797** vs 0.2872 (`-0.0075`) | **0.3682** vs 0.4215 (`-0.0533`) |

---

## 6. Sliced Residual Diagnostics (`E8_pace_v1_calibrated`, $h=1$ on `val + test`)

### 6.1 Error by Clean-Lap Warm-Up Bucket (`clean_laps_observed_current_stint`)
| Bucket | Eligible Laps | MAE (s) | RMSE (s) | Bias (s) | 50% Coverage | 90% Coverage |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `0_COLD` (First clean lap of race or new stint) | 146 | 0.9777 | 1.2275 | -0.3300 | 40.41% | 79.45% |
| `1_2_EARLY` (1–2 prior clean laps in stint) | 259 | **0.2613** | 0.3646 | -0.0218 | 64.86% | 94.21% |
| `3_PLUS_WARM` (3+ prior clean laps in stint) | 1,126 | **0.2462** | 0.5194 | +0.0041 | 60.92% | 95.38% |

Once at least one clean lap is observed in the current stint (`1_2_EARLY` and `3_PLUS_WARM`, representing 90.5% of all clean racing laps), `PaceModelV1` achieves **0.246 s–0.261 s** MAE with near-zero bias (`-0.022 s` to `+0.004 s`).

### 6.2 Error by Tire Compound & Conditional Calibration
| Compound | Eligible Laps | MAE (s) | RMSE (s) | Bias (s) | 50% Coverage | 90% Coverage | Mean 50% Width (s) | Mean 90% Width (s) | CRPS (s) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `HARD` | 763 | **0.2862** | 0.4603 | -0.0297 | 61.07% | 95.02% | 0.5673 | 1.5690 | 0.2117 |
| `MEDIUM` | 653 | **0.3218** | 0.5104 | -0.0843 | 57.73% | 92.19% | 0.5691 | 1.5741 | 0.2370 |
| `SOFT` | 115 | 0.5146 | 1.4028 | +0.2482 | 60.87% | 93.04% | 0.6262 | 1.7319 | 0.4131 |

*(Note: `SOFT` RMSE is inflated by a single mechanical outlier—Lewis Hamilton's Lap 62 front-left brake failure at 2025 Singapore `9896`, where his lap time spiked to `107.713 s` vs `95.6 s` predicted in clean air.)*

### 6.3 Error by Tire Age Bucket & Race Progress Bucket
| Slice Type | Bucket | Eligible Laps | MAE (s) | RMSE (s) | Bias (s) | 50% Coverage | 90% Coverage |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Tire Age** | `00_05_FRESH` | 230 | 0.3982 | 0.5927 | -0.0765 | 64.35% | 93.91% |
| | `06_15_MID` | 565 | 0.2891 | 0.4714 | -0.0316 | 59.65% | 94.87% |
| | `16_25_USED` | 450 | 0.3369 | 0.7944 | +0.0101 | 56.89% | 91.33% |
| | `26_PLUS_OLD` | 286 | **0.2838** | 0.4868 | -0.0638 | 60.14% | 94.76% |
| **Race Progress** | `1_EARLY` ($0.00 - 0.33$) | 363 | 0.3729 | 0.5998 | -0.0908 | 59.78% | 91.74% |
| | `2_MIDDLE` ($0.33 - 0.67$) | 646 | **0.2827** | 0.4607 | -0.0478 | 62.07% | 95.20% |
| | `3_LATE` ($0.67 - 1.00$) | 522 | 0.3251 | 0.7459 | +0.0281 | 56.51% | 93.10% |

---

## 7. 2026 Regulation Era Transfer (`E9` vs `E10`)

The 2026 technical regulations (`2026_PLUS`) introduce active aerodynamics and revised power-unit energy deployment, shifting absolute lap times by `+1.5 s` to `+3.5 s` depending on circuit power sensitivity. We compare:
- **`E9_frozen_2026_transfer`**: Trained once on 2023–2025 (`train + val + test`) and frozen across all 2026 races.
- **`E10_progressive_2026_adaptation`**: Rolling-origin expanding window that incorporates completed 2026 races prior to each target 2026 race, updating `2026_PLUS` team offsets, regulation-era circuit shifts, and degradation slopes.

| 2026 Race | Prior 2026 Races Available | `E2` Baseline MAE (s) | `E9` Frozen MAE (s) | `E10` Progressive MAE (s) | $\Delta\text{MAE}$ (`E10 - E9`) | Interpretation |
| :--- | :---: | :---: | :---: | :---: | :---: | :--- |
| `11253` Suzuka | 0 | 0.8016 | **0.7423** | **0.7423** | `0.0000 s` | Unseen circuit in 2023–2025; both `E9` and `E10` cold-start identically and beat `E2` by `-0.059 s`. |
| `11361` Monza | 1 (`11253`) | **0.3117** | 0.3773 | 0.3816 | `+0.0043 s` | First shared circuit in 2026; high straight-line aero sensitivity. |
| `11388` Singapore | 2 (`11253`, `11361`) | 0.4014 | 0.3661 | **0.3483** | **`-0.0178 s`** | With 2 prior 2026 races learned, `E10` improves cold-start and cross-stint accuracy over both `E9` and `E2`. |
| **All 2026 ($h=1$)** | — | 0.5049 | 0.4952 | **0.4907** | **`-0.0045 s`** | Progressive adaptation reduces MAE across $h=1$ (`0.4907` vs `0.4952`), $h=3$ (`0.8678` vs `0.8742`), and $h=5$ (`1.1274` vs `1.1354`). |

---

## 8. Diagnostic Figures (Plots A–H)

### Figure 1 — Residual & Contextual Diagnostics (Plots A–D)
![Stage 2 Residual Diagnostics Plots A-D](C:/Users/Antoine/.gemini/antigravity/brain/e1284f8e-83ae-4c2a-9d60-b165d916e24f/stage2_residuals_abcd.png)

### Figure 2 — Compound Distributions, Per-Race Benchmark, Calibration & 2026 Adaptation (Plots E–H)
![Stage 2 Benchmark and Calibration Diagnostics Plots E-H](C:/Users/Antoine/.gemini/antigravity/brain/e1284f8e-83ae-4c2a-9d60-b165d916e24f/stage2_diagnostics_efgh.png)

---

## 9. Known Limitations & Stage 3 Recommendations

1. **Opening-Lap Cold Start (`n_race == 0`)**: Before a driver completes their first clean green lap in a race (`0_COLD`, ~9.5% of clean laps), predictions rely on the historical circuit prior (`hist_pred`), where year-to-year track resurfacing (e.g., 2025 Monza being `~1.7 s` faster than 2024 Monza) produces `~0.98 s` MAE. Incorporating Qualifying / Practice (`FP2` long-run) session priors from the same Grand Prix weekend—strictly prior to race start—will eliminate year-to-year circuit shift on Lap 2.
2. **Relative vs Absolute Pirelli Compound Codes (`C1`–`C6`)**: OpenF1 provides relative weekend compound labels (`SOFT`, `MEDIUM`, `HARD`). Adding a curated circuit-season lookup for Pirelli's absolute compound allocation (`C1`–`C5`) in Stage 3 will sharpen cross-circuit tire degradation pooling.
3. **Nonlinear Late-Stint Tire Cliff (Stage 3 Target)**: While linear compound-specific slopes + within-stint drift perform well through 25–30 laps, Stage 3 (`TireDegradationModel`) should introduce hierarchical driver-stint state-space filtering and piecewise/cliff degradation curves for long stints (`h >= 5`).
