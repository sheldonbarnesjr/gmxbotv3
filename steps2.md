# Pipeline — Unified Step-by-Step Overview

This document is the comprehensive narrative of how the strategy pipeline transforms raw OHLCV bars into a frozen, holdout-validated set of 864 trading strategies and 20 portfolio variants. It expands the per-phase step-by-step files (`phase2_stepbystep.md`, `phase2_5_stepbystep.md`, `phase3_stepbystep.md`) into one unified reference focused on **why each step exists, how it works, what it produces, and how downstream consumers use it.**

The pipeline is organized into three production phases that run in strict sequence: **Phase 2** turns frozen Phase 1 raw signals into a tradable ensemble of 864 ML-filtered strategies. **Phase 2.5** allocates capital across those 864 strategies through 20 portfolio variants and produces the deployment manifest. **Phase 3** runs every frozen artifact against the untouched 2025 holdout window and reports raw metrics for human inspection. Phase 4 is design-only at this writing.

The whole chain assumes 22 active assets (13 crypto: btc, eth, xrp, ltc, bnb, doge, link, sol, ada, atom, trx, hbar, algo; 9 stocks: aapl, amzn, googl, mcd, meta, msft, nflx, nvda, tsla — all on a 1-hour intraday cadence after the 2026-04-24 WRDS TAQ rebuild) and 12 strategy philosophies. The 12 philosophies × 72 frozen variants per philosophy = **864 frozen strategies**; that count is load-bearing throughout.

Walk-forward cross-validation is universal: `N_SPLITS=8`, `PURGE_BARS=72` (3 days at 1h cadence), `EMBARGO_BARS=24` (1 day). Every train/val/test split anywhere in the pipeline inserts the purge+embargo gap to defend against same-event temporal leakage. The frozen rule is "fold K trains on folds 1..K-1, validates on fold K, purge+embargo bars between them" — no exceptions.

Capital settings are conservative and fixed: `INITIAL_CAPITAL = $1,000`, single-position notional ceiling **$50,000** (so leverage cannot blow past the cap regardless of ensemble confidence), `MAX_LEVERAGE = 20`, per-asset concurrency caps cycled across `[5, 8, 10, 15, 20, 25]` so the holdout reports across cap settings rather than fitting the cap. Concurrency is per-asset; the global concurrency sentinel is disabled.

---

# Phase 2 — ML Ensemble + Variant Assembly

Phase 2 is a multi-stage SLURM chain orchestrated by `slurm/phase2/p2_master_launcher.sh`. The launcher submits the dependency graph (`p2_dataset_build.sh` → `p2_models_train.sh` → `p2_assembly.sh` → `p2_freeze.sh` → `p2_validate.sh`) and exits; from that point the chain self-drives across roughly 14,000 SLURM array tasks.

Phase 2 input is the frozen Phase 1 raw signal libraries (one per philosophy per asset, 12 × 22 = 264 cells), the 200-dim Phase 0 feature stores (LSTM + TFT + Cross-Asset + PatchTST encodings appended to the raw OHLCV indicator panel for ~1,153 columns per bar), and the per-asset Phase 2 OHLCV mirror under `OHLCV/phase2/`.

Phase 2 output is 864 frozen strategy bundles plus the master Phase 3 handoff manifest. Each strategy bundle includes a per-asset config map (which Phase 1 signals fire for which assets), the score-blending recipe, the skip rule, the confidence-bucket boundaries, the size and concurrency overlay, the replacement mode, the exit-stage assignment, and per-fold checkpoints for every component model the strategy depends on.

## Step 1 — Dataset Build (`prompt_1_dataset_build.py`)

Step 1 is the cell-level dataset constructor that every downstream training step keys off. Its job is conceptually simple — replay each frozen Phase 1 raw signal against Phase 2 OHLCV, write a trade table, and tack on the feature matrix at signal-bar timestamps — but the schema contract it emits is load-bearing for the next 8 steps. Anything that downstream training expects to read must be present here.

The script ingests three artifacts per cell. The first is the frozen Phase 1 signal library at `pipeline_state/phase_1/frozen_libraries/{philosophy}/{asset}.json`, which lists every rule the philosophy approved for the asset along with its threshold parameters, take-profit / stop-loss / max-hold settings, and direction. The second is the Phase 2 OHLCV mirror at `OHLCV/phase2/{asset}.parquet`, used to replay each rule deterministically against actual price action. The third is the Phase 2 feature store at `features/phase2/{asset}.parquet`, the ~1,153-column panel produced by Phase 0 from which the trade-level feature matrix is sampled at each signal_bar.

For each rule in the library, the script sweeps the OHLCV window, fires the rule wherever its predicate evaluates true, advances one bar to fill (modeling the realistic next-bar fill), simulates the trade through TP/SL/max-hold, and writes one row per executed trade to `features/phase2/datasets/{asset}_{philosophy}.parquet`. The trade row carries `signal_bar`, `fill_bar`, `exit_bar`, `entry_price`, `exit_price`, `direction`, `holding_bars`, `gross_pnl_pct`, `funding_cost`, `execution_cost`, `net_pnl_pct`, plus signal identity columns (`signal_name`, `signal_family`, `signal_direction`, `signal_category`) and the rule's TP/SL/max-hold parameters. Notably absent is `exit_reason` — this is a circular target leak (the exit reason is downstream of the exit choice the model is being trained to predict), and the MC INPUT BLOCKLIST at Step 4 enforces its absence as a backstop.

The script also emits a quarantine sidecar at `pipeline_state/phase_2/dataset_quarantine.json` listing any signal whose in-window profit factor falls below a defensive PF guard threshold. Downstream training reads this sidecar and excludes quarantined signals from the ensemble, preventing the cascade-saturated PF problem where a single rule with extreme PF dominates per-fold ML and produces an over-optimistic OOS table. The PF guard is part of the live correctness contract for the dataset build.

Step 1 is parallelized as a 22-asset SLURM array (`slurm/phase2/p2_dataset_build.sh --array=0-21`), one task per asset. Per-philosophy parallelism happens implicitly because each task processes all 12 philosophies for its asset together (cheaper to load OHLCV once and replay 12 libraries against it than to schedule 12 × 22 = 264 separate tasks). All 22 array tasks must succeed before Step 2 fires.

## Step 2 — Component Model Training (`prompt_2_*.py` family)

Step 2 is the GPU-heavy core of Phase 2 — the eleven component models that together form the ensemble are trained here, each per-asset, per-philosophy, per-fold. The reason for an eleven-model ensemble instead of a single model is diversity: tree-based learners (XGBoost, LightGBM, CatBoost, NGBoost), recurrent learners (LSTM2, TFT2), attention-based learners (Cross-Asset Transformer #2, TabNet), Bayesian learners (BNN-MC-Dropout), bootstrapped learners (LGB-Bootstrap), and quantile-regression learners (XGB-Quantile) all train on the same trade table but capture different features of the signal. The MetaCombiner at Step 4 sees the 11 outputs and learns which mix wins on out-of-sample data per philosophy.

The eleven trainers each consume the per-cell parquet from Step 1, fit on training folds, and emit per-fold OOS predictions. `prompt_2_model_train.py` handles the XGBoost head — the first-line classifier whose probability score is the strongest single feature of the ensemble. `prompt_2b_timesfm_features.py` produces TimesFM forecast features (price-truth quantiles at 4h/12h/24h horizons) and a per-cell calibration sidecar; the TimesFM weights are frozen Phase 0 artifacts and are not retrained here. `prompt_2c_lstm_classifier.py` trains LSTM2 with attention, initializing the encoder layers from the Phase 0 LSTM pretrained checkpoint at `models/pretrained/lstm_pretrained.pt` when the input dim matches. `prompt_2d_tft_scorer.py` and `prompt_2e_cross_asset_scorer.py` train the TFT and Cross-Asset Transformer scorers; the Cross-Asset Transformer specifically attends across all 22 assets simultaneously to capture inter-market structure that single-asset models cannot.

`prompt_2e_lightgbm.py` and `prompt_2f_catboost.py` train tree-based diversifiers — same target as XGB, different inductive biases. `prompt_2i_xgb_quantile.py` trains a quantile head that regresses on conditional return quantiles rather than win/loss; this gives MetaCombiner a richer signal than a binary approval probability. `prompt_2k_lgb_bootstrap.py` trains a bootstrapped LGB head whose multiple bagged predictions encode predictive variance. `prompt_2l_bnn_mc_dropout.py` trains a Bayesian NN with `MC_SAMPLES=100` Monte Carlo dropout passes at inference; the variance of the 100 samples is the model's intrinsic uncertainty signal. `prompt_2f_leverage_model.py` is the only regression head — it predicts optimal leverage (in 1×–20× range) given upstream quality scores, which Phase 3 sizing then uses to scale position notional.

Every trainer respects the walk-forward purge and embargo. Scalers, imputers, and any model-internal preprocessing are fit on the train fold only and applied frozen to the validation/test folds. Each fold's checkpoint is written atomically (via temp-file + rename) to `models/phase2/{asset}_{philosophy}_{component}_fold_{K}.pt` (or `.json` for tree models, `.cbm` for CatBoost, `.pkl` for NGBoost). Each fold's OOS predictions are stitched chronologically by Step 3.

Step 2 runs as a 22-asset SLURM array with `--gres=gpu:h100:1`, `--cpus-per-task=32`, `--mem=128G`, and `--time=12:00:00`. Off-critical-path companions like `ngboost_train.sh` and `prompt_2_5_metasweep.py` (the meta-sweep that picks the best `REG_LEVEL` for the regime detector via `select_best_reg_level_v2.py`) fire on completion. The bottleneck is GPU time on the recurrent and attention models — typically ~6-8 hours per asset on a single H100.

## Step 3 — OOS Assembly (`prompt_3_oos_assembly.py`)

Step 3 stitches all the per-fold OOS predictions from Step 2 into one unified table, first per-asset and then cross-asset per philosophy. This table is the single source of truth that the MetaCombiner at Step 4, the decision layer at Step 5, and the variant overlay at Step 6 all read. Without it, the 11 component models would each produce isolated per-fold parquets that the ensemble could not consume.

The script reads `models/phase2/{asset}_{philosophy}_{component}_fold_predictions.parquet` for every component model and every OOS fold. The merge contract is `(signal_id, fold)` composite key — using `signal_id` alone is insufficient because the same signal produces predictions across multiple folds, and a single-key merge would either Cartesian-explode or silently overwrite at fold-K boundaries. The composite key plus a defense-in-depth rowcount-delta assert (raises `[D4 H25] WARNING` if post-merge rowcount diverges by more than 1% from pre-merge) prevents both failure modes.

TimesFM features merge in via `entry_time` joined to the TimesFM index after explicit dtype-unit assertion: if `entry_time` looks like a bar index (small integer) but the TimesFM index is datetime, the script raises rather than silently producing all-NaN rows. The unit ambiguity has bitten the pipeline before; the assertion is a correctness backstop.

The script writes `pipeline_state/phase_2/oos_unified/{asset}_{philosophy}.parquet` per cell, plus `pipeline_state/phase_2/oos_unified/{philosophy}_cross_asset.parquet` in `--merge` mode (this latter run is invoked once all 22 per-asset tasks succeed). Score-distribution histograms and classification-vs-expected-PnL correlation stats are written alongside as integrity JSON, providing an automated sanity check that the merged predictions are not pathologically degenerate.

The per-asset run is invoked by the per-asset assembly array (`slurm/phase2/p2_assembly.sh --array=0-21`); the cross-asset `--merge` invocation runs inside `slurm/phase2/p2_freeze.sh` after every philosophy's per-asset table is built. Step 4 cannot fire until all 12 cross-asset tables exist.

## Step 4 — MetaCombiner Training (`prompt_3b_metacombiner.py`)

The MetaCombiner is the highest-leverage and highest-leakage-risk component of the ensemble. It is a small MLP — 20 inputs (the 11 component model outputs plus 9 reliability/context features) → 32 hidden → 16 hidden → 1 sigmoid — trained with SmoothL1 loss against vol-scaled targets. Its single output is the **MC approval score**: a per-trade probability that the trade clears the philosophy-level quality bar. Every downstream gate (decision layer, variant overlay, exit selection) reads this score.

The MetaCombiner trains on the cross-asset OOS table from Step 3 in a two-level walk-forward. The outer loop is the standard 8-fold CV; the inner loop is "for fold K, train on the unioned OOS predictions of folds 1..K-1, validate on fold K-1, score fold K." This double-WF structure means that MetaCombiner predictions for fold K are honestly out-of-sample with respect to the component models' fold-K predictions — a single-level WF would silently leak fold-K component predictions into the MC training set.

Fold 1 is **intentionally NaN** in the output. There are no folds before fold 1 to train on, so the MetaCombiner cannot produce a score for it without contaminating subsequent folds. This NaN-by-design is documented in the schema contract; downstream code treats fold-1 trades as missing-MC-score and skips them rather than substituting a fallback. The decision layer's robustness checks specifically validate that fold-1 MC scores remain NaN.

The clean training label is `net_pnl_pct_at_fixed_exit` — explicitly NOT `net_pnl_pct`, which is contaminated by selected-exit overlay choice. The fixed-exit substrate gives every philosophy the same PnL definition regardless of which exit overlay it chooses downstream, so MetaCombiner is comparing apples to apples across philosophies. The MC INPUT BLOCKLIST is a substring filter that fires at training entry and rejects any column whose name contains `rel_exit`, `selected_exit`, `exit_advantage`, `exit_dispersion`, `sigma_exit`, `dist_exit`, or `_shadow_`. `exit_reason` is also blocked. Any matching column raises `ValueError` before the first training batch — there is no graceful skip, because exit-derived columns cannot enter MC inputs in production under any circumstances.

The MetaCombiner also has 80/20 train/val tail split for early stopping. The split inserts a `PURGE_BARS + EMBARGO_BARS = 96` bar gap between train tail and val head — without the gap, autocorrelation in the score sequence would leak the early-stopping signal across the boundary.

Step 4 outputs `models/phase2/metacombiner_{philosophy}_fold_{K}.pt` (one checkpoint per fold per philosophy, fold 1 produces no checkpoint) plus the merged predictions parquet at `pipeline_state/phase_2/metacombiner/{philosophy}_predictions.parquet`. The decision layer at Step 5 and the exit-overlay bake-off at Step 7 both consume the merged-predictions parquet directly. Step 4 runs once per philosophy inside `p2_freeze.sh` after the cross-asset merge completes.

## Step 5 — Decision Layer (`prompt_4_decision_layer.py`)

The decision layer turns the MC approval score into a binary take-or-skip decision. It searches over three orthogonal axes and keeps the top three combinations as the score-scheme axis of the 72-variant grid that Step 6 overlays.

The first axis is score blending: how much weight to give the MC approval score versus individual component-head scores. The second axis is the skip rule: a percentile threshold below which trades are skipped (e.g. skip the bottom 30% of MC scores). The third axis is the confidence-bucket scheme: how to partition the surviving trades into low/medium/high confidence buckets that downstream sizing reads. The script tests `MAX_COMBINED_SCORING_SCHEMES=3` candidates per axis, takes the cartesian product, and ranks the resulting schemes by composite OOS metric.

Bucket boundaries are picked on the inner-train half of the philosophy's OOS data, then the same boundaries are re-evaluated on the inner-holdout half to detect overfit cuts. Six new keys per scheme stamp the result: `inner_train_bucket_pf_spread` and `_monotonicity` from inner-train, `inner_holdout_bucket_pf_spread` and `_monotonicity` from inner-holdout, `inner_holdout_spread_retention` (ratio of holdout-spread to train-spread), and `inner_holdout_bucket_sane` (boolean — true iff `holdout_spread ≥ 0.5 × train_spread` AND `holdout_monotonicity != "no"`). The sanity stamps are stamp-only at this revision; downstream composite-score discount on insane buckets is deferred until Phase 2 produces a real stamp distribution.

The script writes `pipeline_state/phase_2/prompt_4_schemes_{philosophy}.json` containing the three frozen scheme specs plus per-scheme OOS metrics. A philosophy-agnostic fallback is written to `prompt_4_schemes.json`. Step 5 runs once per philosophy in `p2_freeze.sh`. The three schemes per philosophy are the score-scheme axis of the 72-variant grid Step 6 builds.

## Step 6 — Variant Overlay (`prompt_5_overlay.py`)

Step 6 takes each of the three decision schemes and overlays the size, concurrency, replacement-mode, and exit-stage axes to produce 72 frozen variants per philosophy. The combinatorics are straightforward: 3 score schemes × 3 size/concurrency overlays × replacement mode (3 values: `close_at_market`, `skip`, `swap_by_expected_pnl`) × exit-stage axis (`EXIT_STAGE_AXIS=[1, 2]`) — the 72 cell counts fall out as 3 × 3 × 8 modulo a few explicit pruning rules.

Position sizing in each variant assumes `INITIAL_CAPITAL=$1,000`, compounding (so position notional grows with equity), with a hard `$50,000` single-position cap (so leverage cannot exceed the cap regardless of MC confidence or Kelly fraction). Concurrency caps come from `CONCURRENCY_CAPS = [5, 8, 10, 15, 20, 25]`. The replacement-mode axis governs what happens when a new high-confidence signal fires while the per-asset cap is full: `close_at_market` closes the lowest-confidence open position; `skip` declines the new signal; `swap_by_expected_pnl` closes the open position with the lowest expected residual PnL. The exit-stage axis selects between Stage-1 (single-fire exit at TP/SL/max-hold) and Stage-2 (per-fold `selected_exit_by_fold` reading).

Each variant gets a unique `variant_number` post-emit (`len(all_variants) + 1`); the legacy group ID is preserved as `variant_group`. Uniqueness is asserted at end-of-run — a duplicate would cascade to package-filename collisions at Step 8 and fold-mismatch at Phase 3. The script emits one frozen-config JSON per variant under `configs/frozen/{philosophy}/{variant_id}.json`, plus a per-philosophy summary table at `pipeline_state/phase_2/prompt_5_variants_{philosophy}.parquet`. Each record carries `variant_id`, `variant_number`, `variant_group`, `scheme_label`, `risk_profile`, `exit_stage`, `replacement_mode_axis`, and the full frozen-spec dict.

All 72 variants per philosophy proceed to Step 7. No variant is dropped here. Across 12 philosophies this materializes 864 frozen-spec JSONs that Step 8 packages into per-strategy bundles.

## Step 7 — Robustness Audit + Per-Strategy Calibrations

Step 7 is a fan-out of 7 quasi-independent sub-steps that all consume the variant configs from Step 6 plus the MetaCombiner trade universe and emit calibration sidecars Phase 3 reads at execution time. They run in parallel where possible; the main critical-path entries are:

`prompt_5b_robustness_audit.py` validates each variant under Hansen SPA and Holm-Bonferroni multiple-testing correction with stationary block-bootstrap p-values (the block length comes from the asset-specific shared block-length cache at `scripts/common/utils/block_permutation.py::get_shared_block_length`, which estimates the per-asset autocorrelation horizon to defend against iid-bootstrap underestimation of variance). SPA p-values are reported but Holm is the binding filter for the family-wide significance assessment.

`prompt_exit_selection.py` and `prompt_exit_selection_per_strategy.py` together pick the best exit overlay from `CHALLENGERS = ["atr_scaled", "sigma_exit", "gp", "dist_exit"]` against the `fixed` baseline. Selection is fold-indexed: `selected_exit_by_fold[K]` is computed using only folds 0..K-1 to avoid leaking fold-K results back into the fold-K decision. Sigma-exit is kept in the selection registry but ships shadow-only (its predictions are computed but never trade); per-bar replay would change this but is gated on per-bar replay infrastructure that lives outside Phase 2.

`prompt_sizing_selection.py` and `prompt_rl_sizing.py` together fit the RL-based sizing model (a contextual policy that maps quality score + recent-volatility + concurrency-utilization to a position-size fraction) and the analytic Kelly head. The Kelly fit applies Bayesian shrinkage toward the prior BEFORE the divisor clip, so high-edge tails are not saturated to 1.0 before the prior pulls them back. The RL training universe is `PHASE2_ACTIVE_ASSETS=22` (not `ASSETS=26`); a flag-gated label-substrate swap (`RL_LABEL_SUBSTRATE=overlay_exit`) lets the user pull `net_pnl_pct_at_fixed_exit` rather than the contaminated `net_pnl_pct` once the RL policy is retrained on the clean substrate.

`prompt_2n_distribution_exit.py`, `check_timesfm_calibration_per_cell.py`, and `tune_dist_exit_threshold.py` together produce the dist_exit calibration sidecar at `pipeline_state/phase_2/tfm_calibration_per_cell.json`. The grid is `(asset × direction × horizon × regime)` = 22 × 2 × 3 × 6 = **792 cells**. For each cell the script records the Wilson 95% lower-confidence-bound on direction accuracy, the calibration slope (regression of forecast quantile on realized return), coverage rates at 90% and 95%, plus a 4-tier pooled-fallback ladder for cells with too few trades — `n ≥ 80 / 200 / 350 / 600` thresholds determine whether the cell uses its own calibration, pools across direction, pools across direction-and-horizon, or pools across the full ladder. The dist_exit gate is **deny-default**: cells without a passing entry are ineligible to use dist_exit; the variant falls back to the `fixed` exit. The fold-indexed `cells_by_fold` structure inside the sidecar is what Phase 3 reads at execution time.

`prompt_5b` also writes `configs/frozen/exit_selection.json`, `configs/frozen/sizing_selection.json`, `configs/frozen/dist_exit_thresholds.json`, `configs/frozen/timesfm_calibration_report.json`, plus per-variant audit JSONs under `pipeline_state/phase_2/audit/`.

## Step 8 — Final Assembly (`prompt_6_assembly.py`)

Step 8 is the final Phase 2 packaging stage. It runs once after every philosophy has completed Steps 1-7, verifies that all `NUM_PHILOSOPHIES × NUM_VARIANTS_PER_LIBRARY = 864` frozen-config JSONs exist, packages them into per-strategy bundles, and writes the master handoff manifest for Phase 3.

The script reads `configs/frozen/{philosophy}/{variant_id}.json` × 864, plus the global calibration sidecars (`exit_selection.json`, `sizing_selection.json`, `dist_exit_thresholds.json`, `tfm_calibration_per_cell.json`) and the audit outputs from Step 7. It emits the master manifest at `pipeline_state/phase_3/phase3_handoff_manifest.json` with per-strategy bundle paths, SHA pins for every frozen config the strategy depends on, and chmod 0o444 (read-only) on every packaged file. A post-chmod write-bit verification pass detects races where a late writer might re-touch the file with default permissions; if any package is left writable, the script raises rather than ship an unfrozen artifact.

A global Holm-Bonferroni step at the tail of the assembly aggregates exit-selection, sizing-selection, and MC-input ablation p-values into one family of ~M tests, applies Holm correction across the entire family, and stamps `selection_sig_global` on each test record at `pipeline_state/phase_2/selection_pvalues.jsonl`. This catches the case where individual per-step Holm passed but the family-wide correction at production-run scale tightens the critical p-value enough to push some tests above the threshold. A generic per-step Holm sidecar collector at `pipeline_state/phase_2/holm_inputs/*.jsonl` lets new D2 child sites (cell selector, ensemble ablation, leverage binomial, model agreement, TimesFM accuracy, sizing 36-grid, metasweep 15-cell) drop p-values into the global Holm pool without schema break.

Step 8 runs at the tail of `p2_freeze.sh` and is gated by every prior step in the chain. Failure at this step aborts the chain before `p2_validate.sh` fires.

## Step 9 — Validation Gate (`p2_validate.sh` + `validate_phase2_outputs.py`)

Step 9 is the final gate before Phase 2.5. It enforces that the chain actually produced what the manifest claims it produced, that no Phase 3 data was accidentally accessed during Phase 2, and that the OOS prediction tables carry the ensemble columns the bundle metadata advertises.

The script reads the master manifest from Step 8 plus every referenced bundle file. It checks that all 864 frozen configs exist, that every component-model checkpoint is on disk and loadable, that MetaCombiner fold coverage is `[2..N_SPLITS]` (fold 1 NaN by design), that no Phase 3 OHLCV path appears in any Phase 2 experiment registry row, that the OOS tables for each philosophy carry every component column the bundle metadata claims, and that the handoff manifest is internally self-consistent (each row references files that exist and match the SHA pin).

It emits `pipeline_state/phase_2/p2_validate_report.json` with pass/fail per check. On failure, the chain stops and Phase 2.5 does not launch — the user is expected to diagnose, fix, and re-run from the failed step. On success, `slurm/phase2_5/p25_master_launcher.sh` is fired with `--dependency=afterok:$JID_VAL`.

## Phase 2 wall-clock

Per `p2_master_launcher.sh`, an unblocked Phase 2 run on 8 nodes × 4 H100s completes in roughly **9 days end-to-end**. The 8-stage Quartz-dependency variant (`launch_phase2.sh`) compresses the critical path to **~15 hours** across ~14,000 array tasks when GPU allocation is unblocked. The bottleneck is Step 2 (component model training) on the recurrent and attention models; the rest of the steps sum to under 8 hours.

---

# Phase 2.5 — Portfolio Construction

Phase 2.5 turns the 864 frozen Phase 2 strategies into 20 distinct portfolio variants and emits the cell-selected manifest that drives Phase 3 holdout. The variant taxonomy is defined in `select_phase3_candidates.py::VARIANT_REGISTRY`: 14 primary variants (the production allocators), 4 soft-kill diagnostic baselines that ship for comparison only and do not consume Phase 3 capacity, and 2 future-deferred LinUCB overlays excluded entirely from enumeration (`variant_7c`, `variant_8c`). The block-bootstrap baselines emitted by `prompt_7g_portfolio_baselines.py` round out the comparison set.

The per-cell selector at the end of the chain flattens the deployment universe to a 468-cell `(asset, library)` matrix and picks one (portfolio_variant, strategy_variant, exit_stage, strategy_id) per cell, cutting the universe by roughly 94% from the legacy 11,232-row cross product down to about **702 deployed strategies** when `PHASE3_USE_CELL_MANIFEST=True`. Phase 3 reads this manifest as a deny-default whitelist — no strategy outside the manifest fires on holdout.

## Step 1 — HRP Base + LinUCB Bandit (`prompt_7_portfolio_construction.py`)

The portfolio chain opens with hierarchical risk parity and the LinUCB regime-adaptive bandit. HRP performs hierarchical clustering on the strategy correlation matrix (computed from Phase 2 OOS-fold returns), then walks the cluster tree from leaves to root distributing inverse-variance budget. The result is two HRP books: a conservative HRP-A book that tightens the leaf-cluster cap (max weight per cluster lower than equal-weight) and an aggressive HRP-B book that lifts the cap to allow more concentration on tight-correlation winners. Both books are exported as `hrp_conservative_weights.parquet` and `hrp_aggressive_weights.parquet` under `pipeline_state/phase_2_5/`.

The LinUCB layer is a satellite overlay on top of HRP, not a replacement for it. The context dimension is `LINUCB_CONTEXT_DIM=18`: 4 base regime z-scores (vol, return, correlation, breadth) + 5 EXTRA_REGIME_FEATURES (market/portfolio state) + 4 deterministic regime one-hots + 5 LSTM PCA components. Arms are the regime philosophies (`NUM_REGIMES=6`). At each timestep the bandit predicts a UCB score per regime, picks the highest, and applies a small per-asset weight tilt (typically ±20%) on top of the HRP base. Anti-whipsaw protection enforces a minimum hold-days window per arm and a transitions-per-window cap, so the bandit cannot whipsaw between regimes on noisy daily transitions.

Bandit state is **reset between training and serialization** so the persisted pickle replays deterministically — without the reset, the in-memory state carries the last training-fold transition timestamps and the deserialized bandit at Phase 3 inference would behave non-deterministically depending on training seed. The reset is also re-applied on entry to `apply_bandit_overlay` in `linucb_context_builder.py` as defense in depth.

The 50/50 blend variant averages HRP-A with HRP-B-plus-LinUCB to give a middle book that still respects the HRP correlation structure but captures some of the LinUCB regime adaptation.

## Step 2 — Neural Optimizer (`prompt_7c_portfolio_optimizer.py`)

The neural portfolio optimizer is the learned weight predictor — a small NN that ingests per-strategy expected-return and risk features alongside regime descriptors and emits a learned weight vector. Input dimension is `NEURAL_OPT_INPUT_DIM=103`, decomposed as `PORTFOLIO_TOP_K=75` strategy slots (the Ridge stage upstream picks the top 75 strategies by composite OOS metric) plus `N_REGIME_FEATURES=28` regime descriptors (4 base + 15 LSTM encoder summaries + 4 regime one-hots + 5 EXTRA_REGIME_FEATURES). A module-level invariant assert at import time fails loud if the config constant disagrees with the runtime breakdown — drift between the two is the silent-mismatch class the C6 audit caught.

The model itself is a bagged ensemble: `N_ENSEMBLE_MODELS` independent SmallPortfolioNet instances, each trained on a stationary block bootstrap sample of the training data (block length = `PURGE_BARS / 24` days). The block bootstrap respects autocorrelation in daily returns, where iid bootstrap-with-replacement would destroy the structure the model is trying to learn. The ensemble's prediction is the average of the bagged predictions; the per-bag predictions also encode the model's aleatoric uncertainty.

Inputs include `metacombiner_oos_predictions.parquet`, the regime feature panel, and the HRP weights from Step 1. The training window is purge/embargo-gapped: 80% train, then a `_gap_d = max(1, PURGE_BARS / 24) + max(1, EMBARGO_BARS / 24)` day gap, then 20% val. Outputs land at `pipeline_state/phase_2_5/neural_optimizer_weights.parquet`, an ensemble manifest JSON listing each bagged checkpoint with its dim manifest sidecar, and `neural_optimizer_state_dict.pt`. The loader at `load_portfolio_optimizer_with_check` validates `state["fc1.weight"].shape[1] == expected_input_dim` before every forward pass; checkpoint dim drift raises `RuntimeError` rather than silently zero-padding.

The optimizer feeds variant_7 (`neural_optimizer`) directly. It is also consumed by the MetaDD overlay (variant_7b) and the inverse-uncertainty blender (variant_10). Output weights are normalized to sum to 1 and clipped to the per-asset concurrency cap downstream.

## Step 3 — Kelly Walk-Forward (`prompt_7e_kelly_portfolio.py`)

Kelly-fraction portfolio sizing runs as a walk-forward routine producing variant_8 (the analytic Kelly portfolio). The implementation respects month-boundary embargo: each test month is fit on a trailing window with the last `_gap_d` days of the prior month explicitly embargoed before the test month begins. Without the embargo, trades whose holding period spans the month boundary would leak into the Kelly fit for that month.

The per-strategy Kelly fraction is computed under a Student-t distribution assumption with `nu` clipped to `[2.01, +inf]` (avoiding the divergent variance below ν=2). The raw Kelly fraction `f = mu / sigma²` is then multiplied by a tail adjustment that shrinks the fraction proportionally to t-tail thickness. The Bayesian shrinkage (lambda=0.5 toward a 0.25 prior, i.e. quarter-Kelly) is applied **before** the divisor clip — applying after the clip would saturate high-edge tails to 1.0 before the prior could pull them back. The final fraction is projected onto the per-asset concurrency budget so the analytic prediction respects the real position-cap constraints Phase 3 enforces.

Inputs are the Phase 2 strategy-level fold returns and the MC approval mask. Outputs are `variant_8_kelly_weights.parquet` and `variant_8_kelly_returns.parquet`. The Kelly walker emits per-month weight vectors that the MetaDD overlay scales by inverse `(1 + dd_sigma)` to produce variant_8b. The companion dispatcher `canonical_allocator_extensions.py` provides additional Kelly-family routines (`variant_19_kelly_max`, `variant_20_kelly_r1r2`, `variant_22_half_kelly_eta`) for the baseline expansion at Step 6.

## Step 4 — Student-t MetaDD Predictor (`prompt_7f_metadd_predictor.py`)

The MetaDD predictor produces forward drawdown forecasts with a Student-t output head. For each portfolio day, the head emits five quantities: `dd_mu` (location), `dd_sigma` (scale), `dd_nu` (degrees-of-freedom shape proxy for tail thickness), and the upper quantiles `dd_q90` and `dd_q95`. Pre-committed thresholds in the overlay code mark `nu < 5.0` as a fat-tail regime, `dd_sigma > 0.08` as high-uncertainty, and `dd_q90 > 0.05` as an elevated crash-risk regime. These thresholds drive the overlay siblings at Step 5.

Inputs are the realized portfolio drawdown trajectories from each base variant's walk-forward returns plus the regime feature panel. The training split is purge-embargo-gapped (PURGE_BARS+EMBARGO_BARS bars between train tail and val head). The output is `metadd_predictions.parquet` with the five Student-t columns indexed by date.

The MetaDD predictor is the canonical drawdown forecaster but companion drawdown-quantile heads (`prompt_7f_gp_drawdown.py`, `prompt_7f_lgb_bootstrap_dd.py`, `prompt_7f_xgb_quantile_dd.py`, `prompt_7d_pysr_drawdown.py`) provide model-diversity ensembles for ablation comparison. The MetaDD predictor's outputs feed Step 5; the others are diagnostic.

## Step 5 — MetaDD Overlay Siblings (`prompt_7_metadd_overlay.py`)

The MetaDD overlay replays each of the 8 base variants through a pre-committed modification rule and emits its `_metadd` twin, then adds two MetaDD-only variants (`variant_9_dd_capped_neural`, `variant_10_portfolio_blender`) for a total of 10 pair artifacts. The modification siblings each apply a different transformation:

- `_cap_by_q90` caps each strategy's weight at the `dd_q90` ceiling per day, then renormalizes.
- `_scale_by_q90` scales the entire weight vector by `1 - 2 × dd_q90` when `dd_q90 > 0.05`, holding HRP structure but dialing down on elevated tail risk.
- `_flatten_by_sigma` pushes high-`dd_sigma` HRP-A weights toward equal-weight via a smooth interpolation; the floor is `MIN_WEIGHT_SCALE=0.40` of HRP, the ceiling is `MAX_BLEND_SHIFT=0.30` toward equal.
- `_shrink_linucb_by_nu` shrinks the LinUCB satellite deltas toward zero when `dd_nu < 5.0`, recognizing that fat-tail regimes are exactly when the bandit's confidence is least trustworthy.
- `_shift_toward_conservative` shifts HRP-B mass toward HRP-A when `dd_nu` is low — same fat-tail logic as the LinUCB shrink but applied to the aggressive book.
- `_adjust_ab_blend` tilts the 50/50 ratio by `dd_nu` so fat-tail regimes get more conservative weight.
- `_scale_kelly_by_sigma` scales the Kelly weights by `1 / (1 + dd_sigma)`, dialing back leverage on high-uncertainty days.
- `_scale_neural_by_sigma` does the same for the neural optimizer weights.
- `_inverse_uncertainty_blend` blends all base variants with weight inversely proportional to `dd_sigma`, so confident-low-uncertainty variants get more allocation.

Every overlay function applies `.shift(1)` to the MetaDD inputs before reindexing — the day-T overlay decision uses MetaDD predictions from day T-1 only. Without the shift, the overlay would use MetaDD predictions for day T to decide day-T weights, which is logically circular even if the predictor itself is causal. The shift is enforced in 7 sibling functions plus `_cap_by_q90`.

A walk-forward calibration gate flags every `_metadd` variant whose calibration slope on out-of-sample days falls outside `[0.75, 1.25]`. Flagged variants are still written to the artifact dir but `select_phase3_candidates.py` filters them at Step 9; their slot stays empty rather than being filled from the soft-kill bucket, so Phase 3 reflects the actual MetaDD reliability.

## Step 6 — Block-Bootstrap Baselines (`prompt_7g_portfolio_baselines.py`)

The baseline stage emits 12 block-bootstrap reference allocations so the model-driven variants have honest non-trivial benchmarks. The block length is sampled from a stationary geometric distribution with mean tuned to the autocorrelation horizon of daily returns (typically ~20 days for crypto-1h aggregated to daily, ~5 for stocks). The baseline weights are equal-weight, inverse-vol, mean-variance, risk-parity-notional (`variant_21`), HERC (`variant_23`), plus several cluster-tree perturbations.

The TUNE/EVAL split inside the baseline emit is also purge-embargo gapped: the first 50% TUNE half ranks strategies by Sharpe; the last 50% EVAL half (with a `_gap_d` day gap before it) reports out-of-sample portfolio returns for the ranking decision. Without the gap, TUNE-tail ranking signal would leak into the EVAL-head metric.

These baselines never get MetaDD twins — they exist purely to anchor SPA and Holm-Bonferroni gates. If a learned variant (variant_7, variant_8, variant_9, variant_10) cannot beat the strongest non-trivial bootstrap baseline at Holm-corrected significance, the user should treat it as adding no real edge over the simpler comparison.

## Step 7 — Asset Admission Gate (`prompt_7h_asset_admission.py`)

The asset admission gate enforces the canonical `PHASE2_ACTIVE_ASSETS` whitelist of 22 assets (13 crypto plus 9 stocks). Each asset must clear a 2-of-3 pass rule on three independent metrics: `oos_sharpe`, `fold_win_rate`, and `worst_regime_sharpe`. If at least 2 of the 3 pass their thresholds, the asset is admitted; failing this rule excludes the asset from per-cell selection at Step 8.

The thresholds come from `KILL_RULES` in config: `asset_min_oos_sharpe=0.50`, `asset_min_fold_win_rate=0.50`, `asset_min_worst_regime_sharpe=0.00`. The thresholds are pre-committed and the rule is published in code, so the user cannot tune them after seeing OOS results without explicit code change + commit hash record.

Inputs are the per-asset OOS metric tables aggregated across philosophies. The script hard-fails when the source metric parquet is missing per-asset rows — no silent fallback to a 26-asset universe; the fallback is restricted to `PHASE2_ACTIVE_ASSETS` (22) and only triggers in genuinely-broken cases. Output is `asset_admission_report.parquet` with one row per asset and pass/fail flags per metric, plus an `admitted_assets.json` consumed by the per-cell selector. The whitelist is written via atomic-replace at `configs/frozen/phase3_asset_whitelist.json` with `schema_version: 2` for forward-compatible reader-side validation.

## Step 8 — Per-Cell Selector (`prompt_7j_per_cell_selector.py`)

The per-cell selector flattens the deployment universe to a 468-cell `(asset, library)` matrix — 22 admitted assets times approximately 12 libraries per asset (a few asset-library pairs are vacant where Phase 1 found no surviving signals). For each cell, the selector picks the dominant `(portfolio_variant, strategy_variant, exit_stage, strategy_id)` based on per-cell OOS metrics, then writes the deploy manifest.

Selection inside the cell uses a dynamic-K rule (`select_dynamic_k`): always keep the top-1 by composite OOS metric; keep top-2 and top-3 only if the score gap is under 5% AND the bootstrap p-value (one-sided paired bootstrap, autocorrelation-respecting block length) exceeds 0.10. This means low-confidence ties keep multiple candidates but high-confidence winners narrow to 1. Each winner record carries `exit_stage` and `strategy_id` (canonical format `{asset}__{library}__{pv}__{sv}__es{N}`) so Phase 3 can resolve the execution path without re-deriving identifiers.

Inputs are the variant returns and weights from Steps 1-6, the asset-admission JSON, and the Phase 2 strategy registry. Outputs are `phase3_deploy_manifest.parquet` (or JSON) with rows of schema `(asset, library, portfolio_variant, strategy_variant, exit_stage, strategy_id, schema_version)`, plus a companion `cells_by_fold` calibration sidecar that mirrors the Step-7 dist_exit calibration structure but at portfolio-cell granularity.

When `PHASE3_USE_CELL_MANIFEST=True`, the manifest cuts the universe by roughly 94% from the legacy 11,232-row cross product down to about **702 deployed strategies**. Phase 3 holdout reads the manifest as a deny-default whitelist; any strategy not in the manifest is silently skipped on holdout. The schema-version field gives the reader-side validator a forward-compat hook; readers expecting `schema_version: 2` (the `rows`-key layout) hard-fail on legacy `cells`-key payloads with an explicit message rather than silently coercing to empty.

## Step 9 — Final Variant Filter (`select_phase3_candidates.py`)

The final variant filter reads `VARIANT_REGISTRY` and partitions variants into three buckets: `PRIMARY_VARIANTS`, `SOFT_KILL_VARIANTS`, and `FUTURE_DEFERRED_VARIANTS`. Soft-kill variants ship for diagnostic comparison only and do not consume Phase 3 capacity quota — they exist so the user can read holdout numbers and decide whether the soft-kill candidate has revived. Future-deferred entries (`variant_7c`, `variant_8c`) are excluded from enumeration entirely so the runner cannot accidentally try to score an unimplemented overlay.

The selector also applies the MetaDD calibration gate to every `_metadd` variant: if `calibration_slope` is outside `[0.75, 1.25]` the variant is dropped from the primary bucket. A primary variant that fails the gate may NOT be replaced from the soft-kill bucket — the slot simply stays empty so Phase 3 reflects the actual MetaDD reliability rather than masking it via cherry-picked replacement.

Inputs are all `<variant_id>_returns.parquet` files plus the MetaDD calibration metadata. Output is `phase3_candidates.json` enumerating which variants enter holdout, with their status tag preserved (PRIMARY / SOFT_KILL / FAILED_CALIBRATION) for the assembly stage at Phase 3 entry.

## Phase 2.5 wall-clock

End-to-end Phase 2.5 runs in 6-10 hours on a single Hopper H100 node. The neural optimizer bagged ensemble fit (~2-3 hours) and the MetaDD predictor walk-forward (~2 hours) dominate the timeline. HRP, LinUCB training, Kelly walk-forward, baselines, asset admission, per-cell selection, and final filtering together complete in under 2 hours.

---

# Phase 3 — Holdout Validation

Phase 3 runs the 864 frozen Phase 2 strategies (and their 20 portfolio variant wrappings from Phase 2.5) on the untouched 2025 holdout slice. The phase performs **no retraining and no threshold tuning**. It loads the frozen logic, executes against held-out OHLCV plus Phase 3 TimesFM features, and reports raw metrics. The user inspects the resulting reports and decides whether to deploy. If the holdout metrics are poor, the philosophy is that the failure must be reported honestly and not "fixed" by changing logic.

The phase is a 10-step SLURM chain orchestrated by `slurm/phase3/run_phase3_full.sh`. Pre-flight validation runs first; if it fails the chain halts before any compute is spent. The main holdout execution is the most expensive step (12-18 hours), and the analyses (regime, ensemble, leverage, agreement, TimesFM accuracy, portfolio, exit comparison) all run after holdout completes — they are read-only consumers of the holdout output.

## Step 0 — Pre-flight (`validate_phase3_inputs.py`)

The pre-flight gate runs before any holdout work begins. Its purpose is to fail fast when the handoff bundle is incomplete or contaminated, so a multi-day SLURM allocation does not burn slots on a bad input set. The script executes between 9 and 14 named checks depending on which optional bundles are present in the run tree.

The checks include the snapshot SHA digest (every Phase 3 OHLCV mirror must match the immutable snapshot manifest); presence of every frozen model file (TimesFM checkpoint, MetaCombiner per-philosophy weights, LinUCB pickle, MetaDD checkpoint, Neural optimizer state dict); the 22-asset whitelist enforced against `PHASE2_ACTIVE_ASSETS`; the four dimensional regime label columns (`regime_label_trend`, `regime_label_vol`, `regime_label_composite`, `regime_label_phase`) present in every Phase 3 feature store; absence of any Phase 3 contamination row in earlier-phase experiment registries (a row in the Phase 1 or Phase 2 registry that references a Phase 3 OHLCV path is a red-flag indicator that a developer ran Phase 1/2 against Phase 3 data); the exit-config SHA pins matching the handoff manifest; cell-deploy manifest internal consistency including schema-version validation; frozen-artifact read-only mode (every packaged config is `chmod 0o444`); and disk-space headroom (5 GB FATAL, 10 GB WARN).

The script reads the Phase 2 and Phase 2.5 handoff packages, the per-asset feature stores under `features/phase3/`, the OHLCV mirrors under `OHLCV/phase3/`, frozen model directories, the snapshot manifest, the cell deploy manifest at `configs/frozen/phase3_handoff/phase3_handoff_manifest.json`, and the experiment registries. It writes nothing; it emits pass/fail rows on stdout and exits non-zero on any failure. Any failure aborts the run before Step 1.

## Step 1 — TimesFM Feature Generation (`generate_timesfm_phase3.py`)

This step runs the frozen TimesFM foundation model in inference mode over the Phase 3 OHLCV window for every active asset. It exists because all downstream signal generation, ensemble blending, and TimesFM-accuracy auditing depend on the same TimesFM feature schema produced for Phase 1 and Phase 2; Phase 3 must compute the same columns from held-out data without touching the model weights.

The script reads `OHLCV/phase3/{asset}.parquet` and the frozen TimesFM checkpoint at `models/pretrained/timesfm_*.pt`, then writes `features/phase3/{asset}_timesfm.parquet`. The output columns include the multi-horizon direction signals `timesfm_4h_direction`, `timesfm_12h_direction`, `timesfm_24h_direction`, the cross-horizon agreement metric `timesfm_alignment_score` (which ranks +1 when all three horizons agree on direction and -1 when they disagree), and the full `tfm_q*_h*` quantile-band stack across the requested horizons (q05, q10, q25, q50, q75, q90, q95 at h=4, h=12, h=24).

Behavior is strictly causal: for bar N, the encoder consumes only `close[N - CTX .. N - 1]`, never bar N's own close. The output schema matches Phase 1 and Phase 2 column-for-column so feature-store readers downstream do not need version-aware loaders. The frozen weights are loaded read-only and never serialized back to disk. Phase 0 produced the original TimesFM features for Phases 1 and 2; Phase 3 just runs the same model on new bars.

## Step 2 — Holdout Execution (`prompt_1_holdout_run.py`)

This is the main execution step. It runs all 864 frozen strategies on the 2025 holdout, producing per-strategy trade ledgers, capped and uncapped equity curves, and the raw inputs every analysis script consumes. It is the single largest compute block in the phase.

For each strategy, the runner pulls the frozen Phase 1 raw signal library, loads the corresponding component-model checkpoints from Phase 2, applies the MetaCombiner per-philosophy weights, runs the decision-layer scheme to convert MC scores into binary take/skip decisions, applies the size and concurrency overlay, and routes the result through the strategy's frozen exit overlay. The fold-indexed exit assignment is read via `selected_exit_by_fold[K]` so ES2 variants honor the per-fold exit that was frozen at Phase 2 close. Per-asset concurrency caps cycle through `[5, 8, 10, 15, 20, 25]` and the simulator emits separate equity curves for each cap setting.

The simulator compounds equity from `INITIAL_CAPITAL = $1,000` while clamping any single-position notional at `$50,000` — both capped and uncapped equity series are emitted side by side so leverage analysis at Step 5 can decompose the cap impact. The capped curve is the deployable backtest; the uncapped curve is the no-cap reference.

Inputs are the frozen strategy library at `pipeline_state/phase_2/frozen/`, the cell deploy manifest, the Phase 3 feature stores including the TimesFM columns from Step 1, and the Phase 3 OHLCV mirror. Outputs land under `reports/phase3/{exit_variant}/strategy_{sid}_detail.json` together with a sibling parquet `strategy_{sid}_trades.parquet` carrying the full trade ledger (one row per executed trade, with fill_bar, exit_bar, asset, direction, entry_price, exit_price, holding_bars, gross_pnl_pct, net_pnl_pct, MC score, ensemble component scores). The trades parquet is what downstream regime analysis reads.

## Step 3 — Regime-Conditioned Analysis (`prompt_2_holdout_analysis.py`)

This step performs the regime-conditioned breakdown on the strategy outputs from Step 2. Its purpose is to expose how each strategy behaves across distinct market regimes rather than reporting a single blended figure that hides regime-specific catastrophic failures. A strategy that's PF 1.5 across the full holdout but PF 0.6 in high-vol-crash regimes is a different deployment risk from one that's uniformly PF 1.5.

The script reads the per-strategy trade parquets and detail JSONs from `reports/phase3/{exit_variant}/`, joins them on entry timestamp to the four regime-label columns in the Phase 3 feature stores (`regime_label_trend`, `regime_label_vol`, `regime_label_composite`, `regime_label_phase`), and writes `reports/phase3/regime_analysis/regime_breakdown_{sid}.json` plus an aggregate roll-up. The four regime dimensions break down the 6-class composite regime into more interpretable axes: trend (strong_uptrend / neutral / strong_downtrend), volatility (extreme_high / high / normal / low), the composite class itself (0-5 = bull_trending / bear_trending / sideways / high_volatility / low_vol_compression / crash_capitulation), and Wyckoff-style phase (markup / distribution / markdown / accumulation / uncertain).

For each strategy and each regime bucket the script computes trade count, win rate, profit factor, net PnL, and average holding bars. The aggregation step ranks strategies within each regime so downstream consumers can identify regime specialists versus all-weather performers. A regime concentration metric (`max_pnl_share`, the share of total PnL coming from a single regime) is also reported; concentration > 0.40 flags the strategy as regime-dependent.

## Step 4 — Ensemble Contribution Ablation (`prompt_3_ensemble_contribution.py`)

This step runs the ensemble ablation, attributing realized PnL to each of the eleven component models inside MetaCombiner. It exists so the ensemble's holdout edge can be decomposed and the user can tell which components actually carry signal on out-of-sample data — a component that contributes less than the within-MC noise floor is a candidate for removal from future Phase 2 rebuilds.

The script reads the strategy detail JSONs and trade parquets from Step 2 along with the frozen MetaCombiner checkpoint and component-model artifacts. It writes `reports/phase3/ensemble_summary.json` with per-component contribution weights and leave-one-out delta metrics.

The script replays each trade through MetaCombiner with one component masked at a time. The delta in approval rate, hit rate, and net PnL is attributed to that component. Component contributions are reported raw; **no thresholds are tuned and no components are dropped during Phase 3**. The output is purely informational. The 11 × 864 = 9,504 ablation tests get hierarchical Holm-Bonferroni correction (correct within model first, then across models) to avoid the over-conservative gate that would Holm-zero everything under naïve flat correction.

## Step 5 — Leverage Analysis (`prompt_4_leverage_analysis.py`)

This step quantifies how the `$50,000` single-position cap and the `MAX_LEVERAGE = 20` ceiling reshape strategy outcomes. It exists because the capped and uncapped equity curves emitted by Step 2 must be reconciled into a single leverage-attribution table for the deployment report.

It reads both equity curves and the trade parquet for every strategy and writes `reports/phase3/leverage_summary.json`. The summary table reports per-strategy capped Sharpe, uncapped Sharpe, the dollar shortfall caused by the position cap (uncapped final equity - capped final equity), the count of trades that hit the cap, and the fraction of equity growth attributable to compounding under the cap.

The metric set is fixed at the schema frozen at Phase 2 handoff. **No strategies are filtered, re-ranked, or dropped.** The output is a transparent attribution that the user reads alongside the regime breakdown to decide whether the cap is binding (small cap shortfall = the strategy never hit the cap; large cap shortfall = the strategy is leverage-dependent and the deployment story is more nuanced).

## Step 6 — Model Agreement (`prompt_5_model_agreement.py`)

This step measures cross-model agreement among the eleven MetaCombiner components on the holdout. Its purpose is to surface ensembles where agreement collapsed out-of-sample — an early-warning signal for regime shift even when realized PnL still looks acceptable. If 11 components used to vote 9-2 in-sample but vote 6-5 on holdout, the ensemble is operating in a different regime than it was trained for, regardless of whether the ensemble vote happens to be correct on holdout.

The script reads the per-trade component prediction columns embedded in the Phase 3 detail JSONs and writes `reports/phase3/agreement_summary.json`. It computes pairwise prediction correlation, majority-vote rate, and the fraction of approvals where every component agreed in sign.

Agreement metrics are reported per strategy, per regime bucket, and per asset. The script does not reweight or recalibrate the ensemble; it only reports. The 11 × 864 = 9,504 agreement tests get hierarchical Holm correction same as Step 4.

## Step 7 — TimesFM Accuracy Audit (`prompt_6_timesfm_accuracy.py`)

This step audits TimesFM's directional forecast accuracy on the holdout window. It exists to make sure the foundation-model signals carrying weight inside MetaCombiner actually predict the right direction at the horizons the ensemble consumes them. If TimesFM is no better than chance on holdout, MetaCombiner's blending of TimesFM features is doing nothing useful.

It reads the Phase 3 TimesFM feature parquets from Step 1 and the raw Phase 3 OHLCV. It writes `reports/phase3/timesfm_accuracy.json` with per-horizon hit rates, alignment chi-square statistics, and adjusted p-values.

Ground truth `actual_direction` is computed as `sign(close[fill_bar + H] - close[fill_bar])` from raw OHLCV at bar `fill_bar+H`, where `H ∈ {4, 12, 24}` bars matching the TimesFM horizon set. Critically, ground truth is **never** derived from realized trade PnL — `(net_pnl_pct > 0)` is contaminated by the strategy's exit choice and costs, so scoring TimesFM against PnL conflates model accuracy with strategy outcome. Bar-index arithmetic on raw OHLCV is the only valid construction.

Two independent Holm-Bonferroni passes run side by side: an alignment chi-square family of `m = 864` (one chi-sq test per strategy, asking whether MC-approved trades have higher TimesFM-direction alignment than skipped trades) and a direction-accuracy binomial family of `m = 2592` covering the three horizons `{4h, 12h, 24h}` across all strategies (one binomtest per strategy per horizon, against null hypothesis p=0.5). Raw and adjusted p-values are reported; nothing is gated on them inside Phase 3.

## Step 8 — Portfolio Holdout (`prompt_7_portfolio_holdout.py`)

This step evaluates the 20 portfolio variants on the same frozen trade universe produced in Step 2. Its purpose is to compare allocator designs under identical entry, sizing, and cost assumptions so any performance delta is attributable to the allocator alone, not to upstream selection. Without this step, the user could not distinguish "the allocator worked" from "the underlying strategy mix was lucky."

The script reads the strategy trade parquets, the frozen portfolio artifacts under `data/portfolio/phase2_5/` (HRP weights, regime bandit, neural optimizer ensemble, MetaDD pair sidecars), and the variant registry. It writes per-variant outputs under `reports/phase3/portfolio_holdout/{variant_id}_returns.parquet` and a roll-up across all 20 variants.

The 20 variants span HRP conservative and aggressive baselines, two LinUCB satellites (variant_3 + variant_5), the 50/50 blend, the neural optimizer, the analytic Kelly portfolio, eight MetaDD-overlaid twins of those primaries, plus baseline and soft-kill diagnostics. The LinUCB context builder operates over the 18-dimensional context vector across six regimes; the bandit's anti-whipsaw state is reset on every overlay entry so prior in-sample adaptation cannot leak into the holdout decision sequence. The neural optimizer's loader validates `state["fc1.weight"].shape[1] == NEURAL_OPT_INPUT_DIM` before forward pass, catching checkpoint dim drift before it produces silent zeros.

## Step 9 — Exit Comparison (`prompt_8_exit_comparison.py`)

This step compares the production exit overlays (`fixed`, `atr_scaled`, `dist_exit`, plus the shadow-only `sigma_exit` channel) on the same MC trade universe. It exists so the relative merit of each overlay is visible without permitting any of them to influence MC training, which remains label-blind.

It reads the per-strategy detail JSONs and trade parquets and writes `reports/phase3/exit_comparison/` with per-overlay aggregate metrics and per-strategy deltas against the `fixed` baseline. Realized PnL per overlay is recomputed on-the-fly from the bake-off (every overlay re-prices the same set of MC-approved entries against the same cost model); no per-overlay sidecar parquets are written, keeping the artifact footprint small.

The comparison preserves identical entries, identical MC approval, and identical execution costs across overlays so the only varying axis is the exit method. Exit selection itself respects the per-cell calibration gate at `tfm_calibration_per_cell.json` and the fold-indexed `selected_exit_by_fold` assignment that was frozen at Phase 2 close. The `dist_exit` overlay's deny-default gate means cells without a passing calibration entry fall back to `fixed` rather than firing dist_exit blindly — which keeps the comparison honest.

## Phase 3 wall-clock

End-to-end Phase 3 runs in roughly **18-26 hours** on Hopper. The breakdown: ~2-3 hours for TimesFM Phase 3 generation, **12-18 hours for the 864-strategy holdout execution** under the per-asset concurrency caps (this dominates), and 3-5 hours for the combined regime, ensemble, leverage, agreement, TimesFM-accuracy, portfolio, and exit-comparison analyses running in parallel after holdout completes.

---

# Phase 4 — Online-Learning Portfolio Allocator (DESIGN ONLY)

**Status: planning only — no code on disk yet.** Phase 4 sits on top of the Phase 2.5 winning portfolio variant and adds an online-learning allocator that observes current market state, proposes free-form allocation weights across the frozen 864 strategies, projects those weights onto a hard risk-budget feasibility set via CVXPY, and updates its policy as new bars arrive. The full design lives at [`docs/phase4_plan.md`](docs/phase4_plan.md); this section is the executive summary that mirrors the structure of the other phases for navigation. Phase 4 cannot start until Phase 2.5 ships a winner (~27 days from a fresh Phase 1 launch), since the allocator's training data is the per-strategy rollout from Phase 2.5.

The defining principle of Phase 4 is that **the allocator proposes weights freely** — no arbitrary "±2%" tilt cap that loses money when one strategy genuinely deserves a 10% concentrated bet — and the safety comes from projecting the proposal onto a risk-space feasibility set. The CVXPY projection is differentiable (via `cvxpylayers`) so end-to-end training stays gradient-flow-friendly. When the baseline HRP allocation is wrong, the allocator can fully lean away; when HRP is right, the allocator converges back toward it via a smoothness penalty `λ_smooth · ||w - w_prev||²`.

The chain is broken into five sub-phases (4.0 through 4.4) of increasing ambition. The user runs them in sequence and stops at the highest-numbered tier that beats the previous tier's holdout numbers under Holm-Bonferroni correction; the goal is not to get all the way to 4.4 but to find the simplest tier that adds real edge.

## Step 1 — Layer 1: TimesFM Distributional Forecaster (already shipped)

The first layer of the Phase 4 spine is the TimesFM forecaster the rest of the pipeline already uses. It produces forward-looking distributional features at horizons `h ∈ {25, 72, 168}` bars: q10/q25/q50/q75/q90 quantile bands, fan width (`q90 - q10`), lower-tail spread (`q50 - q10`), upper-tail spread (`q90 - q50`), asymmetry, trend slope, uncertainty expansion (`dq90/dt`), fan area delta, reversal flag, and persistence score. This adds up to ~30 features per relevant horizon plus a TimesFM-uncertainty-acceleration second derivative.

Layer 1 is **already in production**. It is the same `tfm_*` feature family Phase 0 generates and Phase 2/2.5/3 consume. Phase 4 does not retrain TimesFM; it consumes the existing per-bar parquet output. The only Phase 4-specific work is wiring the forecast features into the Layer 3 allocator's input vector.

The role of this layer in the Phase 4 narrative is "describe the future" — it never decides weights, but its distributional view of where prices are headed at multiple horizons feeds both Layer 2 (danger estimation) and Layer 3 (allocation proposal). Decoupling the forecaster from the allocator lets the user reason about each layer independently and ablate them cleanly.

## Step 2 — Layer 2: MetaDD Student-t Predictor (already shipped at Phase 2.5)

The second layer is the MetaDD Student-t predictor that already ships in Phase 2.5 Step 4 (`scripts/phases/phase_2_5/prompt_7f_metadd_predictor.py`). For each portfolio day it emits `dd_mu`, `dd_sigma`, `dd_nu`, `dd_q90`, `dd_q95`, plus derived `prob(dangerous_drawdown)` and a `tail_risk_state` categorical. The Phase 4 allocator consumes these as a Block-C portfolio-risk feature and as a CVXPY constraint anchor.

Inputs to Layer 2 are the realized portfolio drawdown trajectories from each base variant's walk-forward returns plus the regime feature panel and the TimesFM features from Layer 1. Outputs are the same `metadd_predictions.parquet` Phase 2.5 already writes. The role of this layer is "estimate danger" — it gives Layer 3 a probabilistic view of forward drawdown risk that the CVXPY constraint set can enforce against (e.g. `projected_DD_q95(w) ≤ 25%`).

Phase 4 does not retrain MetaDD; the Phase 2.5 fit is consumed read-only. The MetaDD calibration gate (`calibration_slope ∈ [0.75, 1.25]`) gates whether the allocator is allowed to use MetaDD-derived constraints at all on a given day; out-of-calibration MetaDD predictions fall back to Phase 2.5 baseline DD bounds.

## Step 3 — Layer 3a: Constrained-Free Allocator Proposal (NEW)

Layer 3a is the new Phase 4 component — a neural or linear model that proposes raw weights `w_raw ∈ R^N` over the frozen strategy set. The default architecture is a 2-layer MLP with a softmax-Dirichlet head that emits mean-only weights (no Bayesian sampling at inference; the head's variance is captured in the constraint set, not the proposal). An alternative architecture under consideration is LinUCB-over-sleeves with a neural ranker over strategies within each sleeve.

The training target is **NOT direct weights** — that's too noisy and easy to overfit. Phase 4.1 (the MVP) trains on the **continuous marginal-contribution score**: `score_i = expected_risk_adjusted_excess_contribution_of_strategy_i`. The allocator picks high-score strategies greedily until the CVXPY risk budget saturates. Alternative target formulations under consideration are 3-class per-strategy tilt classifier (`{upweight, neutral, downweight}`), top-K membership probability, and Bradley-Terry-style pairwise rank scores.

Inputs are the 5 context blocks from `portfolio_context.md`: **Block A** strategy health (~15 features × N strategies — rolling Sharpe/Sortino/Calmar at 30d/90d windows, hit rate, payoff ratio, recent firing rate, aging score, regime-conditional Sharpe), **Block B** relationship/redundancy (~8 features — pairwise correlation, trade overlap Jaccard, HRP cluster ID, marginal diversification benefit), **Block C** portfolio risk (~10 features — current HHI/Gini, effective-N, projected DD q90/q95 from Layer 2 directly, weight instability), **Block D** market regime (~10 features — 6-class regime, vol regime, trend regime, dispersion, mean-reversion score, regime confidence as a soft score), and **Block E** TimesFM forward (~30 features per horizon from Layer 1). Total feature dimensionality at Layer 3 is ~70 market/portfolio features plus ~15 × N strategy-specific features.

The proposal model **rolling-retrains nightly** on a trailing 90-180d window of realized portfolio returns plus regime-labeled state. Outputs are unbounded-tilt raw weights `w_raw` consumed by Layer 3b.

## Step 4 — Layer 3b: CVXPY Risk-Budget Projection (NEW)

Layer 3b is the differentiable CVXPY projection layer that turns `w_raw` into the deployable `w_final`. It runs at every allocation step (typically daily) and solves a small convex problem in 50-200 milliseconds. The objective is `||w - w_raw||² + λ_smooth · ||w - w_prev||²` (stay close to the proposal, but also stay close to yesterday's deployed weights to avoid thrashing) subject to a hard constraint set:

The constraints span: budget (`sum(w) == portfolio_budget` after leverage scaling), per-strategy lower/upper bounds (`w_lower_i ≤ w_i ≤ strategy_cap(regime_confidence)` where the cap is 15% in high-confidence regimes, 10% medium, 6% low), per-asset exposure cap (`per_asset_exposure(w) ≤ 25%`), per-library exposure cap (`per_library_exposure(w) ≤ 30%`), concentration cap (`HHI(w) ≤ 0.15`), minimum diversification (`effective_N(w) ≥ 20`), MetaDD-driven projected drawdown (`projected_DD_q95(w, Σ_shrunk) ≤ 25%`), turnover (`turnover(w, w_prev) ≤ 50%` as anti-thrashing), regime-confidence-scaled leverage (`leverage(w) ≤ leverage_cap(regime_confidence)` — 2× in high-confidence regimes, 1.5× medium, 1× low), and a portfolio volatility budget.

Why this beats bounded-tilt approaches: the allocator can be aggressive within constraints (e.g. 15% on one strategy and 0% on others when the regime is right) without external arbitrary tilt caps that would cap useful concentrations. Blow-up protection lives in risk space (DD, vol, HHI) rather than weight space, so the constraints are tighter and more interpretable than weight bounds. The smoothness penalty `λ_smooth · ||w - w_prev||²` is the only soft regularizer; everything else is a hard convex constraint.

All caps in Phase 4.1 are **fixed values** from the CLAUDE.md risk policy. Phase 4.2 (constraint tuning) learns the caps via walk-forward cross-validation over the holdout, replacing fixed values with CV-tuned ones.

## Step 5 — Phase 4.0: Regime-Gated HRP Baseline (~2 eng-days)

Phase 4.0 is the floor that every more-ambitious Phase 4 tier must beat under Holm-Bonferroni significance. The construction is straightforward: train one HRP allocation per regime class (4-way: bull_trending / bear_trending / sideways / high_volatility — using the simpler 4-class regime taxonomy rather than the 6-class composite for tractability). Deploy the per-regime HRP that matches the current regime classifier output. Switch books only on regime transition (with the same hold-days + transitions-per-window anti-whipsaw the LinUCB layer uses).

This proves regime → weights is a real signal and gives Phase 4.1 a non-trivial benchmark. If Phase 4.1's full constrained-free allocator does not significantly beat 4.0 at Holm-corrected p < 0.05, the user should not deploy 4.1 — the added complexity has no edge.

## Step 6 — Phase 4.1: Constrained-Free Allocator MVP (~7-10 eng-days)

Phase 4.1 is the first ambitious tier — the full 3-layer architecture (TimesFM → MetaDD → Constrained-Free Allocator) with all 5 feature blocks, the CVXPY risk projection, the rolling 90-day retrain, and the marginal-contribution-score proposal head. The goal is "regime-aware allocator with risk-space safety."

The MVP runs offline on Phase 2.5 rollouts for pre-training, then online on holdout with strict walk-forward (the proposal model is forbidden from looking at any future bar). Reward signal for the online learning loop is the **Sharpe delta versus regime-baseline HRP** — this isolates the allocator's contribution from the underlying strategy mix. Update frequency is per-day; feedback horizon is 90-day rolling Sharpe; allocation granularity is per-cluster (KMeans/HRP over strategy daily returns produces ~80 clusters, the allocator learns cluster-level weights, then equal-weights within each cluster — reduces dimensionality and groups near-duplicate strategies).

If Phase 4.1 beats Phase 4.0 under Holm-Bonferroni, the allocator deploys. If not, the user falls back to 4.0 and may try Phase 4.3 (architecture upgrade) before deciding 4.1 is dead.

## Step 7 — Phase 4.2: Feature-Block Ablation + Constraint Tuning (~3-4 eng-days)

Phase 4.2 is the refinement pass over the deployed 4.1 allocator. Two parallel jobs: ablate each of the 5 feature blocks (drop Block A, drop Block B, etc., re-train, re-evaluate) to identify the highest-value blocks; tune the CVXPY constraint values via walk-forward CV (replacing the fixed caps with learned ones, testing whether context-dependent caps improve risk-adjusted return). Also tests transaction-cost model upgrade from linear (spread-only) to quadratic (linear spread + market-impact term).

Output is a refined allocator with leaner feature inputs (drop blocks that don't move the needle) and tuned constraint values. Becomes the production allocator if it significantly outperforms 4.1.

## Step 8 — Phase 4.3: Proposal Architecture Upgrade (~5-7 eng-days, only if 4.1 plateaus)

If 4.1 plateaus or 4.2 doesn't lift it enough, Phase 4.3 replaces the softmax MLP proposal head with an attention-over-strategies transformer. The motivation is that the MLP cannot easily capture non-linear interactions between TimesFM state and the strategy menu — e.g. "in regime X with TimesFM forecast Y, prefer strategies Z1+Z3 because their pairwise correlation flips negative under that joint state." Attention naturally encodes this kind of conditional pairwise interaction.

Phase 4.3 is gated on Phase 4.1 + 4.2 results — it ships only if the simpler models hit a real ceiling. The architecture upgrade adds significant complexity and training time but pays back if the allocator is genuinely under-fitting at 4.1.

## Step 9 — Phase 4.4: RL Variant (Phase 5 territory; ~3-4 weeks, only if 4.3 also plateaus)

The terminal escalation is full reinforcement learning — a PPO agent that outputs the weight proposal, with CVXPY still doing the risk-space projection. Reward is the same Sharpe-delta-vs-baseline-HRP signal, but optimized via policy gradient rather than supervised marginal-contribution regression.

Phase 4.4 is multiweek work and is treated as Phase 5 territory (a fresh research cycle, not just a Phase 4 sub-phase). It ships only if Phase 4.3 also plateaus and the user decides the additional complexity is justified by holdout numbers.

## Phase 4 building blocks already in the pipeline

| Component | File | Phase 4 role |
|---|---|---|
| TimesFM v2.2 (~342 `tfm_*` cols) | `scripts/phases/phase_0/run_timesfm.py` + extras | Layer 1 — distributional forecaster (no retraining) |
| MetaDD Student-t | `scripts/phases/phase_2_5/prompt_7f_metadd_predictor.py` | Layer 2 — danger estimator (no retraining) |
| LinUCB contextual bandit | `scripts/phases/phase_2/prompt_7_portfolio_construction.py` | Prototype for Layer 3a proposal |
| Neural portfolio optimizer | `scripts/phases/phase_2/prompt_7c_portfolio_optimizer.py` | Can become Layer 3a neural backbone |
| HRP | `scripts/phases/phase_2/prompt_7_portfolio_construction.py` | Phase 4.0 baseline + smoothness anchor |
| Kelly variant 8/8b | `scripts/phases/phase_2/prompt_7e_kelly_portfolio.py` | Leverage-budget input to CVXPY constraint |
| Regime labels | `scripts/phases/phase_0/build_regime_labels.py` | Block D features + regime_confidence input |
| BTC context | `scripts/phases/phase_0/build_btc_context.py` | Block D cross-asset features |

## Gaps to close before Phase 4 deploys

The Phase 4 plan calls out 7 prerequisites that are currently unbuilt: (1) a raw-pool sidecar at `pipeline_state/phase_1/raw_pool_{asset}.parquet` written during Phase 1 consolidation containing all canonical-rescored survivors pre-dedup so Phase 4 has the full strategy menu; (2) dedup-threshold relaxation (Jaccard 0.80→0.95, PnL-correlation 0.92→0.97, GP_K_PER_COMBO 5→15) to preserve learnable redundancy; (3) per-strategy exit sidecar (currently Phase 2 freezes one exit per philosophy — Phase 4 wants per-strategy exits for regime-conditioned exit learning); (4) bar-by-bar online-learning replay driver (the current pipeline is batch); (5) the CVXPY projection layer itself (using `cvxpylayers` for differentiability); (6) a rolling-retrain scheduler (SLURM or cron); (7) per-strategy rolling-metric precomputation persisted as time series (rolling Sharpe / Sortino / Calmar / hit-rate / PF at 30d and 90d windows).

## Phase 4 wall-clock

Phase 4 work cannot start until Phase 2.5 ships a winner. From that point: Phase 4.0 (regime-gated HRP baseline) is **~2 engineering days**. Phase 4.1 (constrained-free allocator MVP) is **~7-10 days**. Phase 4.2 (ablation + constraint tuning) is **~3-4 days**. Phase 4.3 (transformer proposal) is **~5-7 days**. Phase 4.4 (RL variant) is **~3-4 weeks**. The expected production deployment runs through 4.0 → 4.1 → 4.2 and stops; 4.3 and 4.4 are only invoked if the simpler tiers plateau under holdout-validated significance.

---

# Cross-Phase Concepts

A few invariants and contracts span all three phases. They are documented per-step where they apply but consolidated here for reference.

**Walk-forward CV.** Universal across Phase 2 and Phase 2.5: `N_SPLITS=8`, `PURGE_BARS=72`, `EMBARGO_BARS=24`. Every train/val split inserts the purge+embargo gap. The purge zone removes bars within ±72 bars (3 days) of the validation window from the training set; the embargo extends 24 bars past the validation window during which no training data may be sampled. This defends against same-event leakage (a multi-bar event that touches both train and val).

**MC INPUT BLOCKLIST.** Substring filter at MC training entry. The 7 substrings — `rel_exit`, `selected_exit`, `exit_advantage`, `exit_dispersion`, `sigma_exit`, `dist_exit`, `_shadow_` — plus `exit_reason` are rejected with `ValueError`. The blocklist exists because exit-derived columns are downstream of the very exit choice the MC is being trained to inform; using them as inputs is circular. Re-introduction of any blocklisted feature requires (1) computation from folds < K, (2) sourcing from MC-independent exits only (`fixed`, `atr_scaled`), (3) group-ablation showing >5% MC contribution, (4) a new MC checkpoint compared against the C+ baseline on Phase 2 OOS folds 4-8. The `STRICT_MC_LABEL=1` env flag (default) hard-fails the ablation if the clean target column `net_pnl_pct_at_fixed_exit` is absent.

**EXIT_STAGE axis.** `EXIT_STAGE_AXIS=[1, 2]` is one of the four axes the variant overlay at Step 6 builds across. ES1 (Stage-1) is single-fire exit at TP/SL/max-hold. ES2 (Stage-2) routes through `selected_exit_by_fold[K]` so each fold uses the exit overlay frozen for that fold; Phase 3 reads the package summary's `exit_stage` field and dispatches accordingly. The axis lifts from the per-cell winner dict through the deploy manifest row through the package summary; if any link drops it, ES2 silently collapses to ES1 and the variant is wasted.

**Fold-indexed exit selection.** Per-strategy exit selection (`selected_exit_by_fold[K]`) is computed using only folds 0..K-1 — the fold-K exit choice cannot use fold-K data. This is enforced at `prompt_exit_selection_per_strategy.py:90-247` via the `OOS_FOLDS_ZEROIDX` indexing convention plus a `max_fold` kwarg that caps the data window per fold computation.

**Calibration gate (deny-default).** The dist_exit calibration sidecar at `pipeline_state/phase_2/tfm_calibration_per_cell.json` is fold-indexed (`cells_by_fold[K]`). Cells without a passing calibration entry are ineligible to fire dist_exit; the variant falls back to `fixed`. The gate is deny-default — missing/empty/uncomputed cells return `False`, so a producer omission (e.g. the calibration input parquet not yet built) cannot silently grant dist_exit access.

**22-asset whitelist.** `PHASE2_ACTIVE_ASSETS` lists 22 assets. The 4 dropped at the 2026-04-18 prune (`coin`, `pltr`, `avax`, `dot`) are kept in the canonical 26-asset `ASSETS` list for legacy purposes but are NOT admitted to Phase 2/2.5/3 by default. The asset-admission gate at Step 7 of Phase 2.5 + the Phase 3 pre-flight at Step 0 both enforce this whitelist.

**Atomic write contract.** Every config / sidecar / manifest write goes through `scripts/common/utils/atomic_io.py::atomic_write_json` or `atomic_write_parquet`, which writes to a temp path then atomically renames into place. This prevents readers from seeing partial files during a concurrent write — Phase 2.5 `freeze_phase25_artifacts.py` and Phase 2 `prompt_6_assembly.py` rely on this for the chmod 0o444 step.

**Block-bootstrap autocorrelation.** The shared block-length cache at `scripts/common/utils/block_permutation.py::get_shared_block_length` estimates per-asset autocorrelation horizon and serves the same block length to every consumer (sizing-selection bootstrap, exit-selection paired bootstrap, robustness-audit Hansen SPA, baselines TUNE/EVAL). All callers use the same cache so the same returns vintage produces the same block length everywhere — diverging block lengths would make the multiple-testing pool inconsistent.

**Active asset universe vs canonical universe.** `ASSETS` is the canonical 26-asset list (kept for legacy reasons). `PHASE2_ACTIVE_ASSETS` is the 22-asset production universe. The 4 dropped assets (`coin`, `pltr`, `avax`, `dot`) failed Phase 1 quality gates and are excluded from all Phase 2/2.5/3 work. The RL training universe at `prompt_rl_sizing.py:266` iterates `PHASE2_ACTIVE_ASSETS`, not `ASSETS` — using the canonical 26 would pull predictions for assets that never trained Phase 2 frozen libraries.

**Regime label dimensions.** Phase 0 produces 4 regime label columns: `regime_label_trend` (categorical: strong_uptrend / neutral / strong_downtrend), `regime_label_vol` (categorical: extreme_high / high / normal / low), `regime_label_composite` (int 0-5 = bull / bear / sideways / high_vol / low_vol_compression / crash_capitulation), and `regime_label_phase` (categorical: markup / distribution / markdown / accumulation / uncertain). Phase 3 Step 0 hard-fails if any of these are missing from the Phase 3 feature stores; Phase 3 Step 3 reads them for the regime-conditioned breakdown.

---

**Wall-clock budget summary.** Phase 1 is upstream of this document but takes 8-16 days end-to-end (Quartz BF4 + GP + PySR cascade). Phase 2 is **9 days** at 8 nodes × 4 H100s, **15 hours** at full-allocation Quartz (`launch_phase2.sh`). Phase 2.5 is **6-10 hours** on a single H100. Phase 3 is **18-26 hours** on Hopper. Phase 4 is **design-only** — no shipped code yet — but the planned rollout is 4.0 (~2 eng-days) + 4.1 (~7-10) + 4.2 (~3-4), summing to roughly **2-3 weeks of post-2.5 implementation work** before the online-learning allocator hits production. From the moment Phase 1 finishes, the production critical path to a fully holdout-validated portfolio is **~10 days at standard allocation, ~2 days at full allocation**, with Phase 4 layered on top once the user picks the Phase 2.5 winner.
