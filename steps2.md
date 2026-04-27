# Phase 2.5 — Detailed Step-by-Step Audit (2026-04-27)

**Audit closeout state:** 4 of 51 audit findings touched Phase 2.5 (M22 MetaDD shift, H14 SOFT_KILL_VARIANTS, H15 MANDATORY_VARIANTS reason, H16 baseline TUNE/EVAL). All shipped 2026-04-26.

**What Phase 2.5 produces:** 21 distinct portfolio variants from 864 frozen Phase 2 strategy packages. Per-cell selector cuts ~702 strategies (94% reduction from legacy 11,232) into Phase 3 holdout. Frozen output drives Phase 3 position sizing + 33,696 holdout runs.

**Wall-clock estimate:** ~1.5–2.5 days end-to-end (parallel-safe by cell + by philosophy).

**Goal of this document:** for each Phase 2.5 step, the user can audit (1) what the code does, (2) why each guard exists, (3) what fixes shipped, (4) what could still be improved, BEFORE Phase 2.5 production launches.

---

## Table of Contents
- [Pipeline Map](#pipeline-map)
- [Step 0 — Inputs from Phase 2](#step-0--inputs-from-phase-2)
- [Step 1 — MetaDD Predictor (Student-t, walk-forward)](#step-1--metadd-predictor-student-t-walk-forward)
- [Step 2 — HRP Construction (3 base + 5 LinUCB variants)](#step-2--hrp-construction-3-base--5-linucb-variants)
- [Step 3 — LinUCB Contextual Bandit (18-dim, 6-regime)](#step-3--linucb-contextual-bandit-18-dim-6-regime)
- [Step 4 — Neural Portfolio Optimizer (75-dim, two-stage)](#step-4--neural-portfolio-optimizer-75-dim-two-stage)
- [Step 5 — Analytic Kelly + DD-Aware Haircut (variant_8 / 8b)](#step-5--analytic-kelly--dd-aware-haircut-variant_8--8b)
- [Step 6 — MetaDD Overlay (10 pair artifacts)](#step-6--metadd-overlay-10-pair-artifacts)
- [Step 7 — Baselines (4 dumb-but-honest, H16 TUNE/EVAL split)](#step-7--baselines-4-dumb-but-honest-h16-tuneeval-split)
- [Step 8 — Asset Admission Gate](#step-8--asset-admission-gate)
- [Step 9 — Variant Selection + Promotion Gates](#step-9--variant-selection--promotion-gates)
- [Step 10 — Per-Cell Selector (468 cells, dynamic-K ≤ 3)](#step-10--per-cell-selector-468-cells-dynamic-k--3)
- [Step 11 — GP Portfolio (variant_11, TODO #28)](#step-11--gp-portfolio-variant_11-todo-28)
- [Step 12 — Phase 3 Handoff](#step-12--phase-3-handoff)
- [Audit Closeout — 4 Findings Shipped](#audit-closeout--4-findings-shipped)
- [Operational Follow-ups](#operational-follow-ups)

---

## Pipeline Map

### Directory inventory (`scripts/phases/phase_2_5/`)

| Script | Purpose | Entry |
|---|---|---|
| `prompt_7f_metadd_predictor.py` | Walk-forward MetaDD calibration (dd_mu, dd_sigma, dd_nu, dd_q90, dd_q95) | `main()` L612 |
| `prompt_7d_pysr_drawdown.py` | SymbolicRegression DD model | `main()` L867 |
| `prompt_7f_gp_drawdown.py` | GP-evolved DD model | `main()` |
| `prompt_7f_lgb_bootstrap_dd.py` | LightGBM bootstrap DD quantile | — |
| `prompt_7f_xgb_quantile_dd.py` | XGBoost quantile DD predictor | — |
| `prompt_7_metadd_overlay.py` | Apply MetaDD to 8 base → 8 pairs + 2 extras | `run()` L229 |
| `prompt_7g_portfolio_baselines.py` | 4 dumb-but-honest baselines (H16 fix) | `main()` L295 |
| `prompt_7h_asset_admission.py` | Per-asset whitelist post-Phase-2 | `main()` L174 |
| `prompt_7i_gp_portfolio.py` | GP-evolved portfolio (variant_11 future) | `main()` L214 |
| `prompt_7j_per_cell_selector.py` | **CORE:** 468-cell selector, dynamic-K ≤ 3 | `main()` L476 |
| `select_phase3_candidates.py` | **CORE:** variant scoring + mandatory advance | `main()` L519 |
| `freeze_phase25_artifacts.py` | Archive Phase 2.5 before Phase 3 | `main()` L70 |
| `prompt_sizing_selection.py` | Position-sizing allocation | `main()` L255 |
| `ab_validate_optimizer.py` | A/B test optimizers vs baselines (paired bootstrap) | `main()` L259 |
| `ablation_runner.py` | Variant ablation (sensitivity to K, selectivity) | `main()` L234 |
| `ablation_k_mode.py` | dynamic-K vs fixed-K per cell | `main()` L60 |
| `ablation_per_cell_fixed_vs_dynamic.py` | fixed-K per-strategy vs per-cell | `main()` L56 |
| `canonical_allocator_extensions.py` | Allocator optimization extensions (future) | — |

HRP/LinUCB construction lives upstream in [prompt_7_portfolio_construction.py](scripts/phases/phase_2/prompt_7_portfolio_construction.py) (Phase 2 boundary).

### Canonical execution order (12 stages)

1. **MetaDD Predictor** — fit Student-t over 6 base DD models, walk-forward; emit `metadd_predictions.parquet`
2. **HRP Construction** — Sharpe-distance clustering + Ledoit-Wolf cov bisection (in Phase 2 prompt_7)
3. **LinUCB Bandit** — 18-dim context, walk-forward training, ±20% satellite adjustment
4. **Neural Optimizer** — Stage 1 Ridge → Stage 2 SmallPortfolioNet; 75-dim input
5. **Analytic Kelly** — Ledoit-Wolf shrunk cov + Kelly fraction (γ=4 quarter-Kelly)
6. **MetaDD Overlay** — 10 pair artifacts via 8 modifier functions
7. **Baselines** — 4 dumb-but-honest equal-weight portfolios (H16 TUNE/EVAL)
8. **Asset Admission** — 22-asset whitelist (P25-Q-S8 ≥2-of-3 criteria)
9. **Variant Selection** — composite score + 4 promotion gates
10. **Per-Cell Selector** — 468 cells, dynamic-K ≤ 3, Holm m=468
11. **GP Portfolio** — variant_11 (TODO #28, not yet integrated)
12. **Phase 3 Handoff** — emit `phase3_deploy_manifest.json` + freeze artifacts

### Variant registry (21 distinct variants)

**Base variants (8) — `select_phase3_candidates.py:99-126`:**

| ID | Type | Status |
|---|---|---|
| `variant_1_best_single` | Single highest-Sharpe strategy | **soft_kill** |
| `variant_2_hrp_conservative` | HRP Core 60% (Lib 6,7) + Satellite 40% (Lib 2,8,9,11) | primary |
| `variant_3_hrp_a_linucb` | HRP-A + LinUCB ±20% on satellite | **soft_kill** |
| `variant_4_hrp_aggressive` | HRP Core 60% (Lib 1,2,5,3) + Satellite 40% (Lib 4,9,11,10) | primary |
| `variant_5_hrp_b_linucb` | HRP-B + LinUCB ±20% on satellite | **soft_kill** |
| `variant_6_50_50_blend` | 0.5×Conservative + 0.5×Aggressive | primary |
| `variant_7_neural_optimizer` | NN-optimized weights (75-dim two-stage) | **soft_kill** |
| `variant_8_kelly` | Analytic Kelly (Ledoit-Wolf cov + γ=4) | primary, **MANDATORY** |

**MetaDD A/B pairs (8):**

| ID | Modifier | Function |
|---|---|---|
| `variant_1b_best_single_metadd` | `_scale_by_q90` | Scale position size ↓ on dd_q90 spike |
| `variant_2b_hrp_conservative_metadd` | `_flatten_by_sigma` | Spread cluster weights on high dd_sigma |
| `variant_3b_hrp_a_linucb_metadd` | `_shrink_linucb_by_nu` | dd_nu as 17th LinUCB context |
| `variant_4b_hrp_aggressive_metadd` | `_shift_toward_conservative` | Low dd_nu (fat tails) → shift toward A |
| `variant_5b_hrp_b_linucb_metadd` | `_shrink_linucb_by_nu` | Same as 3b |
| `variant_6b_50_50_blend_metadd` | `_adjust_ab_blend` | dd_nu adjusts A/B split |
| `variant_7b_neural_metadd` | `_flatten_by_sigma` | Neural + dd_sigma flatten |
| `variant_8b_kelly_metadd` | `_scale_kelly_by_sigma` | Kelly × inverse uncertainty |

**MetaDD-only extras (2):**
- `variant_9_dd_capped_neural` — `_cap_by_q90` with **M22 shift(1) fix**
- `variant_10_portfolio_blender` — Inverse-uncertainty blend across 8 base variants

**Future / experimental (3):**
- `variant_7c_neural_linucb` — TODO #13 (LinUCB overlay on Neural)
- `variant_8c_kelly_linucb` — TODO #13 (LinUCB overlay on Kelly)
- `variant_11_gp_portfolio` — TODO #28 (GP-evolved formula)

**Frozensets exported (H14 fix):**
```python
SOFT_KILL_VARIANTS = frozenset({"variant_1", "variant_3", "variant_5", "variant_7"})
PRIMARY_VARIANTS  = frozenset({...all primaries...})
MANDATORY_VARIANTS = ["variant_8_kelly"]  # only one (2026-04-14)
```

### Cell key structure

```python
Cell = (asset, philosophy)
# 22 assets × 12 philosophies = 264 base cells
# 468 cells when including additional sizing-method dimension OR per-philosophy variant-subset enumeration
# Exact 468 derivation lives in select_dynamic_k logic at prompt_7j_per_cell_selector.py:236-265
```

PHASE3_USE_CELL_MANIFEST=True flag (per CLAUDE.md) cuts ~702 strategies into Phase 3 from legacy 11,232 (94% reduction).

### Wall-time totals

| Stage | Time |
|---|---|
| MetaDD calibration | 4-6h |
| HRP+LinUCB (Phase 2 boundary) | ~60-120s |
| Neural Optimizer | 6-12h |
| Kelly | seconds (analytic) |
| MetaDD Overlay | ~30 min |
| Baselines | ~30 min |
| Asset admission | ~10 min |
| Variant selection | ~15 min |
| Per-cell selector | 6-8h (parallelizable by cell) |
| GP portfolio (when shipped) | ~6h |
| **Total** | **~1.5-2.5 days** |

---

## Step 0 — Inputs from Phase 2

### What this step does

Step 0 documents the contract between Phase 2 and Phase 2.5. Phase 2.5 reads ONLY frozen Phase 2 outputs.

### Inputs from Phase 2

| Input | Path | Schema |
|---|---|---|
| 864 frozen strategy packages | `configs/frozen/phase3_handoff/strategy_*_package.json` | `{variant_id, asset, philosophy, strategy_name, ...}` |
| Phase 3 handoff manifest | `configs/frozen/phase3_handoff_manifest.json` | Master registry with `exit_config_shas` (Gap 6 SHA-pin) |
| Per-philosophy OOS returns | `pipeline_state/phase_2_5/strategy_oos_returns.parquet` | wide: index=date, cols=strategy_id |
| Base variant weights (8×) | `pipeline_state/phase_2_5/{variant_N}_weights.parquet` | index=date, cols=strategy_id |
| Base variant returns (8×) | `pipeline_state/phase_2_5/{variant_N}_returns.parquet` | index=date, single col |
| MetaCombiner predictions | `{philosophy}_cross_asset_metacombiner_predictions.parquet` | mc_mu/sigma/nu, mc_kelly_leverage, etc. |

### Outputs to Phase 3

| Output | Path | Schema |
|---|---|---|
| Advancing variants | `reports/phase2_5/phase3_candidate_variants.json` | `{advancing_variants: [...], scored_variants: {...}, composite_score_weights: {...}}` |
| Per-cell selections | `configs/frozen/phase3_cell_selections.json` | `{cell_key: {winners: [...], p_value, n_combos}, ...}` |
| Phase 3 deploy manifest | `configs/frozen/phase3_deploy_manifest.json` | Flat: `[{variant_id, asset, philosophy, ...}, ...]` |
| Cell trade ledgers | `pipeline_state/phase_2_5/cell_*_trades.parquet` | Per-cell simulation ledger |
| MetaDD predictions | `pipeline_state/phase_2_5/metadd_predictions.parquet` | Daily dd_mu/sigma/nu/q90/q95 |
| Baseline comparison | `reports/portfolio_baselines/baselines_returns.parquet` | Dumb-but-honest baseline returns |
| Asset whitelist | `configs/frozen/phase3_asset_whitelist.json` | 22-asset admission |

### Open concerns
1. **Phase 2 inputs depend on H21 re-emit** — feature stores carry the 1-bar lookahead leak in `tfm_fp_fr_corr_*` until Phase 0 re-emit completes. MetaCombiner trained on biased inputs would propagate bias into Phase 2.5.
2. **bf4 chain dependency** — Phase 2.5 cannot run until Phase 2 production completes. Phase 1 BF chain currently ~T+2.5d; bigcaps add ~12-18h.

---

## Step 1 — MetaDD Predictor (Student-t, walk-forward)

**Script:** [scripts/phases/phase_2_5/prompt_7f_metadd_predictor.py](scripts/phases/phase_2_5/prompt_7f_metadd_predictor.py)

### What this step does

Trains a per-strategy 7-day max-drawdown forecaster combining 6 base predictors via Student-t MLP head. Outputs distributional drawdown estimates (mu, sigma, nu, q90, q95) used by all `_metadd` variants. Walk-forward chronological; fold 1 = NaN by construction.

### Architecture

**6 base DD predictors (upstream, fold-aware):**
| Predictor | Outputs | File |
|---|---|---|
| PySR DD | q50, q90 | `prompt_7d_pysr_drawdown.py` |
| NGBoost DD | mu, sigma | trained on vol features |
| Vol baseline | mean/std (30-day window) | deterministic |
| GP DD | q50, spread | `prompt_7f_gp_drawdown.py` |
| XGB-Quantile DD | q10, q50, q90 | `prompt_7f_xgb_quantile_dd.py` |
| LGB-Bootstrap DD | mu, sigma | `prompt_7f_lgb_bootstrap_dd.py` |

**Student-t MLP head:**
- Hidden: 16
- LR: 3e-3
- Epochs: 300
- Patience: 30
- ν floor: `STUDENT_NU_MIN = 2.5` (B3 fix 2026-04-16, ensures finite variance)
- ν ceiling: `STUDENT_NU_MAX = 30.0`
- ν init: 5.0 (moderate tails)

### Outputs (`metadd_predictions.parquet`)

| Column | Meaning |
|---|---|
| `dd_mu` | Expected 7-day max drawdown |
| `dd_sigma` | Uncertainty scale |
| `dd_nu` | Tail shape (low → fat tails / crash risk) |
| `dd_q90` | 90th percentile from Student-t CDF |
| `dd_q95` | 95th percentile from Student-t CDF |

### Calibration metric
**Exceedance rate** = fraction of days where `realized_DD > predicted_q90`.
Well-calibrated slope ≈ 1.0 (10% exceedance rate). Used by `select_phase3_candidates.py` as gating criterion (`CALIBRATION_SLOPE_RANGE = (0.75, 1.25)`).

### Anti-leakage guards
- Walk-forward chronological folds (fold 1 = NaN — insufficient training)
- At day T: all inputs use data strictly before T; target = forward window T+1..T+7
- Each base predictor enforces own fold-safe preprocessing

### Wall-time
**4-6h** (468-cell walk-forward × 6 base models × MLP head training).

### Open concerns
1. **MetaDD calibration outside [0.75, 1.25] range** — variants flagged but still advance (gates in `select_phase3_candidates`)
2. **PySR/GP DD models** are research-grade; verify they don't quietly fall back to vol baseline in production
3. **Student-t initialization** at ν=5 may bias early-fold predictions toward Gaussian tails

---

## Step 2 — HRP Construction (3 base + 5 LinUCB variants)

**Script:** [scripts/phases/phase_2/prompt_7_portfolio_construction.py:598-697](scripts/phases/phase_2/prompt_7_portfolio_construction.py#L598-L697) (lives at Phase 2 boundary)

### What this step does

Hierarchical Risk Parity (HRP) clusters strategies hierarchically based on risk-adjusted co-movement, then recursively bisects to equalize risk contribution within each cluster. More robust than mean-variance (no full covariance inversion).

### HRP algorithm

| Component | Implementation | Notes |
|---|---|---|
| Distance metric | Sharpe-series correlation (21-day rolling) | P2.5-1: risk-adjusted, not raw returns (lines 646-650) |
| Linkage method | `config.HRP_LINKAGE_METHOD`, default "average" | v3.7 Tier A.5: switched single → average to reduce chaining (lines 653-656) |
| Weight computation | Recursive bisection with Ledoit-Wolf shrunk cov | Cov only for bisection; distance for clustering |
| NaN handling (R4) | Drops strategies with ≥20% NaN fraction pre-HRP | Prevents delisted assets from absorbing weight (lines 618-636) |
| Stability filter | Bootstrap resampling: zeros strategies not in top-K ≥80% of resamples | Anti-overfit (`PORTFOLIO_TOP_K`, `PORTFOLIO_BOOTSTRAP_THRESHOLD`, lines 679-692) |

### HRP variants (7 base)

| Variant | Type | Base structure |
|---|---|---|
| `variant_2_hrp_conservative` | Conservative | Core 60% (Lib 6,7) + Satellite 40% (Lib 2,8,9,11), static HRP |
| `variant_3_hrp_a_linucb` | Conservative + LinUCB | Same + LinUCB ±20% on satellite only |
| `variant_4_hrp_aggressive` | Aggressive | Core 60% (Lib 1,2,5,3) + Satellite 40% (Lib 4,9,11,10), static HRP |
| `variant_5_hrp_b_linucb` | Aggressive + LinUCB | Same + LinUCB ±20% on satellite only |
| `variant_6_50_50_blend` | Blend | 0.5×Conservative + 0.5×Aggressive |
| `variant_7c_neural_linucb` | Future | LinUCB overlay on Neural (TODO #13) |
| `variant_8c_kelly_linucb` | Future | LinUCB overlay on Kelly (TODO #13) |

### Inputs / outputs

**Inputs:**
- 1,296 strategy curves (72 variants × 18 libraries pre-prune; post-prune 864 = 12 phil × 72 var)
- Returns matrix: (T, K), T ≈ 500-750 days
- Regime contexts: (T, 18) constructed daily

**Outputs:**
- Per-variant weight matrices: (T, K), index=date, columns=strategy_id
- Variant returns parquet (single-column daily returns)
- Metadata JSON: Sharpe, max DD, Sortino, monthly win rate, turnover, stability score

### Wall-time
~2-5 seconds for HRP weight computation across all strategies. Full Phase 2.5 HRP+LinUCB+core-satellite pipeline: ~60-120s.

### Open concerns
1. **HRP stability filter threshold (80%)** — may be over-aggressive on short OOS samples
2. **Linkage method choice (average)** — was single pre-v3.7 Tier A.5; document basis for switch

---

## Step 3 — LinUCB Contextual Bandit (18-dim, 6-regime)

**Script:** [scripts/phases/phase_2/prompt_7_portfolio_construction.py:970-1181](scripts/phases/phase_2/prompt_7_portfolio_construction.py#L970-L1181)

### What this step does

LinUCB contextual multi-armed bandit learns which library (arm) performs best under different market regimes (contexts). Combines exploitation + exploration via upper confidence bound. Daily 18-dim regime features drive ridge-regression model per library, producing UCB scores that adjust HRP satellite weights ±20%.

### Anti-whipsaw constraints
- 7-day minimum hold
- 10% hysteresis (transition cost threshold)
- Max 2 transitions/month

### 18-dim context features (post 2026-04-24, was 16)

| # | Feature | Source | Lag |
|---|---|---|---|
| 0-2 | Vol regime one-hot {high/mid/low} | 21-day rolling vol on agg portfolio returns | 1d |
| 3-4 | Trend regime one-hot {bull/bear} | 21-day rolling avg return | 1d |
| 5-6 | Correlation regime {high/low} | Pairwise rolling corr | 1d |
| 7 | Market breadth | Frac strategies with positive return | 1d |
| 8-11 | Phase 0 regime label one-hot (4 cols) | Phase 0 regime_label, daily mode | 1d |
| 12-15 | LSTM encoder PCA (4 cols) | Phase 0 BTC LSTM encoder, z-scored | 1d |
| 16-18 | Ensemble uncertainty (3 cols) | MetaCombiner: ensemble_sigma_z, epistemic_variance_ratio, calibration_drift | No lag |

**Total dim:** d = 8 + 4 + 4 + 2 = 18 (post 2026-04-24). `LINUCB_CONTEXT_DIM = 18` in config.

**Variant 3b adds dd_nu as 17th feature** (overflow beyond 18-dim — order undefined).

### 6-regime conditioning

LinUCB does NOT train one bandit per regime. Single bandit with one A (d×d) + b (d-vector) per arm; regime labels are one-hot encoded as features (cols 8-11). Regime signal influences weights indirectly through ridge regression `θ = A⁻¹ b`.

**6 regimes** (Phase 0 labels): bull_trending, bear_trending, sideways, high_volatility, low_vol_compression, crash_capitulation (last 2 added 2026-04-24 Ship 2).

### Train/test split: walk-forward chronological

```
Months 1–3 (90 days):  Train (minimum window)
Month 4 (30 days):     Test (pick arm at T, reward = return[T-1])
Month 5 (30 days):     Test
...
```

**Anti-leakage assertion** at lines 1173-1175: reward index (t-1) strictly precedes context index (t).

### Outputs
- Trained `LinUCBBandit` object with persisted A/b matrices, ctx_mean/ctx_std, ctx_dim
- Daily satellite weight adjustments ±20%

### Wall-time
~10-20 seconds for full walk-forward on 500-day dataset.

### Anti-leakage guards
1. Phase 2 OOS data only (line 349-353)
2. Causal regime lag (1 day on all features; lines 1540-1543, 1561-1567)
3. LinUCB reward lag: reward = return[t-1], context = day T (lines 1173-1175)
4. LSTM encoder leakage-safe (Phase 0 frozen, lines 1555-1560)
5. Regime label lag: explicit 1-day forward-fill, no backfill (lines 185-195)
6. Context normalization stats persisted for Phase 3 replication (lines 1589-1599)
7. No Phase 3 contamination (header lines 20-21)

### Open concerns
1. **Regime feature store completeness** — fallback returns all zeros (silent degradation to 8-dim), not error
2. **Variant 3b 17th feature** — dd_nu overflow ordering not formally documented
3. **Walk-forward warm-up offset 21:** persist for Phase 3 replication; verify stored in artifact

---

## Step 4 — Neural Portfolio Optimizer (75-dim, two-stage)

**Script:** [scripts/phases/phase_2/prompt_7c_portfolio_optimizer.py](scripts/phases/phase_2/prompt_7c_portfolio_optimizer.py) (1,200+ lines)

### What this step does

Two-stage portfolio optimizer:
- **Stage 1:** Ridge regression on 1,324 features (864 strategies + 28 regime features) → ranks strategies, selects top-75
- **Stage 2:** SmallPortfolioNet MLP on 103 dims (75 selected strategies + 28 regime) → softmax weights

Loss: utility-based Sharpe maximization with turnover/concentration/drift/bound penalties.

### Stage 1 input dims (1,324)

- 864 strategy returns
- 28 regime features:
  - 4 base (vol_z, ret_z, corr, breadth)
  - 15 LSTM encoder summaries (enc_pca_0-3, momentum_a/c/emb_6h/24h, agreement_ab/ac/bc, volatility_24/168)
  - 6 regime one-hot (post Ship 2 + 2026-04-24)
  - 3 market/portfolio (rv_24h_vs_168h_ratio, btc_dominance_trend_30d, portfolio_dd_depth, ensemble_sigma_mu_ratio, days_since_regime_transition)

### Stage 2 input dims (103 = 75 + 28)

`NEURAL_OPT_INPUT_DIM = 75` (was 73 pre-2026-04-24)

### Architecture (SmallPortfolioNet)

- 103 → 8 (hidden, config-controlled L0-L4) → 75 (softmax output)
- ~1,730 parameters
- Input dropout (B5, 2026-04-16): `NEURALOPT_DROPOUT=0.1`
- Output modes:
  - **PURE:** softmax on 75-dim
  - **ADJUSTMENT:** tanh delta from HRP base, ±5%

Alternative: AnchoredLinearPortfolioNet (`PORTFOLIO_OPTIMIZER_VARIANT` config flag).

### Loss function

```
L = -Sharpe(w) + λ_turnover · E[turnover] + λ_concentration · concentration
    + λ_drift · weight_drift + λ_bound · bound_hits
```

| Term | Weight |
|---|---|
| `λ_turnover` | 0.10 |
| `λ_concentration` | 0.08 |
| `λ_drift` | 0.06 |
| `λ_bound` | 0.04 |

- Sharpe window: 30 trading days
- Annualization: √365.25 (crypto trades 24/7)

### Training procedure

- Walk-forward: train months 1..K, predict month K+1, minimum 12 training months
- AdamW, lr=1e-3, weight_decay=0.01 (L0; varies by REG_LEVEL)
- Epochs: 200/300/400/500/600 (L0..L4)
- Early stopping: patience=20
- Batch size: 64
- Recency weighting: exp(1) ≈ 2.7× recent vs oldest

### Weight constraints

- Per-strategy floor: 0.1% (prevents zeroing)
- Per-strategy cap: 5-8% (inference: 8% hard max)
- Minimum 20 non-zero positions (else equal-weight fallback if >1,276 zeroed)
- Renormalize after clamping

### Dimension manifest sidecar

Path: `prompt_7c_portfolio_optimizer.py` emits `.dim_manifest.json` alongside checkpoint.

```json
{
  "neural_opt_input_dim": 75,
  "num_regimes": 6,
  "timestamp": "2026-04-26T...",
  "config_hash": "..."
}
```

**Helper:** `load_portfolio_optimizer_with_check()` hard-fails on dim mismatch when `STRICT_REGIME=1` (default). Prevents loading stale 4-regime artifact with new 6-regime context (silent garbage).

### Outputs

- `data/portfolio/phase2_5/variant_7_neural_optimizer_weights.parquet` (T × 864 daily weights, but only top-75 nonzero)
- Diagnostic JSONs: fold-by-fold Sharpe, turnover, concentration

### Wall-time
**~6-12h** per CLAUDE.md (walk-forward over 12+ months).

### Open concerns
1. **Encoder summary feature availability** — Phase 0 LSTM retraining required for new assets
2. **Regime feature completeness** — zero-fill on missing assets/phase
3. **Recency weighting (exp(1))** — may over-weight recent regime; verify on adversarial hold-out

---

## Step 5 — Analytic Kelly + DD-Aware Haircut (variant_8 / 8b)

**Script:** [scripts/phases/phase_2/prompt_7e_kelly_portfolio.py](scripts/phases/phase_2/prompt_7e_kelly_portfolio.py) (650+ lines)

### What this step does

Computes per-strategy Kelly fraction from Phase 2 expectancy + Ledoit-Wolf shrunk covariance. variant_8 is a **mandatory baseline** — always advances to Phase 3 even if DD violates hard gate (H15 fix flags violation in `s.reason`).

### Formula

```
w_kelly = (1/γ) × Σ⁻¹ × μ
```

| Term | Definition |
|---|---|
| `μ` | Per-strategy expected daily returns (Phase 2 OOS realized mean OR MetaCombiner blend_mu if available) |
| `Σ` | Ledoit-Wolf shrunk covariance (handles 864-strategy rank-deficiency) |
| `γ` | Risk aversion = 4.0 (default → quarter-Kelly) |

### Kelly Bayesian shrinkage (H10 Phase 2 propagation)

Flows from MetaCombiner (Phase 2 Step 4) via `mc_kelly_leverage`:
```python
# Phase 2 prompt_2f_kelly_params.py:72-100
KELLY_DIVISOR_MC = 4.0
KELLY_SHRINK_LAMBDA = 0.5
KELLY_PRIOR = 0.25

raw = (df["mc_kelly_leverage"] / KELLY_DIVISOR_MC).clip(0.01, 1.0)
df["kelly_frac"] = (
    KELLY_SHRINK_LAMBDA * raw + (1.0 - KELLY_SHRINK_LAMBDA) * KELLY_PRIOR
).clip(0.01, 1.0)
```
Phase 2.5 Kelly variant consumes `kelly_frac` from MetaCombiner predictions (already shrunk).

### Variant 8b (DD-aware haircut, 2026-04-16)

Two-stage haircut on top of vanilla Kelly:

**Stage 1 (γ escalation):**
```python
ν_portfolio = min(dd_nu over active strategies)
if ν_portfolio < KELLY_DD_NU_THRESHOLD (4.0):
    γ_effective = γ × KELLY_DD_NU_MULTIPLIER (1.5)
    # Re-solve Kelly with γ_effective → sixth-Kelly instead of quarter
    w = (1/γ_effective) × Σ⁻¹ × μ
```

**Stage 2 (Q95 cap):**
```python
projected_dd_q95 = Σ |w_j| · dd_q95_j
if projected_dd_q95 > KELLY_DD_Q95_CAP (0.25):
    w *= (KELLY_DD_Q95_CAP / projected_dd_q95)  # Scale down
```

### Constraints

- Max weight per strategy: 5% (`KELLY_W_MAX`)
- Min nonzero positions: 20 (safety guardrail)
- Minimum training window: 3 months
- Walk-forward retrain: monthly

### Mandatory baseline status

Per `select_phase3_candidates.py:73`:
```python
MANDATORY_VARIANTS = ["variant_8_kelly"]  # Only one (2026-04-14)
```

variant_1_best_single and variant_6_50_50_blend removed from mandatory list — must earn placement via composite score.

**H15 fix:** if variant_8_kelly's `max_dd > HARD_DD_GATE`, advances anyway with `DD_VIOLATION_FLAG` in reason string. Phase 3 reports this honestly.

### Wall-time
**Seconds** (analytic, no optimization).

### Open concerns
1. **mc_kelly_leverage variance NaN** — fallback to fixed; Bayesian shrinkage 0.5 may undershrink on high-noise samples
2. **γ escalation threshold (ν=4)** — not back-tested against multiple regimes
3. **Q95 cap (0.25)** — global; per-asset adjustment may help

---

## Step 6 — MetaDD Overlay (10 pair artifacts)

**Script:** [scripts/phases/phase_2_5/prompt_7_metadd_overlay.py](scripts/phases/phase_2_5/prompt_7_metadd_overlay.py)

### What this step does

Single MetaDD predictor runs once (Step 1); each `_metadd` variant reads same `metadd_predictions.parquet` and applies a modifier function. A/B test: whether feeding dd_mu/sigma/nu improves walk-forward Sharpe.

### 10 pair artifacts

| Pair | Base | Modifier function | Logic |
|---|---|---|---|
| `variant_1b_best_single_metadd` | best_single | `_scale_by_q90` | Position size ↓ on dd_q90 spike |
| `variant_2b_hrp_conservative_metadd` | hrp_conservative | `_flatten_by_sigma` | High dd_sigma → spread cluster weights |
| `variant_3b_hrp_a_linucb_metadd` | hrp_a_linucb | `_shrink_linucb_by_nu` | dd_nu as 17th LinUCB context |
| `variant_4b_hrp_aggressive_metadd` | hrp_aggressive | `_shift_toward_conservative` | Low dd_nu → shift toward A |
| `variant_5b_hrp_b_linucb_metadd` | hrp_b_linucb | `_shrink_linucb_by_nu` | dd_nu as 17th LinUCB context |
| `variant_6b_50_50_blend_metadd` | 50_50_blend | `_adjust_ab_blend` | dd_nu adjusts A/B split |
| `variant_7b_neural_metadd` | neural_optimizer | `_flatten_by_sigma` | Neural × dd_sigma flatten |
| `variant_8b_kelly_metadd` | kelly | `_scale_kelly_by_sigma` | Kelly × inverse uncertainty `1/(1+dd_sigma)` |
| `variant_9_dd_capped_neural` | (none) | `_cap_by_q90` (M22) | Neural with drawdown q90 cap |
| `variant_10_portfolio_blender` | (none) | `_inverse_uncertainty_blend` | Inverse-uncertainty blend across 8 base |

### M22 fix deep dive — `_cap_by_q90` defense-in-depth shift(1)

**File:** [prompt_7_metadd_overlay.py:179-196](scripts/phases/phase_2_5/prompt_7_metadd_overlay.py#L179-L196)

```python
def _cap_by_q90(weights: pd.DataFrame, metadd: pd.DataFrame) -> pd.DataFrame:
    """9 dd_capped_neural: cap each strategy's weight at dd_q90 ceiling.
    
    2026-04-26 M22 fix: shift dd_q90 by 1 bar before reindex so day T's cap
    uses dd_q90 from day T-1 only (causal). The caller upstream may already
    enforce this contract; this is defense-in-depth.
    """
    dd_q90 = metadd["dd_q90"].shift(1).reindex(weights.index).fillna(0.20)
    n = weights.shape[1]
    cap = (dd_q90 / n * 3.0).clip(1.0 / n * 0.5, 1.0)
    result = weights.copy()
    for col in result.columns:
        result[col] = result[col].clip(upper=cap)
    row_sums = result.sum(axis=1).replace(0, 1)
    return result.div(row_sums, axis=0)
```

**Why:** day T's drawdown cap uses yesterday's q90 only — prevents look-ahead. Default `fillna(0.20)` = 20% conservative cap on missing historical data.

### Variant 10 (portfolio_blender) — BUG-BL-018 fix

Original divided by total_inv → near-zero daily weights. Fixed: equal-weight average across variants (not per-variant uncertainty-weighted because all share single MetaDD model).

### Wall-time
**~30 minutes** for all 10 overlays.

### Open concerns
1. **Single MetaDD model shared by all `_metadd` variants** — they're A/B tests of overlay logic, not independent ensembles
2. **`_shrink_linucb_by_nu` 17th feature ordering** — undocumented relative to standard 18-dim LinUCB context

---

## Step 7 — Baselines (4 dumb-but-honest, H16 TUNE/EVAL split)

**Script:** [scripts/phases/phase_2_5/prompt_7g_portfolio_baselines.py](scripts/phases/phase_2_5/prompt_7g_portfolio_baselines.py)

### What this step does

4 dumb-but-honest baselines that optimizers must beat on paired bootstrap KILL_RULES. Optimizers losing still flow to Phase 3 for honest reporting (flagged `optimizer_justified=False`).

### 4 baselines (lines 118-149)

| Baseline | Selection rule |
|---|---|
| `best_single` | Highest OOS Sharpe single strategy |
| `equal_weight_top5` | Equal weight across top-5 by Sharpe (TUNE-ranked) |
| `equal_weight_top10` | Equal weight across top-10 by Sharpe (TUNE-ranked) |
| `equal_weight_all` | All approved strategies (no selection) |

### H16 fix — TUNE/EVAL split for top-N ranking

```python
def build_baselines(rets, approved):
    n = len(rets)
    split = n // 2
    if split < 30:
        warnings.warn("H16: only N bars — TUNE/EVAL split requires ≥60. "
                      "Falling back to full-series ranking (in-sample bias).")
        tune_rets = rets
        eval_rets = rets
    else:
        tune_rets = rets.iloc[:split]
        eval_rets = rets.iloc[split:]

    # Rank on TUNE half
    per_strat_sharpe = {s: _sharpe(tune_rets[s]) for s in present}
    ranked = sorted(per_strat_sharpe, key=per_strat_sharpe.get, reverse=True)

    return {
        "portfolio_best_single": _ew(ranked[:1], eval_rets),
        "portfolio_equal_weight_top5": _ew(ranked[:5], eval_rets),
        "portfolio_equal_weight_top10": _ew(ranked[:10], eval_rets),
        "portfolio_equal_weight_all": _ew(present, rets),  # No selection — full series
    }
```

**Why H16:** previous behavior ranked on FULL OOS, then reported portfolio Sharpe of top-N — in-sample top-N selection inflates baseline performance and contaminates comparison vs optimizers.

**Now:** rank on first 50% (TUNE), evaluate portfolio on second 50% (EVAL). `equal_weight_all` unaffected (no selection happens).

### Wall-time
**~30 min**.

### Open concerns
1. **Fallback when N<60 bars** — emits warning + reverts to in-sample ranking (rare but possible on short OOS)
2. **Block bootstrap for paired test** — verify block size matches asset's BLOCK_SIZE per M19 fix

---

## Step 8 — Asset Admission Gate

**Script:** [scripts/phases/phase_2_5/prompt_7h_asset_admission.py](scripts/phases/phase_2_5/prompt_7h_asset_admission.py)

### What this step does

Drops assets if they fail ≥2 of 3 Phase 2 OOS criteria (P25-Q-S8 fix 2026-04-16, was AND→all fail, now OR-style ≥2-of-3).

### 3 criteria (defaults)

| Criterion | Threshold |
|---|---|
| `fold_win_rate` | < 0.50 |
| `oos_sharpe` | < 0.50 |
| `worst_regime_sharpe` | < 0.00 |

Asset fails if **2 or more** criteria are below threshold.

### Output

`configs/frozen/phase3_asset_whitelist.json` — Phase 3 runner reads this before launching 33,696 holdout runs.

### Wall-time
**~10 min**.

### Open concerns
1. **Defaults are conservative** — may admit assets that look OK on aggregate but fail in specific regimes (regime check is min, not consistent)
2. **No asset-specific override** — same thresholds for all 22 assets

---

## Step 9 — Variant Selection + Promotion Gates

**Script:** [scripts/phases/phase_2_5/select_phase3_candidates.py](scripts/phases/phase_2_5/select_phase3_candidates.py)

### What this step does

Scores all 21 variants, applies 4 promotion gates (with mandatory bypass for variant_8_kelly), emits `phase3_candidate_variants.json`.

### Composite score formula (lines 47-58)

```python
SCORE_WEIGHTS = {
    "median_12mo_sharpe":   0.30,
    "median_12mo_sortino":  0.15,
    "rolling_stability":    0.20,
    "positive_month_frac":  0.15,
    "max_dd_penalty":       0.15,    # negative weight
    "turnover_penalty":     0.05,    # negative weight
    "deployment_bonus":     0.10,    # OFF by default; Stage 6 cutover
}
```

P25-Q-S1 fix (2026-04-16): all 6 terms clipped to [0,1] for homogeneous scale.

### Promotion gates (lines 386-443)

```python
if variant in MANDATORY_VARIANTS:  # ["variant_8_kelly"]
    advances = True
    if max_dd > HARD_DD_GATE (0.40):
        flag "DD_VIOLATION_FLAG" (H15 fix: bypass with reporting)
else:
    meets_score = composite_score >= median(all scores)
    meets_dd = max_dd_pct <= HARD_DD_GATE
    meets_calibration = True
    if uses_metadd:
        meets_calibration = (CALIBRATION_SLOPE_RANGE[0] <= calibration_slope <= CALIBRATION_SLOPE_RANGE[1])
        # CALIBRATION_SLOPE_RANGE = (0.75, 1.25)
    
    advances = meets_score AND meets_dd AND meets_calibration
```

### H15 fix — Mandatory variants reason field

```python
@dataclass
class VariantScore:
    variant_id: str
    composite_score: float
    advances: bool
    reason: str  # H15: "mandatory_baseline", "composite_score", "gate_2_max_dd", "DD_VIOLATION_FLAG"
    ...
```

Every advancing variant has named `reason`. Phase 3 reads this for honest reporting.

### Output

`reports/phase2_5/phase3_candidate_variants.json`:
```json
{
  "selection_rule": {
    "weights": SCORE_WEIGHTS,
    "hard_dd_gate": 0.40,
    "calibration_slope_range": [0.75, 1.25],
    "mandatory_variants": ["variant_8_kelly"]
  },
  "variant_registry": [...21 variants with status field (H14)...],
  "scored_variants": [...VariantScore per variant...],
  "advancing_variants": [...names...],
  "missing_from_input": [...]
}
```

### Wall-time
**~15 min**.

### Open concerns
1. **Median threshold for `meets_score`** — purely relative; if all 21 variants are bad, ~half still advance
2. **Calibration slope range (0.75-1.25)** — wide; flag variants near edges
3. **deployment_bonus (0.10)** — Stage 6 feature, no activation timeline documented

---

## Step 10 — Per-Cell Selector (468 cells, dynamic-K ≤ 3)

**Script:** [scripts/phases/phase_2_5/prompt_7j_per_cell_selector.py](scripts/phases/phase_2_5/prompt_7j_per_cell_selector.py)

### What this step does

For each (asset, philosophy) cell, enumerates all (variant, strategy) combos, computes composite scores, applies Holm correction (m=468), selects dynamic-K ≤ 3 winners per cell. Cuts 94% from legacy 11,232 → ~702 strategies.

### Cell structure

```python
Cell = (asset, philosophy)
# Base cells: 22 × 12 = 264
# 468 cells when including additional sizing-method dimension OR variant-subset enumeration
```

Exact 468 derivation in `select_dynamic_k` logic (lines 236-265).

### Per-cell calculation

1. Load trade ledger for (asset, library) cell
2. Compute cell metrics: Sharpe, PF, Calmar, DD, turnover
3. DSR-adjust (deflated Sharpe ratio)
4. Compute 8-term composite score
5. `select_dynamic_k`: top-K via Option D (K ≤ 3 rule)
6. Apply Holm m=468 correction (`HOLM_CELL_P_THRESHOLD = 0.10`)

### Outputs

```
configs/frozen/phase3_cell_selections.json   (full audit)
configs/frozen/phase3_deploy_manifest.json   (flat list for Phase 3)
```

Per cell: winners list with `(portfolio_variant, strategy_variant, composite, rank, p_value, p_holm, significance)`.

### Fallback (lines 208-229)

If cell fails Holm, inherit most-common (portfolio_variant, strategy_variant) combo from same library across successful assets.

### Wall-time
**6-8h** (parallelizable by cell).

### Open concerns
1. **468 cell derivation** — not 22×12=264; exact additional dimension not documented in visited code
2. **Holm m=468** — harsh; may reject genuinely good cells under noise
3. **dynamic-K ≤ 3** — Option D rule should be documented in code-level comments
4. **Fallback inheritance** — most-common-combo is heuristic; may inherit a poor combo if successful assets are themselves marginal

---

## Step 11 — GP Portfolio (variant_11, TODO #28)

**Script:** [scripts/phases/phase_2_5/prompt_7i_gp_portfolio.py](scripts/phases/phase_2_5/prompt_7i_gp_portfolio.py)

### What this step does

Evolves a per-strategy weight formula via genetic programming:
```
weight_i = f(sharpe_i, pf_i, calmar_i, dd_i, corr_avg_i, n_trades_i, win_rate_i, mc_sigma_i, mc_nu_i)
```
Softmax-normalize across 1,296 strategies. Top-K=50 allocation.

### Configuration

- 5K population × 500 generations
- 15 fitness runs (5 variants × 3 seeds): sharpe, pf, ic, expectancy, calibration
- Fitness: `Portfolio_Sharpe × √n_days × (1 − DD_penalty) × (1 − variance_penalty)`

### Outputs

- `configs/frozen/variant_11_gp_portfolio.txt` — evolved expression
- `configs/frozen/variant_11_gp_portfolio.json` — diagnostic

### Status: TODO #28 — NOT YET INTEGRATED into Phase 2.5 pipeline.

### Wall-time
**~6h** when shipped.

### Open concerns
1. **Not yet integrated** — listed in registry as experimental placeholder
2. **5K × 500 generations** — large search space, may overfit
3. **9 input features** — fewer than neural optimizer's 75; may miss regime context

---

## Step 12 — Phase 3 Handoff

**Script:** [scripts/phases/phase_2_5/freeze_phase25_artifacts.py](scripts/phases/phase_2_5/freeze_phase25_artifacts.py)

### What this step does

Freezes Phase 2.5 artifacts before Phase 3 launches. Archives outputs, computes SHA256 hashes, writes deploy manifest.

### Outputs to Phase 3

| Output | Path |
|---|---|
| Deploy manifest | `configs/frozen/phase3_deploy_manifest.json` |
| Cell selections | `configs/frozen/phase3_cell_selections.json` |
| Asset whitelist | `configs/frozen/phase3_asset_whitelist.json` |
| Candidate variants | `reports/phase2_5/phase3_candidate_variants.json` |
| Cell trade ledgers | `pipeline_state/phase_2_5/cell_*_trades.parquet` |
| MetaDD predictions | `pipeline_state/phase_2_5/metadd_predictions.parquet` |
| Baselines | `reports/portfolio_baselines/baselines_returns.parquet` |

### Read-only enforcement

Same H13 chmod 0o444 pattern as Phase 2 final assembly (Linux/SLURM only; `FROZEN_READONLY_DISABLE=1` bypass).

### Phase 3 reads `package["selected_sizing"]` (from Step 8) and per-cell winners from `phase3_deploy_manifest.json` to launch 33,696 holdout runs.

### Wall-time
**< 1 minute** (atomic writes + chmod).

---

## Audit Closeout — 4 Findings Shipped

Phase 2.5 received 4 of the 51 audit findings shipped 2026-04-26.

### M22 — MetaDD Causality Defense-in-Depth

**File:** [prompt_7_metadd_overlay.py:179-196](scripts/phases/phase_2_5/prompt_7_metadd_overlay.py#L179)

`dd_q90.shift(1).reindex(weights.index)` — day T's drawdown cap uses dd_q90 from day T-1 only.

### H14 — SOFT_KILL_VARIANTS Frozenset

**File:** [select_phase3_candidates.py:131-136](scripts/phases/phase_2_5/select_phase3_candidates.py#L131)

```python
SOFT_KILL_VARIANTS = frozenset({"variant_1", "variant_3", "variant_5", "variant_7"})
PRIMARY_VARIANTS  = frozenset({...14 primaries...})
```

CLAUDE.md claimed "23 total = 14 primary + 5 baseline + 4 soft-kill" but no explicit set existed. H14 adds `status` field per variant + frozensets.

### H15 — Mandatory Variants DD-Violation Flag

**File:** [select_phase3_candidates.py:403-415](scripts/phases/phase_2_5/select_phase3_candidates.py#L403)

```python
if variant in MANDATORY_VARIANTS:
    s.advances = True
    if s.max_dd_pct > HARD_DD_GATE:
        s.reason = "mandatory_baseline (DD_VIOLATION_FLAG)"
    else:
        s.reason = "mandatory_baseline"
```

Mandatory variants (variant_8_kelly only) advance regardless, but DD violations flagged in `s.reason` — Phase 3 reports honestly.

### H16 — Baseline TUNE/EVAL 50/50 Split

**File:** [prompt_7g_portfolio_baselines.py:118-149](scripts/phases/phase_2_5/prompt_7g_portfolio_baselines.py#L118)

Top-N selection uses TUNE half (first 50%); evaluation reports portfolio Sharpe on EVAL half. Prevents in-sample top-N selection bias vs optimizers.

---

## Operational Follow-ups

1. **Phase 0 H21 re-emit dependency** — Phase 2 + 2.5 inputs may carry 1-bar lookahead leak in `tfm_fp_fr_corr_*` until re-emit lands. MetaCombiner must re-train post-H21.

2. **Phase 1 BF chain + bigcaps must complete** before Phase 2.5 can run. Current ETA: T+2.5 days.

3. **variant_11 GP portfolio** — TODO #28; integrate before Phase 2.5 production OR document as deferred to Phase 2.6+.

4. **`variant_7c/8c LinUCB overlays`** — TODO #13; integrate or remove from registry to clean up.

5. **`PHASE3_USE_CELL_MANIFEST=True` flag** — verify enabled in Phase 3 runner; without it, falls back to legacy 11,232 runs.

6. **Asset admission whitelist** — verify Phase 3 runner reads it; otherwise wastes compute on dropped assets.

7. **Wall-time validation** — empirical timings not yet measured for full Phase 2.5 sweep; may differ from estimates above.

---

**Wall-clock estimate:** ~1.5–2.5 days end-to-end (parallelizable by cell + by philosophy). Total path Phase 2 → 2.5 → Phase 3-ready: T+11.5–14.5 days from now.

**Document version:** 2026-04-27 (post 51-finding audit closeout).
