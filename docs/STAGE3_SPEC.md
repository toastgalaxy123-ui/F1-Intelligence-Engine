# Stage 3 Specification — Tire Degradation & Future Stint Simulation (`PaceV2`)

## 1. Prediction Objective & Mathematical Formulation

Stage 3 extends the F1 Race Intelligence Engine from single-step lap-time forecasting (`PaceV1`) to **open-loop multi-lap stint trajectory forecasting** (`PaceV2`).

At forecast cutoff lap $l_c$ (with timestamp `forecast_as_of_utc` equal to the completion of lap $l_c$), given a specified tire scenario $S \in \{\text{CONTINUE\_CURRENT\_STINT}, \text{NEW\_STINT\_FRESH\_TYRE}, \text{NEW\_STINT\_USED\_TYRE}\}$ with starting lap $l_{\text{start}} = l_c + 1$, compound $c$, initial tire age $a_{\text{start}}$, and horizon $H \in \{1, 3, 5, 10, 15, 20\}$, the model predicts the joint clean-air green-flag lap-time trajectory:
\[
\mathbf{T}_{d,r}(l_{\text{start}} : l_{\text{start}} + H - 1 \mid \mathcal{I}_{l_c}, S) = \bigl(T_{d,r,l_{\text{start}}}, \dots, T_{d,r,l_{\text{start}}+H-1}\bigr)
\]
and the cumulative clean-air stint time:
\[
S_{d,r}(l_{\text{start}}, H \mid \mathcal{I}_{l_c}, S) = \sum_{k=1}^{H} T_{d,r,l_{\text{start}}+k-1}
\]

### Three-Component Decomposition (Zero Double-Counting)
Each projected lap $l_k = l_{\text{start}} + k - 1$ with projected tire age $a_k = a_{\text{start}} + k - 1$ and race progress $p_k = l_k / L_r$ is decomposed into three mutually exclusive components:
\[
\mathbb{E}[T_{d,r,l_k} \mid \mathcal{I}_{l_c}, S] = \underbrace{\bigl(\mu_{d,r}^{(0)} + b_s\bigr)}_{\text{Component A: Base Pace}} + \underbrace{\bigl(F_r(p_k) + W_r(x_{l_c})\bigr)}_{\text{Component B: Race Progression \& Weather}} + \underbrace{\bigl(C_{r,c} + D_{r,c}(a_k) + d_s \cdot (a_k - a_{\text{ref}})\bigr)}_{\text{Component C: Tire Compound, Degradation \& Stint Slope}}
\]
where:
1. **Component A (Driver/Car/Stint Base Pace)**: Empirical-Bayes historical circuit/team/driver prior blended with weekend qualifying/grid pace (for cold starts) and cross-stint race pace history, plus the online Bayesian stint level offset $b_s$.
2. **Component B (Race Progression & Weather)**: Jointly estimated fuel-burn/track-evolution progression effect $F_r(p_k) = \beta_{\text{prog}} \cdot p_k$ and frozen cutoff track-temperature effect $W_r(x_{l_c})$.
3. **Component C (Tire Compound, Age Degradation & Dynamic Stint Slope)**:
   - Weekend compound step offset $C_{r,c}$ (partially pooled across circuit and relative compound).
   - Three-phase nonlinear degradation curve $D_{r,c}(a_k)$ normalized so $D_{r,c}(0) = 0$:
     \[
     D_{r,c}(a) = w_c \bigl(e^{-a / 1.5} - 1\bigr) + \bigl(\beta_{\text{lin},c} + \delta_{\text{era},C(r,c)} + \delta_{\text{circuit},c}\bigr) a + \gamma_{\text{cliff},c} \left(\frac{\max(0, a - \kappa_c)}{10}\right)^2
     \]
   - Online Bayesian latent stint slope deviation $d_s \cdot (a_k - a_{\text{ref}})$, which resets to $0$ upon a pit stop / new tire set (`tire-change state reset`).

---

## 2. Correlated Trajectory Uncertainty (`CorrelatedTrajectorySampler`)

Because cumulative stint-time variance is dominated by persistent stint-level errors rather than independent lap noise:
\[
\text{Var}\!\left(\sum_{k=1}^{H} T_{l_k}\right) = \sum_{k=1}^{H} \text{Var}(T_{l_k}) + 2 \sum_{1 \le i < j \le H} \text{Cov}(T_{l_i}, T_{l_j})
\]
`CorrelatedTrajectorySampler` (`D8`) samples $M = 2{,}000$ trajectories per forecast using:
1. Persistent pace-level error $\eta_{\text{pace}} \sim \mathcal{N}(0, \sigma_{\text{pace}}^2)$ (conditioned on cold-start regime `0A`, `0B`, `1_2_early_stint`, or `3_plus_warm_stint`).
2. Persistent degradation-slope error $\eta_{\text{deg}} \sim \mathcal{N}(0, \sigma_{\text{deg}}^2)$ scaled by step horizon and historical support status (`SUPPORTED`, `LIMITED_SUPPORT`, `EXTRAPOLATED`, `UNSUPPORTED`).
3. Autocorrelated $\text{AR}(1)$ heavy-tailed lap innovations $\epsilon_k = \rho \epsilon_{k-1} + \sqrt{1 - \rho^2}\,\sigma_{\text{lap}}\,\xi_k$.

---

## 3. Data Architecture & Leakage Prevention

- `silver.compound_allocations`: Curated Pirelli weekend compound mappings (`HARD`/`MEDIUM`/`SOFT` $\to$ `C1`–`C6`), separated by `regulation_era` (`2023_2025` vs `2026_PLUS`) and guarded by `available_as_of_utc`.
- `gold.stint_observations`: One row per observed driver stint `(session_key, driver_number, stint_number)` with `used_tyre_set`, `was_pit_terminated`, `ended_by_retirement`, and empirical within-stint slope summaries.
- `gold.stint_lap_observations`: Lap-level longitudinal stint table with `tyre_age_at_lap_start`, `stint_lap_index`, `compound_absolute`, and `race_progress`.
- `gold.stint_training_examples`: Multi-horizon point-in-time evaluation table ($H \in \{1, 3, 5, 10, 15, 20\}$) supporting both **Dataset A** (endpoint lap evaluation) and **Dataset B** (complete clean trajectory cumulative evaluation with explicit censoring codes `COMPLETE_CLEAN`, `FUTURE_PIT_TRANSITION`, `TRAFFIC_CONTAMINATION`, `NEUTRALIZATION_SC_VSC_RED`, `RETIREMENT_OR_INCOMPLETE`).

---

## 4. Controlled Experiment Matrix (`B0`, `B4`, `D0`–`D10`)

- `B0_E2_compound_median`: Stage 2 `E2` compound-specific recent clean-lap median wrapped as an open-loop stint forecaster.
- `B4_adaptive_reset_progress`: Online recent clean-lap median + fresh-tire reset + fixed progress drift (`AdaptiveTireResetProgressBaseline`).
- `D0_frozen_pace_v1`: Frozen Stage 2 `PaceModelV1` (`E8`) wrapped as an open-loop stint forecaster.
- `D1_linear_age_trend`: Recent clean pace + within-stint linear tire-age trend without joint fuel/progress separation.
- `D2_joint_linear_progress_age`: Jointly estimated linear race-progression + compound-specific tire-age degradation (`LinearTireDegradationModel`).
- `D3_nonlinear_age`: Three-phase nonlinear degradation curve (warm-up + linear stable phase + penalized late-stint quadratic hinge cliff, `NonlinearTireDegradationModel`).
- `D4_pirelli_absolute_compound`: Era-qualified Pirelli `C1`–`C6` absolute compound hierarchy (`HierarchicalTireDegradationModel`).
- `D5_hierarchical_circuit_compound`: Partial pooling across `circuit x compound` shrunk by effective clean stint count.
- `D6_weekend_adaptation`: Weekend qualifying/grid pace anchor + track-temperature interaction.
- `D7_dynamic_stint_state`: Online Bayesian latent stint-state filter (`BayesianStintStateEstimator`) updating $(b_s, d_s)$ within-stint and resetting on pit stops.
- `D8_correlated_trajectory_uncertainty`: Full `PaceModelV2` with calibrated `CorrelatedTrajectorySampler` for cumulative 50%/90% intervals and CRPS.
- `D9` & `D10` (`2026` Regulation Transfer): Zero-shot `2023–2025` prior transfer (`D9`) vs progressive race-by-race `2026` adaptation (`D10`).
