# Phase 2 — Step-by-Step (post 2026-04-25 audit, all 9 PRs shipped)

**Goal:** From **264 frozen Phase 1 libraries** → **864 frozen strategies** (12 philosophies × 72 variants) ready for Phase 3 holdout, with MC-approval scoring + per-philosophy exit overlay + per-philosophy sizing.

**Wall-clock estimate:** ~6–9 days end-to-end (post 11-model prune; was 9–14 days).

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
- [Open Gaps (must fix before production launch)](#open-gaps-must-fix-before-production-launch)

---

## Step 0 — Inputs (frozen from Phase 1)

| Artifact | Path | Count |
|---|---|---|
| Phase 1 libraries | `pipeline_state/phase_1/library_{asset}_{philosophy}.parquet` | **264** (22 assets × 12 philosophies) |
| Raw signal pool (Phase 4 reserve) | `pipeline_state/phase_1/raw_pool_{asset}.parquet` | **22** |
| Feature store | `features/phase{1,2}/feature_store_{asset}_phase{N}.parquet` | ~3,100 cols |

**Feature families present:** `tfm_*`, `lstm_*`, `mtf_*`, `rs_*`, `exp_*`, `p2f_*`, `wavelet_*`.

All Phase 1 outputs are **frozen** — any modification invalidates the downstream gate.

---

## Step 1 — Dataset Build

**Script:** `prompt_1_dataset_build.py`
**Granularity:** per `(asset, philosophy)` → **264 datasets total**

### Procedure
1. Load Phase 1 library rules → simulate trades on Phase 2 OOS data with **full execution realism**:
   - Per-asset cost overrides (`config.ASSET_COST_OVERRIDES`)
   - Fees, funding, slippage, latency, capacity friction
   - $1,000 initial capital, compounding
   - $50,000 max single-position cap
   - Per-asset concurrency cap (global cap disabled via `GLOBAL_CONCURRENCY_SENTINEL=9999`)
2. Build per-trade feature matrix — **bar N uses bars 0..N-1 only** (anti-leakage rule).
3. **PR-F integration** (line 1308): adds **18 ATR-normalized signed TimesFM cols** (6 features × 3 horizons `h5`/`h25`/`h168`). Direction-signed via `dir_sign × tfm_q50` so anti-trade-direction forecasts score **NEGATIVE** edge.
4. Compute both labels:
   - `net_pnl_pct_at_fixed_exit` — **canonical MC label** (PR-E)
   - `net_pnl_pct` — legacy diagnostic (contaminated by selected-exit overlay; do **not** use for MC)
5. **Fold-safe preprocessing:** scalers/imputers fit on TRAIN fold only.

**Output:** `pipeline_state/phase_2/{asset}_{philosophy}_dataset.parquet`

---

## Step 2 — Component Models (11 surviving post-prune)

**Cross-validation:** 8-fold purged walk-forward, `purge=72`, `embargo=24`.

**Dropped 5 via LOO confirm (2026-04-25):** TabNet, LSTM2, TCN, bias kernels, unified cross-asset. Result: **~30% MC training wall-time reduction** vs prior 16-model setup.

| # | Script | Output Cols | Notes |
|---|---|---|---|
| 1 | `prompt_2_model_train.py` | `xgb_proba`, `xgb_expectancy` | Primary classifier + expectancy head |
| 2 | `prompt_2b_timesfm_features.py` | `tfm_*` | Pre-computed in feature store |
| 3 | `prompt_2d_tft_scorer.py` | `tft2_score` | Temporal Fusion Transformer v2 |
| 4 | `prompt_2e_cross_asset_scorer.py` | `ca_score` | Per-asset cross-asset (unified dropped) |
| 5 | `prompt_2e_lgbm.py` | `lgbm_proba` | LightGBM classifier |
| 6 | `prompt_2f_cat.py` | `cat_proba` | CatBoost classifier |
| 7 | `prompt_2h_ngb.py` | `ngb_mu`, `ngb_sigma` | NGBoost — distributional |
| 8 | `prompt_2g_regime.py` | `regime_score` | 6-regime classifier |
| 9 | `prompt_genetic_programming.py` | `gp_score` | **Entry-side only**, NOT exits (gp dropped from `CHALLENGERS`) |
| 10 | `prompt_autoencoder_anomaly.py` | `ae_anomaly` | Reconstruction-error anomaly score |
| 11 | `prompt_cascade_filter.py` | `cascade_pass` | Hard-filter gate |

Each writes per-fold predictions to `pipeline_state/phase_2/predictions/`.

---

## Step 3 — OOS Assembly

**Script:** `prompt_3_oos_assembly.py`

Joins all 11 model prediction columns + dataset features → **unified per-trade table** per `(asset, philosophy)`.

**Hard assert:** `net_pnl_pct_at_fixed_exit` is the label substrate (**PR-E enforcement**). Job aborts if missing or null-filled.

---

## Step 4 — MetaCombiner Training

**Script:** `prompt_3b_metacombiner.py`

### Architecture
- **MLP:** 20 → 32 → 16 → 1
- **Loss:** SmoothL1
- **Targets:** vol-scaled `net_pnl_pct_at_fixed_exit`
- **Two-level walk-forward** — fold 1 = NaN by construction (highest leakage risk; monitored)

### MC INPUT BLOCKLIST (PR-E, **substring match**)

```python
_MC_INPUT_BLOCKLIST_SUBSTRINGS = (
    "rel_exit", "selected_exit", "exit_advantage", "exit_dispersion",
    "sigma_exit", "dist_exit", "_shadow_",
)
```

Any column **containing** one of these substrings → `ValueError` at training entry. Catches `_shadow_*` accidents and forgotten `shadow_mode=True` flags.

### Label Rewire (PR-E, 7 sites)

`MC_TARGET_COL = "net_pnl_pct_at_fixed_exit"` enforced at:

| Site | Line | Purpose |
|---|---|---|
| Training y-tensor | 702 | Forward pass label |
| Sigma-decile | 1592 | Stage 1 dispersion |
| Coverage gate | 1660–1669 | Coverage check |
| Stage 1 NLL | 1728 | Negative log-likelihood |
| Stage 1 CRPS | 1742 | Continuous ranked probability |
| Stage 2 evaluators | 1757–1767 | Two-stage eval |
| Expectancy / GATE-1 | 1827, 1837, 1940, 2006 | Gate threshold |

### Reliability Features
`RELIABILITY_FEATURES = []` — `rel_exit_cluster_dispersion` and `rel_exit_selected_advantage` **dropped**.

**Re-introduction policy** (must satisfy ALL):
1. Computed from folds < K only
2. Sourced from MC-INDEPENDENT exits ONLY (`fixed`, `atr_scaled`)
3. Group ablation shows >5% MC contribution
4. Fresh MC checkpoint trained + compared against C+ baseline on Phase 2 OOS folds 4–8

**Output:** MC checkpoints per `(asset, philosophy, fold)` + predictions parquet with `mc_score`, `mc_sigma`, `mc_expectancy`.

---

## Step 5 — Decision Layer (72 variants per philosophy)

**Script:** `prompt_4_decision_layer.py`
**Total strategies:** 12 philosophies × 72 variants = **864 frozen**

### Variant Grid

| Knob | Values | Count |
|---|---|---|
| Concurrency caps | `[5, 8, 10, 15, 20, 25]` | 6 |
| Replacement modes | `["close_at_market", "skip", "swap_by_expected_pnl"]` | 3 |
| MC threshold buckets | Q1 / Q2 / Q3 / Q4 of `mc_score` distribution | 4 |

`6 × 3 × 4 = 72 variants`. Note: `swap_by_expected_pnl` is **flag-gated** behind `ENABLE_SWAP=False` for now.

### Monotonicity Check (lines 172–250)
Per bucket: enforces **higher MC confidence → better PF**. Best bucket picked by `monotonicity_score` on **inner_train fold only** (LEAK-G04 prevention — never on the OOS fold being evaluated).

---

## Step 6 — Per-Philosophy Exit Selection (post-MC overlay)

**Production stack:** `fixed`, `atr_scaled`, `dist_exit` (per-cell calibration-gated). `sigma_exit` is **SHADOW-ONLY** until per-bar replay ships.

### 6a — Philosophy-level (`prompt_exit_selection.py`)

- `CHALLENGERS = ["atr_scaled", "sigma_exit", "dist_exit"]` (`gp` dropped, **PR-C**)
- **Holm-Bonferroni** on OOS folds `[3, 4, 5, 6, 7]`
- **Runtime guard (PR-H):** `sigma_exit` raises if `SIGMA_EXIT_APPROX != "per_bar_replay"` AND skipped when `SIGMA_EXIT_ELIGIBLE_FOR_SELECTION=False`
- **Bake-off PnL on-the-fly** per overlay (line 247+) — no per-overlay parquets (Option C+)
- Winner per philosophy → `configs/frozen/exit_selection.json` ← **what Phase 3 reads**

### 6b — Per-strategy fold-indexed (`prompt_exit_selection_per_strategy.py`)

- `selected_exit_by_fold[K]` using **only folds 0..K-1** (audit-fixed)
- Holm α = 0.01 (PR-B, `configs/exit_selection_config.yaml`) ⚠️ **see Open Gap #3**
- **Reserved for Phase 4** (online allocator), NOT Phase 3

### `dist_exit` Hardening (PR-I)

| Setting | Value |
|---|---|
| Tune folds | `{4, 5}` |
| Eval folds | `{6, 7}` |
| Reserved fold | `8` |
| Score on | TUNE only |
| Report | `capture_ratio_mean` on EVAL |
| Calibration gate | Per-cell from PR-G sidecar — ineligible cells skipped |
| Causality guard | Monotonic-index assertion at compute entry (line 192–198) |
| Threshold | Single global across all `(asset, philosophy)` |
| Inputs | 18 ATR-normalized signed TFM cols from PR-F |

---

## Step 7 — TimesFM 792-Cell Calibration

**Script:** `check_timesfm_calibration_per_cell.py` (**PR-G**)

**Cell grid:** `22 assets × 2 directions × 3 horizons × 6 regimes = 792 cells`

### Per-cell pass criteria (ALL required)

| Metric | Threshold |
|---|---|
| `direction_accuracy` Wilson LCB 95% | > 0.50 |
| Calibration slope | ∈ [0.85, 1.15] |
| Coverage_90 | ∈ [0.85, 0.95] |
| Coverage_95 | ∈ [0.92, 0.98] |

### 4-Tier Pooled Fallback

| Tier | Pool | Min n |
|---|---|---|
| Primary | asset × dir × horizon × regime | 80 |
| L1 | asset × dir × horizon | 200 |
| L2 | dir × horizon × regime | 350 |
| L3 | dir × horizon | 600 |
| Fail | — | `pass=false` → `dist_exit` ineligible |

**Output:** `pipeline_state/phase_2/tfm_calibration_per_cell.json` — consumed by PR-I.

---

## Step 8 — Sizing per Philosophy

**Script:** `prompt_5_sizing.py`

**Production sizing methods:** `["fixed", "kelly", "rl"]`
- PySR + XGB sizing dropped in Ship 3 (2026-04-24).

**Selection:** Holm test on Sharpe vs fixed baseline picks **one method per philosophy** → written to frozen package.

---

## Step 9 — Robustness Audit

**Script:** `prompt_5b_robustness_audit.py`
**Granularity:** per `(asset, philosophy, variant)`

### Tests

| Test | Detail |
|---|---|
| Bootstrap PF/Sharpe | 1000 samples |
| Block permutation p-values | **NOT iid** — preserves autocorrelation |
| Q1-vs-Q4 monotonicity | + multi-bucket variant |
| Event clustering | 6-bar same-asset, 3-bar cross-asset |
| Concentration ratio flag | > 0.40 |

### Output Tags
Each variant marked: **ROBUST** / **FLAGGED** / **FAIL**.

---

## Step 10 — Final Assembly

**Script:** `prompt_6_assembly.py`

- **Global Holm step** aggregates exit + sizing + MC-input p-values family-wide.
- **Hansen SPA** implemented (`hansen_spa.py`) but **NOT YET wired** — TODO.
- Selects **864 frozen strategies** (12 × 72).
- Writes per strategy:
  ```
  configs/frozen/phase2/{asset}_{philosophy}_variant_{N}_frozen_config.json
  ```

### Frozen config contents

| Field | Description |
|---|---|
| `rule_text` | Phase 1 rule (verbatim) |
| `mc_threshold` | MC score cutoff (chosen bucket) |
| `exit_variant` + params | From Step 6a |
| `sizing_method` | From Step 8 |
| `concurrency_cap` | From Step 5 |
| `replacement_mode` | From Step 5 |
| `expected_metrics` | OOS PF, Sharpe, MaxDD, trade count |
| `gate_passed` | `True` (always — failed variants excluded) |
| `sha256_pin` | Hash of config bytes |

---

## Step 11 — Phase 3 Handoff Manifest

- **`phase3_handoff_manifest.json`** — all **864** strategy SHA-256 hashes.
- `PHASE3_REQUIRED_KEYS` enforced in Phase 3 reader.
- Frozen files become **read-only**.
- Any tampering invalidates the holdout run.

---

## Open Gaps (must fix before production launch)

| # | Gap | Severity | Location / Action |
|---|---|---|---|
| 1 | `SIGMA_EXIT_ELIGIBLE_FOR_PHASE3` flag NOT enforced in `_load_exit_config(sigma_exit)` Phase 3 path | **HIGH** | Phase 3 loader |
| 2 | `dist_exit` Phase 3 keys (`asset`, `phase`, `base_times`, `entry_timestamp`) not populated by `_load_exit_config` | **HIGH** | `prompt_1_holdout_run.py` |
| 3 | α=0.01 Holm not actually wired in per-strategy selector — PR-B claim doesn't match code | **MEDIUM** | `prompt_exit_selection_per_strategy.py` |
| 4 | Tiebreak is order-based (first in `CHALLENGERS`), should be `argmax(test_stat)` | **MEDIUM** | Exit selectors |
| 5 | Exit selector scores on `net_pnl_pct` — verify column source is fixed-exit, not overlay | **HIGH** | `prompt_exit_selection.py` |
| 6 | `exit_selection_config.yaml` + `dist_exit_thresholds.json` not SHA-pinned | **MEDIUM** | Frozen-package manifest |
| 7 | `min_trades=5` Sharpe floor too loose at K=4 | **MEDIUM** | `prompt_exit_selection_per_strategy.py` |

---

**Wall-clock estimate:** ~6–9 days end-to-end (post 11-model prune; was 9–14 days).
