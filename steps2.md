# Phase 2 — Step-by-Step

Phase 2 consumes the frozen Phase 1 signal libraries and trains an 11-model machine-learning ensemble on top of them. Walk-forward cross-validation uses `N_SPLITS=8`, `PURGE_BARS=72`, `EMBARGO_BARS=24` throughout. The ensemble feeds a MetaCombiner that emits a single approval score per trade, which then drives a decision-layer search over score-blending, skip rules, and confidence-bucket boundaries. A size and concurrency overlay multiplies those schemes against replacement-mode and exit-stage axes to produce 72 frozen variants per philosophy. Across the 12 philosophies this yields **864 frozen strategies**, packaged with a master handoff manifest for Phase 3 holdout validation. The chain is orchestrated by `slurm/phase2/p2_master_launcher.sh`, which submits the full SLURM dependency graph and exits.

---

## Step 1 — prompt_1_dataset_build.py

This step builds the per-asset, per-philosophy ML training dataset that the entire ensemble consumes. It reads the frozen Phase 1 signal library for one cell, replays each rule against Phase 2 OHLCV bars, and emits a trade table augmented with feature-matrix columns aligned at signal-bar timestamps. It also emits a quarantine sidecar at `pipeline_state/phase_2/dataset_quarantine.json` so that downstream training excludes signals that fail the in-window PF guard.

It reads `pipeline_state/phase_1/frozen_libraries/{philosophy}/{asset}.json`, `OHLCV/phase2/{asset}.parquet`, and `features/phase2/{asset}.parquet`. It writes the canonical per-cell dataset to `features/phase2/datasets/{asset}_{philosophy}.parquet`, plus a per-cell metadata JSON. Trade-level columns kept include `signal_bar`, `fill_bar`, `exit_bar`, `entry_price`, `exit_price`, `direction`, `holding_bars`, `gross_pnl_pct`, `funding_cost`, `execution_cost`, `net_pnl_pct`, `signal_name`, `signal_family`, `signal_direction`, `signal_category`, `tp_pct_used`, `sl_pct_used`, `max_hold_used`. `exit_reason` is excluded — it is a circular target leak and the MC blocklist enforces this as a backstop.

This step runs as a per-asset SLURM array (`slurm/phase2/p2_dataset_build.sh`, `--array=0-21`). Every downstream training step keys off the parquet path written here, so the schema contract is load-bearing.

## Step 2 — prompt_2_model_train.py + per-model trainers

This step trains the gradient-boosting backbone of the ensemble and the auxiliary scorers. `prompt_2_model_train.py` handles the XGBoost head per fold. Companion trainers run alongside for the auxiliary models: `prompt_2b_timesfm_features.py` produces TimesFM forecast features and a per-cell calibration sidecar, `prompt_2c_lstm_classifier.py` trains an LSTM2 classifier with attention initialized from `models/pretrained/lstm_pretrained.pt` when shapes match, `prompt_2d_tft_scorer.py` trains a TFT scorer, `prompt_2e_cross_asset_scorer.py` produces cross-asset context scores, `prompt_2e_lightgbm.py` and `prompt_2f_catboost.py` train tree-based diversifiers, `prompt_2i_xgb_quantile.py` trains a quantile head, `prompt_2k_lgb_bootstrap.py` trains a bootstrapped LGB head, `prompt_2l_bnn_mc_dropout.py` trains a Bayesian MC-dropout head with `MC_SAMPLES=100`, and `prompt_2f_leverage_model.py` trains a regression head that predicts optimal leverage from the upstream quality scores.

Inputs are the per-cell dataset from Step 1. Outputs are per-fold checkpoints under `models/phase2/{asset}_{philosophy}_{component}_fold_{K}.pt|.json` and per-fold OOS prediction parquets that are stitched chronologically in Step 3. Each trainer respects the walk-forward purge and embargo, fits scalers and imputers on the train fold only, and writes via atomic-replace.

This step is the GPU-heavy core of Phase 2. It runs as `slurm/phase2/p2_models_train.sh` — a 22-asset array with `--gres=gpu:h100:1`, `--cpus-per-task=32`, `--mem=128G`, `--time=12:00:00`. Off-critical-path companions (`ngboost_train.sh`, ablation runners) fire on its completion.

## Step 3 — prompt_3_oos_assembly.py

This step stitches the per-fold OOS prediction tables from Step 2 into a unified per-asset table, then merges those into a cross-asset table per philosophy. It is the single source of truth for the ensemble's OOS predictions consumed by the MetaCombiner and the decision layer. It enforces OOS integrity — no duplicate timestamps, no chronological gaps, full metadata preservation across folds.

It reads `models/phase2/{asset}_{philosophy}_fold_predictions.parquet` for each component model and each of 5 OOS folds. It emits `pipeline_state/phase_2/oos_unified/{asset}_{philosophy}.parquet` in per-asset mode, and `pipeline_state/phase_2/oos_unified/{philosophy}_cross_asset.parquet` in `--merge` mode. Score-distribution and classification-vs-expected-PnL correlation stats are written alongside as integrity JSON.

The per-asset run is invoked by the per-asset assembly array (`slurm/phase2/p2_assembly.sh`, `--array=0-21`). The cross-asset `--merge` invocation runs inside `slurm/phase2/p2_freeze.sh` once all 22 per-asset tasks succeed.

## Step 4 — prompt_3b_metacombiner.py

This step trains the MetaCombiner — a small MLP (20 → 32 → 16 → 1, SmoothL1 loss, vol-scaled targets) that takes the unified ensemble outputs and emits one approval score per trade. It is the highest-leakage-risk component because it operates on already-OOS predictions across folds in a two-level walk-forward; fold 1 is intentionally NaN.

It reads the cross-asset OOS table from Step 3 and writes `models/phase2/metacombiner_{philosophy}_fold_{K}.pt`, plus a merged predictions parquet at `pipeline_state/phase_2/metacombiner/{philosophy}_predictions.parquet`. The clean training label is `net_pnl_pct_at_fixed_exit`. The MC INPUT BLOCKLIST enforces a substring match at training entry and rejects any column containing `rel_exit`, `selected_exit`, `exit_advantage`, `exit_dispersion`, `sigma_exit`, `dist_exit`, or `_shadow_`. A `ValueError` is raised before the first batch if any column matches; `exit_reason` is also blocked as defense in depth.

This step runs once per philosophy inside `p2_freeze.sh` after the per-asset cross-asset merge. The per-fold checkpoints and the merged-predictions parquet are the binding inputs for the decision layer at Step 5 and for exit-overlay bake-offs at Step 7.

## Step 5 — prompt_4_decision_layer.py

This step searches over decision-layer schemes that turn MetaCombiner approval scores plus ensemble heads into a binary take-or-skip decision. It tests score-blending weights, skip rules, and confidence-bucket boundaries, retains the top three (`MAX_COMBINED_SCORING_SCHEMES=3`), and forwards all three to Step 6 with no selection or elimination.

It reads the MetaCombiner predictions parquet and the cross-asset OOS table for one philosophy. It writes `pipeline_state/phase_2/prompt_4_schemes_{philosophy}.json` containing the three frozen scheme specs plus per-scheme OOS metrics. A philosophy-agnostic fallback is written to `prompt_4_schemes.json`.

This step runs once per philosophy in `p2_freeze.sh`. The three schemes survive into Step 6 as the score-scheme axis of the 72-variant grid.

## Step 6 — prompt_5_overlay.py

This step takes each of the three decision schemes and overlays the size, concurrency, replacement-mode, and exit-stage axes on top to produce 72 frozen variants per philosophy. The combinatorics are `3 score schemes × 3 size/concurrency overlays × replacement_mode × EXIT_STAGE_AXIS=[1, 2]`. Position sizing assumes $1,000 initial capital, compounding, with a $50,000 single-position cap; concurrency caps come from `CONCURRENCY_CAPS = [5, 8, 10, 15, 20, 25]`; `REPLACEMENT_MODES = ["close_at_market", "skip", "swap_by_expected_pnl"]`. Per-variant identity is `{variant_id}{_mode_suffix}{_stage_suffix}`.

It reads the three schemes from Step 5 and emits one frozen-config JSON per variant under `configs/frozen/{philosophy}/{variant_id}.json`, plus a per-philosophy summary table at `pipeline_state/phase_2/prompt_5_variants_{philosophy}.parquet`. Each variant record carries `variant_id`, `scheme_label`, `risk_profile`, `exit_stage`, `replacement_mode_axis`, and a frozen-spec dict.

All 72 variants per philosophy proceed to Step 7. No variant is dropped here. Across 12 philosophies this materializes 864 frozen-spec JSONs, which Step 8 packages.

## Step 7 — prompt_5b_robustness_audit.py + prompt_exit_selection.py + prompt_sizing_selection.py + prompt_2n_distribution_exit.py + check_timesfm_calibration_per_cell.py + tune_dist_exit_threshold.py + prompt_rl_sizing.py

This step runs the robustness audit, the per-strategy fold-indexed exit selection, the sizing selection, the dist_exit threshold calibration, and the RL sizing fit. The robustness audit validates each variant under Hansen SPA and Holm-Bonferroni multiple-testing correction with stationary block-bootstrap p-values, where SPA p-values are reported but Holm is the binding filter. Per-strategy exit selection picks the best exit overlay from `CHALLENGERS = ["atr_scaled", "sigma_exit", "gp", "dist_exit"]` against the `fixed` baseline, fold-indexed via `selected_exit_by_fold[K]` using only folds 0..K-1. Sigma-exit is shadow-only.

It reads the variant configs from Step 6 plus the MetaCombiner trade universe. It writes `configs/frozen/exit_selection.json`, `configs/frozen/sizing_selection.json`, `configs/frozen/dist_exit_thresholds.json`, `configs/frozen/timesfm_calibration_report.json`, `pipeline_state/phase_2/tfm_calibration_per_cell.json`, and per-variant audit JSONs under `pipeline_state/phase_2/audit/`.

The TimesFM per-cell calibration sidecar grids on `(asset × direction × horizon × regime)` — 22 × 2 × 3 × 6 = 792 cells. It records Wilson 95% LCB on direction accuracy, calibration slope, coverage at 90% and 95%, plus a 4-tier pooled-fallback ladder at `n ≥ 80 / 200 / 350 / 600`. The dist_exit gate is deny-default — cells without a passing entry are ineligible. The fold-indexed `cells_by_fold` structure is what Phase 3 reads at execution time.

## Step 8 — prompt_6_assembly.py

This step is the final Phase 2 assembly. It runs once after every philosophy has completed Steps 1-7, verifies that all `NUM_PHILOSOPHIES × NUM_VARIANTS_PER_LIBRARY = 864` frozen-config JSONs exist, packages them into per-strategy bundles, and writes the master handoff manifest for Phase 3. No snapshot verification, no holdout data access, no tuning. A global Holm step aggregates exit, sizing, and MC-input p-values family-wide.

It reads `configs/frozen/{philosophy}/{variant_id}.json` × 864, plus `configs/frozen/exit_selection.json`, `configs/frozen/sizing_selection.json`, `configs/frozen/dist_exit_thresholds.json`, the per-cell calibration sidecar, and the audit outputs. It writes the master manifest at `pipeline_state/phase_3/phase3_handoff_manifest.json` with per-strategy bundle paths, SHA pins, and chmod 0o444 on all packaged files. The manifest is the asset-whitelist-validated cell-deploy contract for Phase 3.

This step runs at the tail of `p2_freeze.sh` and is gated by every prior step in the chain. Failure here aborts the chain before `p2_validate.sh` fires.

## Step 9 — p2_validate.sh

This step is the final gate before Phase 2.5. It checks that all 864 frozen configs exist, that every component-model checkpoint is on disk, that MetaCombiner fold coverage is correct, that no Phase 3 data was accessed during Phase 2, that OOS prediction tables carry the ensemble columns the manifest claims, and that the Phase 3 handoff manifest is internally valid.

It reads the master manifest from Step 8 plus all referenced bundle files. It emits `pipeline_state/phase_2/p2_validate_report.json` with pass/fail per check. On failure, the chain stops and Phase 2.5 does not launch. On success, `slurm/phase2_5/p25_master_launcher.sh` is fired with `--dependency=afterok:$JID_VAL`.

## Wall-clock estimate

Per `p2_master_launcher.sh`: 8 nodes × 4 H100s ≈ ~9 days end-to-end. Per `launch_phase2.sh` (8-stage Quartz dependency variant): ~15 hours total across ~14,000 array tasks when the full critical path runs unblocked.

# Phase 2.5 — Step-by-Step

Phase 2.5 turns the 864 frozen Phase 2 strategies (72 variants per philosophy times 12 philosophies) into deployable portfolio variants and a Phase 3 cell-selected manifest. The variant registry in `select_phase3_candidates.py` enumerates 20 portfolio variants total: 14 primary, 4 soft-kill diagnostic baselines (`variant_1`, `variant_3`, `variant_5`, `variant_7`), and 2 future-deferred LinUCB overlays (`variant_7c`, `variant_8c`); together with a small set of block-bootstrap baselines emitted by `prompt_7g_portfolio_baselines.py` the assembly stage routes the full deployment universe through a 468-cell `(asset, library)` per-cell selector that cuts roughly 94% of strategy-cell combinations down to about 702 entries when `PHASE3_USE_CELL_MANIFEST=True`.

## Step 1 — prompt_7_portfolio_construction.py (HRP base + LinUCB bandit)

The first portfolio stage builds the canonical HRP allocation across the 864 frozen strategies and overlays the LinUCB regime-adaptive bandit. HRP performs hierarchical clustering on the strategy correlation matrix, then distributes inverse-variance weight down the cluster tree so that highly correlated strategies share a single budget cell. The result is the conservative HRP-A book and an aggressive HRP-B book that lifts the leaf-cluster cap.

Inputs are the per-strategy `net_pnl_pct_at_fixed_exit` series from Phase 2 OOS folds and the regime label sidecar produced by `prompt_7b_regime_detector.py`. Outputs land in `pipeline_state/phase_2_5/` as `hrp_conservative_weights.parquet`, `hrp_aggressive_weights.parquet`, and `linucb_state.pkl`.

The LinUCB layer runs as a satellite overlay on HRP. Context dimension is `LINUCB_CONTEXT_DIM=18` and the regime taxonomy uses `NUM_REGIMES=6`. Anti-whipsaw protection enforces a minimum hold-days window per arm and a transitions-per-window cap; bandit state is reset between training and serialization so the persisted pickle replays deterministically. The 50/50 blend variant averages HRP-A with HRP-B-plus-LinUCB to give a middle book.

## Step 2 — prompt_7c_portfolio_optimizer.py (Neural optimizer)

The neural portfolio optimizer ingests per-strategy expected-return and risk features alongside regime descriptors and emits a learned weight vector. Input dimension is `NEURAL_OPT_INPUT_DIM=103`, decomposed as `PORTFOLIO_TOP_K=75` strategy slots plus `N_REGIME_FEATURES=28` regime descriptors. The model is a bagged ensemble trained via stationary block bootstrap to respect serial dependence in returns.

Inputs include `metacombiner_oos_predictions.parquet`, the regime feature panel, and the HRP weights from Step 1. Outputs are `neural_optimizer_weights.parquet`, an ensemble manifest JSON listing each bagged checkpoint, and a `neural_optimizer_state_dict.pt`. A state-dict shape check fires on load so that any input-dimension drift errors immediately rather than silently zero-padding.

The optimizer feeds variant_7 (`neural_optimizer`) and is consumed by the MetaDD overlay (variant_7b) and the inverse-uncertainty blender (variant_10). Output weights are normalized and clipped to the per-asset concurrency cap downstream.

## Step 3 — prompt_7e_kelly_portfolio.py (Kelly walk-forward)

Kelly-fraction portfolio sizing runs as a walk-forward routine producing variant_8. Each test month is fit on a trailing window with a one-month embargo separating training from the test month so that no within-month cross-fit signal leaks across the boundary. The per-strategy Kelly fraction is capped to a half-Kelly ceiling and projected onto the per-asset concurrency budget.

Inputs are the Phase 2 strategy-level fold returns and the MC approval mask. Outputs are `variant_8_kelly_weights.parquet` and `variant_8_kelly_returns.parquet`, written to `pipeline_state/phase_2_5/` for downstream MetaDD pairing.

The Kelly walker emits per-month weight vectors that the MetaDD overlay scales by inverse `(1 + dd_sigma)` to produce variant_8b. The `canonical_allocator_extensions.py` dispatcher provides additional Kelly-family routines (`variant_19_kelly_max`, `variant_20_kelly_r1r2`, `variant_22_half_kelly_eta`) for downstream baseline expansion.

## Step 4 — prompt_7f_metadd_predictor.py (Student-t MetaDD)

The MetaDD predictor produces forward drawdown forecasts with a Student-t head. For each portfolio day it emits five quantities: `dd_mu` (location), `dd_sigma` (scale), `dd_nu` (degrees-of-freedom shape proxy for tail thickness), and the `dd_q90` and `dd_q95` upper quantiles. Pre-committed thresholds in the overlay code mark `nu < 5.0` as fat-tail, `dd_sigma > 0.08` as high-uncertainty, and `dd_q90 > 0.05` as elevated crash risk.

Inputs are realized portfolio drawdown trajectories from each base variant's walk-forward returns plus the regime feature panel. The output is `metadd_predictions.parquet` with the five Student-t columns indexed by date.

Companion drawdown-quantile heads (`prompt_7f_gp_drawdown.py`, `prompt_7f_lgb_bootstrap_dd.py`, `prompt_7f_xgb_quantile_dd.py`, `prompt_7d_pysr_drawdown.py`) provide model-diversity ensembles. The Student-t MetaDD is the canonical consumer for the overlay siblings.

## Step 5 — prompt_7_metadd_overlay.py (10 MetaDD pair artifacts)

The MetaDD overlay replays each of the 8 base variants through a pre-committed modification rule and emits its `_metadd` twin, then adds two MetaDD-only variants (`variant_9_dd_capped_neural`, `variant_10_portfolio_blender`) for a total of 10 pair artifacts. The modification siblings are `_cap_by_q90` (cap each strategy weight at the `dd_q90` ceiling), `_scale_by_q90` (size by `1 - 2*dd_q90` when `dd_q90 > 0.05`), `_flatten_by_sigma` (push high-`dd_sigma` HRP-A weights toward equal), `_shrink_linucb_by_nu` (low `dd_nu` shrinks LinUCB satellite deltas), `_shift_toward_conservative` (low `dd_nu` shifts HRP-B mass toward HRP-A), `_adjust_ab_blend` (tilt the 50/50 ratio by `dd_nu`), `_scale_kelly_by_sigma`, `_scale_neural_by_sigma`, and `_inverse_uncertainty_blend` for variant_10.

Inputs are `metadd_predictions.parquet` and each base variant's weight series. Outputs are 10 `<variant_id>_weights.parquet` and `<variant_id>_returns.parquet` pairs, with the floor `MIN_WEIGHT_SCALE=0.40` and the ceiling `MAX_BLEND_SHIFT=0.30` enforced inside the rule.

A walk-forward calibration gate flags every `_metadd` variant when the MetaDD calibration slope falls outside `[0.75, 1.25]`; flagged variants are still written but `select_phase3_candidates.py` filters them.

## Step 6 — prompt_7g_portfolio_baselines.py (12-variant block bootstrap)

The baseline stage emits 12 block-bootstrap reference allocations so the model-driven variants have honest non-trivial benchmarks. The block length is sampled from a stationary geometric distribution with mean tuned to the autocorrelation of daily returns, and weights are equal-weight, inverse-vol, mean-variance, risk-parity-notional (`variant_21`), and HERC (`variant_23`) flavors plus several cluster-tree perturbations.

Inputs are the per-strategy daily return panel and the cluster tree from the HRP step. Outputs land in `pipeline_state/phase_2_5/baselines/` as one parquet per baseline variant.

These baselines never get MetaDD twins — they exist to anchor SPA and Holm-Bonferroni gates and to reveal whether learned variants actually clear a non-trivial bootstrap floor.

## Step 7 — prompt_7h_asset_admission.py (22-asset gate)

The asset admission gate enforces the canonical `PHASE2_ACTIVE_ASSETS` whitelist of 22 assets (13 crypto plus 9 stocks). Each asset must clear a 2-of-3 pass rule on `oos_sharpe`, `fold_win_rate`, and `worst_regime_sharpe`; failing this rule excludes the asset from per-cell selection.

Inputs are the per-asset OOS metric tables aggregated across philosophies. Output is `asset_admission_report.parquet` with one row per asset and pass/fail flags per metric, plus an `admitted_assets.json` consumed by the per-cell selector.

The admission gate runs once per Phase 2.5 cycle and its output is consumed by `prompt_7j_per_cell_selector.py` and ultimately by the Phase 3 deploy manifest writer.

## Step 8 — prompt_7j_per_cell_selector.py (468-cell deploy manifest)

The per-cell selector flattens the deployment universe to a 468-cell `(asset, library)` matrix (22 admitted assets times approximately 12 libraries with a few asset-library pairs vacant). For each cell it picks the dominant `(portfolio_variant, strategy_variant, exit_stage, strategy_id)` based on per-cell OOS metrics, then writes the deploy manifest.

Inputs are the variant returns and weights from Steps 1-6, the asset-admission JSON, and the Phase 2 strategy registry. Outputs are `phase3_deploy_manifest.parquet` with rows of schema `(asset, library, portfolio_variant, strategy_variant, exit_stage, strategy_id, schema_version)` and a companion `cells_by_fold` calibration sidecar.

When `PHASE3_USE_CELL_MANIFEST=True` the manifest cuts the universe by roughly 94% from the legacy 11,232-row cross product down to about 702 deployed strategies, and Phase 3 holdout reads the manifest as a deny-default whitelist.

## Step 9 — select_phase3_candidates.py (final variant filter)

The final selector reads `VARIANT_REGISTRY` and partitions variants into `PRIMARY_VARIANTS`, `SOFT_KILL_VARIANTS`, and `FUTURE_DEFERRED_VARIANTS`. Soft-kill variants ship for diagnostic comparison only and do not consume Phase 3 capacity quota; future-deferred entries (`variant_7c`, `variant_8c`) are excluded from enumeration entirely so the runner cannot accidentally try to score an unimplemented overlay.

Inputs are all `<variant_id>_returns.parquet` files plus the MetaDD calibration metadata. Output is `phase3_candidates.json` enumerating which variants enter holdout, with their status tag preserved for the assembly stage.

The selector applies the MetaDD calibration gate to every `_metadd` variant: if `calibration_slope` is outside `[0.75, 1.25]` the variant is dropped from the primary bucket. A primary variant that fails the gate may not be replaced from the soft-kill bucket — the slot stays empty so Phase 3 reflects the actual MetaDD reliability.

Wall-clock estimate: 6-10 hours end-to-end on a single Hopper H100 node, dominated by the neural optimizer bagged ensemble fit (~2-3h) and the MetaDD predictor walk-forward (~2h); HRP, LinUCB, Kelly, baselines, admission, and per-cell selection together complete in under 2 hours.

# Phase 3 — Step-by-Step

Phase 3 runs the 864 frozen strategies (12 philosophies x 72 variants) on the
untouched 2025 holdout slice. The phase performs no retraining and no
threshold tuning; it executes the frozen logic against held-out OHLCV plus
TimesFM features and reports raw metrics. Capital settings are
`INITIAL_CAPITAL = $1,000`, single-position notional ceiling `$50,000`, and
`MAX_LEVERAGE = 20`. The user inspects the resulting reports and decides
whether to deploy.

## Step 0 — validate_phase3_inputs.py

This script is the pre-flight gate that runs before any holdout work begins.
Its purpose is to fail fast when the handoff bundle is incomplete or
contaminated, so a multi-day SLURM allocation does not burn slots on a bad
input set. It executes between nine and fourteen named checks depending on
which optional bundles are present in the run tree.

The script reads the Phase 2 and Phase 2.5 handoff packages, the per-asset
feature stores under `features/phase3/`, the OHLCV mirrors under
`OHLCV/phase3/`, frozen model directories, the snapshot manifest, the cell
deploy manifest at `configs/frozen/phase3_handoff/phase3_handoff_manifest.json`,
and the Phase 1 / Phase 2 experiment registries. It writes nothing; it emits
pass/fail rows on stdout and exits non-zero on any failure.

Verifications include the snapshot SHA digest, presence of all frozen model
files (TimesFM, MetaCombiner, LinUCB, MetaDD, Neural optimizer), the 22-asset
whitelist enforced against `PHASE2_ACTIVE_ASSETS`, the four dimensional regime
columns (`regime_label_trend`, `regime_label_vol`, `regime_label_composite`,
`regime_label_phase`) in every Phase 3 feature store, absence of any Phase 3
contamination row in earlier-phase experiment registries, exit-config SHA
pins matching the handoff manifest, cell-deploy manifest internal
consistency, frozen-artifact read-only mode (`chmod 0o444`), and disk-space
headroom. Any failure aborts the run before Step 1.

## Step 1 — generate_timesfm_phase3.py

This step runs the frozen TimesFM foundation model in inference mode over
the Phase 3 OHLCV window for every active asset. It exists because all
downstream signal generation, ensemble blending, and TimesFM-accuracy
auditing depend on the same TimesFM feature schema produced for Phase 1 and
Phase 2; Phase 3 must compute the same columns from held-out data without
touching the model weights.

The script reads `OHLCV/phase3/{asset}.parquet` and the frozen TimesFM
checkpoint, then writes `features/phase3/{asset}_timesfm.parquet`. Output
columns include the multi-horizon direction signals
`timesfm_4h_direction`, `timesfm_12h_direction`, `timesfm_24h_direction`,
the cross-horizon agreement metric `timesfm_alignment_score`, and the full
`tfm_q*_h*` quantile-band stack across the requested horizons.

Behavior is strictly causal: for bar N, the encoder consumes only
`close[N - CTX .. N - 1]`, never bar N's own close. The output schema
matches Phase 1 and Phase 2 column-for-column so feature-store readers
downstream do not need version-aware loaders. The frozen weights are loaded
read-only and never serialized back to disk.

## Step 2 — prompt_1_holdout_run.py

This is the main execution step. It runs all 864 frozen strategies on the
2025 holdout, producing per-strategy trade ledgers, capped and uncapped
equity curves, and the raw inputs every analysis script consumes. It is the
single largest compute block in the phase.

Inputs are the frozen strategy library at `pipeline_state/phase_2/frozen/`,
the cell deploy manifest, the Phase 3 feature stores including the TimesFM
columns from Step 1, and the Phase 3 OHLCV. Outputs land under
`reports/phase3/{exit_variant}/strategy_{sid}_detail.json` together with a
sibling parquet `strategy_{sid}_trades.parquet` carrying the full trade
ledger for downstream regime breakdown.

For each strategy, the runner pulls the fold-indexed exit assignment via
`selected_exit_by_fold[K]` so ES2 variants honor the same per-fold exit that
was frozen at Phase 2 close. Per-asset concurrency caps cycle through
`[5, 8, 10, 15, 20, 25]`. The simulator compounds equity from
`INITIAL_CAPITAL = $1,000` while clamping any single-position notional at
`$50,000`; both capped and uncapped equity series are emitted so leverage
analysis can decompose the cap impact.

## Step 3 — prompt_2_holdout_analysis.py

This step performs the regime-conditioned breakdown on the strategy outputs
from Step 2. Its purpose is to expose how each strategy behaves across
distinct market regimes rather than reporting a single blended figure.

It reads the per-strategy trade parquets and detail JSONs from
`reports/phase3/{exit_variant}/`, joins them on entry timestamp to the four
regime-label columns in the Phase 3 feature stores, and writes
`reports/phase3/regime_analysis/regime_breakdown_{sid}.json` plus an
aggregate roll-up.

The breakdown dimensions are trend, volatility, composite, and phase. For
each strategy and each regime bucket the script computes trade count, win
rate, profit factor, net PnL, and average holding bars. The aggregation
step ranks strategies within each regime so downstream consumers can
identify regime specialists versus all-weather performers.

## Step 4 — prompt_3_ensemble_contribution.py

This step runs the ensemble ablation, attributing realized PnL to each of
the eleven component models inside MetaCombiner. It exists so the
ensemble's holdout edge can be decomposed and the user can tell which
components actually carry signal on out-of-sample data.

It reads the strategy detail JSONs and trade parquets from Step 2 along
with the frozen MetaCombiner checkpoint and component-model artifacts. It
writes `reports/phase3/ensemble_summary.json` with per-component
contribution weights and leave-one-out delta metrics.

The script replays each trade through MetaCombiner with one component
masked at a time. The delta in approval rate, hit rate, and net PnL is
attributed to that component. Component contributions are reported raw;
no thresholds are tuned and no components are dropped during Phase 3.

## Step 5 — prompt_4_leverage_analysis.py

This step quantifies how the `$50,000` single-position cap and the
`MAX_LEVERAGE = 20` ceiling reshape strategy outcomes. It exists because
the capped versus uncapped equity curves emitted by Step 2 must be
reconciled into a single leverage-attribution table for the deployment
report.

It reads both equity curves and the trade parquet for every strategy and
writes `reports/phase3/leverage_summary.json`. The summary table reports
per-strategy capped Sharpe, uncapped Sharpe, the dollar shortfall caused
by the position cap, the count of trades that hit the cap, and the
fraction of equity growth attributable to compounding under the cap.

The metric set is fixed at the schema frozen at Phase 2 handoff. No
strategies are filtered, re-ranked, or dropped. The output is a transparent
attribution that the user reads alongside the regime breakdown.

## Step 6 — prompt_5_model_agreement.py

This step measures cross-model agreement among the eleven MetaCombiner
components on the holdout. Its purpose is to surface ensembles where
agreement collapsed out-of-sample, an early-warning signal for regime
shift even when realized PnL still looks acceptable.

The script reads the per-trade component prediction columns embedded in
the Phase 3 detail JSONs and writes
`reports/phase3/agreement_summary.json`. It computes pairwise prediction
correlation, majority-vote rate, and the fraction of approvals where every
component agreed in sign.

Agreement metrics are reported per strategy, per regime bucket, and per
asset. The script does not reweight or recalibrate the ensemble; it only
reports.

## Step 7 — prompt_6_timesfm_accuracy.py

This step audits TimesFM's directional forecast accuracy on the holdout
window. It exists to make sure the foundation-model signals carrying
weight inside MetaCombiner actually predict the right direction at the
horizons the ensemble consumes them.

It reads the Phase 3 TimesFM feature parquets from Step 1 and the raw
Phase 3 OHLCV. It writes `reports/phase3/timesfm_accuracy.json` with
per-horizon hit rates, alignment chi-square statistics, and adjusted
p-values.

Ground truth `actual_direction` is computed as
`sign(close[fill_bar + H] - close[fill_bar])` from raw OHLCV, never from
realized trade PnL, so the audit is untainted by exit overlays or sizing.
Two independent Holm-Bonferroni passes run side by side: an alignment
chi-square family of `m = 864` (one per strategy) and a direction-accuracy
binomial family of `m = 2592` covering the three horizons `{4h, 12h, 24h}`
across all strategies. Raw and adjusted p-values are reported; nothing is
gated on them inside Phase 3.

## Step 8 — prompt_7_portfolio_holdout.py

This step evaluates the 20 portfolio variants on the same frozen trade
universe produced in Step 2. Its purpose is to compare allocator designs
under identical entry, sizing, and cost assumptions so any performance
delta is attributable to the allocator alone, not to upstream selection.

The script reads the strategy trade parquets, the frozen portfolio
artifacts under `data/portfolio/phase2_5/` (HRP weights, regime bandit,
neural optimizer, MetaDD pair sidecars), and the variant registry. It
writes per-variant outputs under `reports/phase3/portfolio_holdout/` and a
roll-up across all 20 variants.

The 20 variants span HRP conservative and aggressive baselines, two LinUCB
satellites, the 50/50 blend, the neural optimizer, the analytic Kelly
portfolio, eight MetaDD-overlaid twins of those primaries, plus baseline
and soft-kill diagnostics. The LinUCB context builder operates over an
18-dimensional context vector across six regimes, and the bandit's
anti-whipsaw state is reset on every overlay entry so prior in-sample
adaptation cannot leak into the holdout decision sequence.

## Step 9 — prompt_8_exit_comparison.py

This step compares the production exit overlays (`fixed`, `atr_scaled`,
`dist_exit` plus the shadow-only `sigma_exit` channel) on the same MC
trade universe. It exists so the relative merit of each overlay is
visible without permitting any of them to influence MC training, which
remains label-blind.

It reads the per-strategy detail JSONs and trade parquets and writes
`reports/phase3/exit_comparison/` with per-overlay aggregate metrics and
per-strategy deltas against the `fixed` baseline. Realized PnL per overlay
is recomputed on-the-fly from the bake-off; no per-overlay sidecar
parquets are written.

The comparison preserves identical entries, identical MC approval, and
identical execution costs across overlays so the only varying axis is the
exit method. Exit selection itself respects the per-cell calibration gate
and the fold-indexed `selected_exit_by_fold` assignment that was frozen at
Phase 2 close.

## Wall-clock estimate

End-to-end Phase 3 runs in roughly 18-26 hours on Hopper: ~2-3 hours for
TimesFM Phase 3 generation, 12-18 hours for the 864-strategy holdout
execution under the per-asset concurrency caps, and 3-5 hours for the
combined regime, ensemble, leverage, agreement, TimesFM-accuracy,
portfolio, and exit-comparison analyses running in parallel.
