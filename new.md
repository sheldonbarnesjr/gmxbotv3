# Pipeline: Metasweep → Phase 3 (Full Detail)

---

## PRE-PHASE 2: REGULARIZATION SWEEP (`launch_reg_sweep.sh`)

**Purpose:** Select the best regularization level before committing to 16-model full training.

**Scripts:**
- `reg_sweep_cpcv.py` — runs Combinatorial Purged CV (CPCV) with 15 path combos × 40 Optuna trials per philosophy on 8 probe assets (btc, eth, sol, doge, aapl, nvda, tsla, jpm). Uses light-XGB with AUC-proxy fitness. Persists `trial_sharpes_{philosophy}.parquet` for downstream DSR adjustment.
- `reg_sweep_per_level.sh` — 5-array job (one per regularization level: L1/L2/ElasticNet variants), submits per-philosophy training sub-jobs.
- `select_best_reg_level.py` / `select_best_reg_level_v2.py` — Wilcoxon signed-rank + Kendall-W concordance to pick the winner. If top-2 not significantly different, picks the more conservative (lower complexity) reg level. Writes `configs/frozen/best_reg_level.json`.
- `cpcv_gate.sh` → `prompt_cpcv_gate.py` — Compares CPCV-selected params vs standard Optuna via Deflated Sharpe Ratio. If CPCV significantly better (DSR > threshold), it replaces Optuna's winner.

**Output:** `configs/frozen/best_reg_level.json`, `trial_sharpes_{philosophy}.parquet`

---

## PHASE 2: ML ENSEMBLE

### Step 1 — Dataset Build (`p2_dataset_build.sh` → `prompt_1_dataset_build.py`)

**26-asset array job.** Per asset:
- Reads Phase 2 OHLCV + Phase 2 feature store (~4,270 cols per bar)
- Replays frozen Phase 1 signal libraries on Phase 2 data (no refitting)
- Constructs 8-fold walk-forward datasets with purge=72 bars, embargo=24 bars
- Builds 6 feature groups:
  - Group A: Raw OHLCV
  - Group B: Phase 1 signal outcomes (frozen replayed signals)
  - Group C: Phase 0 encodings (LSTM A/B/C/D, SR transformer, TimesFM)
  - Group D: Technical indicators + microstructure
  - Group E: Event-level metadata (family ID, cluster size, concurrency)
  - Group F: Cross-asset features (BTC context, ETH broadcast)
- Anti-leakage: METADATA_COLUMNS excluded from feature matrices; scalers/imputers fit on train fold only, saved per fold
- If `args.asset in PHASE1_DROPPED_ASSETS` → `sys.exit(0)` clean no-op (coin/pltr/avax/dot)
- **Output:** `data/phase2_datasets/{asset}_{philosophy}_fold{N}.parquet` (8 folds × 26 assets × 18 philosophies)

### Step 2 — 16-Model Training (`p2_models_train.sh` → `prompt_2_model_train.py` + individual model scripts)

**26-asset array, H100 GPU, ~8h/task.** For each asset × philosophy × 8 folds:

**Core classifiers (direction prediction):**
1. **XGBoost** (`prompt_2_model_train.py`) — `eval_metric='logloss'` (not AUC — unblocks isotonic calibration). Huber regressor alongside for sizing (`reg:pseudohubererror`, δ=0.5×MAD(y_train)). Sample weights = `|net_pnl_pct| × 1/sqrt(n_events)` joint-normalized. Saves `.json` native + imputer `.pkl` to `models/phase2/`.
2. **LightGBM** (`prompt_2e_lightgbm.py`) — same sample-weight scheme, AdamW-equivalent leaf regularization. Saves `.txt` + imputer.
3. **CatBoost** (`prompt_2f_catboost.py`) — Huber loss per fold, Optuna TPESampler seed=`config.RANDOM_SEED`. Saves `.cbm`.
4. **LSTM #2** (`prompt_2c_lstm_classifier.py`) — bidirectional, 2-layer, attention head. AdamW optimizer, `clip_grad_norm_(1.0)`, `num_workers=4, pin_memory=True`. Early stopping on OOS loss (fold split 80/20 internal).
5. **TFT #2** (`prompt_2d_tft_scorer.py`) — Temporal Fusion Transformer, multi-horizon attention, variable selection network.
6. **Cross-Asset Transformer #2** (`prompt_2e_cross_asset_scorer.py`) — 26-asset context window, shared embedding, per-asset head.
7. **TCN** (`prompt_2_tcn.py`) — Temporal Convolutional Network, dilated causal convolutions. AdamW + `clip_grad_norm_(1.0)`.
8. **TabNet** (`prompt_2_tabnet.py`) — interpretable tabular attention with feature masking, sparse regularization.
9. **XGB-Quantile** (`prompt_2i_xgb_quantile.py`) — q25/q50/q75 quantile regression via vectorized 3-point PAV isotonic projection (`_pav_3point_vec`). Outputs `xgbq_q25`, `xgbq_q50`, `xgbq_q75`, `xgbq_iqr`.
10. **LGB-Bootstrap** (`prompt_2k_lgb_bootstrap.py`) — B=16 bootstrap samples per fold, per-sample seed=`RANDOM_SEED*1000+b_idx`. Outputs `lgb_mu`, `lgb_sigma`. Cross-bootstrap σ<1e-3 → warning.
11. **BNN+MC Dropout** (`prompt_2l_bnn_mc_dropout.py`) — single batched forward pass ×50 MC samples (was 50 sequential calls — fixed for 50× speedup). Outputs epistemic uncertainty.
12. **Unified Cross-Asset** (`prompt_2_unified.py`) — single model trained on all assets simultaneously, asset-embedding layer.
13. **TimesFM Features Phase 2** (`prompt_2b_timesfm_features.py`) — re-runs TimesFM inference on Phase 2 bars (asset-only, no `--philosophy`), adds fresh quantile/forecast features.
14. **Autoencoder Anomaly** (`prompt_autoencoder_anomaly.py`) — reconstruction error filter. GATE 3: must remove ≥10% more losers than winners AND retain ≥85% winners. Writes `{philosophy}_filter_efficiency.json`.
15. **Cascade Filter** (`prompt_cascade_filter.py`) — stacked XGB → LGB → CatBoost pass/fail cascade. Also checked by GATE 3.
16. **Regime XGB** (`prompt_2g_regime_xgb.py`) — classifies trending/ranging/breakout/mean-reversion. Optuna seed pinned.

**Parallel off critical path:**
- **NGBoost** (`prompt_2h_ngboost.py`, `ngboost_train.sh`) — probabilistic boosting, natural gradient. Normal distribution head. Separate job, non-blocking.
- **Leverage model** (`prompt_2f_leverage_model.py`) — XGB trained on regime → Kelly leverage multiplier. Uses analytic Kelly when `USE_ANALYTIC_KELLY_LEVERAGE=True`.
- **Bias kernels** (`prompt_2h_bias_kernels.py`) — detects and corrects systematic prediction biases per regime.
- **Adversarial** (`prompt_adversarial.py`) — generates adversarial samples to test model robustness.
- **SMOTE** (`prompt_smote.py`) — oversampling of minority class (winners) on train fold only. Seed=`config.RANDOM_SEED`.

### Step 2b — Symbolic Distillation (`prompt_2g_symbolic_distillation.py`)

Takes trained XGB → PySR symbolic regression distillation. Produces interpretable rules that approximate XGB outputs. Feeds into decision layer as alternative score signal.

### Step 2c — Sigma Exits Training (`sigma_exits_train.sh` → `prompt_2m_sigma_exits.py`)

After assembly. Trains σ-scaled exit thresholds using MetaCombiner σ outputs. The exit fires when realized MFE crosses MetaCombiner-predicted σ band. Writes per-asset exit config.

### Step 2d — GP Exits (`prompt_2j_gp_exits.py`)

Evolves exit rules via GP. Uses same walk-forward structure. Exit fitness: Sharpe-delta base (n-smoothed) + DD improvement + payoff delta + preservation + soft parsimony.

### Step 2e — PySR Exits & Sizing (`prompt_2i_pysr_exits.py`, `prompt_2j_pysr_sizing.py`)

- PySR exit: HuberLoss(0.1), 5 bps MFE floor, sample-size smoothing ×n/(n+30)
- PySR sizing: symbolic expression for position size = f(uncertainty, regime, drawdown_q95)

### Step 2f — RL Sizing (`prompt_rl_sizing.py`)

SAC agent trained to select position sizes. State = (MetaCombiner outputs + regime + current DD). Reward = risk-adjusted PnL.

### Step 2g — Distribution Exit (`prompt_2n_distribution_exit.py`)

Option C: 4 triggers using precomputed TimesFM features (no MetaCombiner per-bar calls):
1. Reversal signal (quantile crossover)
2. Direction-gone (hysteresis=2 bars)
3. MFE-peak pullback
4. Kelly floor (hysteresis=2)

Regime-aware: ×0.8 high_vol / ×1.2 sideways. ν-aware (fat tail tightening).

### Step 2h — ATR Exits (`prompt_atr_exits.py`)

ATR-scaled trailing stops. Per-asset ATR multiplier tuned on OOS folds 4-8.

### Step 3 — TODO #27 Isotonic Gate (`isotonic_gate_array.sh` → `prompt_isotonic_gate.py`)

26-asset × 54 invocations (each philosophy × fold combination). For each fold:
- Reads per-fold calibration data saved by trainers via `isotonic_gate_io.save_fold_cal_data()`
- Fits `sklearn.IsotonicRegression` on OOS predictions vs realized outcomes
- Champion-challenger: if isotonic-calibrated model improves Brier score ≥ threshold vs uncalibrated → `isotonic_active=True`
- Writes `{philosophy}_isotonic_gate.json`. Assembly reads this to choose calibrated vs raw probabilities.

### Step 4 — OOS Assembly (`p2_assembly.sh` → `prompt_3_oos_assembly.py`)

**26-asset array.** For each asset:
- Merges all 16 model OOS predictions across 8 folds
- Aligns timestamps, handles missing models gracefully
- Applies ENSEMBLE_PRED_COLS (canonical 40-feature list for MetaCombiner)
- Computes reliability features: fold_sharpe_spread, tail_ratio_fold_cv, ulcer_over_max_dd, regime_cond_sharpe_std, payoff_ratio_smoothed (5 v2 reliability candidates)
- Also runs `--merge` pass: cross-asset merge of all 26 assets' OOS predictions
- **Output:** `data/phase2/oos_merged_{asset}.parquet`

### Step 5 — MetaCombiner (`p2_freeze.sh` → `prompt_3b_metacombiner.py`)

**Per-philosophy (18 jobs).** Two-level walk-forward:
- **Level 1:** Per fold, train MetaCombiner on prior folds' OOS predictions
- Fold 1 = NaN (insufficient history)
- **Architecture:** `concat(models) → Dropout(per-group, 3 separate modules post-slice) → 512 → 256 → 128 → (mu, log_sigma, log_nu)`
- **Loss:** `nn.SmoothL1Loss(beta=0.005)` on vol-scaled net_pnl_pct (flag `MC_VOL_SCALE`)
- **Regularization:** input dropout p=0.2 + L1 λ=1e-4 (from TODO #21 ablation)
- **GATE 1:** If MetaCombiner OOS score ≤ simple blend ×1.05 → freeze simple blend instead. Writes `{philosophy}_metacombiner_tie_break.json`
- **Post-hoc:** Platt calibration of `metacombiner_confidence` vs realized direction. Writes `platt_mc_confidence_{philosophy}.json`, preserves raw as `metacombiner_confidence_raw`
- **Outputs:** `mc_mu`, `mc_sigma`, `mc_nu`, `mc_kelly_leverage` (quarter-Kelly 1×-20×), `mc_skip_score`, `mc_tail_risk_flag` (ν<5.0), `metacombiner_confidence`

### Step 5b — MetaCombiner Ablation (`mc_ablation.sh` → `prompt_3b_metacombiner_ablation.py`)

For each of 52 MC input candidates per philosophy: VIF check + block-permutation importance. Writes `{philosophy}_mc_inputs.json`. MetaCombiner reads this to select dynamic input list. Also emits canonical `selected_inputs` + `activation_threshold` block.

### Step 5c — Super Learner (`prompt_super_learner.py`)

Stacked ensemble: trains a meta-model (Ridge + LogReg) on OOS predictions from all 16 base models. Writes `super_learner_score` column. MetaCombiner uses this as an additional input.

### Step 5d — Model Aging (`prompt_model_aging.py`)

Detects and flags models whose OOS performance degrades over time (concept drift). Outputs per-model `aging_score`; assembly can down-weight aged models.

### Step 6 — Decision Layer (`prompt_4_decision_layer.py` + `prompt_4_decision_scheme_evolution.py`)

**3 decision schemes:**
1. **P50 threshold** — fire if mc_mu > φ₅₀ (median of training fold scores)
2. **Adaptive threshold** — φ adjusts to current volatility regime
3. **Regime scheme** — different thresholds per trending/ranging/breakout/mean-reversion

**5 overlays** applied to each scheme:
1. Regime filter (block trades in mismatched regime)
2. Vol filter (adjust position in high/low vol)
3. Momentum filter (trend alignment check)
4. Liquidity filter (spread/volume gate)
5. Cost filter (fee-adjusted expected value gate)

= **15 base variants** × **~5 entry-indicator families** = **72 strategy variants** per (asset, philosophy)

**Parallel:** `prompt_4_decision_scheme_evolution.py` / `decision_scheme_gp.sh` — GP-evolves new decision schemes beyond the 3 canonical ones. Writes candidate schemes to `pipeline_state/phase_2/evolved_schemes.json`.

### Step 7 — Overlay / Robustness (`prompt_5_overlay.py` → `prompt_5b_robustness_audit.py`)

`prompt_5_overlay.py`: applies the 5 overlays to produce 72 final strategy variants. For each variant, runs trade simulation with realistic costs (fees, funding, slippage, spread, latency). Tests 3 concurrency caps `[5, 8, 10]` per asset.

`prompt_5b_robustness_audit.py`:
- **GATE 2** (Uncertainty calibration): checks if Q4 (high uncertainty) underperforms Q1 (low uncertainty) in net_pnl. Fail → `uncertainty_calibration_pass=False` → Kelly leverage clamps to 1×.
- **v2 philosophy checks** (flag-only): regime concentration, tail-CV, ulcer/maxDD ratio, philosophy faithfulness, cross-asset scale, trimmed fold-stability, cost-stress slope.
- **CRPS** + **Brier decomposition** added (calibration quality metrics).
- Fail condition: `resolution_over_uncertainty < 0.05` can flip GATE 2 even if Q1>Q4 passes.

### Step 7b — TimesFM Calibration Check (`check_timesfm_calibration.py`)

Verifies TimesFM prediction intervals are properly calibrated on Phase 2 OOS data. If calibration drift detected, applies correction factor. Non-gating.

### Step 7c — Distribution Exit Threshold Tuning (`tune_dist_exit_threshold.py`)

Tunes the global threshold for `dist_exit` (Option C). Sweep over threshold values, select one maximizing Phase 2 OOS composite score. Locked before exit selection.

### Step 8 — Exit Selection (`exit_select.sh` → `prompt_exit_selection.py`)

**18-array, one per philosophy.** Uses Phase 2 OOS folds 4-8 ONLY (never folds 1-3, never holdout).

For each philosophy, compare 6 exit methods: `fixed`, `gp`, `pysr`, `atr`, `sigma`, `dist`.

**Composite score per exit:**
- 0.35 × Sharpe_improvement (DSR-adjusted, n_trials=5)
- 0.20 × capture_ratio (MFE capture vs fixed)
- 0.15 × fold_stability (CV of improvement across folds)
- 0.15 × dd_improvement (reduction in max drawdown)
- 0.10 × trade_preservation (≥ 70% of trades still taken)
- 0.05 × holding_efficiency
- minus complexity_penalty

**Statistical tests:**
- Block bootstrap n=10,000 paired test against fixed baseline
- Holm-Bonferroni correction across 90 effective tests (6 exits × 15 philosophy pairs)
- `dist_exit` extra gates: calibration PASS + 0.08 min margin + ≥300 trades

**5 hard gates (ALL must pass):**
1. Composite score margin ≥ 0.05 vs fixed
2. Bootstrap p-value ≤ 0.05
3. ≥5 of 8 folds won
4. DD ratio ≤ 1.10
5. Trade preservation ≥ 0.70

**Tiebreak:** simplicity. Fail-safe: `selected_exit = 'fixed'`.

**Output:** `configs/frozen/{philosophy}_exit_selection.json`. Phase 3 run count: 33,696 → 11,232 (1 exit per philosophy, not 6).

### Step 8b — Sizing Selection (`sizing_select.sh` → `prompt_sizing_selection.py`)

**18-array.** Same structure as exit selection. Compares: `{fixed, kelly, pysr, rl}`. (XGB leverage dropped — interaction guard #5.) Same 5-criterion gate. Writes `configs/frozen/{philosophy}_sizing_selection.json`.

### Step 9 — Assembly & Freeze (`p2_freeze.sh` → `prompt_6_assembly.py`)

**Per-philosophy (18 tasks in p2_freeze.sh).** The final packaging step:

- Reads all ablation decisions (`mc_inputs`, `skip_config`, `selected_exit`, `fallback_exit`, `selected_sizing`, `gate_flags`, `admitted_assets`, `decision_scheme`, `overlay_variant`)
- Applies GATE 1 (simplicity tie-break), GATE 2 (uncertainty calibration flag), GATE 3 (filter efficiency), GATE 4 (philosophy faithfulness — flag-only)
- Burns `selected_exit` + `fallback_exit` into each strategy package
- **Global Holm step** (last, after all 18 philosophies): collects all p-values from exit + sizing + MC ablation JSONs, applies `multipletests(method='holm')` across full family, writes `pipeline_state/phase_2/selection_pvalues.jsonl` with `p_holm_global` + `selection_sig_global`
- Packages each of 1,296 strategies as a frozen JSON with SHA256 hash
- **Output:** `configs/frozen/phase2_strategies/{philosophy}_{variant}.json` (1,296 files), `configs/frozen/phase2_freeze_manifest.json`

### Step 10 — Parallel Ablation Suite (Non-gating, informational)

All run in parallel after model training:
- **`feature_ablation.py`** — permutation importance of feature groups
- **`skip_ablation.py` / `prompt_skip_ablation.py`** — forward selection of skip mechanisms (AE/Cascade as MC inputs)
- **`placebo_runner.py` + `placebo_gate.py`** — trains on shuffled labels, compares AUC vs real. Gate: real must beat placebo significantly.
- **`cost_stress_runner.py`** — 3×-5× cost scenarios to check strategy robustness to fee increases
- **`model_ladder_gate.py`** — tests model promotion gates sequentially (naive → linear → tree → neural). Checks each level adds measurable OOS value before promoting to next complexity tier.

### Step 11 — Validate (`p2_validate.sh` → `validate_phase2_outputs.py`)

Hard gate: verifies 1,296 frozen strategies exist with valid SHA256 hashes, correct schema, no NaN outputs, walk-forward integrity. FAILS the chain if any issue found.

---

## PHASE 2.5: PORTFOLIO CONSTRUCTION

### Step 1 — `run_all.sh` (5 internal steps in sequence)

1. **HRP (`prompt_7_portfolio_construction.py`)** — Hierarchical Risk Parity on 1,296 strategy OOS daily returns (folds 4-8). Ledoit-Wolf covariance shrinkage. R4 fix: NaN strategies get weight=0 (not boosted via LW zero-variance). Output: `hrp_weights.parquet`.

2. **Regime Detector (`prompt_7b_regime_detector.py`)** — classifies current market regime (trending/ranging/breakout/high_vol/mean_reversion). Per-bar regime labels for downstream LinUCB.

3. **Neural Portfolio Optimizer (`prompt_7c_portfolio_optimizer.py`)** — `SmallPortfolioNet: 1296→512→256→1296`. AdamW. `nn.Dropout(NEURALOPT_DROPOUT=0.1)` before fc1. Minimizes portfolio variance + concentration. ±30% deviation from HRP base. Output: `neural_opt_weights.parquet`.

4. **PySR Drawdown (`prompt_7d_pysr_drawdown.py`)** — symbolic regression for predicting drawdown. Used as one of 6 MetaDD base predictors.

5. **Kelly Portfolio (`prompt_7e_kelly_portfolio.py`)** — variant 8: κ = μ/σ² (analytic Kelly). Ledoit-Wolf Σ. variant 8b: when portfolio ν < KELLY_DD_NU_THRESHOLD=4.0, escalate γ×1.5 (sixth-Kelly in fat tails), then post-solve `s = min(1, KELLY_DD_Q95_CAP=0.25 / projected_dd_q95)` monotone clamp.

### Step 2 — MetaDD Predictor (`prompt_7f_metadd_predictor.py`)

**6 base predictors → Student-t MLP:**
1. PySR DD (from run_all step 4)
2. NGBoost DD
3. Vol baseline (rolling realized vol)
4. GP DD (`prompt_7f_gp_drawdown.py`, 3 seeds)
5. XGB-Quantile DD (`prompt_7f_xgb_quantile_dd.py`)
6. LGB-Bootstrap DD (`prompt_7f_lgb_bootstrap_dd.py`, B=16, per-bootstrap seed=RANDOM_SEED×1000+b_idx)

**MetaDD combiner:**
- Ridge combiner → `metadd_ridge_combiner.pkl`
- Student-t output heads: ν softplus init → ν≈5, range [2.5, 30.0]
- Optional log-prior: `0.5×(log(ν)−log(5))²` in `_student_t_nll`
- Post-hoc isotonic calibration on dd_q90 + dd_q95 → `dd_q90_cal`, `dd_q95_cal`
- Saves `metadd_ngboost_dd.pkl`, `xgb_quantile_dd.pkl`, `lgb_bootstrap_dd.pkl`

### Step 3 — GP Portfolio (`prompt_7i_gp_portfolio.py`)

GP-evolved portfolio allocation rules (variant 11). Uses same GP machinery as Phase 1 but fitness = portfolio Sharpe improvement. Runs in parallel with MetaDD.

### Step 4 — Portfolio Baselines (`prompt_7g_portfolio_baselines.py`)

**Tier-5 gate** (all must pass):
- Equal-weight Sharpe ≥ 0.50
- Max drawdown ≤ 0.30
- Calmar ≥ 1.50
- Win rate ≥ 0.55
- Gini coefficient ≤ 0.70

Also runs kill-rule bootstrap using `get_shared_block_length()` for B4/B9 interaction guard. Fail-safe: pipeline continues with warning, not hard kill.

### Step 5 — Asset Admission (`prompt_7h_asset_admission.py`)

**≥2-of-3 pass rule** (was AND — bug fix):
1. Walk-forward Sharpe ≥ 0.25 per asset
2. Calibrated uncertainty: isotonic calibration slope within acceptable range
3. Diversity: pairwise correlation ρ < 0.70 with existing admitted assets

Whitelist JSON SHA256 pins source parquet. Date-like columns asserted ≤ 2024-12-31 (anti-leakage). Fail-safe: admit top-5 anyway if <2 pass.

### Step 6 — Per-Cell Selector (`prompt_7j_per_cell_selector.py`)

**468 cells (22 assets × 18 philosophies after dropped assets).**

**Dynamic-K Manifest (Option D):**
For each cell, 8-term composite score (all normalized to [0,1]):
- 0.28 × DSR_sharpe (n_trials=1,656 per cell)
- 0.18 × (1 − dd_ratio)
- 0.14 × fold_stability
- 0.10 × calmar
- 0.10 × deployment_score (flag-gated)
- 0.08 × (1 − concentration)
- 0.07 × trade_preservation
- 0.05 × tail_ratio
- minus 0.03 × complexity_penalty

Holm-Bonferroni correction across 468 cells. Dynamic K ≤ 3: selects top-K variants per cell where K is determined by score gap (diminishing returns). Phase 3 run count: 11,232 → ~702.

**12 vectorized deployment metrics** (`deployment_metrics.py`):
TWN, fill rate, Gini/HHI concentration, effective_N, skip distribution, firing rate, Jaccard similarity, leverage distribution, intraday variance, ceiling projection, correlation-adjusted deployment.

**Output:** `configs/frozen/phase3_cell_selections.json`, `configs/frozen/phase3_deploy_manifest.json`

### Step 7 — Freeze Artifacts (`freeze_phase25_artifacts.py`)

SHA-pins all artifacts:
- `linucb_context_scaler_{philosophy}.pkl`
- `platt_mc_confidence_{philosophy}.json`
- `dd_isotonic_{philosophy}.json`
- `trial_sharpes_{philosophy}.parquet`
- `metadd_ridge_combiner.pkl`
- `neural_optimizer_weights.pt`
- `phase3_cell_selections.json`, `phase3_deploy_manifest.json`
- `variant_overrides.json`, `venue_liquidity_caps.json`
- Records `universe_hash` (sha256 over sorted asset tuple, R5 drift detection)

**Output:** `configs/frozen/phase2_5_freeze_manifest.json` — **Hard gate. Phase 3 cannot start without this.**

### Track A/B/C (Parallel diagnostics, not gating)
- **Track A** (`track_a_skip_telemetry.sh` → `prompt_skip_telemetry.py`) — Phase 3 skip-mechanism heatmaps showing which assets/philosophies fire skip most
- **Track B** (`track_b_firing_rates.sh` → `strategy_firing_rates.py`) — strategy firing rate audit across 468 cells
- **Track C** (`track_c_ablation.sh` → `ablation_runner.py`) — 4-mode allocator ablation (HRP / neural / Kelly / equal-weight)

---

## PHASE 3: HOLDOUT (NO RETRAINING, NO TUNING)

### Step A — Holdout Run (`slurm_p3_a_holdout.sh` → `prompt_1_holdout_run.py`)

**The only run on the Phase 3 holdout data (2025-01-01 → 2025-12-31). Never touched before.**

Array job. When `PHASE3_USE_CELL_MANIFEST=True` (default): loads `phase3_deploy_manifest.json` and filters to ~702 selected strategies. Legacy fallback: 11,232 runs.

For each strategy:
1. Load frozen Phase 2 strategy package (SHA256 validated at load time — hard fail if mismatch)
2. Required-key hard-fail if any key missing from package
3. Load Phase 3 OHLCV + feature store
4. Run `generate_timesfm_phase3.py` to get Phase 3 TimesFM features (inference only, no training)
5. Apply AE + Cascade filters (respecting GATE 3 bypass flag)
6. Apply asset admission whitelist filter
7. Run `linucb_context_builder.py` → LinUCB 12-arm contextual bandit selects satellite adjustment (frozen weights from Phase 2.5)
8. Execute selected_exit (from frozen package). On runtime failure → fallback_exit. Column `applied_skip_mechanism` per trade.
9. GATE 2 enforcement: if `uncertainty_calibration_pass=False` → Kelly leverage clamps to 1× (σ/ν not used for sizing). Column `applied_uncertainty_gate`.
10. Simulate trades with full execution realism (fees, funding, slippage, spread, latency, capacity)
11. Per-asset concurrency cap enforced (max 10 per asset, `GLOBAL_CONCURRENCY_SENTINEL=9999`)

**Output:** `reports/phase3/holdout_results.parquet` (raw metrics, NO verdicts)

### Step B — Bootstrap CI (`slurm_p3_b_bootstrap.sh` → `prompt_1b_bootstrap_ci.py`)

Block bootstrap (n=10,000) to compute 95% confidence intervals on Sharpe, Calmar, max-DD. Preserves temporal autocorrelation structure. NOT iid shuffling.

### Step C — Fee Stress (`slurm_p3_c_fee_stress.sh` → `prompt_1c_fee_stress.py`)

Re-runs selected strategies at 2×, 3×, 5× fee multiples. Shows which strategies survive realistic fee increases. Pure diagnostic.

### Step D — Regime Analysis (`slurm_p3_d_regime.sh` → `prompt_2b_regime_holdout.py`)

Breaks down performance by market regime (trending/ranging/breakout/high_vol). Checks if each strategy's claimed specialty holds in the holdout regime distribution.

### Step E — Portfolio Holdout (`slurm_p3_e_portfolio.sh` → `prompt_7_portfolio_holdout.py`)

Tests all selected Phase 2.5 portfolio variants (up to 23 variants) on holdout. For each variant:
- Applies frozen weights (HRP / neural optimizer / Kelly variants 8 and 8b / MetaDD-gated)
- LinUCB satellite adjustments (±20% from HRP base, frozen weights)
- Computes 7 deployment metrics when `trade_log`/`skipped_df`/`weights` provided (backward-compatible)
- **Mandatory variants tested:** variant_8_kelly (Kelly optimal, always in)
- **Evidence-based pruning rule** (post-Phase-3 only): drop variant if `twn_p50 < 0.20 AND sharpe rank bottom-5 AND not sanity anchor`

### Step F — Monte Carlo (`slurm_p3_f_montecarlo.sh` → `prompt_3c_montecarlo.py`)

Permutes trade order within bootstrap to estimate distribution of outcomes. Tests: what is the 5th percentile Sharpe? Could results be due to lucky sequencing?

### Step H — Full Analysis Suite (`slurm_p3_h_analyses.sh`)

Runs prompts 2-10 in sequence:
- **`prompt_2_holdout_analysis.py`** — detailed per-asset holdout breakdown, trade-level attribution
- **`prompt_3_ensemble_contribution.py`** — which of 16 base models contributed most to final returns
- **`prompt_3b_portfolio_combos.py`** — tests combinations of portfolio variants
- **`prompt_4_leverage_analysis.py`** — XGB leverage vs Kelly vs RL side-by-side comparison
- **`prompt_5_model_agreement.py`** — pairwise agreement between base models; detects clustered failures
- **`prompt_6_timesfm_accuracy.py`** — TimesFM calibration accuracy on Phase 3 data
- **`prompt_7_comprehensive_metrics.py`** — full 40+ metric report (Sharpe, Sortino, Calmar, PF, WR, max-DD, CVaR, Ulcer, tail ratio, payoff ratio, etc.)
- **`prompt_8_exit_comparison.py`** — DIAGNOSTIC ONLY: validates pre-committed exit selection vs fixed baseline on holdout. Writes `exit_selection_holdout_validation.json`. NO selection decisions made here.
- **`prompt_10_best_combo.py`** — MetaDD gate: identifies best-performing strategy combo per cell. Reports raw metrics only.

### Step I — Contribution Scorecard (`slurm_p3_i_scorecard.sh` → `prompt_contribution_scorecard.py`)

**DIAGNOSTIC ONLY.** Cross-references Phase 2 gate verdicts vs Phase 3 holdout deltas:
- Per-gate-type accuracy breakdown: MetaCombiner / Uncertainty / AE+Cascade / Exit / Portfolio / Ablation / Leverage
- Per-philosophy generalization summary: helped/hurt/neutral per library
- V2 overfit detector flag: if `hurt:helped ratio > 1.5` → flag as possible overfit
- Writes `reports/phase3/contribution_scorecard.{json,md}`

### Step G — Final Report (`slurm_p3_g_analysis.sh` → `prompt_7_final_report.py`)

**Depends on ALL prior steps (B,C,D,E,F,H,I).** Compiles everything into a single report:
- All 40+ metrics per strategy and per portfolio variant
- Bootstrap CIs, fee stress curves, regime breakdowns, model agreement heatmaps
- **NO automated pass/fail verdict. User decides.**
- Writes `reports/phase3/final_report.{json,md,html}`

### Final — Deployment Report (`prompt_9_deployment_report.py`)

Generates paper-trading configuration for selected strategies:
- Position sizing per asset (Kelly/fixed/selected method)
- Entry/exit thresholds (frozen values)
- Concurrency caps per asset
- Risk limits from MetaDD dd_q95_cal
- Human-readable deployment runbook

---

## Summary Dependency Graph

```
Phase 1 (frozen signal libraries)
         │
         ▼
[PREREQ] reg_sweep + CPCV gate → best_reg_level.json
         │
         ▼
dataset_build (26-asset array)
         │
         ▼
models_train (16 models × 26 assets × 8 folds, GPU)  ←── isotonic_gate
         │
    ┌────┴─────────────────────────────────────────────────────────┐
    │    off-critical: NGBoost, feature_ablation, mc_ablation,     │
    │                  skip_ablation, placebo, cost_stress,        │
    │                  model_ladder, decision_scheme_gp            │
    ▼                                                              │
OOS assembly → MetaCombiner (18 phil) → decision_layer (72 var)   │
         │                                                         │
         ▼                                                         │
overlay/robustness (GATE 2) → sigma_exits → exit_selection ←──────┘
                                          → sizing_selection
         │
         ▼
prompt_6_assembly (global Holm, 1,296 packages, SHA256 freeze)
         │
         ▼
p2_validate (hard gate)
         │
         ▼
Phase 2.5:
run_all (HRP + regime + neural_opt + PySR_DD + Kelly)
         │
    ┌────┴──────────────────┐
    ▼                       ▼
metadd_predictor       gp_portfolio (variant 11)
    │                       │
    └───────┬───────────────┘
            ▼
    portfolio_baselines (Tier-5 gate)
            │
            ▼
    asset_admission (≥2-of-3)
            │
            ▼
    per_cell_selector (468 cells, dynamic-K ≤ 3 → ~702 strategies)
            │
            ▼
    freeze_artifacts (SHA256 manifest — hard gate)
            │
            ▼
Phase 3 (holdout, NO retraining):
    holdout_run (~702 strategies, 2025 data)
         │
    ┌────┴────────────────────────────────────────┐
    ▼      ▼       ▼      ▼         ▼             │
bootstrap  fee   regime   MC   portfolio_holdout  │
    CI    stress                                  │
    │      │       │      │         │             │
    └──────┴───────┴──────┴─────────┘             │
                      │                           │
                      ▼                           │
            full analysis suite (prompts 2-10) ←─┘
                      │
                      ▼
            contribution scorecard (diagnostic)
                      │
                      ▼
            final report (raw metrics, user decides)
                      │
                      ▼
            deployment report (paper-trading config)
```
\
