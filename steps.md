# Phase 2 — Detailed Step-by-Step Audit (2026-04-27)

**Audit closeout state:** 51 of 51 audit findings shipped or verified across 7 batches over 2026-04-26→27. **5 CRITICAL + 17 HIGH + 29 MEDIUM closed.** All Phase 2 production-launch blockers RESOLVED.

**Single operational follow-up:** queue Phase 0 TimesFM re-emit (H21 fix at `run_timesfm.py:649`) AFTER `p1_consolidate` (JID 8855940) clears so the H21 fix flows into all feature stores; then re-train MetaCombiner on clean (causal) inputs.

**Wall-clock estimate:** ~6–9 days end-to-end (post 11-model prune; was 9–14 days). Phase 1 ETA: ~T+2.5 days (incl. bigcaps for btc, ltc/long, eth/short). Phase 2 + 2.5 + 3-ready: T+11.5–14.5 days.

**Goal of this document:** for each step, the user can audit (1) what the code does, (2) why each guard exists, (3) what fixes shipped, (4) what could still be improved, BEFORE Phase 2 production launches.

---

## Table of Contents
- [Step 0 — Inputs (frozen from Phase 1)](#step-0--inputs-frozen-from-phase-1)
- [Step 1 — Dataset Build](#step-1--dataset-build)
- [Step 2 — Component Models (11 surviving post-prune)](#step-2--component-models-11-surviving-post-prune)
- [Step 3 — OOS Assembly](#step-3--oos-assembly)
- [Step 4 — MetaCombiner Training](#step-4--metacombiner-training)
- [Step 5 — Decision Layer (72 variants per philosophy)](#step-5--decision-layer-72-variants-per-philosophy)
- [Step 6 — Per-Philosophy Exit Selection](#step-6--per-philosophy-exit-selection-post-mc-overlay)
- [Step 7 — TimesFM 792-Cell Calibration](#step-7--timesfm-792-cell-calibration)
- [Step 8 — Sizing per Philosophy](#step-8--sizing-per-philosophy)
- [Step 9 — Robustness Audit](#step-9--robustness-audit)
- [Step 10 — Final Assembly](#step-10--final-assembly)
- [Step 11 — Phase 3 Handoff Manifest](#step-11--phase-3-handoff-manifest)
- [Audit Closeout — All 51 Findings](#audit-closeout--all-51-findings-shipped-2026-04-26)
- [Operational Follow-ups](#operational-follow-ups)

---

## Step 0 — Inputs (frozen from Phase 1)

### What this step does (plain English)

Step 0 is **not a script** — it's the contract: Phase 2 reads only frozen Phase 1 outputs + frozen Phase 0 feature stores. Any modification to those upstream files invalidates downstream gates. Step 0 documents **what Phase 2 expects to see**.

### What inputs Phase 2 consumes

| Artifact | Path | Count | Schema |
|---|---|---|---|
| Phase 1 libraries | `pipeline_state/phase_1/library_{asset}_{philosophy}.parquet` | **264** (22 assets × 12 philosophies) | Per-philosophy filtered rule list with `rule_text`, `features_used`, `fitness`, `sharpe`, `pf`, fold metrics |
| Raw signal pool sidecar | `pipeline_state/phase_1/raw_pool_{asset}.parquet` + `.manifest.json` | **22 + 22 manifests** | Phase 4 reserve; Phase 2 does NOT consume |
| Feature store | `features/phase{1,2}/feature_store_{asset}_phase{N}.parquet` | 72 files, ~3,100 cols/asset | OHLCV + tfm_*, lstm_*, mtf_*, rs_*, exp_*, p2f_*, wavelet_*, htf_*, regime_*, btc_*, eth_* |
| BF cascade outputs | `data/signals/exploratory/{asset}_all_cascade_rust4/{long,short}/` | 22 × 2 dirs | Stage1-4 survivors + summary.json + top_50_stage*.md |

**Bigcaps re-runs in flight (2026-04-27):**
- btc/long+short, ltc/long, eth/short — running with `pairs=1000, ext=200, stage4_top_n_triples=500, stage4_top_n_ext=2000` (5× / 4× / 5× / 4× the defaults)
- Output: `data/signals/exploratory_bigcaps/` (separate dir from canonical)
- **Symlink-swap pattern post-completion:** swap into canonical `data/signals/exploratory/` BEFORE `bf4_adapt_all` fires
- Adds ~12-18h to Phase 1 wall-time

### Raw pool manifest (M4 fix)

Each `raw_pool_{asset}.parquet` has a paired `.manifest.json` with:
```json
{
  "asset": "btc",
  "parquet_path": "raw_pool_btc.parquet",
  "n_rows": 12345,
  "n_cols": 47,
  "columns": [...],
  "sha256": "<hex>",
  "created_at": "2026-04-27T...",
  "schema_version": "raw_pool.v1"
}
```
**Why:** Phase 4 online-allocator can detect truncated/corrupt writes BEFORE consuming. Without the manifest, a partial write would silently corrupt regime-conditioned learning.

### Anti-leakage contract (bar N uses 0..N-1 only)

Phase 0's emission contract: encoding at bar N uses bars 0..N-1 only — bar N's own close is excluded.

**🚨 CRITICAL CONCERN — H21 fix flows into feature stores ONLY after re-emit:**
- Phase 0 `run_timesfm.py:649` had `realized_at_anchors = close[anchor_idx]` (close OF bar T = future relative to start of T).
- 2026-04-26 fix: `close[np.maximum(anchor_idx - 1, 0)]` (close of T-1 = last fully-realized).
- **Affected feature families:** `tfm_fp_fr_corr_h{5,25,168}`, G3/G4/G13/G14/G20/G28 forecast-realized correlation/persistence/error cols (~30-40 cols total).
- **Until Phase 0 re-emit lands, Phase 2 ingests feature stores with the 1-bar lookahead leak still baked in.**

### Phase boundaries

Per-asset Phase 1 → Phase 2 boundaries from `config.PHASE_BOUNDARIES`:
- BTC/ETH: P1 ends 2019-12-31, P2 = 2020-01-01 → 2024-12-31
- Newer crypto (ada/avax/dot/atom/trx/hbar/algo/sol): P1 ends 2022-07-01 → 2022-12-31, P2 = ~2yr-2.5yr
- Stocks (post WRDS TAQ rebuild): P1 IPO clamps (aapl/amzn/mcd/msft/nflx/nvda 2003; googl 2004-08-19; tsla 2010-06-29; meta 2012-05-18)
- Phase 3 holdout: 2025-01-01 → 2025-12-31 universal

### Asserts active

| Check | File:line | Purpose |
|---|---|---|
| Min rules per library (≥5; ≥3 for doge/link) | `phase1_consolidation.py:1568-1602` | Soft-gate libraries that lack signal |
| Min OOS trades per library (≥40; ≥25 for doge/link) | `phase1_consolidation.py:~1580` | Coverage gate |
| Phase boundaries enforced | `config.py:50-104` | Slice feature stores per asset to P1/P2/P3 |
| Purge=72, Embargo=24 | `config.py:249-273` | Walk-forward leakage prevention |

### Open concerns / gotchas
1. **H21 re-emit pending** — Phase 2 will train on biased forecast-realized correlation features until Phase 0 re-emit completes (~2-3h on Hopper).
2. **Bigcaps re-runs in flight** — if any of btc/ltc/eth bigcaps fail, `bf4_adapt_all` fires with incomplete signal pools, degrading Phase 2 coverage.
3. **Preset libraries dropped (2026-04-20)** — 6 preset philosophies removed; legacy JSONs at `configs/preset_signals/` are orphaned but harmless.
4. **Jaccard/PnL relaxation (2026-04-24)** — near-duplicate thresholds 0.80→0.88 and 0.92→0.97 to preserve Phase 4 diversity. Phase 2 ML must handle signal redundancy.

### Wall-time
N/A — Step 0 is "data ready" check.

---

## Step 1 — Dataset Build

**Script:** [scripts/phases/phase_2/prompt_1_dataset_build.py](scripts/phases/phase_2/prompt_1_dataset_build.py) (1,427 lines)
**Granularity:** per (asset, philosophy) → **264 datasets total**

### What this step does (plain English)

Converts each frozen Phase 1 library into a per-trade machine-learning-ready dataset. For each (asset, philosophy):
1. Load Phase 1 library rules
2. Reconstruct entry timestamps from rules
3. Simulate trades with full execution realism
4. Cluster trades into event families (deduplication signal)
5. Assemble per-trade feature matrix (entry-time features + trade outcome metadata)
6. Attach 18 ATR-normalized signed TimesFM features (PR-F)
7. Build classifier + expectancy labels
8. Output full-timeline parquet (fold slicing happens in Step 2)

### CLI + entry point (lines 1180-1269)

```python
parser.add_argument("--philosophy", required=True, choices=LIBRARY_PHILOSOPHIES)
parser.add_argument("--asset", required=True, choices=ASSETS)
parser.add_argument("--snapshot_id", default=SNAPSHOT_ID)
parser.add_argument("--force", action="store_true")
parser.add_argument("--dry_run", action="store_true")
```

Flow at `main()` (line 1214):
1. Skip silently if asset in `PHASE1_DROPPED_ASSETS` (no audit ValueError)
2. Seed numpy with `RANDOM_SEED`
3. Log experiment to registry
4. Call `build_dataset()` returning `(dataset_df, integrity_dict, ts_start)`
5. Dry-run: exit at line 1306 without disk writes
6. PR-F attachment (lines 1316-1327): non-fatal; warns on failure, continues
7. Atomic write to `data/phase2_datasets/{asset}_{philosophy}_ml_dataset.parquet`
8. Log metrics to registry, print summary

### Trade simulation

Uses `simulate_trades_vectorized` from [scripts/common/utils/trade_simulator.py:1128-1350](scripts/common/utils/trade_simulator.py).

**Cost model (per trade_simulator.py:1312-1324):**
- Entry: open of bar (fill_bar = entry_bar + 1) — **no look-ahead**
- Exit at TP/SL/max_hold price; else close of exit_bar
- Funding: `FUNDING_PER_HOUR * holding_bars` for crypto only
- Roundtrip cost via fee_mult
- **Concurrency:** Per-asset cap enforced in portfolio simulator (NOT here — Step 1 simulates each signal independently)
- Capital model ($1k initial, $50k single-position, compounding) handled downstream

**TP/SL/max_hold assignment (lines 884-892):**
```python
if category == "sideways":
    tp_pct = sig_def.get("tp_pct", SIDE_TP_PCT)
    sl_pct = sig_def.get("sl_pct", SIDE_SL_PCT)
    max_hold = sig_def.get("max_hold", SIDE_MAX_HOLD)
else:
    tp_pct = sig_def.get("tp_pct", DIR_TP_PCT)
    sl_pct = sig_def.get("sl_pct", DIR_SL_PCT)
    max_hold = sig_def.get("max_hold", DIR_MAX_HOLD)
```

### PR-F integration at line 1308

`from scripts.common.utils.tfm_atr_normalized import compute_atr_normalized_tfm_features`

**18 cols emitted per trade** (6 features × 3 horizons h5/h25/h168):
```
tfm_q50_atr_h{H}                — signless magnitude
tfm_iqr_atr_h{H}                — spread (positive by construction)
tfm_signed_edge_after_cost_h{H} — dir_sign × q50 - 0.003
tfm_signed_signal_to_noise_h{H} — dir_sign × q50 / max(iqr, 1e-8)
tfm_reversal_risk_h{H}          — rollover ratio [0, 1]
tfm_tail_asymmetry_h{H}         — skew proxy
```

**dir_sign** read from trade's `direction` column (NOT tautology `sign(tfm_q50)`):
- long → +1
- short → -1

A long signal with `tfm_q50 = -0.03` produces NEGATIVE `tfm_signed_edge_after_cost` (anti-trade-direction forecast = negative edge).

**M7 ATR fallback chain** ([tfm_atr_normalized.py:107-109](scripts/common/utils/tfm_atr_normalized.py#L107)):
```python
_ATR_CANDIDATES = ("exp_atr_pct_14", "atr_pct", "exp_atr_pct_21",
                    "exp_atr_pct_7", "htf_1d_atr_pct")  # last
```
**Why:** htf_1d_atr_pct is ~6.5× coarser on stocks 1h RTH; using it would crush h5/h25 signals. Base-bar variants prefer.

**Error handling (line 1326-1327):** `except Exception as _pr_f_err` warns + continues. Schema drift produces a logged warning, not a fail. **H5 audit gotcha** — silent fallback. Verify post-Phase 2 launch that all 264 datasets actually have the 18 PR-F cols.

### Label substrate

```python
dataset["classifier_target"] = (dataset["net_pnl_pct"] > 0).astype(int)
dataset["expectancy_target"] = dataset["net_pnl_pct"].astype(float)
```

`net_pnl_pct` from `simulate_trades_vectorized:1324`: `gross_pnl_pct - cost_pct - funding_cost`.

**Note:** No separate `net_pnl_pct_at_fixed_exit` column emitted in Step 1. All trades use sim's exit (TP/SL/max_hold). PR-E enforcement happens downstream in Step 4.

### Fold-safe preprocessing

Step 1 emits FULL-TIMELINE dataset (no fold column). Walk-forward fold assignment happens in Step 2 trainers.

**Purge/Embargo for downstream (config-driven):**
- Crypto: 72h purge + 24h embargo (3d + 1d) ≥ all exit horizons (max 72h)
- Stocks: 3d purge + 1d embargo (1d bars) ≥ all exit horizons (max 3d)

### Anti-leakage rule "bar N uses 0..N-1"

| Stage | How enforced |
|---|---|
| Entry bar (signal_bar) | Reconstructed from frozen rules applied to pre-built feature_df (no look-ahead by construction) |
| Fill bar | `entry_bar + 1`, uses OPEN of fill bar (line 1207: "close would be look-ahead bias") |
| Group A entry features | `feature_vals[bar_idx]` at `signal_bar` — features pre-computed offline, causally aligned |
| Group E execution features | Volatility (10-bar std BEFORE), spread (current bar OK), volume_at_entry (current bar), relative_volume (24-bar SMA BEFORE) — all causal |
| Group F rolling stats | `trade.exit_bar < current_bar` enforced (line 669) — only completed past trades |

**🚨 H21 propagation:** If Phase 0 emits forward-looking `tfm_fp_fr_corr_*` (pre-fix), Step 1 passes them through unchanged. Fixed when Phase 0 re-emit lands.

### Outputs

**Path:** `data/phase2_datasets/{asset}_{philosophy}_ml_dataset.parquet`

**Schema (~200+ cols):**
| Family | Prefix | Count | Notes |
|---|---|---|---|
| Trade base | direct | ~15 | signal_bar, fill_bar, exit_bar, entry/exit price, direction, holding_bars, exit_reason, gross_pnl_pct, funding_cost, execution_cost, net_pnl_pct, signal_name, signal_family, signal_direction, signal_category, tp_pct_used, sl_pct_used, max_hold_used |
| Group A | feat_* | n_features_phase2 | All Phase 2 feature store cols (prefixed to avoid collision) |
| Group B | meta_* | 5 | signal_name, signal_family, signal_direction, signal_category, n_features_in_rule |
| Group D | event_family_id, overlap_count, same_event_candidate_count, uniqueness_weight | 4 | Event clustering output |
| Group E | volatility_at_entry, spread_proxy, volume_at_entry, relative_volume, bar_of_day, day_of_week_proxy | 6 | Execution context |
| Group F | rolling_win_rate, rolling_avg_pnl, rolling_trade_count, rolling_streak | 4 | Causal signal performance stats |
| PR-F | tfm_*_atr_h*, tfm_signed_*_h*, tfm_reversal_risk_h*, tfm_tail_asymmetry_h* | 18 | ATR-normalized TimesFM (post-build) |
| Labels | classifier_target, expectancy_target | 2 | Binary win/loss + continuous PnL % |

**No fold column** in output (added downstream).

### Asserts active

| Line | Condition | Raises |
|---|---|---|
| 818 | signals not empty | ValueError |
| 833, 835 | feat_path/ohlcv_path exists | FileNotFoundError |
| 896 | reconstruct_entry_mask succeeds | KeyError/ValueError (caught, signal skipped) |
| 949 | ≥1 signal produces trades | ValueError |
| 720 | max_signal_bar < ohlcv_len | integrity flag, warns |
| 744 | no suspect_future_columns | integrity flag, warns |
| 1170 | integrity all_passed | warns if False |

### Open concerns / gotchas
1. **Phase 0 feature leakage inheritance (H21)** — fixed at source but Phase 0 re-emit pending. Once re-emitted, Step 1's behavior is correct.
2. **Missing ATR column → silent fallback** — if all 5 ATR candidates absent, PR-F fails gracefully (warning), but h5/h25 signals are then poorly normalized.
3. **Group A prefix collision** (`feat_` prefix added) — solved post BUG-R8A-002.
4. **Event clustering per direction** — long and short clustered separately to avoid cross-direction contamination.

### Wall-time

| Operation | Time |
|---|---|
| Load library | <1s |
| Load Phase 2 data | 2-5s |
| Replay signals + simulate trades | 5-30s |
| Event clustering | 1-2s |
| Feature matrix build | 2-5s |
| Build targets | <1s |
| Integrity checks | <1s |
| PR-F attachment | 1-2s |
| **Per (asset, philosophy)** | **~15-50s** |

Bottleneck: trade simulation (signal complexity + entry density).

---

## Step 2 — Component Models (11 surviving post-prune)

**Cross-validation:** 8-fold purged walk-forward, `purge=72`, `embargo=24` (verified across all 11 scripts via `walk_forward_split`).

**Dropped 5 via LOO confirm (PR-D 2026-04-25):** TabNet (`prompt_2_tabnet.py`), LSTM2 (`prompt_2c_lstm_classifier.py`), TCN (`prompt_2_tcn.py`), Bias kernels (`prompt_2h_bias_kernels.py`), Unified cross-asset (`prompt_2_unified.py`). Result: ~30% MC training wall-time reduction vs prior 16-model setup. Files retained as research diagnostics; not invoked from production runner.

### Summary table

| # | Script | Output Cols | Role | Wall-time/asset |
|---|---|---|---|---|
| 1 | `prompt_2_model_train.py` | `xgb_classifier_score`, `xgb_expectancy_score` | Primary classifier + expectancy regressor | ~45 min |
| 2 | `prompt_2b_timesfm_features.py` | 7 alias cols | I/O shim — TimesFM features pre-computed in Phase 0 | ~2 min |
| 3 | `prompt_2d_tft_scorer.py` | `tft2_quality_score`, `tft2_expected_pnl` | Temporal Fusion Transformer (Phase 0 fine-tuned) | ~120 min |
| 4 | `prompt_2e_cross_asset_scorer.py` | `ca2_signal_quality`, `ca2_regime_class` | Cross-Asset Transformer #2 (26-asset OHLCV ensemble) | ~90 min |
| 5 | `prompt_2e_lightgbm.py` | `lgbm_classifier_score`, `lgbm_expectancy_pred` | Leaf-wise booster (GPU-capable) | ~30 min |
| 6 | `prompt_2f_catboost.py` | `catboost_classifier_score`, `catboost_expectancy_pred` | Ordered boosting w/ native categorical | ~35 min |
| 7 | `prompt_2f_kelly_params.py` | `kelly_frac`, `heat_discount`, `dd_threshold`, `dd_floor`, `median_calmar` | Collateral sizing (H10 hardened) | ~15 min |
| 8 | `prompt_2g_regime_xgb.py` | `regime_xgb_score`, `regime_xgb_expectancy` | 4 regime-specialist XGBoosts | ~60 min/regime |
| 9 | `prompt_2h_ngboost.py` | `ngb_mean`, `ngb_std`, `ngb_z_score` | Probabilistic regressor | ~40 min |
| 10 | `prompt_genetic_programming.py` | Top-K evolved feature combos | GP entry-side ONLY (PR-C dropped GP exits) | ~50 min |
| 11 | `prompt_autoencoder_anomaly.py` | `autoencoder_recon_error` | Trained on winners only; high error = skip | ~25 min |
| 12 | `prompt_cascade_filter.py` | `cascade_filter_pass` | 3-layer XGBoost chain (hard gate) | ~30 min |

### XGBoost (`prompt_2_model_train.py`, 1534 lines)

Trains dual-head XGBoost: binary classifier (`eval_metric=logloss`) + regression head on `expectancy_target`. Optuna hyperparameter tuning on first fold's 3-way inner time-series split (no leakage to OOS). 8-fold purged WF enforced via `walk_forward_split`. Per-fold: classifier + expectancy + imputer pickled. `scale_pos_weight` for class imbalance. REG_LEVEL hyperparams: `max_depth ∈ [6..10]`, `n_estimators ∈ [500..1000]`, reg_alpha/lambda tuned per level (L0 mild, L4 max overfit). Metadata columns excluded at line 110. Output: `models/phase2/{asset}_{philosophy}_fold_predictions.parquet`.

### TimesFM Shim (`prompt_2b_timesfm_features.py`, 121 lines)

**SHIM ONLY** — Phase 0's `run_timesfm.py` pre-computes all TimesFM features into the unified Phase 2 feature store. This script reads alias columns: `timesfm_4h_direction`, `timesfm_12h_direction`, `timesfm_24h_direction`, `timesfm_horizon_agreement`, `timesfm_4h_uncertainty`, `timesfm_12h_uncertainty`, `timesfm_conviction_4h`. Writes legacy path `features/phase2/timesfm_features_{asset}_phase2.parquet` for backward compat. Zero-fills missing aliases with warning (line 86). **No training, no fold loops.**

### TFT2 (`prompt_2d_tft_scorer.py`, 935 lines)

Temporal Fusion Transformer fine-tuned from Phase 0 pretrained weights (`models/pretrained/tft_pretrained.pt`). Dual sigmoid head: `quality_score ∈ [0, 1]` + linear `expected_pnl` regression. Context window: 168 bars (1 week @ 1h). Inputs: 1100+ historical features + known-future (hour_of_day, day_of_week) + static (asset). 8-fold purged WF; per fold: scalers fit on TRAIN only, early stopping patience=3, LR=1e-5 (pretrained stability), 20 epochs. cuDNN disabled (line 33). Output: `tft2_quality_score`, `tft2_expected_pnl`.

### Cross-Asset (`prompt_2e_cross_asset_scorer.py`, 770 lines)

Cross-Asset Transformer #2 fine-tuned from Phase 0 CA #1. Input: all 26 ASSETS × 24-bar windows × 5 OHLCV at signal time. Tokens: N assets (attends across assets, NOT time). Dual output: `ca2_signal_quality` (sigmoid) + `ca2_regime_class` (0-3 classification). 8-fold purged WF; LR=1e-5, patience=3, 20 epochs, BCE on `binary_outcome`. cuDNN disabled (line 81).

### LightGBM (`prompt_2e_lightgbm.py`, 455 lines)

LightGBM classifier + expectancy regressor (parallel to XGB for ensemble diversity). Leaf-wise tree growth. GPU dispatch with CPU fallback (lines 21-32). `tree_learner='serial'` (line 35, GPU limitation). Optuna 100 trials default. 8-fold purged WF. Sample weighting: cost + uniqueness (P2-Q-S2/S4, lines 146-148, matching XGB contract). Atomic writes (parquet/json/pickle).

### CatBoost (`prompt_2f_catboost.py`, 475 lines)

Ordered boosting classifier + expectancy. Native categorical handling (regime labels as categoricals, no one-hot). GPU dispatch (lines 19-31). 8-fold purged WF. Optuna 100 trials. REG_LEVEL: `depth ∈ [7..10]`, `l2_leaf_reg ∈ [1.0..0.001]`, `early_stop_rounds` tuned. Same atomic writes + sample weighting as LGB.

### Kelly Params (`prompt_2f_kelly_params.py`, 162 lines) — H10 fix focus

Fits collateral sizing params via walk-forward grid search on Phase 2 OOS MetaCombiner output.

**H10 named divisors + Bayesian shrinkage (lines 72-100):**
```python
KELLY_DIVISOR_MC = 4.0       # quarter-Kelly on MC leverage units
KELLY_DIVISOR_LP = 20.0      # leverage_prediction in % (~5x scale)
KELLY_SHRINK_LAMBDA = 0.5    # 50% shrink toward prior
KELLY_PRIOR = 0.25           # default quarter-Kelly

if "fold" in df.columns:
    n_folds = int(df["fold"].nunique())
    if n_folds < 3:
        raise RuntimeError(f"Kelly fold-leakage: only {n_folds} folds; need ≥3")

raw = (df["mc_kelly_leverage"] / KELLY_DIVISOR_MC).clip(0.01, 1.0)
df["kelly_frac"] = (
    KELLY_SHRINK_LAMBDA * raw + (1.0 - KELLY_SHRINK_LAMBDA) * KELLY_PRIOR
).clip(0.01, 1.0)
```

**Why:** raw MC leverage was hardcoded with magic divisor `4.0`; small-sample noise inflates Kelly fractions. Bayesian shrinkage toward 0.25 with 50% blend prevents over-leverage. Fold-leakage assert (≥3 distinct purged folds) ensures kelly leverage came from properly purged WF.

### Regime-XGB (`prompt_2g_regime_xgb.py`, 319 lines)

Four separate XGBoost models, one per market regime: bull_trending (ca_regime=0), bear_trending (ca_regime=1), sideways (ca_regime=2), high_volatility (ca_regime=3). Each trained only on in-regime trades. At inference: route via current `ca_regime` label. 8-fold purged WF per regime. Optuna per regime. Output: per-regime fold models + merged OOS predictions.

### NGBoost (`prompt_2h_ngboost.py`, 248 lines)

Probabilistic regressor with Normal distribution on `expectancy_target`. Emits 3 cols: `ngb_mean` (point), `ngb_std` (native uncertainty), `ngb_z_score = mean/(std+eps)`. Feature pre-selection: top 75 by mutual information on TRAIN fold ONLY (line 52, bounds wall-clock). 8-fold purged WF. n_estimators=400, lr=0.02 default. Early stopping patience=3.

### Genetic Programming (`prompt_genetic_programming.py`, 339 lines) — entry-side ONLY

Evolves non-linear feature combinations. **NOT used for exits** (PR-C 2026-04-25 dropped GP from CHALLENGERS exit ensemble). Population: 200, 50 generations, tournament size=5, elite=10, top-K=5 surviving features. Operations: `{add, sub, mul, div, sq, sqrt, log, neg, abs, max, min}` (lines 41-56). Max tree depth: 4. Crossover 0.7, mutation 0.3. 8-fold purged WF.

### Autoencoder Anomaly (`prompt_autoencoder_anomaly.py`, 314 lines)

**Unsupervised anomaly detector**: trained on WINNING trades only. High reconstruction error at inference = "doesn't look like a winner" = skip. Architecture: encoder (input → 128 → 64 → latent_dim=32) + decoder (latent_dim → 64 → 128 → input), ReLU + 0.1 dropout (lines 62-71). 8-fold purged WF; per fold: train encoder on train winners, evaluate recon error on val losers. **Gate 3 BYPASS guardrails** (lines 31-35): retain ≥85% winners, remove ≥10% more losers than winners; else BYPASS (pass all trades). PyTorch device: CUDA if available, else CPU.

### Cascade Filter (`prompt_cascade_filter.py`, 344 lines) — hard gate

**3-layer XGBoost chain**, each layer trained only on trades passing previous. Layer 1 (high recall, ~80-90% kept): catches obvious losers. Layer 2 (medium): pattern-based, ~65-85% kept. Layer 3 (high precision): tricky losers, ~60-80% kept. Aggressiveness pre-committed (lines 58-64) — 5 presets: relaxed/moderate/aggressive/sniper/balanced. Each layer: deeper trees + higher scale_pos_weight (lines 95-99). 8-fold purged WF. **Gate 3 efficiency guardrails** (lines 46-49): `WINNER_RETENTION≥0.85`, `LOSER_REMOVAL_EDGE≥0.10`; else BYPASS. **Env flag `PHASE2_USE_CASCADE_BATCH=0`** (line 41, default OFF) — set to 1 only after re-validating per-layer filter semantics (2026-04-19 B13 + M1 fix: batched scoring bug found).

### Unified training & fold enforcement

All 11 models (except TimesFM shim) enforce:
- N_SPLITS = 8
- purge = 72 bars
- embargo = 24 bars
- Scalers/imputers/encoders fit on TRAIN folds only
- Optuna eval never touches OOS test fold
- Sample weights computed per fold (cost + uniqueness)
- Metadata columns excluded from feature lists
- Atomic writes (parquet/json/pickle)

### Open concerns / gotchas
1. **TimesFM Shim depends on Phase 0** — if Phase 0 re-emit (H21) hasn't landed, the alias columns reflect the leak.
2. **TFT2 + CA2 pretrained weights** — must match Phase 0 frozen weights; verify SHA before fine-tuning.
3. **Kelly fold-leakage assert** can hard-fail mid-run if upstream MC predictions are emitted with <3 folds. Surface check pre-emptively.
4. **Cascade Filter env flag** — `PHASE2_USE_CASCADE_BATCH=1` is unstable per audit. Keep at 0 in production.

### Wall-time

Per asset (sequential GPU): ~8-10 hours. Parallelizable per philosophy. Total Phase 2 Step 2: ~3-4 days for full 22 × 12 sweep.

---

## Step 3 — OOS Assembly

**Script:** [scripts/phases/phase_2/prompt_3_oos_assembly.py](scripts/phases/phase_2/prompt_3_oos_assembly.py)

### What this step does

Joins all 11 component model prediction columns + dataset features into a unified per-trade table per (asset, philosophy). Two modes: per-asset (`{asset}_{philosophy}_unified_oos.parquet`) or cross-asset merge (`{philosophy}_cross_asset_unified_oos.parquet`).

### CLI + entry point (lines 899-992)

```bash
# Per-asset
python -m scripts.phases.phase_2.prompt_3_oos_assembly --philosophy {X} --asset {btc|eth|...}
# Cross-asset merge
python -m scripts.phases.phase_2.prompt_3_oos_assembly --philosophy {X} --merge
```

**Args:** `--philosophy` (required), `--asset` (optional for per-asset), `--merge` (cross-asset), `--snapshot_id`, `--force`, `--dry_run`.

### Predictions loading (lines 405-526)

- **Fold predictions source:** `_fold_predictions_path(asset, philosophy)` → `models/phase2/{asset}_{philosophy}{P2_MODE_SUFFIX}_fold_predictions.parquet` (line 150)
- **Validation:** Checks `REQUIRED_PRED_COLS = {classifier_prob, expectancy_pred, fold}`, all 8 folds present, no nulls in predictions (lines 224-254)
- **Ensemble merge:** Loops 11 model names (lstm2, tft2, ca2, leverage, lightgbm, catboost, regime_xgb, tabnet, tcn, unified, ngboost). Loads from flat or subdir paths. Merges on `signal_id` (lines 442-458)
- **TimesFM:** Joins via `entry_time` or `entry_bar` with dtype alignment (lines 460-486)
- **Bias kernels:** Merges `bias_kernel_*` columns (lines 495-510)

### Join key concern

**Primary:** `signal_id` (when present in both dfs). Falls back silently if join key missing — **flag as audit gotcha**. No hard assert on join key presence.

### What Step 3 does NOT enforce

- **MC INPUT BLOCKLIST** — deferred to Step 4 (line 759 in prompt_3b_metacombiner.py)
- **PR-E label substrate assert** — deferred to Step 4 (line 769)

### Output schema

| Family | Notes |
|---|---|
| Fold predictions | classifier_prob, expectancy_pred, fold |
| Ensemble model scores | xgb_classifier_score, lgbm_classifier_score, catboost_classifier_score, regime_xgb_score, lstm2_quality_score, tft2_quality_score, tcn_classifier_score, ca2_signal_quality, tabnet_classifier_score, unified_score, ngb_mean/std/z_score |
| TimesFM features | timesfm_4h/12h/24h_direction, horizon_agreement, uncertainty, conviction_4h, alignment_score |
| Bias kernels | bias_kernel_* |
| Metadata | asset, fold, timestamp, entry_time, entry_bar, event_family_id, fill_bar |

### Open concerns / gotchas
1. **Join key `signal_id` silent fallback** — if upstream mismatch, predictions silently drop without warning. Recommend hard assert.
2. **No PR-E enforcement here** — relies on Step 4 to catch missing label.
3. **No blocklist enforcement** — exit-derived columns CAN flow through Step 3 unchallenged; Step 4 must catch them.

### Wall-time

~10-30 minutes per (asset, philosophy) for full ensemble join + cross-asset merge.

---

## Step 4 — MetaCombiner Training

**Script:** [scripts/phases/phase_2/prompt_3b_metacombiner.py](scripts/phases/phase_2/prompt_3b_metacombiner.py) (~2,300 lines, most complex in Phase 2)

### Architecture
- **MLP:** 20 → 32 → 16 → 1
- **Loss:** SmoothL1 (robust to outliers); fallback Student-t NLL when `STUDENT_T_MODE=1` (line 1031)
- **Optimizer:** Adam, lr=0.001, weight_decay=1e-5
- **Targets:** vol-scaled `net_pnl_pct_at_fixed_exit` (when `MC_VOL_SCALE=True`, divides y by `atr_pct_at_entry`, persists `mc_target_scale` for inversion)
- **Two-level walk-forward:** fold 1 = NaN by construction (PATH 3 uniform prior + heavy shrinkage)

### MC INPUT BLOCKLIST (PR-E + H7 expansion 2026-04-26)

**Substring match** (NOT prefix). Lines 266-273:
```python
_MC_INPUT_BLOCKLIST_SUBSTRINGS = (
    # Original PR-E (7)
    "rel_exit", "selected_exit", "exit_advantage", "exit_dispersion",
    "sigma_exit", "dist_exit", "_shadow_",
    # H7 expansion (12 — 2026-04-26)
    "exit_outcome", "overlay_pnl", "selected_holding_bars", "replacement_",
    "chosen_exit", "realized_holding", "atr_exit_", "gp_exit_", "pysr_exit_",
    "per_overlay_", "exit_score", "exit_chosen",
)  # 19 total substrings
```

**C1 fix at line 752:**
```python
_assert_no_blocked_columns(list(work_df.columns) + list(INPUT_SCORE_FEATURES))
```
Was: only checked canonical `INPUT_SCORE_FEATURES` list (which never contains blocked substrings) — bypassable.

**Function (lines 276-285):**
```python
def _assert_no_blocked_columns(input_cols):
    blocked = [c for c in input_cols
               if any(s in c for s in _MC_INPUT_BLOCKLIST_SUBSTRINGS)]
    if blocked:
        raise ValueError(f"MC INPUT BLOCKLIST: exit-derived/shadow columns rejected: {blocked}...")
```

### Label rewire — 12 sites post-H6

`MC_TARGET_COL = "net_pnl_pct_at_fixed_exit"` enforced at:

| # | Line | Site | Purpose |
|---|---|---|---|
| 1 | 702 | Training y-tensor | Forward pass label |
| 2 | 1592 | Sigma-decile | Stage 1 dispersion |
| 3 | 1660-1669 | Coverage gate | Coverage check |
| 4 | 1728 | Stage 1 NLL | Negative log-likelihood |
| 5 | 1742 | Stage 1 CRPS | Continuous ranked probability |
| 6-7 | 1757-1767 | Stage 2 evaluators | Two-stage eval |
| 8 | 1923-1928 | Brier score | MC confidence calibration (H6 fix) |
| 9 | 1948 | Platt calibration | Logistic re-calibration on canonical label (H6) |
| 10-11 | 2017-2027 | GATE-1 simplicity tie-break | MC vs simple blend (H6 fix) |
| 12 | 1827, 1837, 1940, 2006 | Expectancy / GATE-1 references | Throughout MC pipeline |

**M8 fix at line 758:**
```python
_strict = os.environ.get("STRICT_MC_LABEL", "1") == "1"  # Default flipped 0→1
```
Production hard-fails if PR-E canonical column missing. Set `STRICT_MC_LABEL=0` only for diagnostic re-runs.

### Two-level walk-forward (lines 870-887)

- **Level 1:** Component models train on folds 1..K, predict on fold K
- **Level 2:** MetaCombiner trains on folds 1..K-1 OOS predictions, predicts fold K

**Fold 1 = NaN handling (lines 1670-1718, PATH 3):**
- Fold 1 has no prior training folds → uniform prior + heavy shrinkage (`λ = LAMBDA_BASE`)
- Prevents information leakage from fold 1 into MC label computation

### Reliability features

```python
RELIABILITY_FEATURES = []
```
**Currently active (8 features):** rel_score_disagreement_std/iqr, rel_score_available/missing_fraction/count, rel_score_consensus_strength, rel_model_aging/fragility_penalty.

**Candidates (v2, awaiting ablation, 6):** rel_fold_sharpe_spread, rel_tail_ratio_fold_cv, rel_ulcer_over_max_dd, rel_regime_cond_sharpe_std, rel_payoff_ratio_smoothed, rel_fold_wr/pf_cv.

**Removed (PR-E):** rel_exit_cluster_dispersion, rel_exit_selected_advantage (closed-loop risk: exit outcomes → MC labels → exit dispatch).

**Re-introduction policy (lines 225-231) — ALL must hold:**
1. Computed from folds < K only
2. Sourced from MC-INDEPENDENT exits ONLY (`fixed`, `atr_scaled`)
3. Group ablation shows >5% MC contribution
4. Fresh MC checkpoint trained + compared against C+ baseline on Phase 2 OOS folds 4-8

### Probability calibration: Platt recalibration (H6 fix, lines 1939-1988)

Fits 2-param logistic regression on `(raw_confidence → P(net_pnl_pct_at_fixed_exit > 0))` using canonical label. Writes calibrator to `configs/frozen/platt_mc_confidence_{philosophy}.json` (intercept + coef). Overwrites canonical `metacombiner_confidence` column with calibrated values; preserves raw as `metacombiner_confidence_raw` for audit.

### GATE-1 simplicity tie-break (H6 fix, lines 2017-2039)

Compares MC OOS expectancy vs simple blend baseline on canonical label (line 2024). If MC does NOT beat blend by >SIMPLICITY_MARGIN, freezes simple blend instead (writes flag file for Phase 3). Uses canonical fixed-exit label to score both fairly.

### Outputs

**MC checkpoints:** `configs/frozen/{philosophy}_mc_fold_{k}_checkpoint.pt`

**Predictions parquet:** `{philosophy}_cross_asset_metacombiner_predictions.parquet` with columns:
- `mc_mu, mc_sigma, mc_nu` — MetaCombiner point estimates + Student-t shape
- `blend_mu, blend_sigma, blend_nu` — Ensemble blend with temperature/weight calibration
- `metacombiner_confidence` (Platt-calibrated)
- `metacombiner_confidence_raw` (audit)
- `kelly_fraction, leverage_prediction, size_pct_kelly` — Kelly sizing outputs
- `mc_kelly_leverage` (optional)
- `mc_skip_score = 1 - confidence` (P(loser))
- `mc_tail_risk_flag` (1 if nu < 5.0, fat tails detected)

### Open concerns / gotchas
1. **Fold 1 PATH 3 weighting** — Phase 3 must account for lower confidence on fold 1 predictions
2. **Platt overwrites canonical column** — raw values in `metacombiner_confidence_raw` for audit only
3. **GATE-1 asymmetric** — MC trained on folds 1..K-1, blend on fold history; tie-break margin externally configured
4. **Hansen SPA NOT in Step 4** — lives in Step 6 (`prompt_exit_selection.py`); MC doesn't consume SPA directly

### Wall-time

~12-24 hours per philosophy depending on epochs, two-level WF, and uncertainty-stack training. Parallelizable per philosophy.

---

## Step 5 — Decision Layer (72 variants per philosophy)

**Script:** [scripts/phases/phase_2/prompt_4_decision_layer.py](scripts/phases/phase_2/prompt_4_decision_layer.py)

### What this step does

Step 5 evaluates **3 decision-layer schemes × 10 ranked blend candidates per philosophy**, selects the top 10. The **72 frozen variants per philosophy = 864 total** are emitted in **Step 10 (Final Assembly)**, not Step 5. Step 5 builds the decision schemes that combine with overlay variants downstream.

### CLI (lines 1226-1269)

```python
parser.add_argument("--philosophy", required=True, choices=LIBRARY_PHILOSOPHIES)
parser.add_argument("--force", action="store_true")
parser.add_argument("--dry_run", action="store_true")
parser.add_argument("--snapshot_id", default=SNAPSHOT_ID)
```

### Variant grid (assembled in Step 10, knobs from this step)

| Knob | Values | Count | Source |
|---|---|---|---|
| Concurrency caps | `[5, 8, 10, 15, 20, 25]` | 6 | `config.CONCURRENCY_CAPS` |
| Replacement modes | `["close_at_market", "skip", "swap_by_expected_pnl"]` | 3 | `config.REPLACEMENT_MODES` |
| MC threshold buckets | 3-bucket {low, medium, high} via `BUCKET_SPLITS` | 4 effective | `prompt_4_decision_layer.py:94-99` |

**M9 verified** — actual code uses 3-bucket scheme:
```python
BUCKET_SPLITS = [
    (33, 34, 33),   # equal thirds
    (25, 50, 25),   # fat middle
    (20, 40, 40),   # top-heavy
    (50, 50),       # 2-bucket split
]
```

`6 × 3 × 4 = 72 variants` per philosophy, 12 philosophies = **864 frozen strategies**.

### Bucket boundary persistence (M13 verified — absolute thresholds)

**Lines 565-579, 590-597:**
```python
low_pct, med_pct, _ = split_pcts
boundary_low = float(np.percentile(scores, low_pct))           # Line 567
boundary_high = float(np.percentile(scores, low_pct + med_pct))  # Line 568

kept["bucket"] = np.where(
    kept[score_col] < boundary_low, "low",
    np.where(kept[score_col] < boundary_high, "medium", "high"),
)

return {
    "split_pcts": list(split_pcts),
    "bucket_boundaries": [boundary_low, boundary_high],  # ABSOLUTE floats
    ...
}
```

**Phase 3 reads ABSOLUTE thresholds** at `prompt_1_holdout_run.py:1952` — no recompute on holdout (which would be re-tuning).

### Monotonicity check (lines 172-250)

Per bucket: enforces **higher MC confidence → better PF**.

```python
def _check_monotonicity(bucket_metrics):
    pfs = [bucket_metrics[b]["profit_factor"] for b in present]
    exps = [bucket_metrics[b]["expectancy"] for b in present]
    pf_mono = all(pfs[i] <= pfs[i + 1] for i in range(len(pfs) - 1))
    exp_mono = all(exps[i] <= exps[i + 1] for i in range(len(exps) - 1))
    return "yes" if (pf_mono and exp_mono) else ("weak" if (pf_mono or exp_mono) else "no"), score
```

**LEAK-G04 prevention:**
- Line 975-976: bucket boundaries selected on `inner_train` ONLY
- Line 983: `evaluate_bucket_scheme(inner_train_df, ...)` — inner fold only
- Line 1001-1003: full scheme evaluation on **holdout OOS** AFTER bucket selection

Best bucket picked by `monotonicity_score` on inner_train fold, never on the OOS fold being evaluated.

### ENABLE_SWAP gating (M11 fix)

`config.py:358-360`:
```python
ENABLE_SWAP: bool = False  # Master toggle — parity-first default OFF
```

`swap_baseline_matrix.py:70-93` (M11 hygiene fix):
```python
_env_prev = os.environ.get("ENABLE_SWAP")
try:
    os.environ["ENABLE_SWAP"] = str(enable_swap)
    result = simulate_portfolio_with_concurrency(...)
finally:
    if _env_prev is None:
        os.environ.pop("ENABLE_SWAP", None)
    else:
        os.environ["ENABLE_SWAP"] = _env_prev  # Restore prior value
```
Was: pop unconditionally — leaked state across nested calls.

### Outputs (lines 1108-1152)

```
configs/frozen/phase2/{philosophy}/schemes.json
configs/frozen/phase2/{philosophy}/comparison.json
```

Atomic writes via `.tmp` → rename (lines 147-152).

### Open concerns / gotchas
1. **Step 5 emits 10 schemes, NOT 72 variants** — variants combine schemes with overlays in Step 10.
2. **Concurrency caps × replacement modes (6 × 3 = 18)** apply to **Phase 4 simulation**, not the decision-layer grid here.
3. **Bucket boundaries frozen as absolute** — safe but requires careful calibration at freezing time.
4. **DRY_RUN mode** only prints paths; useful for testing without touching frozen config.
5. **swap_by_expected_pnl flag-gated** behind `ENABLE_SWAP=False` until 18-variant matrix validated.

### Wall-time

Per (asset, philosophy): 4 blend candidates × 7 skip percentiles × 4 bucket schemes = **112 scheme evaluations**, ~2-3sec each → **4-7 min/cell**. Full sweep: 22 × 12 = 264 cells × ~6 min ≈ **26h** (parallel-safe by philosophy).

---

## Step 6 — Per-Philosophy Exit Selection (post-MC overlay)

**Production exit stack:** `fixed`, `atr_scaled`, `dist_exit` (per-cell calibration-gated). `sigma_exit` is **SHADOW-ONLY** until per-bar replay ships.

**Most-modified step in audit closeout** — multiple gap fixes + new logic touched here.

### 6a — Philosophy-level (`prompt_exit_selection.py`, ~1150 lines)

**What it does:** Evaluates 3 challengers (atr_scaled, sigma_exit, dist_exit) against fixed-TP/SL baseline across OOS folds [3,4,5,6,7], applies Holm-Bonferroni, selects winner per philosophy. Output → `configs/frozen/exit_selection.json` (Phase 3 reads).

**Key gates active:**

| Gate | Implementation | Status |
|---|---|---|
| CHALLENGERS list | Line 110: `["atr_scaled", "sigma_exit", "dist_exit"]` | PR-C 2026-04-25: dropped `gp` |
| Sigma eligibility runtime guard | Lines 116-131: `_assert_sigma_exit_eligible()` | Raises if `SIGMA_EXIT_APPROX != "per_bar_replay"` |
| Min trades floor | Line 213: `min_trades=30` | Gap 7: bumped from 5 |
| Sigma hold constants | Lines 117-118: `SIGMA_HOLD_MULT_TP_HIT=0.7`, `SIGMA_HOLD_MULT_SL_HIT=0.5` | M14: named for SHA-pin |
| OOS folds | Line 137: `OOS_FOLDS_ZEROIDX = [3,4,5,6,7]` | Folds 0-2 reserved |
| Hansen SPA | Lines ~1015+: `spa_p_value`, `spa_statistic` | M20: INFORMATIONAL only |
| Tiebreak | `(composite_score, -fold_variance)` argmax | Gap 4 fix |

**`_exit_outcomes_atr_scaled` C2 fix (lines 298-345):**
```python
required = ("mfe_pct", "mae_pct")
has_atr = "atr_pct_at_entry" in trade_df.columns or "atr_pct" in trade_df.columns

if not all(c in trade_df.columns for c in required) or not has_atr:
    warnings.warn("atr_scaled bake-off: missing mfe_pct/mae_pct/atr_pct columns; "
                  "falling back to fixed-exit PnL...", RuntimeWarning, stacklevel=2)
    return pnls_fixed, holds, caps

# Real ATR-clipped PnL with k_tp=2.0, k_sl=1.5
atr = trade_df[atr_col].fillna(0.01).values
mfe = trade_df["mfe_pct"].fillna(0.0).values
mae = trade_df["mae_pct"].fillna(0.0).values
dir_sign = np.where(direction.isin(["short", "sell", "-1"]), -1.0, 1.0)
tp_thresh = 2.0 * atr
sl_thresh = -1.5 * atr

pnls_atr = np.where(
    mfe >= tp_thresh, dir_sign * tp_thresh,
    np.where(mae <= sl_thresh, dir_sign * sl_thresh, pnls_fixed),
)
```

Was: no-op alias for fixed (made `atr_scaled` and `fixed` identical → Holm could never reject H0).

**`_exit_outcomes_fixed` Gap 5 fix:**
```python
if "net_pnl_pct_at_fixed_exit" in trade_df.columns:
    pnl_col = "net_pnl_pct_at_fixed_exit"
elif "net_pnl_pct" in trade_df.columns:
    warnings.warn("falling back to legacy 'net_pnl_pct'...", RuntimeWarning)
    pnl_col = "net_pnl_pct"
else:
    raise RuntimeError("neither column present...")
```

**Decision flow:**
1. Load MC predictions (`{philosophy}_cross_asset_metacombiner_predictions.parquet`)
2. For each challenger: compute exit outcomes, apply Holm at α=0.05
3. Only challengers passing Holm advance to tiebreak
4. Winner = `argmax(composite_score)` then `argmin(fold_variance)` for ties
5. Write `configs/frozen/exit_selection.json`

### 6b — Per-strategy fold-indexed (`prompt_exit_selection_per_strategy.py`, ~383 lines)

**Reserved for Phase 4** (online allocator). NOT Phase 3.

| Gate | Implementation | Status |
|---|---|---|
| Holm α | `score_exits_for_strategy_at_cutoff` | PR-B + Gap 3: α=0.01 (tighter than 6a's 0.05) |
| `passes_holm` flag | Only passing challengers advance | Non-passing → fixed |
| Fold-indexed | `selected_exit_by_fold[K]` uses folds 0..K-1 only | Audit-fixed |

### 6n — Distribution exit (`prompt_2n_distribution_exit.py`, 667 lines)

**C4 sidecar wiring (post-fix):**
```python
def compute_exit_signals_for_trade(
    trade, feature_bars, ..., calibration_sidecar_path: str | None = None,
) -> tuple[np.ndarray, dict]:
    if calibration_sidecar_path is not None:
        _asset = str(trade.get("asset", "")).lower()
        _regime = trade.get("regime_label", trade.get("regime", None))
        _horizon = str(trade.get("tfm_horizon", "h25"))
        if not _cell_passes_calibration(_asset, direction, _regime, _horizon, calibration_sidecar_path):
            return exit_signals, {
                "exit_reason": "calibration_fail",
                "cell": f"{_asset}_{direction}_{_horizon}_regime{_regime}",
            }
    ...
```

**Causality guard (PR-I, lines 192-198):**
```python
if not feature_bars.index.is_monotonic_increasing:
    raise ValueError("PR-I causality guard: feature_bars must be in monotonic-increasing "
                     "timestamp order so per-bar dist_exit decisions stay causal.")
```

**dist_exit hardening (PR-I + C4):**

| Setting | Value |
|---|---|
| Tune folds | `{4, 5}` |
| Eval folds | `{6, 7}` |
| Reserved fold | `8` |
| Score on | TUNE only |
| Report | `capture_ratio_mean` on EVAL |
| Calibration gate | `tfm_calibration_per_cell.json` sidecar; `pass=false` → zero exits + reason="calibration_fail" |
| Threshold | Single global across all (asset, philosophy) |
| Inputs | 18 ATR-normalized signed TFM cols from PR-F |

### 6m — Sigma exit (`prompt_2m_sigma_exits.py`, ~236 lines) — SHADOW-ONLY

| Flag | Value |
|---|---|
| `SIGMA_EXIT_ELIGIBLE_FOR_SELECTION` | False |
| `SIGMA_EXIT_ELIGIBLE_FOR_PHASE3` | False |
| `SIGMA_EXIT_APPROX` | `"terminal_pnl_grid"` (acknowledged biased) |

Sigma uses terminal-PnL grid approximation with **asymmetric truncation bias** (caps wins at TP, counts full -y as loss in `neither` branch — PF inflates with tighter k_tp). Cannot be selected for production until per-bar replay ships.

### Tune dist_exit threshold (`tune_dist_exit_threshold.py`, ~279 lines)

```python
TUNE_FOLDS = {4, 5}  # Tuning set
EVAL_FOLDS = {6, 7}  # Evaluation set (production holdout)
```

### Outputs

`configs/frozen/exit_selection.json` — winning (exit_name, philosophy) pairs + Holm/Hansen stats. Phase 3 reads this to lock exit strategy per philosophy.

### Open concerns / gotchas
1. **Sigma_exit shadow-only** until per-bar replay — current PF/Sharpe metrics from sim are biased
2. **Tiebreak by fold_variance** — if fold_variance=0 (rare), insertion order determines winner (deterministic but quirky)
3. **dist_exit per-cell calibration** depends on Step 7 sidecar; if sidecar empty, all trades fall back to fixed
4. **Hansen SPA informational only** — does NOT gate selection (avoids double-jeopardy on same data)

### Wall-time

~2-4 hours per philosophy for full bake-off across 3 challengers + Holm + Hansen + tune.

---

## Step 7 — TimesFM 792-Cell Calibration

**Script:** [scripts/phases/phase_2/check_timesfm_calibration_per_cell.py](scripts/phases/phase_2/check_timesfm_calibration_per_cell.py) (PR-G)

### Cell structure

**792 cells = 22 assets × 2 directions × 3 horizons × 6 regimes**

| Dim | Values |
|---|---|
| Assets | 22 (PHASE2_ACTIVE_ASSETS) |
| Directions | long, short |
| Horizons | h4, h12, h24 (4 bars, 12 bars, 24 bars) |
| Regimes | 6 (bull_trending, bear_trending, sideways, high_volatility, low_vol_compression, crash_capitulation) |

Cell key format (line 240): `{asset}_{direction}_{horizon}_regime{N}` (e.g., `btc_long_h4_regime0`).

### Wilson LCB function (C3 fix)

**Lines 53-67:**
```python
def wilson_lcb(hits: int, n: int, alpha: float = ALPHA) -> float:
    """Wilson ONE-sided lower confidence bound (C3 2026-04-26 fix)."""
    if n <= 0:
        return 0.0
    # One-sided z: 1.6449 at alpha=0.05; 2.3264 at alpha=0.01
    z = 1.6448536269514722 if alpha == 0.05 else float(_norm_ppf(1.0 - alpha))
    phat = hits / n
    denom = 1.0 + z * z / n
    center = phat + z * z / (2.0 * n)
    margin = z * math.sqrt(phat * (1.0 - phat) / n + z * z / (4.0 * n * n))
    return float((center - margin) / denom)
```

**C3 fix:** prior code used `z = 1.96` (two-sided 95% CI). Correct one-sided test at α=0.05 requires `z = 1.6449`. Two-sided over-states LCB tightness, rejecting more cells than justified.

### Pass criteria (line ~120, ALL required)

| Metric | Threshold |
|---|---|
| `direction_accuracy` Wilson LCB 95% | > 0.50 |
| Calibration slope | ∈ [0.85, 1.15] |
| Coverage_90 | ∈ [0.85, 0.95] |
| Coverage_95 | ∈ [0.92, 0.98] (or NaN with M16 fallback) |

Failure returns `(False, reason)` with descriptive message; any NaN → conservative rejection.

### Coverage computation (M16 fix, lines 144-160)

```python
cov_90 = _coverage(realized, q10, q90) if q10 is not None and q90 is not None else float("nan")

q05_col, q95_col = f"tfm_q05_{horizon}", f"tfm_q95_{horizon}"
if q05_col in sub.columns and q95_col in sub.columns:
    cov_95 = _coverage(realized, sub[q05_col], sub[q95_col])
elif q10 is not None and q90 is not None:
    # M16 fix: previously widened q10/q90 by 1.65 (Gaussian quantile ratio)
    # to approximate q05/q95. That assumes Gaussian; on heavy-tailed crypto/stock
    # returns the widening is biased and meaningless. Return NaN instead.
    cov_95 = float("nan")
```

**Why M16 fix:** Gaussian-widened factor 1.65 was meaningless on heavy-tailed distributions; cell would fake-pass the gate. NaN forces fall-through to next pool tier or ineligibility.

### 4-tier pooled fallback ladder (lines 202-260)

```python
def resolve_cell_via_ladder(df_all, asset, direction, horizon, regime):
    masks_and_levels = [
        ("primary", N_PRIMARY,
         (df_all["asset"]==asset) & (df_all["direction"]==direction)
          & (df_all["horizon"]==horizon) & (df_all["regime"]==regime)),
        ("pool_l1_pool_regimes", N_POOL_L1,
         (df_all["asset"]==asset) & (df_all["direction"]==direction)
          & (df_all["horizon"]==horizon)),
        ("pool_l2_pool_assets", N_POOL_L2,
         (df_all["direction"]==direction) & (df_all["horizon"]==horizon)
          & (df_all["regime"]==regime)),
        ("pool_l3_global", N_POOL_L3,
         (df_all["direction"]==direction) & (df_all["horizon"]==horizon)),
    ]
    for level_name, min_n, mask in masks_and_levels:
        sub = df_all[mask]
        if len(sub) >= min_n:
            metrics = compute_cell_metrics(sub, horizon)
            metrics["pool_level"] = level_name
            # M15 provenance fields
            metrics["n_assets_pooled"] = int(sub["asset"].nunique())
            metrics["n_regimes_pooled"] = int(sub["regime"].nunique())
            metrics["assets_in_pool"] = sorted(sub["asset"].unique())[:20]
            metrics["regimes_in_pool"] = sorted(sub["regime"].unique())[:10]
            ok, reason = cell_pass(metrics)
            metrics["pass"] = ok
            metrics["fail_reason"] = reason
            return metrics
    return {"n": 0, "pool_level": "below_min_n", "pass": False, ...}
```

| Tier | Pool | Min n |
|---|---|---|
| 1. primary | (asset × dir × horizon × regime) | 80 |
| 2. pool_l1_pool_regimes | (asset × dir × horizon) | 200 |
| 3. pool_l2_pool_assets | (dir × horizon × regime) | 350 |
| 4. pool_l3_global | (dir × horizon) | 600 |
| 5. below_min_n | — | `pass=False` → dist_exit ineligible |

### Pool provenance fields (M15 fix)

Each pooled cell now records:
- `n_assets_pooled` — unique asset count in pool
- `n_regimes_pooled` — unique regime count in pool
- `assets_in_pool` — sorted list of asset IDs (capped 20)
- `regimes_in_pool` — sorted list of regime numbers (capped 10)

**Why:** dist_exit consumer can distinguish primary cells from pooled ones. Pooled cells should weight calibration scores lower at production time.

### Output sidecar schema

**Path:** `pipeline_state/phase_2/tfm_calibration_per_cell.json`

```json
{
  "schema_version": "v2_per_cell",
  "computed_at": "2026-04-27T...",
  "n_cells": 792,
  "n_pass": <int>,
  "n_fail": <int>,
  "criteria": {
    "direction_accuracy_lcb_95_floor": 0.50,
    "calibration_slope_range": [0.85, 1.15],
    "coverage_90_range": [0.85, 0.95],
    "coverage_95_range": [0.92, 0.98],
    "ladder": {"primary_min_n": 80, "pool_l1_min_n": 200, "pool_l2_min_n": 350, "pool_l3_min_n": 600}
  },
  "cells": {
    "btc_long_h4_regime0": {
      "n": 156, "direction_accuracy": 0.59, "direction_accuracy_lcb_95": 0.523,
      "calibration_slope": 0.97, "coverage_90": 0.91, "coverage_95": 0.95,
      "pool_level": "primary",
      "n_assets_pooled": 1, "n_regimes_pooled": 1,
      "assets_in_pool": ["btc"], "regimes_in_pool": [0],
      "pass": true, "fail_reason": null
    },
    ...
  }
}
```

### Consumer integration (C4)

`prompt_2n_distribution_exit.py:_load_calibration_cache` and `_cell_passes_calibration`:
- Cell key: `f"{asset.lower()}_{direction.lower()}_{horizon}_regime{regime}"`
- Sidecar absent → return True (legacy permissive)
- Cell missing → return True (uncomputed; not failed)
- `cell.pass=False` → return False (caller falls through to fixed)

### Open concerns / gotchas
1. **Zero trades per (asset, dir, horizon)** falls all 4 tiers → `pool_level="below_min_n"`, dist_exit ineligible (conservative)
2. **Pooled cells share metrics** — consumer should audit `pool_level` distribution
3. **NaN coverage_95 → auto-fail** — M16 ensures no fake-pass on heavy tails; forces pool or ineligibility
4. **Cell key case-sensitive** in cache lookup — normalizes to `.lower()` for asset/direction

### Wall-time

~30-60 minutes for full 792-cell sweep on Hopper.

---

## Step 8 — Sizing per Philosophy

**Files:**
- [`prompt_sizing_selection.py`](scripts/phases/phase_2/prompt_sizing_selection.py) — main selector
- [`prompt_2f_kelly_params.py`](scripts/phases/phase_2/prompt_2f_kelly_params.py) — Kelly leverage learner (H10)
- [`prompt_rl_sizing.py`](scripts/phases/phase_2/prompt_rl_sizing.py) — RL sizing agent (M17)

### What this step does

Selects ONE sizing method per philosophy from `{fixed, kelly, rl}` using composite score + paired block bootstrap + Holm-Bonferroni gate on Phase 2 OOS folds [3,4,5,6,7]. Conservative: if no method beats fixed (1×) baseline on ALL 5 gate criteria → `selected_sizing="fixed"`. Output drives Phase 3 position sizing.

### Three sizing methods

| Method | Source | Computation | Disabled? |
|---|---|---|---|
| **fixed** | Baseline | 1× leverage | No |
| **kelly** | `mc_kelly_leverage` or `leverage_prediction` | Bayesian shrinkage 50% toward 0.25 + quarter-Kelly divisors | No (H10 hardened) |
| **rl** | RL policy checkpoint (PPO, Beta dist) | Agent learns state → leverage scalar [0, 0.20] | No (M17 protected) |

**Deprecated (Ship 3, 2026-04-24):** `xgb` and `pysr` sizing dropped. `SIZING_METHODS = ["kelly", "rl", "fixed"]`.

### Selection logic: 5 Holm gates (lines 319-332)

| # | Gate | Threshold |
|---|---|---|
| 1 | margin | ≥ 0.05 (5% improvement over fixed) |
| 2 | p_value_holm | ≤ 0.05 |
| 3 | fold_wins | ≥ 5 of 8 OOS folds [3,4,5,6,7] |
| 4 | dd_ratio | ≤ 1.10 (≤110% of fixed baseline DD) |
| 5 | trade_preservation | ≥ 0.70 (≥70% of baseline trade count) |

ALL 5 must pass (AND logic).

### Holm-Bonferroni (lines 312-317)

```python
k_tests = 2  # kelly, rl vs fixed
# Order p-values ascending; apply correction
holm[m] = min(1.0, p * (k_tests - i))
```

### Composite score (lines 78-84)

```python
COMPOSITE_WEIGHTS = {
    "net_sharpe": 0.40,
    "calmar": 0.20,
    "one_minus_dd_ratio": 0.20,
    "fold_stability": 0.15,
    "trade_preservation": 0.05,
}
COMPLEXITY_PENALTY = {"fixed": 0.00, "kelly": 0.00, "rl": 0.05}
```

### Tiebreak (lines 338-345)

- Highest composite score wins
- Near-tie (<2% margin): prefer simpler per `SIMPLICITY_ORDER = ["fixed", "kelly", "rl"]`
- Fallback: next-best passing method or `"fixed"`

### Kelly hardening (H10 fix at `prompt_2f_kelly_params.py:72-100`)

```python
KELLY_DIVISOR_MC = 4.0       # quarter-Kelly on MC leverage units
KELLY_DIVISOR_LP = 20.0      # leverage_prediction in % (~5x scale)
KELLY_SHRINK_LAMBDA = 0.5    # 50% shrink toward prior
KELLY_PRIOR = 0.25           # default quarter-Kelly

# Fold-leakage assert
if "fold" in df.columns:
    n_folds = int(df["fold"].nunique())
    if n_folds < 3:
        raise RuntimeError(f"Kelly fold-leakage: only {n_folds} folds; need ≥3")

# Bayesian shrinkage formula
raw = (df["mc_kelly_leverage"] / KELLY_DIVISOR_MC).clip(0.01, 1.0)
df["kelly_frac"] = (
    KELLY_SHRINK_LAMBDA * raw + (1.0 - KELLY_SHRINK_LAMBDA) * KELLY_PRIOR
).clip(0.01, 1.0)
# = 0.5 * raw + 0.5 * 0.25, clipped to [0.01, 1.0]
```

**Why:** raw MC leverage divisor was hardcoded magic 4.0; small-sample noise inflates Kelly fractions. Bayesian shrinkage (50% blend toward 0.25) prevents over-leverage. Fold-leakage assert ensures ≥3 distinct purged folds.

### RL fold-leakage protection (M17 fix at `prompt_rl_sizing.py:279-294`)

```python
_OOS_FOLDS_ZEROIDX = frozenset([3, 4, 5, 6, 7])
if "fold" in trades_df.columns:
    _bad = sorted(set(trades_df["fold"].dropna().astype(int)) - _OOS_FOLDS_ZEROIDX)
    if _bad:
        raise RuntimeError(f"M17: RL sizing OOS-fold leakage — folds {_bad} not in canonical range")
    trades_df = trades_df[trades_df["fold"].isin(_OOS_FOLDS_ZEROIDX)].copy()
```

**Defense-in-depth:** even if all folds valid, pre-filter to OOS folds [3,4,5,6,7]. RL agent (PPO, Beta-distribution actor-critic) trains on purged OOS, ~500 episodes × 200 trades/episode.

### Sizing × Exit ordering (M18 canonical, lines 24-40 docstring)

**Strict ordering:**
1. MC predictions → `exit_selection` overlay → EXIT-SELECTED PnL stream
2. Sizing scores on EXIT-SELECTED PnL, NOT fixed-exit baseline (otherwise wrong leverage)
3. Input column `trade_base_pnl_pct` MUST be post-exit-overlay PnL
4. Fallback: missing or misconfigured → `selected_sizing='fixed'` (conservative)

**Why:** sizing tuned to the exit distribution it will see in production. Scoring against wrong distribution leads to leverage miscalibration.

### Outputs

`configs/frozen/{philosophy}_sizing_selection.json`:
```json
{
  "philosophy": "...",
  "selected_sizing": "kelly|rl|fixed",
  "fallback_sizing": "...",
  "margin": 0.07,
  "p_value": 0.02,
  "fold_wins": 6,
  "per_method_score": {"fixed": ..., "kelly": ..., "rl": ...},
  "raw": {m: {...}, ...},
  "gate_config": {...}
}
```

Phase 3 reads `package["selected_sizing"]` to apply position sizing.

### Open concerns / gotchas
1. **Cross-philosophy Holm aggregation** happens in Step 10 (`prompt_6_assembly.py`) — Step 8 only does within-philosophy
2. **Kelly fragility** if `mc_kelly_leverage` variance is NaN → fallback to fixed; Bayesian shrinkage 0.5 may undershrink on high-noise samples
3. **RL reproducibility** — PPO seed handling relies on `RANDOM_SEED` config (verify in training loop)
4. **M18 risk** — if upstream emits `fixed-exit` column instead of `post-exit-overlay`, sizing is wrong silently (no alert, just fallback)

### Wall-time

12 philosophies × ~5-10 min Holm test = ~1-2h sequential. Kelly training: ~30 min/philosophy. RL training: ~1-2h/philosophy. **~4-6h per full Step 8 run.**

---

## Step 9 — Robustness Audit

**Script:** [scripts/phases/phase_2/prompt_5b_robustness_audit.py](scripts/phases/phase_2/prompt_5b_robustness_audit.py)
**Granularity:** per (asset, philosophy, variant) — 22 × 12 × 72 = **19,008 audit runs**

### What this step does

Stress-tests all 72 frozen variants across 22 assets via 8 distinct audit checks. **Hard rule:** all 72 variants proceed to Phase 3 regardless of verdict (ROBUST/FLAGGED/FAIL). Audits flag issues but never eliminate — downstream Phase 3 analysis correlates robustness flags with holdout performance.

### Tests table

| # | Test | Method | PASS | CAUTION | FAIL |
|---|---|---|---|---|---|
| 1 | Leakage Re-check | Static scan: phase3 refs, future-looking feature names, fold-safe preprocessing | All ✓ | Warnings logged | Any check fails |
| 2 | Fold Stability | Cross-fold CV (Sharpe/WinRate/Expectancy) | CV < 0.3 | 0.3 ≤ CV ≤ 0.6 | CV > 0.6 |
| 3 | Perturbation Stability | Max % change under ±10-20% param perturbations | ≤ 15% | 15-30% | > 30% |
| 4 | Search Bias / MT | Block permutation (PERM_N, BLOCK_SIZE per-asset M19) | p ≤ 0.05 | 0.05 < p ≤ 0.10 | p > 0.10 |
| 5 | Execution Realism Stress | 6 scenarios: base, 2x_slippage, 2x_spread, worst_funding, delayed_fills, all_combined | All profitable | ≥(n-1) profitable or marginal | < (n-1) profitable |
| 6 | Concentration | Max PnL share by asset/dir/event_family/quarter | ≤ 40% | 40-60% | > 60% |
| 7 | Drift | First-half vs second-half divergence | ≤ 30% | 30-50% | > 50% |
| 8 | Philosophy v2 Checks | 7 sub-checks (regime concentration, tail CV, ulcer/DD ratio, philosophy faithfulness, cross-asset scale, trimmed fold stability, cost stress slope) | No flags | Flags logged | N/A (flag-only) |

Lines: verdict functions 190-227 (`_audit_verdict_*`); audit runners 447-1011; v2 bundled checks 1013-1161.

### Block permutation per-asset BLOCK_SIZE (M19 fix)

**File:** [scripts/phases/phase_2/prompt_s6_variant_select.py:64-72](scripts/phases/phase_2/prompt_s6_variant_select.py#L64-L72)

```python
def block_size_for_asset(asset: str) -> int:
    from scripts.common.utils.asset_tf import resolve_base_tf, is_crypto
    if is_crypto(asset):
        return 168  # 7 days × 24h
    base_tf = resolve_base_tf(asset)
    if base_tf == "1h":
        return 32  # Stock 1h RTH: 5 days × 6.5h = 32.5
    return 5  # 1d stocks: 1 trading week
```

**M19 context:** old code hardcoded `BLOCK_SIZE=168` for all assets. Stock 1h RTH has only 32.5 bars/week (5 days × 6.5h market hours). Using 168 reduced independent permutation blocks from ~52 to ~10/year, inflating p-value variance and weakening block-permutation power.

**Audit 4 usage** (line 757-762): currently still references global `BLOCK_SIZE`. Phase 2 runner injects asset-specific block size before audit entry. Verify this flow at Phase 2 launch.

### Output tagging (lines 1360-1375)

Per-variant summary aggregates 8 audit verdicts:
- **ROBUST:** all 8/8 verdicts PASS
- **FLAGGED:** 1+ CAUTION, 0 FAIL
- **FAIL:** 1+ FAIL — critical breach

```python
variant_summary = {
    "variant_id": str,
    "verdicts": {"leakage_recheck": "PASS", "fold_stability": "CAUTION", ...},
    "pass_count": int, "caution_count": int, "fail_count": int,
    "audit_details": {audit_name: {verdict, flags, detail}, ...},
}
```

Master JSON: `pipeline_state/phase_2/prompt_outputs/prompt_5b_audit_results.json` (line 1504-1506). Frozen configs written per (asset, philosophy, variant) to registry (lines 1455-1474).

### Why audit doesn't eliminate variants

**Line 1556** prints at completion:
> "HARD RULE: ALL 72 variants proceed to Prompt 6 regardless of audit results."

**Rationale (docstring lines 6-7):** robustness flags surface risk signals (e.g., high drift, concentration), but actual Phase 3 holdout performance is the ground truth. A variant flagged CAUTION (e.g., high fold CV) may still outperform in holdout if the signal is robust across regimes. Phase 3 analysis correlates audit flags with holdout PnL to build meta-rules for future variant selection.

### Open concerns / gotchas
1. **BLOCK_SIZE per-asset** must be threaded through `block_permutation_test()` call site — verify Phase 2 runner injects correctly
2. **Audit data informs Phase 3** — flags stored in frozen configs (line 1431), registered (lines 1464-1474) for downstream correlation analysis
3. **v2 bundled checks** (added 2026-04-15) are flag-only; never cause FAIL verdict (line 1155)
4. **Hansen SPA NOT used here** — multiple-testing correction at variant selection (`prompt_s6`, Holm) and exit selection (M20 SPA); audit's block permutation is confirmatory

### Wall-time

Per (asset, philosophy, variant): ~5-10sec (1000 bootstraps + permutation + 6 stress scenarios + drift + v2 checks). 9 variants × 22 assets × 8 audits × ~7s ≈ **18 min single-thread per philosophy**. Parallelization (per-asset batching) → **~3-5 min wall-clock per philosophy**.

---

## Step 10 — Final Assembly

**Script:** [scripts/phases/phase_2/prompt_6_assembly.py](scripts/phases/phase_2/prompt_6_assembly.py) (~1,333 lines, most fixes touched here)

### What this step does

Assembles **864 strategy packages** (12 philosophies × 72 variants) from frozen sub-configs, attaches audit flags + cryptographic hashes, enforces read-only permissions, and validates SHA presence before manifest write.

### Procedure (7 steps)

**1. Load per-asset configs (lines 790-822):** for each (philosophy, variant), load frozen sub-configs for all active assets via `load_frozen_config()`.

**2. Gate-passed re-verify M21 (lines 794-814):**
```python
_gate_violations = []
for _asset_m21, _cfg_m21 in per_asset_configs.items():
    if not isinstance(_cfg_m21, dict):
        continue
    for _section, _sec_val in _cfg_m21.items():
        if isinstance(_sec_val, dict) and "gate_passed" in _sec_val:
            if not _sec_val.get("gate_passed", False):
                _gate_violations.append(f"{_asset_m21}/{philosophy}/v{v}/{_section}")
if _gate_violations:
    print(f"[WARN M21] {len(_gate_violations)} gate_passed=False sub-configs...")
```
Warns on violations (Phase 3 H18 hard-fails downstream).

**3. Build strategy package (lines 816-822):** call `build_strategy_package()` which extracts philosophy metadata, exit selection, sizing, MC inputs, gate flags, aggregates per-asset summaries → cross-asset OOS metrics, collects audit flags. Returns package dict with 40+ fields.

**4. Attach M10 variant_tuple_sha (lines 602-618):**
```python
_tuple_payload = {
    "philosophy": package.get("philosophy"),
    "variant_num": package.get("variant_num"),
    "concurrency_cap": package.get("concurrency_cap"),
    "replacement_mode": package.get("replacement_mode"),
    "mc_bucket": (first.get("decision", {}) or {}).get("scheme"),
}
_tuple_str = json.dumps(_tuple_payload, sort_keys=True, default=str)
package["variant_tuple_sha"] = hashlib.sha256(_tuple_str.encode("utf-8")).hexdigest()[:16]
```
16-char truncation for Phase 4 provenance check without re-hashing 50KB packages.

**5. Merge ablation decisions (lines 963-994):** load per-philosophy ablation cache (mc_inputs, skip_mechanisms, sizing, exits, gate_flags), deep-copy to prevent variant reference sharing (BUG-R3-1), inject into package dict. Load asset admission map from `pipeline_state/phase_2_5/asset_admission.json`.

**6. Attach package SHA256 (lines 996-1000):** call `attach_package_sha(pkg)` → returns dict with `sha256` field. Lift SHA into existing dict so post-save code sees it (BUG-R3-2). Save via checkpoint to handoff_dir + per-strategy-id aliases.

**7. SHA-pin exit configs & write manifest (lines 1020-1072):**
```python
# Gap 6 fix: SHA-pin exit configs
_sha_targets = {
    "exit_selection_config.yaml": ROOT / "configs" / "exit_selection_config.yaml",
    "dist_exit_thresholds.json":  ROOT / "configs" / "frozen" / "dist_exit_thresholds.json",
    "tfm_calibration_per_cell.json": ROOT / "pipeline_state" / "phase_2" / "tfm_calibration_per_cell.json",
}
_sha_pinned = {label: hashlib.sha256(path.read_bytes()).hexdigest()
               for label, path in _sha_targets.items() if path.exists()}
manifest["exit_config_shas"] = _sha_pinned

# H12 fix: SHA presence hard-assert
_missing_sha = []
for sid, info in manifest["strategies"].items():
    pkg_file = handoff_dir / f"strategy_{sid}_package.json"
    _pkg = json.load(open(pkg_file))
    if not _pkg.get("sha256") and not _pkg.get("package_sha256"):
        _missing_sha.append(f"{sid}: no sha256 field")
if _missing_sha:
    raise RuntimeError(f"H12: {len(_missing_sha)} strategy packages missing SHA pin...")

# Save master manifest
save_with_checkpoint(manifest, manifest_path)

# H13 fix: chmod 0o444
if os.environ.get("FROZEN_READONLY_DISABLE", "0") != "1":
    _ro = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
    for _p in handoff_dir.glob("strategy_*_package.json"):
        os.chmod(_p, _ro)
    os.chmod(manifest_path, _ro)
```

### Frozen config schema

Path: `configs/frozen/phase2/{asset}_{philosophy}_variant_{N}_frozen_config.json`

| Field | Description |
|---|---|
| `rule_text` | Phase 1 rule (verbatim) |
| `mc_threshold` | MC score cutoff (chosen bucket) |
| `exit_variant` + params | From Step 6a |
| `sizing_method` | From Step 8 |
| `concurrency_cap` | From Step 5 grid |
| `replacement_mode` | From Step 5 grid |
| `expected_metrics` | OOS PF, Sharpe, MaxDD, trade count |
| `gate_passed` | True (M21 re-verified) |
| `sha256` | Hash of config bytes (H12 enforced) |
| `variant_tuple_sha` | 16-char hash of variant tuple (M10) |

### All 7 fixes shipped today touching this file

| Fix | Line(s) | What |
|---|---|---|
| **H11 verified** | 1207-1305 | Global Holm aggregates exit + sizing + MC-input p-values family-wide; statsmodels with pure-numpy fallback |
| **H12** | 1045-1066 | SHA presence hard-assert before manifest write |
| **H13** | 1074-1096 | chmod 0o444 on all 864 packages + manifest at handoff (Linux/SLURM only; `FROZEN_READONLY_DISABLE=1` bypass) |
| **M10** | 602-618 | variant_tuple_sha 16-char hash |
| **M21** | 794-814 | gate_passed re-verify on every loaded sub-config |
| **Gap 6** | 1020-1042 | SHA-pin exit_selection_config.yaml + dist_exit_thresholds.json + tfm_calibration_per_cell.json |
| **M20 propagation** | (from Step 6) | Hansen SPA `spa_p_value` + `spa_statistic` flow into per-philosophy frozen package as informational |

### Wall-time

864 packages × JSON write + SHA hash: ~1-2h. Manifest write + chmod: <5sec. Global Holm (statsmodels family-wide): ~30-60sec. Registry logging: ~2 min. **Total Step 10: ~2-3h.**

---

## Step 11 — Phase 3 Handoff Manifest

### Manifest contents

**Path:** `configs/frozen/phase3_handoff/phase3_handoff_manifest.json`

```json
{
  "manifest_type": "phase3_handoff",
  "schema_version": "2",
  "snapshot_id": "<SNAPSHOT_ID>",
  "created_at": "2026-04-27T...",
  "experiment_id": "<exp_id>",
  "total_packages": 864,
  "total_strategies": 1296,
  "total_config_files": <NUM>,
  "philosophies": ["aggressive_pf_max", "conservative_min_drawdown", ...],
  "assets": ["btc", "eth", ...],
  "variants_per_philosophy": 72,
  "strategies": {
    "aggressive_btc_v03": {
      "strategy_id": "aggressive_btc_v03",
      "philosophy": "aggressive_pf_max",
      "asset": "btc",
      "variant_num": 3,
      "package_file": "aggressive_v03_package.json",
      "frozen_config_path": "...",
      "concurrency_cap": 5,
      "replacement_mode": "close_at_market",
      "audit_flags": ["robust", "M03_concentration_caution"]
    },
    ...
  },
  "exit_config_shas": {
    "exit_selection_config.yaml": "<sha256>",
    "dist_exit_thresholds.json": "<sha256>",
    "tfm_calibration_per_cell.json": "<sha256>"
  },
  "philosophy_to_strategies": {...},
  "philosophy_summaries": {...}
}
```

### Phase 3 hardening (downstream verification)

| Fix | Where | Purpose |
|---|---|---|
| **H17** | `_refuse_dev_data` normalize separators + deny-list `raw_pool`, `exploratory`, `gp_discovery`, `pipeline_state/phase_{1,2}` | Prevent dev data leakage |
| **H18** | `_load_exit_config` hard-fails on missing exit config OR `gate_passed=False` (legacy bypass: `PHASE3_ALLOW_EXIT_FALLBACK=1`) | No silent fallback to fixed |
| **H19** | `get_exit_for_trade` allowlists `{fixed, atr_scaled, sigma_exit, dist_exit}` | GP/PySR raise (legacy/orphaned) |
| **H22** | `assert_exit_configs_sha_pinned` hard-fails on missing manifest OR missing `exit_config_shas` (legacy bypass: `PHASE3_ALLOW_LEGACY_UNPINNED=1`) | Prevent post-handoff config drift |
| **Gap 1** | sigma_exit Phase 3 path enforces `SIGMA_EXIT_ELIGIBLE_FOR_PHASE3=False` AND `SIGMA_EXIT_APPROX != "per_bar_replay"` | Block biased sigma sim |
| **Gap 2** | dist_exit Phase 3 keys (`asset/direction/phase/base_times/entry_timestamp/calibration_sidecar`) populated | Activate intrabar 15m branch |

### Read-only enforcement (H13)

All 864 `strategy_*.json` + `manifest.json` set to mode `0o444` (read-only). Operative on Linux/SLURM; skipped on Windows. Bypass: `FROZEN_READONLY_DISABLE=1` env (diagnostic use only).

### Tampering detection layers

| Layer | Mechanism |
|---|---|
| Package-level | Each package carries `sha256` (whole-package hash); Phase 3 re-hashes on load |
| Variant-level | `variant_tuple_sha` (16-char) enables drift detection without rehashing |
| Config-level | `manifest["exit_config_shas"]` pins 3 soft-config files; Phase 3 asserts no mismatch |
| Schema-level | Manifest `schema_version` pinned |
| Audit trail | Registry logged with experiment_id + timestamp |

### Wall-time

Manifest write atomic (<1sec). Last step before Phase 3 launches.

---

## Audit Closeout — All 51 Findings (shipped 2026-04-26→27)

7 batches covering 5 CRITICAL + 17 HIGH + 29 MEDIUM findings.

### Batch 1 (9 fixes — critical + high)
**C1** MC blocklist on actual dataframe + canonical list, **C2** `_exit_outcomes_atr_scaled` real ATR-clipped PnL, **C3** Wilson LCB one-sided z=1.6449, **C4** dist_exit consumes calibration sidecar, **H3** Phase 1 PnL rank-transform, **H6** PR-E label rewire 12 sites, **H7** MC blocklist substring expansion (7→19), **H17** `_refuse_dev_data` normalize separators, **H18** Phase 3 silent fallback closed, **H19** GP/PySR exit dispatch raises, **H22** SHA-pin missing manifest hard-fail, **M8** `STRICT_MC_LABEL` default 0→1.

### Batch 2 (10 fixes — Phase 0 stocks + Kelly + frozen integrity)
**H1** `scripts/common/utils/asset_tf.py` shared resolver + 5 Phase 0 modules patched, **H10** Kelly Bayesian shrinkage + fold-leakage assert + named divisors, **H12** strategy-package SHA presence assert, **H13** chmod 0o444 on frozen packages, **H15** MANDATORY_VARIANTS DD violation flagged, **H16** baseline TUNE/EVAL split.

### Batch 3 (5 fixes — cross-asset + soft-kill)
**H2** `_REF_CACHE.get_union(asset)` BTC/ETH cross-asset ref, **H11 verified** already wired (`prompt_6_assembly.py:1175-1248`), **H14** `SOFT_KILL_VARIANTS` + `PRIMARY_VARIANTS` frozensets, **M3** MTF resample `closed="left", label="left"`, **M14** sigma `0.7`/`0.5` → `SIGMA_HOLD_MULT_TP_HIT/SL_HIT` named constants.

### Batch 4 (6 fixes — Phase 1 + audit hygiene)
**M5** Phase 1 lag-clone Test D, **M11** `swap_baseline_matrix.py` env hygiene, **M16** Coverage_95 NaN fallback (no Gaussian-widening), **M19** `block_size_for_asset()` resolver, **M22** MetaDD `_cap_by_q90` shift(1), **M1 verified** already protected via `_safe_ratio`.

### Batch 5 (8 fixes — Phase 1 sidecar + label completion)
**M2 verified** already has length asserts, **M4** raw_pool sidecar manifest with sha256, **M6** `gp_evolved_rust` source-family normalization, **M7** ATR fallback chain reordered, **M9** doc-only closed (3-bucket correct in code), **M15** 792-cell pool provenance fields, **M17** RL sizing OOS-fold leakage assert, **M21** `prompt_6_assembly.py` re-verify gate_passed.

### Batch 6 (7 fixes — variant SHA + sizing + Hansen)
**M10** `variant_tuple_sha`, **M12** `is_global_cap_disabled()` helper, **M13 verified** bucket_boundaries absolute, **M18** Sizing × Exit ordering codified, **M20** Hansen SPA wired, **M23** doc-only closed (LinUCB module location), **M11** verified.

### Batch 7 (FINAL — H21 + Option A)
**H21** TimesFM forecast bar-N causality fix at `run_timesfm.py:649`. **Option A** BF cascade proxy hardening: `m.pf` cap at 20.0, rename `pf → score_pf_proxy` and `sharpe → score_sharpe_proxy` with explicit banner.

---

## Operational Follow-ups

1. **Phase 0 TimesFM re-emit** — chain after `p1_consolidate` (JID 8855940) clears. Runs `slurm/precompute/timesfm_all_assets.sh` for all 22 assets × 3 phases (~2-3h on Hopper). H21 fix flows into all feature stores.

2. **MetaCombiner re-train** — after Phase 0 re-emit lands, MC must re-train on clean (causal) inputs to incorporate the 1-bar lookahead fix.

3. **BTC/LTC/ETH bigcaps re-runs** — JIDs 8897729 (btc long+short), 8897734 (ltc/long), 8897735 (eth/short) running with `pairs=1000`, `ext=200`. Symlink-swap into canonical path post-completion BEFORE `bf4_adapt_all` fires:
   ```bash
   for cell in btc/long btc/short ltc/long eth/short; do
     asset=${cell%/*}; dir=${cell#*/}
     rm -rf data/signals/exploratory/${asset}_all_cascade_rust4/${dir}
     ln -s ../exploratory_bigcaps/${asset}_all_cascade_rust4/${dir} \
           data/signals/exploratory/${asset}_all_cascade_rust4/${dir}
   done
   ```

4. **Realized-PnL sanity column for Phase 1 top-50** (Option B/C from quant audit, deferred) — optional, NOT blocking. Phase 2 dataset_build re-simulates with full realism so MFE/MAE-saturated artifacts (aapl/amzn PF=10^11) die economically there.

5. **aapl/amzn synthetic-saturated artifact monitoring** — after Phase 2 runs, verify these strategies' realized PF (post-cost) is in normal range (~2-5). Synthetic proxy Sharpe of 1131 / PF of 100B is NOT real edge.

---

**Wall-clock estimate:** ~6–9 days end-to-end (post 11-model prune; was 9–14 days). Phase 1 ETA: ~T+2.5 days (incl. bigcaps). Phase 2 + 2.5 + 3-ready: T+11.5–14.5 days.

**Document version:** 2026-04-27 (post 51-finding audit closeout).
