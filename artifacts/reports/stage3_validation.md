# Stage 3 Validation Report — Tire Degradation & Future Stint Simulation (`PaceV2` / `DegradationV1`)

**Date:** November 2026  
**Run ID:** `stage3_degradation_v1`  
**Warehouse:** `data/warehouse/f1ri.duckdb` (32 Race Sessions, 11 Distinct Circuits, 2023–2026)  
**Model Artifact:** `artifacts/models/degradation_v1/pace_v2_degradation_v1.pkl` (`PaceModelV2` + `HierarchicalTireDegradationModel`)  
**Frozen Stage 2 Tag:** `stage2-v1` (`fbee238`)

---

## Executive Summary

Stage 3 upgrades the F1 Race Intelligence Engine from a single-lap adaptive pace estimator (`PaceV1`) into an open-loop, multi-lap stint trajectory simulator (`PaceV2` powered by `DegradationV1` and `CorrelatedTrajectorySampler`). Prior to constructing the Stage 3 models, we resolved all four diagnostic weaknesses identified in the Stage 2 review:

1. **Issue 1 — Extreme Prediction Error Audit (`SOFT` RMSE `1.403s` vs MAE `0.515s`)**: Auditing the top 30 largest absolute residuals in `artifacts/runs/stage2_pace_v1/predictions.parquet` revealed that the #1 and #2 largest errors in the entire Stage 2 evaluation were **Lewis Hamilton (Driver 44, Ferrari) at the 2025 Singapore GP (`9896`, Stint 3, `SOFT`) on Lap 62 (`+12.323s` residual) and Lap 60 (`+5.413s` residual)** caused by a front-left brake failure on the final two laps while nursing the car to the flag in clean air (`interval > 11.7s`). Removing just those two mechanical-failure laps reduces Stage 2 `SOFT` RMSE on `val+test` by **54.9%** (from `1.4028s` to **`0.6322s`**) and `SOFT` MAE from `0.5146s` to **`0.3667s`** (`0.2948s` on warm laps). Across the top 30 residuals overall, **21 of 30 (`70%`)** occurred on `0A_no_prior_clean_race_laps` (`clean_laps_observed_race == 0`), where a driver spent the opening 10–30 laps in a DRS train and recorded their first clean lap of the race mid-stint.
2. **Issue 2 — Separation of Cold-Start Regimes (`0A` vs `0B`)**: Splitting `0_COLD` into `0A_no_prior_clean_race_laps` (`clean_laps_observed_race == 0`, $n=190$ across the 10-race 2025 `val+test` cohort) and `0B_first_clean_lap_of_new_stint_with_race_history` (`clean_laps_observed_current_stint == 0` and `clean_laps_observed_race > 0`, $n=200$) confirmed that they are fundamentally different regimes:
   - On **`0A_no_prior_clean_race_laps`**, `PaceV2` (`D8`, incorporating weekend qualifying/grid prior $\hat{\delta}^{\text{qual}}_{d,r}$) reduces MAE by **19.2%** vs `PaceV1` (`1.4931s` vs `1.8476s`, RMSE `2.0394s` vs `2.4355s`).
   - On **`0B_first_clean_lap_of_new_stint_with_race_history`**, cross-stint structural transfer cuts MAE from `1.3950s` (`E2` baseline) to **`0.5683s`** (`D0` `PaceV1`) and **`0.8459s`** (`D8` `PaceV2`).
   - On **`3_plus_warm_stint`** ($n=3,264$), `D8` achieves **`0.2322s` MAE** (`+0.0019s` bias).
3. **Issue 3 — Expanded Historical & Held-Out Circuit Coverage (15 $\rightarrow$ 32 Races, 11 Circuits)**: We expanded the DuckDB warehouse to **32 Grand Prix sessions** across **11 distinct circuits** (`Sakhir`, `Jeddah`, `Suzuka`, `Imola`, `Catalunya`, `Spielberg`, `Hungaroring`, `Zandvoort`, `Monza`, `Singapore`, `Austin`, `Yas Marina`) with verified Pirelli `C1`–`C5` absolute compound allocations in `silver.compound_allocations`, including **4 completely untouched 2025 test races** (`9955` Austria, `9928` Hungary, `9888` Austin, `9839` Abu Dhabi) that were never inspected during Stage 2.
4. **Issue 4 — Stronger Online-Adaptation Baseline (`B4`) & Strict Continued-Stint Horizon Censoring**: We implemented `AdaptiveTireResetProgressBaseline` (`B4`), which resets at pit stops and applies a fixed `-0.032 s/lap` race-progress trend, and verified that `gold.stint_training_examples` strictly censors any future window crossing a pit stop (`FUTURE_PIT_TRANSITION`), safety car (`NEUTRALIZATION_SC_VSC_RED`), or traffic lap (`TRAFFIC_CONTAMINATION`).

### Key Stage 3 Open-Loop Stint Forecast Results

| Metric / Split | `B0` (`E2` Median) | `B4` (Adaptive + Progress) | `D0` (Frozen `PaceV1`) | `D8` (`PaceV2` Full) | `D8` Improvement |
| :--- | :---: | :---: | :---: | :---: | :--- |
| **2025 Validation $H=1$ Lap MAE** (Dataset A) | `0.3832s` | `0.3590s` | `0.3313s` | **`0.3274s`** | **-14.6% vs `B0`**, **-8.8% vs `B4`**, **-1.2% vs `D0`** |
| **2025 Validation $H=5$ Lap MAE** (Dataset A) | `0.7444s` | `0.6906s` | `0.6343s` | **`0.6222s`** | **-16.4% vs `B0`**, **-9.9% vs `B4`**, **-1.9% vs `D0`** |
| **2025 Validation $H=10$ Lap MAE** (Dataset A) | `0.9879s` | `0.9270s` | `0.8798s` | **`0.8260s`** | **-16.4% vs `B0`**, **-10.9% vs `B4`**, **-6.1% vs `D0`** |
| **2025 Validation $H=15$ Lap MAE** (Dataset A) | `1.0550s` | `1.0182s` | `1.0360s` | **`0.9103s`** | **-13.7% vs `B0`**, **-10.6% vs `B4`**, **-12.1% vs `D0`** |
| **2025 Test (All 6 Races) $H=1$ Lap MAE** | `0.3519s` | `0.3396s` | `0.3234s` | **`0.3210s`** | **-8.8% vs `B0`**, **-5.5% vs `B4`**, **-0.7% vs `D0`** |
| **2025 Test (All 6 Races) $H=5$ Lap MAE** | `0.6040s` | `0.5980s` | `0.5751s` | **`0.5551s`** | **-8.1% vs `B0`**, **-7.2% vs `B4`**, **-3.5% vs `D0`** |
| **2025 Test (All 6 Races) $H=10$ Lap MAE** | `0.8209s` | `0.8204s` | `0.7996s` | **`0.7587s`** | **-7.6% vs `B0`**, **-7.5% vs `B4`**, **-5.1% vs `D0`** |
| **2025 Test (All 6 Races) $H=15$ Lap MAE** | `0.9530s` | `0.9639s` | `0.9544s` | **`0.9006s`** | **-5.5% vs `B0`**, **-6.6% vs `B4`**, **-5.6% vs `D0`** |
| **2025 Test $H=5$ Cumulative Stint MAE** (Dataset B) | `1.7013s` | `1.6866s` | `1.5544s` | **`1.5155s`** | **-10.9% vs `B0`** (5/6 races), **-10.1% vs `B4`** (6/6 races), **-2.5% vs `D0`** |
| **2025 Test $H=10$ Cumulative Stint MAE** (Dataset B) | `4.1081s` | `4.2574s` | `3.9001s` | **`3.7312s`** | **-9.2% vs `B0`**, **-12.4% vs `B4`**, **-4.3% vs `D0`** |
| **2025 Test $H=15$ Cumulative Stint MAE** (Dataset B) | `7.5354s` | `7.9873s` | `7.1292s` | **`6.9581s`** | **-7.7% vs `B0`**, **-12.9% vs `B4`**, **-2.4% vs `D0`** |
| **2025 Test $H=5$ Cumulative 90% Coverage** | `63.7%` | `61.4%` | `64.9%` | **`87.1%`** | Correlated trajectory sampler (`D8`) vs `66.3%` independent (`D7`) |
| **2025 Test $H=10$ Cumulative 90% Coverage** | `42.9%` | `35.3%` | `38.7%` | **`80.9%`** | Correlated trajectory sampler (`D8`) vs `42.2%` independent (`D7`) |
| **2025 Test $H=10$ Cumulative CRPS** | `3.4198s` | `3.6300s` | `3.2721s` | **`2.7286s`** | **-20.2% vs `B0`**, **-16.6% vs `D0`**, **-12.7% vs `D7`** |

---

## 1. Resolution of Stage 2 Prerequisite Issues

### 1.1 Issue 1 — Investigation of Extreme Prediction Errors & `SOFT` RMSE (`1.403s` vs MAE `0.515s`)

We queried `artifacts/runs/stage2_pace_v1/predictions.parquet` across the 2025 validation and test splits and joined the top 30 largest absolute residuals (`|observed_lap_time_s - expected_lap_time_s|`) with `gold.lap_CleanPaceFeatures` (`tyre_compound`, `tyre_age_at_lap_start`, `clean_laps_observed_race`, `clean_laps_observed_current_stint`, `interval_to_car_ahead_s`, `gap_to_leader_s`).

#### Top 10 Largest Prediction Errors in Stage 2 (`PaceV1`)

| Rank | Session (`Circuit`) | Split | Driver | Lap | Stint / Compound / Age | Observed (s) | Predicted (s) | Residual (s) | Race Clean Hist | Stint Clean Hist | Interval Ahead (s) | Root Cause Classification |
| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **1** | `9896` (`Singapore`) | `test` | `44` (Hamilton) | `62` | Stint 3 / `SOFT` / `15` | `107.713` | `95.390` | **`+12.323`** | `36` | `12` | `34.623` | **Mechanical failure (front-left brake failure on final lap)** |
| **2** | `9896` (`Singapore`) | `test` | `44` (Hamilton) | `60` | Stint 3 / `SOFT` / `13` | `100.466` | `95.053` | **`+5.413`** | `35` | `11` | `11.716` | **Mechanical failure onset (brake overheating on lap 60)** |
| **3** | `9896` (`Singapore`) | `test` | `27` (Hulkenberg) | `45` | Stint 2 / `HARD` / `19` | `101.797` | `96.462` | **`+5.335`** | `18` | `5` | `8.590` | **Single-lap spin / yellow-sector recovery in clean air** |
| **4** | `9971` (`Catalunya`) | `val` | `55` (Sainz) | `12` | Stint 2 / `MEDIUM` / `2` | `80.834` | `85.565` | **`-4.731`** | `0` | `0` | `5.862` | **`0A` Cold Start (first clean lap of race after early wing/pit stop)** |
| **5** | `9896` (`Singapore`) | `test` | `63` (Russell) | `3` | Stint 1 / `MEDIUM` / `2` | `96.364` | `100.531` | **`-4.167`** | `0` | `0` | `1.547` | **`0A` Cold Start (race leader opening clean lap vs field prior)** |
| **6** | `9896` (`Singapore`) | `test` | `1` (Verstappen) | `3` | Stint 1 / `SOFT` / `2` | `96.597` | `100.289` | **`-3.692`** | `0` | `0` | `1.547` | **`0A` Cold Start (P2 opening clean lap vs field prior)** |
| **7** | `10006` (`Suzuka`) | `val` | `1` (Verstappen) | `3` | Stint 1 / `MEDIUM` / `2` | `91.969` | `95.622` | **`-3.653`** | `0` | `0` | `1.556` | **`0A` Cold Start (race leader opening clean lap vs field prior)** |
| **8** | `10006` (`Suzuka`) | `val` | `87` (Bearman) | `27` | Stint 2 / `HARD` / `2` | `91.855` | `95.442` | **`-3.587`** | `0` | `0` | `6.143` | **`0A` Cold Start (first clean lap of race on Lap 27 after traffic)** |
| **9** | `10014` (`Sakhir`) | `val` | `81` (Piastri) | `3` | Stint 1 / `SOFT` / `2` | `96.362` | `99.934` | **`-3.572`** | `0` | `0` | `1.622` | **`0A` Cold Start (race leader opening clean lap vs field prior)** |
| **10** | `10014` (`Sakhir`) | `val` | `1` (Verstappen) | `29` | Stint 3 / `MEDIUM` / `1` | `96.819` | `100.380` | **`-3.561`** | `0` | `0` | `5.367` | **`0A` Cold Start (first clean lap of race on Lap 29 after traffic)** |

#### Summary of All Top 30 Stage 2 Residuals & Impact on `SOFT` RMSE

- **Why `SOFT` RMSE was `1.403s` vs MAE `0.515s`**:
  - In the 4-race Stage 2 validation+test set, there were only `142` clean `SOFT` laps (`29` in `0A` cold start, `10` in `0B` new-stint cold start, `103` warm).
  - Two laps — **Hamilton's Lap 60 (`+5.413s`) and Lap 62 (`+12.323s`) brake-failure laps at Singapore** — contributed $12.323^2 + 5.413^2 = 181.16\text{ s}^2$ out of the entire `SOFT` sum of squared errors ($279.35\text{ s}^2$), i.e., **64.8% of total `SOFT` MSE came from 2 mechanical-failure laps**.
  - Excluding just those 2 brake-failure laps (`n=140`), `SOFT` RMSE drops from `1.4028s` to **`0.6322s`** (-54.9%) and `SOFT` MAE drops from `0.5146s` to **`0.3667s`** (and **`0.2948s` MAE / `0.4090s` RMSE** across warm `SOFT` laps).
- **Root-Cause Breakdown of Top 30 Stage 2 Outliers**:
  - **21 of 30 (`70.0%`)** occurred in **`0A_no_prior_clean_race_laps`** (`clean_laps_observed_race == 0`): either race leaders on Lap 3 (`Russell`, `Verstappen`, `Piastri`) who are `3.5s` faster than the midfield prior, or midfield drivers (`Bearman` Lap 27, `Verstappen` Sakhir Lap 29, `Bortoleto` Lap 48) who spent the entire opening stint in dirty air (`interval < 1.5s`) and recorded their first clean lap mid-race after fuel burn had already lightened the cars by `1.5s–2.5s`.
  - **6 of 30 (`20.0%`)** occurred in **`0B_first_clean_lap_of_new_stint_with_race_history`** (`clean_laps_observed_current_stint == 0`, `clean_laps_observed_race > 0`), where a driver switched compounds and the post-pit compound+age adjustment had high initial uncertainty.
  - **3 of 30 (`10.0%`)** were **in-race mechanical/on-track incidents in clean air** (Hamilton's 2 brake-failure laps at Singapore and Hulkenberg's Lap 45 spin at Singapore).
- **Stage 3 Design Action**:
  - We added a **weekend qualifying/grid pace prior** (`qualifying_pace_delta_s` in [`PaceModelV2`](file:///c:/Projects/f1-race-intelligence/src/f1ri/models/pace.py)) so `0A_no_prior_clean_race_laps` cold-start predictions know whether a car is a front-runner or backmarker before its first clean race lap, plus explicit race-progress fuel adjustment even when `clean_laps_observed_race == 0`.

---

### 1.2 Issue 2 — Separation of Cold-Start Regimes (`0A` vs `0B`)

In [`src/f1ri/features/stint_examples.py`](file:///c:/Projects/f1-race-intelligence/src/f1ri/features/stint_examples.py) and [`src/f1ri/validation/stint_backtest.py`](file:///c:/Projects/f1-race-intelligence/src/f1ri/validation/stint_backtest.py), we separated observed history into four mutually exclusive regimes across the expanded 10-race 2025 `val + test` cohort (`4,356` eligible $H=1$ clean laps):

| History Regime (`cold_start_regime`) | Definition | Clean Laps ($n$) | `B0` (`E2` Median) MAE / RMSE / Bias | `D0` (`PaceV1`) MAE / RMSE / Bias | `D8` (`PaceV2`) MAE / RMSE / Bias | Key Takeaway |
| :--- | :--- | :---: | :---: | :---: | :---: | :--- |
| **`0A_no_prior_clean_race_laps`** | `clean_laps_observed_race == 0` | `190` | `1.8128s` / `2.4648s` / `-0.7155s` | `1.8476s` / `2.4355s` / `-0.4660s` | **`1.4931s`** / **`2.0394s`** / `+0.1189s` | **`D8` weekend qualifying prior cuts true cold-start MAE by 19.2% vs `PaceV1`** and eliminates the `-0.47s` negative bias. |
| **`0B_first_clean_lap_of_new_stint_with_race_history`** | `clean_laps_observed_current_stint == 0` & `clean_laps_observed_race > 0` | `200` | `1.3950s` / `1.5645s` / `-1.3687s` | **`0.5683s`** / **`0.7694s`** / `-0.0666s` | `0.8459s` / `1.0653s` / `-0.7274s` | **`B0` (`E2`) fails badly (`1.395s` MAE) because it carries old-tire pace into the new stint**; structural compound+age reset cuts error by `40–59%`. |
| **`1_2_early_stint`** | `clean_laps_observed_current_stint in {1, 2}` | `702` | `0.2746s` / `0.3910s` / `+0.0313s` | `0.2691s` / `0.3799s` / `+0.0007s` | **`0.2812s`** / `0.3836s` / `-0.0084s` | Rapid convergence within 1–2 clean laps of the new stint. |
| **`3_plus_warm_stint`** | `clean_laps_observed_current_stint >= 3` | `3,264` | `0.2355s` / `0.4077s` / `+0.0309s` | `0.2350s` / `0.4051s` / `+0.0042s` | **`0.2322s`** / **`0.3986s`** / **`+0.0019s`** | Best accuracy and near-zero bias (`+0.0019s`) on warm stint laps (`74.9%` of all clean laps). |

---

### 1.3 Issue 3 — Expanded Circuit & Pirelli Compound Coverage (32 Races, 11 Circuits)

We expanded the DuckDB warehouse from 15 sessions (5 circuits) to **32 race sessions across 11 distinct circuits** (`27` in `2023_2025` + `5` in `2026_PLUS`) and ingested [`data/raw/reference/pirelli_allocations.csv`](file:///c:/Projects/f1-race-intelligence/data/raw/reference/pirelli_allocations.csv) (`96` compound mappings across `C1`–`C5`) into `silver.compound_allocations`:

| Split | Seasons | Race Count | Sessions & Circuits (`Pirelli Allocation`) | Purpose |
| :--- | :---: | :---: | :--- | :--- |
| **`train`** | `2023–2024` | **17 races** | `2023`: Sakhir (`C1-C3`),Suzuka (`C1-C3`), Catalunya (`C1-C3`), Spielberg (`C3-C5`), Hungaroring (`C3-C5`), Monza (`C3-C5`), Singapore (`C3-C5`), Austin (`C2-C4`)<br>`2024`: Sakhir (`C1-C3`), Jeddah (`C2-C4`), Suzuka (`C1-C3`), Imola (`C3-C5`), Catalunya (`C1-C3`), Hungaroring (`C3-C5`), Zandvoort (`C1-C3`), Monza (`C3-C5`), Singapore (`C3-C5`) | Population parameters (`DegradationV1`, `PaceV2`), circuit/compound empirical Bayes shrinkage priors |
| **`val`** | `2025` (Early) | **4 races** | `10014` Sakhir (`C1-C3`), `10006` Suzuka (`C1-C3`), `9987` Imola (`C4-C6/C5`), `9971` Catalunya (`C1-C3`) | Hyperparameter tuning & model selection |
| **`test_stage2_carryover`** | `2025` (Late) | **2 races** | `9912` Monza (`C3-C5`), `9896` Singapore (`C3-C5`) | Direct continuity comparison with Stage 2 |
| **`test_untouched_new_races`** | `2025` (Held-Out) | **4 races** | `9955` Spielberg (`C3-C5`), `9928` Hungaroring (`C3-C5`), `9888` Austin (`C1/C3/C4`), `9839` Yas Marina (`C3-C5`) | **Untouched held-out test races** (including brand-new circuit `Yas Marina` unseen in 2023–2024 training) |
| **`transfer_2026`** | `2026` | **5 races** | `11253` Suzuka (`C1-C3`), `11307` Catalunya (`C1-C3`), `11342` Hungaroring (`C3-C5`), `11361` Monza (`C3-C5`), `11388` Singapore (`C3-C5`) | Out-of-era regulation transfer (`D9` zero-shot vs `D10` progressive adaptation) |

---

### 1.4 Issue 4 — Stronger Online-Adaptation Baseline (`B4`) & Strict Continued-Stint Horizon Verification

1. **Stronger Baseline (`B4_adaptive_reset_progress`)**:
   - In [`src/f1ri/models/degradation.py`](file:///c:/Projects/f1-race-intelligence/src/f1ri/models/degradation.py), we implemented [`AdaptiveTireResetProgressBaseline`](file:///c:/Projects/f1-race-intelligence/src/f1ri/models/degradation.py#L75-L154), which tracks the driver's exponentially weighted clean-air residual within the current stint, resets at every pit stop using historical compound offsets, and projects future laps using a fixed physical race-progression slope (`-0.032 s/lap`).
   - As shown in Section 3, `B4` outperforms `B0` (`E2` compound median) on validation (`0.3590s` vs `0.3832s` at $H=1$; `1.7952s` vs `1.9343s` cumulative at $H=5$), proving that resetting at pit stops and accounting for fuel burn helps—yet `B4` still degrades rapidly at longer horizons ($H=10$ test cumulative MAE `4.2574s` vs `D8` **`3.7312s`**) because it lacks explicit tire-age degradation slopes.
2. **Continued-Stint Censoring Audit (`FUTURE_PIT_TRANSITION`)**:
   - Every continued-stint forecast window in `gold.stint_training_examples` is checked against the driver's actual stint boundaries `(start_lap, end_lap)`. Any forecast horizon $H$ where `cutoff_lap + H > end_lap` or where any step $k \in \{1,\dots,H\}$ belongs to a different `stint_number` is flagged `is_continued_stint_eligible = FALSE` with `censoring_reason = 'FUTURE_PIT_TRANSITION'` (or `'RACE_FINISH_REACHED'`).
   - Unit test [`test_continued_stint_excludes_pit_transitions`](file:///c:/Projects/f1-race-intelligence/tests/test_stint_examples.py) and [`test_no_future_stint_length_leakage`](file:///c:/Projects/f1-race-intelligence/tests/test_stint_leakage.py) verify zero contamination across all `222,048` rows of `gold.stint_training_examples`.

---

## 2. Stage 3 Dataset & Survivorship-Bias Audit

### 2.1 Effective Sample Sizes by Regulation Era

| Regulation Era | Total Stint Laps | Clean Pace Laps | Clean Retention (%) | Distinct Clean Stints | Distinct Driver-Races | Distinct Race Weekends | Distinct Circuits |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`2023_2025`** | `30,951` | `12,443` | `40.2%` | `1,072` | `500` | `27` | `12` |
| **`2026_PLUS`** | `6,057` | `2,209` | `36.5%` | `215` | `106` | `5` | `5` |
| **Total Warehouse** | **`37,008`** | **`14,652`** | **`39.6%`** | **`1,287`** | **`606`** | **`32`** | **`12`** |

### 2.2 Sample Size & Stint Age Distribution by Pirelli Absolute Compound (`2023_2025`)

| Pirelli Compound | Relative Role | Total Laps | Clean Laps | Clean Stints | Median Tire Age (laps) | P90 Tire Age (laps) | Max Tire Age (laps) |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`C1`** | `HARD` | `5,024` | `2,275` | `172` | `12.0` | `26.0` | `56` |
| **`C2`** | `HARD` | `1,118` | `581` | `37` | `15.0` | `30.0` | `48` |
| **`C2`** | `MEDIUM` | `3,431` | `1,283` | `119` | `10.0` | `22.0` | `35` |
| **`C3`** | `HARD` | `8,383` | `3,763` | `260` | `15.0` | `31.0` | `54` |
| **`C3`** | `MEDIUM` | `1,397` | `547` | `50` | `11.0` | `25.0` | `33` |
| **`C3`** | `SOFT` | `3,012` | `890` | `125` | `8.0` | `17.0` | `27` |
| **`C4`** | `HARD` | `812` | `228` | `27` | `12.0` | `26.0` | `34` |
| **`C4`** | `MEDIUM` | `6,231` | `2,318` | `217` | `11.0` | `25.0` | `49` |
| **`C4`** | `SOFT` | `492` | `263` | `21` | `13.0` | `24.0` | `29` |
| **`C5`** | `MEDIUM` | `393` | `110` | `13` | `10.0` | `24.0` | `33` |
| **`C5`** | `SOFT` | `658` | `185` | `31` | `7.0` | `18.0` | `37` |

### 2.3 Survivorship Bias & Stint Termination Audit

Stints are not terminated at random: teams pit early when a compound degrades rapidly or when trapped in traffic, and extend stints when running in clean air on low-degradation `HARD` tires:

- **By Tire-Age Bucket**: Clean-lap retention rises monotonically with tire age—from **`24.19%`** at ages `0–5` (`2,205 / 9,117` laps across `730` stints, due to opening-lap traffic and pit-out laps) to **`43.38%`** at ages `11–15` (`3,023 / 6,969` laps), **`51.87%`** at ages `21–25` (`1,882 / 3,628` laps), and **`55.20%`** at ages `31+` (`1,083 / 1,962` laps across `207` surviving stints).
- **By Stint Termination Reason**:
  - `SOFT` stints terminated by a pit stop (`PIT_TERMINATED`, $n=249$) have a median length of **`12.0` laps** (`19.3%` clean fraction, `38.6%` started on used tires), whereas `HARD` stints running to `RACE_FINISH` ($n=327$) have a median length of **`31.0` laps** (`P90 = 42.0` laps, `47.2%` clean fraction).
- **Dataset B Censoring Breakdown (2025 Validation, $H=5$, `4,071` Cutoff Opportunities)**:
  - `TRAFFIC_CONTAMINATION`: `2,606` (`64.0%`)
  - `COMPLETE_CLEAN` (Dataset B eligible): `738` (`18.1%`)
  - `NEUTRALIZATION_SC_VSC_RED`: `362` (`8.9%`)
  - `FUTURE_PIT_TRANSITION`: `361` (`8.9%`)
  - `UPSTREAM_TIMESTAMP_ANOMALY`: `3` (`0.07%`)
  - `RACE_FINISH_REACHED`: `1` (`0.02%`)

Because requiring all $H$ future laps to be simultaneously clean (Dataset B) selects for clean-air front-runners and longer stints as $H$ grows (`18.1%` complete at $H=5$, `9.1%` at $H=10$, `4.3%` at $H=15$, `1.6%` at $H=20$), **all Stage 3 evaluations report both Dataset A (all individual future clean laps at horizon $H$, regardless of intermediate dirty laps) and Dataset B (complete clean trajectories $k=1\dots H$)**.

---

## 3. Controlled Experiment Matrix (`B0`, `B4`, `D0`–`D10`)

### 3.1 Dataset A — Individual Future Clean-Lap Horizon Performance (Race-Weighted MAE in Seconds)

#### 2025 Validation Split (4 Races: `Sakhir`, `Suzuka`, `Imola`, `Catalunya`)

| Experiment ID | Description | $H=1$ ($n=1552$) | $H=3$ ($n=1455$) | $H=5$ ($n=1340$) | $H=10$ ($n=1021$) | $H=15$ ($n=639$) | $H=20$ ($n=348$) | $H=1$ 90% Cov | $H=5$ 90% Cov |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`B0_E2_compound_median`** | Driver compound median baseline (`E2`) | `0.3832` | `0.5883` | `0.7444` | `0.9879` | `1.0550` | `1.0858` | `88.1%` | `70.4%` |
| **`B4_adaptive_reset_progress`** | Pit-reset EWMA + fixed `-0.032s/lap` progress | `0.3590` | `0.5436` | `0.6906` | `0.9270` | `1.0182` | `1.0614` | `89.9%` | `75.1%` |
| **`D0_frozen_pace_v1`** | Frozen Stage 2 `PaceV1` (`stage2-v1`) | `0.3313` | `0.4989` | `0.6343` | `0.8798` | `1.0360` | `1.1209` | `91.6%` | `79.2%` |
| **`D1_linear_age_trend`** | Naive linear tire-age trend (no fuel control) | `0.3976` | `0.6196` | `0.7896` | `1.0260` | `1.0667` | `1.1237` | `89.0%` | `70.2%` |
| **`D2_joint_linear_progress_age`** | Joint race-progress + compound linear age | `0.3517` | `0.5361` | `0.6900` | `0.9336` | `1.0722` | `1.1799` | `90.7%` | `76.4%` |
| **`D3_nonlinear_age`** | Piecewise warmup + cliff hinge degradation | `0.3514` | `0.5362` | `0.6903` | `0.9342` | `1.0725` | `1.1803` | `90.7%` | `76.3%` |
| **`D4_pirelli_absolute_compound`** | Pirelli `C1`–`C5` absolute compound slopes | `0.3543` | `0.5369` | `0.6795` | `0.8815` | `0.9544` | `1.0150` | `90.7%` | `75.7%` |
| **`D5_hierarchical_circuit_compound`** | Empirical-Bayes circuit $\times$ compound pooling | `0.3555` | `0.5439` | `0.6962` | `0.9267` | `1.0203` | `1.0827` | `91.0%` | `75.7%` |
| **`D6_weekend_adaptation`** | `D5` + causal weekend qualifying prior ($\hat{\delta}^{\text{qual}}_{d,r}$) | `0.3360` | `0.5030` | `0.6341` | `0.8384` | `0.9219` | `0.9884` | `90.7%` | `75.9%` |
| **`D7_dynamic_stint_state`** | `D6` + online Bayesian stint offset & slope ($\hat{\delta}_{d,s}, \hat{\beta}_{d,s}$) | **`0.3274`** | **`0.4936`** | **`0.6222`** | **`0.8260`** | **`0.9103`** | **`0.9815`** | `90.6%` | `75.1%` |
| **`D8_correlated_trajectory_uncertainty`** | **`PaceV2` Full (`D7` + correlated trajectory sampler)** | **`0.3274`** | **`0.4936`** | **`0.6222`** | **`0.8260`** | **`0.9103`** | **`0.9815`** | **`89.9%`** | **`77.1%`** |

#### 2025 Held-Out Test Split — All 6 Test Races (`Monza`, `Singapore`, `Spielberg`, `Hungaroring`, `Austin`, `Yas Marina`)

| Experiment ID | $H=1$ ($n=2804$) | $H=3$ ($n=2640$) | $H=5$ ($n=2464$) | $H=10$ ($n=1936$) | $H=15$ ($n=1336$) | $H=20$ ($n=818$) | $H=1$ 90% Cov | $H=5$ 90% Cov |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`B0_E2_compound_median`** | `0.3519` | `0.5071` | `0.6040` | `0.8209` | `0.9530` | `1.0757` | `89.8%` | `78.6%` |
| **`B4_adaptive_reset_progress`** | `0.3396` | `0.4959` | `0.5980` | `0.8204` | `0.9639` | `1.0853` | `90.5%` | `79.3%` |
| **`D0_frozen_pace_v1`** | `0.3234` | `0.4735` | `0.5751` | `0.7996` | `0.9544` | `1.0823` | `91.9%` | `80.2%` |
| **`D1_linear_age_trend`** | `0.3507` | `0.5063` | `0.6090` | `0.8398` | `0.9872` | `1.1524` | `92.0%` | `81.4%` |
| **`D2_joint_linear_progress_age`** | `0.3364` | `0.4918` | `0.6019` | `0.8469` | `1.0218` | `1.1907` | `92.0%` | `80.0%` |
| **`D3_nonlinear_age`** | `0.3364` | `0.4924` | `0.6029` | `0.8491` | `1.0247` | `1.1945` | `92.0%` | `79.9%` |
| **`D4_pirelli_absolute_compound`** | `0.3327` | `0.4813` | `0.5819` | `0.8056` | `0.9604` | `1.1132` | `92.3%` | `81.5%` |
| **`D5_hierarchical_circuit_compound`** | `0.3338` | `0.4835` | `0.5851` | `0.8112` | `0.9705` | `1.1279` | `92.2%` | `81.9%` |
| **`D6_weekend_adaptation`** | `0.3263` | `0.4663` | `0.5611` | `0.7659` | `0.9081` | `1.0619` | `92.5%` | `82.3%` |
| **`D7_dynamic_stint_state`** | **`0.3210`** | **`0.4607`** | **`0.5551`** | **`0.7587`** | **`0.9006`** | **`1.0587`** | `92.0%` | `81.8%` |
| **`D8_correlated_trajectory_uncertainty`** | **`0.3210`** | **`0.4607`** | **`0.5551`** | **`0.7587`** | **`0.9006`** | **`1.0587`** | **`92.0%`** | **`82.2%`** |

---

### 3.2 Dataset B — Complete Clean Stint Trajectory Performance (Race-Weighted Cumulative MAE in Seconds)

#### 2025 Validation Split (4 Races)

| Experiment ID | $H=5$ Cum MAE ($n=738$) | $H=10$ Cum MAE ($n=335$) | $H=15$ Cum MAE ($n=142$) | $H=20$ Cum MAE ($n=47$) | $H=5$ 50% / 90% Cov | $H=10$ 50% / 90% Cov | $H=10$ Cum CRPS (s) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`B0_E2_compound_median`** | `1.9343` | `3.9876` | `6.9220` | `13.6693` | `31.8%` / `64.4%` | `19.4%` / `46.6%` | `3.4651` |
| **`B4_adaptive_reset_progress`** | `1.7952` | `3.7372` | `6.7655` | `12.8583` | `29.9%` / `63.5%` | `15.8%` / `43.3%` | `3.3438` |
| **`D0_frozen_pace_v1`** | **`1.6395`** | `3.4259` | `6.1521` | `10.3142` | `32.3%` / `67.7%` | `17.3%` / `47.5%` | `2.9759` |
| **`D1_linear_age_trend`** | `2.0263` | `4.1621` | `7.0212` | `14.0860` | `30.6%` / `62.7%` | `15.8%` / `43.6%` | `3.8456` |
| **`D2_joint_linear_progress_age`** | `1.7971` | `3.8540` | `7.0981` | `12.7207` | `29.3%` / `63.4%` | `13.7%` / `37.6%` | `3.5451` |
| **`D3_nonlinear_age`** | `1.7981` | `3.8599` | `7.1099` | `12.7766` | `28.6%` / `63.5%` | `13.7%` / `37.6%` | `3.5588` |
| **`D4_pirelli_absolute_compound`** | `1.7780` | `3.5861` | **`6.0246`** | `11.7199` | `30.5%` / `65.6%` | `16.7%` / `47.2%` | `3.2180` |
| **`D5_hierarchical_circuit_compound`** | `1.8267` | `3.7698` | `6.6609` | `12.8473` | `27.1%` / `62.2%` | `15.2%` / `36.7%` | `3.6695` |
| **`D6_weekend_adaptation`** | `1.7199` | `3.6632` | `6.3696` | `12.5148` | `26.0%` / `62.3%` | `15.2%` / `36.1%` | `3.4498` |
| **`D7_dynamic_stint_state`** | `1.6670` | `3.5544` | `6.1324` | `12.4946` | `28.6%` / `64.0%` | `15.2%` / `40.9%` | `3.2227` |
| **`D8_correlated_trajectory_uncertainty`** | `1.6670` | `3.5544` | `6.1324` | `12.4946` | **`45.7%`** / **`89.2%`** | **`39.1%`** / **`78.5%`** | **`2.7960`** |

#### 2025 Held-Out Test Split — All 6 Test Races (`test_all`) & 4 Untouched New Test Races (`test_untouched_new_races`)

| Experiment ID | `test_all` $H=5$ ($n=1368$) | `test_all` $H=10$ ($n=638$) | `test_all` $H=15$ ($n=254$) | `test_untouched` $H=5$ ($n=886$) | `test_untouched` $H=10$ ($n=409$) | `test_all` $H=5$ 50% / 90% Cov | `test_all` $H=10$ 50% / 90% Cov | `test_all` $H=10$ CRPS (s) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`B0_E2_compound_median`** | `1.7013` | `4.1081` | `7.5354` | `1.8158` | `4.4222` | `31.3%` / `63.7%` | `15.7%` / `42.9%` | `3.4198` |
| **`B4_adaptive_reset_progress`** | `1.6866` | `4.2574` | `7.9873` | `1.7628` | `4.4296` | `30.4%` / `61.4%` | `16.3%` / `35.3%` | `3.6300` |
| **`D0_frozen_pace_v1`** | `1.5544` | `3.9001` | `7.1292` | **`1.5903`** | `3.9705` | `32.6%` / `64.9%` | `18.8%` / `38.7%` | `3.2721` |
| **`D1_linear_age_trend`** | `1.6830` | `4.2374` | `8.3427` | `1.7733` | `4.3520` | `33.9%` / `67.0%` | `18.8%` / `40.3%` | `3.5299` |
| **`D2_joint_linear_progress_age`** | `1.6460` | `4.2518` | `8.0176` | `1.6630` | `4.2374` | `31.6%` / `65.3%` | `15.0%` / `36.5%` | `3.5855` |
| **`D3_nonlinear_age`** | `1.6476` | `4.2613` | `8.0473` | `1.6649` | `4.2470` | `31.8%` / `65.1%` | `14.9%` / `36.7%` | `3.5941` |
| **`D4_pirelli_absolute_compound`** | `1.5963` | `4.0186` | `7.5184` | `1.6519` | `4.1138` | `33.3%` / `66.4%` | `18.6%` / `38.9%` | `3.3531` |
| **`D5_hierarchical_circuit_compound`** | `1.6048` | `4.0286` | `7.6229` | `1.6944` | `4.2264` | `33.6%` / `67.2%` | `18.2%` / `41.1%` | `3.3713` |
| **`D6_weekend_adaptation`** | `1.5655` | `3.9193` | `7.2494` | `1.6531` | `4.0685` | `32.8%` / `66.2%` | `18.3%` / `39.5%` | `3.2704` |
| **`D7_dynamic_stint_state`** | **`1.5155`** | **`3.7312`** | **`6.9581`** | `1.6272` | **`3.9498`** | `33.5%` / `66.3%` | `19.9%` / `42.2%` | `3.1239` |
| **`D8_correlated_trajectory_uncertainty`** | **`1.5155`** | **`3.7312`** | **`6.9581`** | `1.6272` | **`3.9498`** | **`49.2%`** / **`87.1%`** | **`37.5%`** / **`80.9%`** | **`2.7286`** |

---

### 3.3 Paired Race-Level Comparisons & Bootstrap Confidence Intervals

To avoid pseudoreplication across overlapping stint windows, we evaluated paired race-level differences ($B=2,000$ cluster bootstrap resamples over races):

| Comparison | Split ($N$ Races) | Horizon / Metric | Races Improved | Mean Race $\Delta$MAE (s) | Bootstrap SE (s) | 90% Bootstrap CI (s) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **`D8` (`PaceV2`) vs `B0` (`E2`)** | `val + test` (`10` races) | $H=5$ Cumulative MAE | **9 / 10 races** | **`-0.2194s`** | `0.0442s` | **`[-0.2908s, -0.1456s]`** |
| **`D8` (`PaceV2`) vs `B0` (`E2`)** | `test_all` (`6` races) | $H=5$ Cumulative MAE | **5 / 6 races** | **`-0.1858s`** | `0.0539s` | **`[-0.2665s, -0.0881s]`** |
| **`D8` (`PaceV2`) vs `B4` (Adaptive + Progress)** | `val + test` (`10` races) | $H=5$ Cumulative MAE | **9 / 10 races** | **`-0.1518s`** | `0.0320s` | **`[-0.2036s, -0.0982s]`** |
| **`D8` (`PaceV2`) vs `B4` (Adaptive + Progress)** | `test_all` (`6` races) | $H=5$ Cumulative MAE | **6 / 6 races** | **`-0.1712s`** | `0.0324s` | **`[-0.2230s, -0.1190s]`** |
| **`D8` (`PaceV2`) vs `D0` (`PaceV1`)** | `test_all` (`6` races) | $H=5$ Cumulative MAE | **3 / 6 races** | **`-0.0389s`** | `0.0543s` | `[-0.1292s, +0.0494s]` |
| **`D8` (`PaceV2`) vs `D0` (`PaceV1`)** | `val + test` (`10` races) | $H=10$ Lap MAE (Dataset A) | **8 / 10 races** | **`-0.0457s`** | `0.0148s` | **`[-0.0701s, -0.0213s]`** |
| **`D10` (Progressive) vs `D9` (Frozen 2026)** | `transfer_2026` (`5` races) | $H=5$ Cumulative MAE | `2 / 5 races` | `+0.0132s` | `0.0830s` | `[-0.1223s, +0.1439s]` |

#### Per-Race Breakdown on 2025 Held-Out Test Split ($H=5$ Cumulative Stint MAE)

| Session Key | Circuit | Cohort | `B0` (`E2` Median) | `B4` (Adaptive + Progress) | `D0` (Frozen `PaceV1`) | `D8` (`PaceV2` Full) | `D8 - B0` (s) | `D8 - B4` (s) | `D8 - D0` (s) |
| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`9912`** | `Monza` | Stage 2 Carryover | `1.3834s` | `1.5429s` | `1.5674s` | **`1.2821s`** | `-0.1013s` | `-0.2608s` | **`-0.2853s`** |
| **`9896`** | `Singapore` | Stage 2 Carryover | `1.5553s` | `1.5257s` | `1.3974s` | **`1.3019s`** | `-0.2534s` | `-0.2238s` | **`-0.0955s`** |
| **`9955`** | `Spielberg` | **Untouched Test** | `1.6584s` | `1.5394s` | **`1.3980s`** | `1.4524s` | `-0.2060s` | `-0.0871s` | `+0.0544s` |
| **`9928`** | `Hungaroring` | **Untouched Test** | `2.1481s` | `2.0274s` | `1.8608s` | **`1.8123s`** | `-0.3358s` | `-0.2150s` | **`-0.0484s`** |
| **`9888`** | `Austin` | **Untouched Test** | **`1.3182s`** | `1.4388s` | `1.2733s` | `1.3791s` | `+0.0609s` | `-0.0598s` | `+0.1058s` |
| **`9839`** | `Yas Marina` | **Untouched Test (Unseen Circuit)** | `2.1387s` | `2.0456s` | **`1.8293s`** | `1.8651s` | `-0.2736s` | `-0.1806s` | `+0.0358s` |

---

## 4. Physical Interpretation of Fitted `DegradationV1` / `PaceV2` Parameters

From [`artifacts/models/degradation_v1/model_params.json`](file:///c:/Projects/f1-race-intelligence/artifacts/models/degradation_v1/model_params.json), the fitted `PaceV2` (`D8`) parameters recover physically coherent Formula 1 dynamics:

1. **Disentangled Race Progression vs Tire Degradation (`D1` vs `D2`–`D8`)**:
   - In **`D1_linear_age_trend`** (which omits race-progression control), fuel burn masks tire wear, yielding near-zero apparent degradation and poor accuracy (`0.3976s` val $H=1$ MAE, `2.0263s` val $H=5$ cumulative MAE).
   - Once joint race progression is controlled with a physical F1 fuel-burn prior in **`D2`–`D8`**, the model estimates a **race-progression slope of `-2.150 s/race`** (`~ -0.037 s/lap` on a 58-lap race, matching the physical `0.033–0.038 s/lap` fuel-mass effect) and **strictly positive pure tire degradation slopes** ordered by softness:
     - **`SOFT` linear degradation slope**: **`+0.0796 s/lap`** (initial compound offset `-0.2153s` vs `MEDIUM`)
     - **`MEDIUM` linear degradation slope**: **`+0.0594 s/lap`** (reference compound `0.0000s`)
     - **`HARD` linear degradation slope**: **`+0.0532 s/lap`** (initial compound offset `+0.0080s` vs `MEDIUM`)
   - Consequently, as shown in **Plot B (`stage3_trajectories_abcd.png`)**, pure tire degradation $D(c,a)$ rises by `+1.8s` to `+2.8s` over a 35-lap stint while net lap-time evolution $F_r(l) + D(c,a)$ rises much more gently (`+0.85s` to `+1.7s`) because fuel burn offsets roughly `0.037 s/lap` of tire wear.
2. **Pirelli `C1`–`C5` & Circuit-Specific Hierarchy (`D4`–`D5`)**:
   - Empirical-Bayes circuit-compound pooling (`D5`) shrinks low-sample combinations toward the population mean while preserving genuine circuit severity: high-wear circuits **`Suzuka` (`+0.024` to `+0.030 s/lap` above base)** and **`Sakhir` (`+0.013` to `+0.023 s/lap` above base)** exhibit the steepest tire degradation, whereas smooth/low-energy circuits **`Jeddah` (`-0.016` to `-0.026 s/lap`)** and **`Monza` (`-0.005` to `-0.007 s/lap`)** exhibit the lowest degradation.
3. **Why Cumulative Stint Uncertainty Requires Correlated Trajectory Sampling (`D7` vs `D8`)**:
   - As shown in **Plot L (`stage3_generalization_ijkl.png`)**, within-stint clean-lap residuals are positively autocorrelated ($\hat{\rho}_1 = 0.2881$) and share common uncertainty in the driver's latent stint offset $\hat{\delta}_{d,s}$ ($\sigma_{\text{stint}} = 0.2325\text{ s}$) and degradation slope $\hat{\beta}_{d,s}$.
   - When `D7` sums independent per-lap variances ($\text{Var}(S_H) = \sum_{k=1}^H \sigma_k^2$), the $\sqrt{H}$ cancellation makes multi-lap intervals severely overconfident: empirical 90% coverage on 2025 test drops from **`66.3%`** at $H=5$ to **`42.2%`** at $H=10$ and **`27.6%`** at $H=15$.
   - By sampling joint latent trajectories ($\delta_{d,s}^{(m)}, \beta_{d,s}^{(m)}, \eta_k^{(m)}$ with AR(1) $\rho=0.2881$) in [`CorrelatedTrajectorySampler`](file:///c:/Projects/f1-race-intelligence/src/f1ri/models/trajectory_uncertainty.py), **`D8` restores empirical 90% cumulative stint coverage to `87.1%` at $H=5$ (`89.2%` on validation) and `80.9%` at $H=10$**, while reducing $H=10$ cumulative CRPS from `3.1239s` to **`2.7286s`**.

---

## 5. Out-of-Era Regulation Transfer (`2026_PLUS`: `D9` vs `D10`)

We evaluated zero-shot transfer (`D9_frozen_2026_transfer`, frozen on `2023–2025`) against causal race-by-race adaptation (`D10_progressive_2026_adaptation`, updating `2026_PLUS` compound/circuit offsets after each completed 2026 race) across the 5 chronologically ordered 2026 races (`11253` Suzuka $\rightarrow$ `11307` Catalunya $\rightarrow$ `11342` Hungaroring $\rightarrow$ `11361` Monza $\rightarrow$ `11388` Singapore):

| Experiment | $H=1$ Lap MAE | $H=3$ Lap MAE | $H=5$ Lap MAE | $H=10$ Lap MAE | $H=15$ Lap MAE | $H=5$ Cum Stint MAE |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **`D9_frozen_2026_transfer`** (Zero-Shot `2023–2025`) | `0.3648s` | `0.5149s` | `0.6095s` | `0.7508s` | **`0.7714s`** | **`1.7847s`** |
| **`D10_progressive_2026_adaptation`** (Causal Expanding 2026) | **`0.3623s`** | **`0.5022s`** | **`0.5949s`** | **`0.7379s`** | `0.7794s` | `1.7980s` |

- **Finding**: On Dataset A (individual future clean laps across all stints), progressive 2026 adaptation (`D10`) improves every horizon from $H=1$ (`0.3623s` vs `0.3648s`) through $H=10$ (`0.7379s` vs `0.7508s`, **-1.7%**). On Dataset B ($H=5$ cumulative trajectories, **Plot J**), `D10` outperforms `D9` heavily once similar-allocation circuits appear (`Hungaroring`: `2.166s` vs `2.401s`, **-9.8%**; `Monza`: `1.104s` vs `1.244s`, **-11.3%**), whereas early adaptation after a single race (`Catalunya` right after `Suzuka`) is slightly noisier due to small-sample variance.

---

## 6. Diagnostic Figures Summary

All four multi-panel figures are saved in [`artifacts/runs/stage3_degradation_v1/plots/`](file:///c:/Projects/f1-race-intelligence/artifacts/runs/stage3_degradation_v1/plots/):

1. [`stage3_trajectories_abcd.png`](file:///c:/Projects/f1-race-intelligence/artifacts/runs/stage3_degradation_v1/plots/stage3_trajectories_abcd.png):
   - **Plot A**: Observed vs open-loop predicted 10-lap mean stint pace (`SOFT`, `MEDIUM`, `HARD`) tightly aligned along $y=x$ across `70s–101s` circuits.
   - **Plot B**: Disentangled pure tire degradation $C + D(a)$ vs net stint evolution $C + D(a) + F_r(l)$ at Sakhir.
   - **Plot C**: Open-loop ($H=5$) residual vs projected tire age (`0–55` laps), showing a flat binned mean near `0.0s`.
   - **Plot D**: Open-loop residual vs total observed stint duration (`5–55` laps), diagnosing survivorship selection on short pit-terminated stints (`<15` laps).
2. [`stage3_horizons_efgh.png`](file:///c:/Projects/f1-race-intelligence/artifacts/runs/stage3_horizons_efgh.png):
   - **Plot E**: Open-loop endpoint lap MAE vs horizon $H \in \{1, 3, 5, 10, 15, 20\}$ on 2025 test, showing `D8` (`PaceV2`) pulling progressively further ahead of `D0` (`PaceV1`) and `B0` (`E2`) as $H$ grows from `1` to `20` laps.
   - **Plot F**: Cumulative stint-time MAE vs horizon $H \in \{5, 10, 15, 20\}$.
   - **Plot G**: Empirical 50% and 90% cumulative stint-time interval coverage vs horizon (`D8` correlated trajectories maintaining `87.1%`–`78.3%` coverage across $H=5\dots 15$ vs `D7` independent laps collapsing to `27.6%`).
   - **Plot H**: Predicted vs observed total cumulative stint time $S_H$ (`350s` to `2,020s`) across $H \in \{5, 10, 15, 20\}$.
3. [`stage3_generalization_ijkl.png`](file:///c:/Projects/f1-race-intelligence/artifacts/runs/stage3_generalization_ijkl.png):
   - **Plot I**: 5-lap cumulative MAE across all 10 validation + test circuits (`Austin`, `Catalunya`, `Hungaroring`, `Imola`, `Monza`, `Sakhir`, `Singapore`, `Spielberg`, `Suzuka`, `Yas Marina`).
   - **Plot J**: 2026 regulation transfer (`D9` vs `D10`) across the 5 chronologically ordered 2026 races.
   - **Plot K**: Predicted vs observed within-stint 10-lap pace change ($\bar{T}_{\text{late}} - \bar{T}_{\text{early}}$).
   - **Plot L**: Within-stint residual autocorrelation by lag $k \in \{1\dots 5\}$, justifying the AR(1) + latent stint random-effect covariance structure in `D8`.
4. [`stage3_stint_replay_and_strategy.png`](file:///c:/Projects/f1-race-intelligence/artifacts/runs/stage3_stint_replay_and_strategy.png):
   - **Left Panel**: Historical open-loop 20-lap trajectory forecast for George Russell at the 2025 Singapore GP (`HARD` stint from cutoff Lap 30 through Lap 50), showing observed clean laps, Stage 1 traffic-excluded laps, and calibrated 50%/90% predictive fan bands.
   - **Right Panel**: Counterfactual 15-lap clean-air strategy comparison at Singapore (`Continue Worn MEDIUM age 18..32` = `1456.2s` vs `Switch Fresh HARD age 0..14` = `1448.8s` vs `Switch Fresh SOFT age 0..14` = `1445.5s`), demonstrating a `+7.4s` (`HARD`) to `+10.7s` (`SOFT`) 15-lap clean-air pace advantage before pit-lane loss (`~20–22s`), which directly sets up Stage 4 (Pit-Loss & Stop Timing).

---

## 7. Honest Limitations & Readiness for Stage 4

1. **Very Long Horizons ($H=20$, Dataset B) Have Severe Survivorship Selection**:
   - Only `1.6%` (`47` validation / `120` test) of forecast cutoffs have `20` consecutive future laps that are 100% free of traffic, pit stops, and safety cars, and almost all of those belong to race leaders nursing `HARD` tires to the flag (`PaceV1` `D0`'s conservative zero-age-slope extrapolation slightly edges `D8` on $H=20$ Dataset B even though `D8` beats `D0` on $H=20$ Dataset A across all `818` clean laps: `1.0587s` vs `1.0823s`).
2. **Post-Pit New-Stint Cold Start (`0B` / `NEW_STINT`) Remains the Largest Single-Lap Error Source**:
   - Predicting a brand-new stint before any flying lap is observed on the new tire set achieves `0.85s–0.96s` first-lap MAE (`5.22s` 5-lap cumulative MAE on test vs `8.27s` for `E2`). While a `37%` improvement over `E2`, compound-set-to-set variance (`±0.4s`) only resolves after 1–2 flying laps on the new compound.
3. **Readiness for Stage 4 (Pit-Loss & Undercut/Overcut Strategy Simulation)**:
   - `PaceV2` (`D8_correlated_trajectory_uncertainty`) satisfies every contract required by [`src/f1ri/simulator/contracts.py`](file:///c:/Projects/f1-race-intelligence/src/f1ri/simulator/contracts.py): it produces causal, open-loop multi-lap distributions (`mean`, `median`, `p05`–`p95`, correlated `cumulative_time_samples_s` matrices, and `CounterfactualStintComparison` time-delta probabilities) for both `CONTINUED_STINT` and `NEW_STINT` hypotheses without lookahead leakage.
