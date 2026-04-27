# Phase 3 — Detailed Step-by-Step Audit (2026-04-27)

**Audit closeout state:** Phase 3 received heavy attention in the 51-finding closeout — **6 of 17 HIGH + 4 Gap fixes** target Phase 3 reader: H17 (`_refuse_dev_data` separators), H18 (hard-fail silent fallback), H19 (exit allowlist), H20 (verify_package_sha), H22 (manifest SHA-pin), Gap 1 (sigma_exit phase 3 enforcement), Gap 2 (dist_exit phase 3 keys), Gap 6 (SHA-pin verification).

**What Phase 3 produces:** raw holdout metrics on 2025-01-01 → 2025-12-31 untouched data. **NO retraining. NO threshold tuning. NO feature changes.** Reads frozen Phase 2.5 outputs (~702 strategies post-cell-selector) + applies them deterministically. Reports per-strategy / per-asset / per-direction / per-regime / per-confidence-bucket breakdowns.

**Wall-clock estimate:** ~5–8 days end-to-end (parallel-safe by strategy).

**Goal of this document:** for each Phase 3 step, audit (1) what the code does, (2) why each guard exists, (3) what fixes shipped, (4) open concerns BEFORE Phase 3 production launches. This is the **last validation before final results** — once Phase 3 runs, holdout is contaminated.

---

## Table of Contents
- [Pipeline Map](#pipeline-map)
- [Step 0 — Inputs from Phase 2.5](#step-0--inputs-from-phase-25)
- [Step 1 — Pre-Flight Validation](#step-1--pre-flight-validation)
- [Step 2 — TimesFM Phase 3 Generation](#step-2--timesfm-phase-3-generation)
- [Step 3 — Holdout Execution (`prompt_1_holdout_run.py`)](#step-3--holdout-execution-prompt_1_holdout_runpy)
- [Step 4 — Regime-Conditioned Analysis (`prompt_2`)](#step-4--regime-conditioned-analysis-prompt_2)
- [Step 5 — Ensemble Contribution Ablation (`prompt_3`)](#step-5--ensemble-contribution-ablation-prompt_3)
- [Step 6 — Leverage Analysis (`prompt_4`)](#step-6--leverage-analysis-prompt_4)
- [Step 7 — Model Agreement (`prompt_5`)](#step-7--model-agreement-prompt_5)
- [Step 8 — TimesFM Accuracy (`prompt_6`)](#step-8--timesfm-accuracy-prompt_6)
- [Audit Closeout — Phase 3 Hardening](#audit-closeout--phase-3-hardening)
- [Operational Follow-ups](#operational-follow-ups)
- [Appendix A — Critical Refusal Rules (CLAUDE.md)](#appendix-a--critical-refusal-rules-claudemd)

---

## Pipeline Map

### Directory inventory (`scripts/phases/phase_3/`)

| Script | Purpose |
|---|---|
| `validate_phase3_inputs.py` | Pre-flight 9-point validation — refuses to launch if any check fails |
| `generate_timesfm_phase3.py` | TimesFM feature shim — reads aliases from unified Phase 2 store, writes legacy path |
| `prompt_1_holdout_run.py` | **Core:** strategy execution on holdout (~2,300 lines) |
| `prompt_2_holdout_analysis.py` | Regime-conditioned + per-asset/dir/bucket breakdown |
| `prompt_3_ensemble_contribution.py` | Drop-one ablation on 11 component models |
| `prompt_4_leverage_analysis.py` | Realized leverage distribution + Kelly performance |
| `prompt_5_model_agreement.py` | Cross-model correlation + redundancy analysis |
| `prompt_6_timesfm_accuracy.py` | Realized vs forecasted direction accuracy on holdout |
| `run_phase3_full.sh` | Full pipeline orchestrator (SLURM) |
| `launch_phase3.sh` | Single-strategy launcher |

### Canonical execution order

```
Step 1: Pre-flight validation
   validate_phase3_inputs.py → 9-point checklist (handoff, OHLCV, models, snapshot, ...)
              ↓ (rc=0 to proceed)
Step 2: TimesFM Phase 3 feature generation (per asset)
   generate_timesfm_phase3.py → writes legacy alias parquet
              ↓
Step 3: Holdout execution (per strategy, ~702 strategies)
   prompt_1_holdout_run.py → reports/phase3/per_strategy/strategy_{sid}_detail.json
              ↓
Steps 4-8: Analysis prompts (parallel, all read holdout output)
   ├─ prompt_2_holdout_analysis.py    (regime breakdown)
   ├─ prompt_3_ensemble_contribution.py (drop-one ablation)
   ├─ prompt_4_leverage_analysis.py    (leverage distribution)
   ├─ prompt_5_model_agreement.py      (cross-model correlation)
   └─ prompt_6_timesfm_accuracy.py     (forecast accuracy)
              ↓
Final reports → user inspects, no automated verdict
```

### Wall-time totals

| Stage | Wall-time |
|---|---|
| Pre-flight validation | ~30-60 sec |
| TimesFM Phase 3 generation | ~3-5 sec/asset × 26 assets = ~2-5 min |
| Holdout execution (~702 strategies) | **~3-5 days** (parallel via SLURM array) |
| Analysis prompts 2-6 (parallel) | ~10-15 min each |
| **Total** | **~5-8 days** |

---

## Step 0 — Inputs from Phase 2.5

### What this step does

Step 0 documents the contract between Phase 2.5 and Phase 3. **Phase 3 reads ONLY frozen Phase 2.5 outputs** — any modification invalidates the holdout result.

### Inputs from Phase 2.5

| Input | Path | Schema |
|---|---|---|
| Phase 3 deploy manifest | `configs/frozen/phase3_handoff/phase3_handoff_manifest.json` | `{schema_version, snapshot_id, total_packages, total_strategies, strategies: {sid: {...}}, exit_config_shas: {...}}` |
| ~702 strategy packages | `configs/frozen/phase3_handoff/strategy_{sid}_package.json` | Per-strategy frozen config with `sha256`, `variant_tuple_sha`, exit_variant, sizing_method, concurrency_cap, etc. |
| Per-asset frozen configs | `configs/frozen/phase2/{asset}_{philosophy}_variant_{N}_frozen_config.json` | Sub-configs with `gate_passed=True`, exit params |
| Cell selections | `configs/frozen/phase3_cell_selections.json` | Per-cell winners (asset × philosophy) with rank + p_value + Holm |
| Asset whitelist | `configs/frozen/phase3_asset_whitelist.json` | 22-asset admission |
| Exit config SHA pins | Embedded in `phase3_handoff_manifest.json` under `exit_config_shas` | sha256 of `exit_selection_config.yaml`, `dist_exit_thresholds.json`, `tfm_calibration_per_cell.json` |
| Frozen models | `models/phase2/{asset}_{philosophy}_xgb_classifier_fold_{k}.pkl` | All 11 component model checkpoints (XGB critical; ensemble optional but recommended) |
| Phase 3 OHLCV | `ohlcv/phase3/phase3_ohlcv_{asset}_{tf}.csv` | 41 files: crypto 1h+1d, stocks 1d (or 1h post WRDS rebuild) |
| Phase 3 feature stores | `features/phase3/feature_store_{asset}_phase3.parquet` | ~3,100 cols, post-H21 re-emit (causal `tfm_fp_fr_corr_*`) |
| Phase 3 TimesFM legacy | `features/phase3/timesfm_features_{asset}_phase3.parquet` | Generated at Step 2 from unified store |

### Read-only enforcement (H13)

All 864 strategy packages + manifest are `chmod 0o444` (set by Phase 2 Step 10). Phase 3 verifies via `assert_exit_configs_sha_pinned()` at startup (H22).

### Snapshot integrity

`SNAPSHOT_ID = "phase_split_v5_perasset"` (config.py:42). Phase 3 verifies snapshot checksum before launching.

### Phase boundaries

Per-asset Phase 1 → Phase 2 → Phase 3 boundaries from `config.PHASE_BOUNDARIES`. Phase 3 holdout window: **2025-01-01 → 2025-12-31** universal.

### Open concerns / gotchas
1. **H21 re-emit dependency** — feature stores must be regenerated post-Phase 0 H21 fix BEFORE Phase 3 launches (otherwise `tfm_fp_fr_corr_*` cols carry 1-bar lookahead leak)
2. **Phase 2.5 freeze required** — Phase 3 cannot launch until `phase3_handoff/` directory is populated by Phase 2.5
3. **Exit config SHA drift** — manual edits to YAML/JSON between handoff and Phase 3 launch will hard-fail at startup
4. **Stale GP/PySR exit_variant in package** — H19 hard-fails if any package has `exit_variant in {gp, pysr}` (legacy/orphaned)

---

## Step 1 — Pre-Flight Validation

**Script:** [scripts/phases/phase_3/validate_phase3_inputs.py](scripts/phases/phase_3/validate_phase3_inputs.py) (~310 lines)

### What this step does

Refuses to launch Phase 3 if any of 9 critical inputs are missing, corrupt, or contaminated. Blocking gate before TimesFM generation + holdout execution.

### CLI + entry point

```bash
python -m scripts.phases.phase_3.validate_phase3_inputs [--verbose]
```

Entry: `if __name__ == "__main__": sys.exit(main())` (line 282).

### 9-point validation checklist (lines 282-290)

| # | Check | What it verifies | Lines | Severity |
|---|---|---|---|---|
| 1 | Handoff packages | `configs/frozen/phase3_handoff/` exists; manifest n_strategies + n_packages match | 86-111 | FATAL |
| 2 | Phase 3 feature stores | All 26 assets have `feature_store_{asset}_phase3.parquet` | 114-122 | FATAL |
| 3 | Phase 3 OHLCV | 41 files (crypto 1h+1d, stocks 1d) | 125-137 | FATAL |
| 4 | Model files | XGB classifiers per (philosophy, asset, fold) — critical; ensemble (LSTM2/TFT2/CA2/etc.) — optional | 140-194 | XGB FATAL, others WARN |
| 5 | No Phase 3 contamination | SQLite registry check: deny any Phase 1/2 experiment notes referencing `%phase3%` | 197-225 | WARN |
| 6 | Snapshot integrity | `verify_snapshot(phase=3, assets=ASSETS)` checks checksums | 228-236 | FATAL |
| 7 | confidence_modifiers module | Import + self-test of `scripts.common.utils.confidence_modifiers` | 239-246 | FATAL |
| 8 | TimesFM availability | Import check; graceful fallback if missing | 249-256 | WARN |
| 9 | Disk space | > 2 GB free required; warns if < 5 GB | 259-265 | WARN if <5GB; FATAL if <2GB |

**Exit codes:** 0 = pass, 1 = fail (only if checks 1, 3, 4-XGB, 6, 7 fail).

### What's NOT yet validated (gaps)

- **Asset whitelist consumption** — Phase 3 needs to read `configs/frozen/phase3_asset_whitelist.json` from Phase 2.5; verify pre-flight checks for it
- **Cell deploy manifest** — `phase3_deploy_manifest.json` should be checked for consistency with strategy packages
- **H22 SHA-pin section presence** — pre-flight should optionally pre-check `manifest["exit_config_shas"]` so SHA mismatch surfaces BEFORE launch (currently only at runtime in `prompt_1_holdout_run.py:assert_exit_configs_sha_pinned`)

### Wall-time
**~30-60 sec** (mostly I/O + registry query).

### Open concerns / gotchas
1. **Pre-flight does NOT verify chmod 0o444 (H13)** — could surface read-only enforcement issue at runtime
2. **Optional ensemble model checks WARN, not FATAL** — Phase 3 can still run with XGB only, but holdout result may differ from Phase 2 expected
3. **Disk space < 2GB threshold may be too low** — 33,696 holdout runs each writing detail+monthly outputs; estimate 5-10 GB needed

---

## Step 2 — TimesFM Phase 3 Generation

**Script:** [scripts/phases/phase_3/generate_timesfm_phase3.py](scripts/phases/phase_3/generate_timesfm_phase3.py)

### What this step does

**SHIM ONLY** — Phase 0 (`run_timesfm.py`) generates the unified TimesFM features into `features/phase3/feature_store_{asset}_phase3.parquet`. This script reads 8 alias columns and writes a legacy backward-compat parquet at `features/phase3/timesfm_features_{asset}_phase3.parquet`.

### CLI

```bash
python -m scripts.phases.phase_3.generate_timesfm_phase3 --asset {btc|eth|...}
python -m scripts.phases.phase_3.generate_timesfm_phase3 --all-assets [--force]
```

### How it differs from Phase 0/2

- **Phase 0 (`run_timesfm.py`)** generates ALL TimesFM features into unified feature store + applies H21 fix (close[anchor-1] for `tfm_fp_fr_corr_*`)
- **Phase 3 shim** reads 8 aliases from unified store and writes legacy path

### 8 alias columns read (lines 30-39)

```
timesfm_4h_direction
timesfm_12h_direction
timesfm_24h_direction
timesfm_horizon_agreement
timesfm_4h_uncertainty
timesfm_12h_uncertainty
timesfm_conviction_4h
timesfm_alignment
```

### H21 fix propagation

The shim **assumes Phase 0 TimesFM v2 (unified generation) has already run with the H21 fix applied** — i.e. `realized_at_anchors = close[np.maximum(anchor_idx - 1, 0)]` at `run_timesfm.py:649`.

- If upstream Phase 0 job hasn't run: **FATAL** (line 54-56), returns exit code 1
- If columns missing: fills with 0.0, **warns** (lines 68-76)

**🚨 Critical pre-Phase-3 step:** `p0_timesfm_all` (JID 8897808 currently queued with dependency on `p1_consolidate` 8855940) must complete BEFORE Phase 3 launches.

### Output schema

**Path:** `features/phase3/timesfm_features_{asset}_phase3.parquet`
**Columns:** timestamp + 8 alias cols above
**Atomicity:** atomic write via `.tmp.parquet` (lines 78-80)

### Wall-time
**~3-5 sec/asset × 26 assets = ~2-5 min total** (parquet I/O only, no computation).

### Open concerns / gotchas
1. **Phase 0 H21 re-emit dependency** — if not completed, shim either fails or fills with 0.0 (silent degradation)
2. **Legacy backward-compat output** — verify all downstream consumers actually need this; could be removed if no consumers exist

---

## Step 3 — Holdout Execution (`prompt_1_holdout_run.py`)

**Script:** [scripts/phases/phase_3/prompt_1_holdout_run.py](scripts/phases/phase_3/prompt_1_holdout_run.py) (~2,300 lines)

### What this step does

Replays ~702 frozen strategy variants × per-asset on 2025 holdout. Zero retraining. Each strategy executes all signals that passed Phase 2.5 admission gates with frozen position sizing, exit logic, ML overlays, and concurrency caps. Outputs equity curves, trade metrics, regime/monthly breakdowns. Both **capped** ($50k single-position cap) and **uncapped** equity curves reported per CLAUDE.md.

### CLI + main() entry

```bash
# All strategies
python -m scripts.phases.phase_3.prompt_1_holdout_run --all

# Specific strategies
python -m scripts.phases.phase_3.prompt_1_holdout_run --strategy_ids 001,002,003

# SLURM array per (philosophy, variant, asset)
python -m scripts.phases.phase_3.prompt_1_holdout_run \
    --philosophy aggressive_pf_max --variant 03 --asset btc \
    --exit_variant {fixed|atr_scaled|sigma_exit|dist_exit}
```

**Args** (lines 2699-2950): `--all | --strategy_ids` (required), `--manifest`, `--output_dir`, `--force`, `--exit_variant`, `--philosophy/--variant/--asset` (SLURM mode).

### Manifest + package loading (H20 + H22)

**`load_manifest()` at line 831:**
- Calls `_refuse_dev_data()` on manifest path (H17 normalize + deny-list)
- Calls `assert_exit_configs_sha_pinned()` (Gap 6 fix at lines 647-722)
- Verifies all ~702 strategy packages exist on disk (line 856)

**`load_frozen_strategy()` at line 869:**
- **H20 enforcement (lines 888-920):** `verify_package_sha` + `assert_required_keys` UNLESS `DEV_MODE=1`. Non-strict packages (missing `sha256` OR `mc_inputs`) raise `ValueError` in production, warning-only in dev
- Loads per-asset frozen configs from `configs/frozen/phase2/{asset}_{philosophy}_variant_{N}_frozen_config.json` (line 939)
- Applies Phase 2.5 admission gate filter: skips non-admitted assets (line 936)

### `_refuse_dev_data` (H17 fix)

**Lines 607-633:**
```python
# Normalize separators: phase_1, phase-1, phase 1 all match
norm = re.sub(r"[\s_\-]+", "", path_str)

# Hard deny-list
_DENY = ("raw_pool", "exploratory", "gp_discovery",
         "pipeline_state/phase_1", "pipeline_state/phase_2",
         "data/signals/phase1", "data/signals/phase2")
for tok in _DENY:
    if tok in path_str: raise RuntimeError(...)

# Whitelist bypass: only allow phase1/2 refs if path contains
# "frozen", "model", or "config"
if has_dev and "frozen" not in path_str and "model" not in path_str \
   and "config" not in path_str:
    raise RuntimeError(f"PHASE 3 VIOLATION: dev data: {path}")
```

### `_load_exit_config` (Gap 1, 2, H18, H19)

**Sigma_exit branch (Gap 1, lines 479-520):**
```python
if exit_variant == "sigma_exit":
    _sigma_p3_ok = bool(getattr(config, "SIGMA_EXIT_ELIGIBLE_FOR_PHASE3", False))
    _sigma_approx = getattr(config, "SIGMA_EXIT_APPROX", "terminal_pnl_grid")
    if not _sigma_p3_ok or _sigma_approx != "per_bar_replay":
        raise RuntimeError(
            f"PR-H gap-1: sigma_exit ineligible for Phase 3 "
            f"(SIGMA_EXIT_ELIGIBLE_FOR_PHASE3={_sigma_p3_ok}, "
            f"SIGMA_EXIT_APPROX={_sigma_approx!r}; need True + 'per_bar_replay')."
        )
    # ... load fold spec, return {k_tp, k_sl, sigma_col, gate_passed: True}
```

**Dist_exit branch (Gap 2, lines 522-550):**
```python
if exit_variant == "dist_exit":
    return {
        "model_path": str(mc_model_glob[-1]),
        "fold_idx": 0,
        "sigma_threshold": 2.0,
        "kelly_exit_floor": 0.001,
        "round_trip_cost": 0.002,
        "gate_passed": True,
        # Gap 2 keys
        "asset": asset,
        "direction": direction,
        "phase": 3,
        "base_times": None,
        "entry_timestamp": None,
        "calibration_sidecar": str(calib_sidecar) if calib_sidecar.exists() else None,
    }
```

**Per-bar replay walker (2026-04-27 consolidation):**

`_compute_distribution_exit_signals` no longer reimplements the walker —
it imports `replay_trade_per_bar` from
`scripts.common.utils.dist_exit_replay` (the single source of truth shared
with Phase 2 `prompt_2n_distribution_exit.py`). The Phase 3 wrapper
applies Phase-3-specific regime + ν tightening to the threshold, builds a
`feature_bars` DataFrame from `entry_time_features: Dict[str, np.ndarray]`,
constructs a `trade` Series, and calls the shared walker. **Same trade →
identical exit array as Phase 2 bake-off** (deterministic, SHA-pinned via
`WALKER_SHA`). The Phase 3 reimplementation that previously lived at
`prompt_1_holdout_run.py:201-350` was deleted to eliminate version skew.

```python
from scripts.common.utils.dist_exit_replay import (
    replay_trade_per_bar, MIN_HOLD_BARS, HYSTERESIS_BARS, KELLY_EXIT_FLOOR,
)

# Build feature_bars DataFrame from entry_time_features dict + threshold
exit_arr, _debug = replay_trade_per_bar(
    trade=pd.Series({"asset": asset, "direction": direction,
                     "fill_bar": 0, "exit_bar": n_bars - 1,
                     "entry_price": entry_price, "regime_label": regime_label,
                     "tfm_horizon": tfm_horizon}),
    feature_bars=feature_bars,
    round_trip_cost=round_trip_cost,
    trend_strength_threshold=adjusted_threshold,
    kelly_exit_breaker=True,
    calibration_sidecar_path=calibration_sidecar,
)
```

**Validation:** `tests/test_dist_exit_replay.py` (11 tests) covers walker
unit triggers, PR-I causality guard, determinism (100-run byte-identical),
calibration gate fallthrough, and MC blocklist regression.

**Generic exits (H18, lines 552-587):**
```python
exit_config_path = ROOT / f"configs/frozen/exits/{asset}_{direction}_{exit_variant}_exit.json"
_allow_fallback = os.environ.get("PHASE3_ALLOW_EXIT_FALLBACK", "0") == "1"

if not exit_config_path.exists():
    if not _allow_fallback:
        raise RuntimeError(f"PHASE 3 H18: missing exit config; cannot silently fall back to fixed.")
    # ... legacy warn-only path

if not exit_config.get("gate_passed", False):
    if not _allow_fallback:
        raise RuntimeError(f"PHASE 3 H18: gate failed; cannot silently fall back.")
```

### `get_exit_for_trade` (H19 allowlist, lines 149-198)

```python
_ALLOWED_PHASE3_EXITS = frozenset({"fixed", "atr_scaled", "sigma_exit", "dist_exit"})
if exit_variant not in _ALLOWED_PHASE3_EXITS:
    raise RuntimeError(
        f"PHASE 3 H19: exit_variant={exit_variant!r} not in allowlist. "
        f"GP/PySR exits dropped per CLAUDE.md (PR-C 2026-04-25 / 2026-04-14 PySR orphan)."
    )

# Anti-leakage assertion (line 161): raise if entry-time features contain known-leaky cols
leaked = set(entry_time_features.keys()) & _LEAKY_FEATURES
assert not leaked, f"LEAKAGE: {leaked} in exit features"
```

### `assert_exit_configs_sha_pinned` (H22 + Gap 6, lines 647-722)

```python
_strict = os.environ.get("PHASE3_ALLOW_LEGACY_UNPINNED", "0") != "1"

if not handoff_manifest_path.exists() or not manifest.get("exit_config_shas"):
    if _strict:
        raise RuntimeError(f"PHASE 3 H22: manifest missing exit_config_shas section.")
    logger.warning(...)
    return

monitored = {
    "exit_selection_config.yaml": ROOT / "configs" / "exit_selection_config.yaml",
    "dist_exit_thresholds.json":  ROOT / "configs" / "frozen" / "dist_exit_thresholds.json",
    "tfm_calibration_per_cell.json": ROOT / "pipeline_state" / "phase_2" / "tfm_calibration_per_cell.json",
}
drift = []
for label, path in monitored.items():
    if pinned[label] != _config_sha256(path):
        drift.append(f"{label}: SHA drift")
if drift:
    raise RuntimeError(f"PHASE 3 FROZEN VIOLATION (gap-6): {drift}")
```

### Per-strategy execution loop (`compute_strategy_results` at line 2180)

1. **Signal replay (lines 2210-2223):** per-asset `replay_signals_on_holdout()` with effective_exit. Fallback to `package_fallback` on exit failure.
2. **Apply frozen ML (line 2226):** `apply_frozen_ml()` runs LSTM2, XGB scoring, MetaCombiner confidence on raw trades.
3. **Event clustering (lines 2231-2235):** per-asset & cross-asset `cluster_events()`.
4. **Overlay (line 2264):** `apply_frozen_overlay()` enforces concurrency_cap (per-asset), replacement_mode, skip_mechanisms.
5. **Equity curves (lines 2274-2297):** `compute_equity_curve()` on capped & uncapped positions; total_return, max_dd, Sharpe (annualization=8760 for hourly bars).
6. **Metrics (lines 2299-2346):** monthly_breakdown, per_asset, per_direction, per_bucket, skip_analysis.

### Position sizing + concurrency caps

- **Position sizing:** reads `selected_sizing` from package (`{kelly|fixed|rl}`). Fixed forces leverage=1.0; others use analytic Kelly or learned weights
- **Concurrency cap:** `apply_frozen_overlay()` enforces per-asset `concurrency_cap` from frozen_config (5/8/10/15/20/25)
- **Single-position cap:** `MAX_POSITION_NOTIONAL = $50k` (config import line 21). Applied per-trade

### Cost realism on holdout

| Cost | Source |
|---|---|
| Fees | `TAKER_FEE_BPS`, `ROUNDTRIP_COST_BPS` per-asset overrides |
| Funding | `FUNDING_PER_HOUR * holding_bars` (crypto only) |
| Slippage | `SLIPPAGE_BPS` per-asset overrides |
| Spread | `SPREAD_BPS` per-asset overrides |
| Latency | 1-bar fill delay (entry_bar → fill_bar) |
| Capacity friction | per-asset concurrency cap + $50k single-position cap |

### Capped vs uncapped equity curves (lines 2274-2297)

- **Capped:** apply_frozen_overlay enforces `MAX_POSITION_NOTIONAL` ($50k); positions sized down if exceed
- **Uncapped:** same trades, no position cap; full Kelly/learned leverage applied
- **Output:** both `final_equity_capped` + `final_equity_uncapped` returned (lines 2375-2376); Sharpe/Calmar computed on **capped** only (production-realistic)

### Outputs

| Path | Schema |
|---|---|
| `reports/phase3/per_strategy/strategy_{sid}_detail.json` | full_year_summary (pf, sharpe_capped, max_dd_capped, calmar_capped, total_return_capped/uncapped, final_equity_capped/uncapped, trade_count, win_rate), monthly_breakdown, per_asset, per_direction, per_bucket, skip_analysis, audit_flags, timestamp_start/end |
| `reports/phase3/monthly/strategy_{sid}_monthly.csv` | Monthly per-strategy breakdown |
| `pipeline_state/phase_3/{exit_variant}/{sid}_holdout.json` | Abbreviated metrics for registry |

### Replay determinism

- **Random seed:** `RANDOM_SEED` at main() entry (line 2700); feature cache pre-warmed (lines 2849-2862)
- **NaN handling:** `_build_feature_arrays()` forward-fills (line 599); bar-by-bar consistency via frozen fold indices
- **Dispatch order:** strategies in manifest order (line 2867 for-loop); per-asset in `ASSETS` order (line 2207); per-direction in signal order (no shuffle)
- **Checkpoint resume:** `_output_exists()` skips if detail_path already present and `--force` not set (line 2871)

### Open concerns / gotchas
1. **Gap-1/2 enforcement only at runtime** — packages are already frozen; re-run must regenerate Phase 2 if configs drift
2. **H22 SHA-pin only checks 3 configs** — granular per-asset pins missing
3. **DEV_MODE=1 permits legacy unsigned packages** — production relies on `verify_package_sha`
4. **Cell-manifest filter with `STRICT_MANIFEST=1`** — if manifest stale vs packages, hard-fails (line 2823)
5. **Fallback to `package_fallback` on exit error (line 2219)** — user-specified `--exit_variant` can be silently swapped; no log if error occurred (only warning printed)

### Wall-time
**~3-5 days** for ~702 strategies. Parallelizable via SLURM array (1 strategy per array task) → ~24-48h with 30-50 concurrent slots.

---

## Step 4 — Regime-Conditioned Analysis (`prompt_2`)

**Script:** [scripts/phases/phase_3/prompt_2_holdout_analysis.py](scripts/phases/phase_3/prompt_2_holdout_analysis.py) (~722 lines)

### What this step does

Per-regime, per-asset, per-direction, per-confidence-bucket breakdown. Identifies regime concentration risk, validates library philosophy fit, suggests portfolio combinations.

### CLI + entry

```bash
python -m scripts.phases.phase_3.prompt_2_holdout_analysis [--all] [--strategy_ids ...]
```
Lines 470-483.

### Inputs
- Per-strategy detail JSONs from `prompt_1` at `reports/phase3/per_strategy/strategy_{id}_detail.json` (line 80)
- Phase 3 regime feature store via `feature_store_path(phase=3, asset=asset)` (line 105)

### Procedure
1. Load per-strategy holdout results & per-asset regime labels (lines 78-124)
2. Extract 4 regime dimensions: trend, volatility, composite, phase (lines 66-71)
3. `compute_regime_breakdown()` per dimension (lines 147-169)
4. Quantify concentration via Sharpe/PnL per regime bucket (lines 172-219)
5. Validate library design intent (intent map at lines 227-274; check at 277-346)
6. Compare 9 variants within each philosophy (lines 353-398)
7. Cross-strategy correlation on daily equity curves (lines 405-451)

### Outputs
| Path | Schema |
|---|---|
| `reports/phase3/regime/strategy_{id}_regime_analysis.json` | Per-strategy 4-regime breakdown (line 611) |
| `reports/phase3/regime/regime_concentration_summary.csv` | 90×9 (strategy, philosophy, regime stats) (line 624) |
| `reports/phase3/regime/design_validation_summary.csv` | Library philosophy fit metrics (line 640) |
| `reports/phase3/regime/variant_comparison_per_philosophy.csv` | 9 variants × philosophy (line 657) |
| `reports/phase3/regime/cross_strategy_regime_correlation.csv` | Regime-conditional correlations (line 677) |
| `reports/phase3/regime/regime_diversified_portfolios.csv` | Strategy rank by regime diversification score (line 695) |

### Why this analysis matters

Validates that each library philosophy performed per **design intent** on holdout. Identifies regime concentration risk (single-regime profit), flags variants unsuitable for portfolio combination. Regime-diversified portfolio suggestions inform final capital allocation.

### Wall-time
**~2-5 min** (regime feature loading + per-asset breakdown).

### Open concerns
- BUG-P3-006: missing daily equity curves file (line 414); cross-strategy analysis fails silently if not present

---

## Step 5 — Ensemble Contribution Ablation (`prompt_3`)

**Script:** [scripts/phases/phase_3/prompt_3_ensemble_contribution.py](scripts/phases/phase_3/prompt_3_ensemble_contribution.py) (~528 lines)

### What this step does

Drop-one ablation on 11 component models. Measures Δ Sharpe per model removed — identifies models that didn't help (candidates for next-cycle prune).

### CLI + entry

```bash
python -m scripts.phases.phase_3.prompt_3_ensemble_contribution [--all] [--strategy_ids ...]
```
Lines 392-405.

### Inputs
- Trade detail JSONs (lines 66-78): `detail["trades"]` array with columns `xgb_classifier_score`, `lstm2_quality_score`, `tft2_quality_score`, `ca2_quality_score`, `timesfm_direction_consistency`, `metacombiner_final`
- Frozen skip threshold from config (line 297)

### Procedure
1. Extract trades from detail JSON (lines 81-92)
2. Define 5 ablation configs (lines 99-125):
   - `full_ensemble`: use `metacombiner_final`
   - `xgb_only`: use XGBoost blend only
   - `no_timesfm`: re-score without TimesFM (lines 147-157)
   - `no_lstm2`: re-score without LSTM2 (lines 160-168)
   - `fixed_5x`: fixed leverage instead of model leverage
3. Simulate each ablation by re-applying score function & leverage (lines 175-277)
4. Compute Δ Sharpe: `full_ensemble vs xgb_only` (lines 351-355)
5. Measure component impacts: TimesFM, LSTM2, leverage (lines 357-370)

### Outputs
| Path | Schema |
|---|---|
| `pipeline_state/phase_3/prompt_outputs/ensemble_ablation/ablation_{sid}.json` | Per-strategy 5×ablation results (line 461) |
| `pipeline_state/phase_3/prompt_outputs/ensemble_contribution_summary.json` | Aggregate deltas + impact means (line 465) |
| `pipeline_state/phase_3/prompt_outputs/ablation_comparison_table.csv` | 90×20 (strategy × ablation metrics) (line 488) |

### Why this analysis matters

Measures whether ensemble adds value over XGBoost alone. If `full_vs_xgb_mean_delta ≤ 0`, ensemble is redundant (prune). If TimesFM impact < 0, disable TimesFM next cycle. Identifies which models truly contribute to holdout performance vs overfitting.

### Wall-time
**~30-60 sec/strategy**.

### Open concerns
- Reblend functions assume canonical column names (lines 147-168); older holdout runs may lack `tft2_quality_score`/`ca2_quality_score` → KeyError instead of graceful degradation

---

## Step 6 — Leverage Analysis (`prompt_4`)

**Script:** [scripts/phases/phase_3/prompt_4_leverage_analysis.py](scripts/phases/phase_3/prompt_4_leverage_analysis.py) (~477 lines)

### What this step does

Realized leverage distribution per variant. Validates Kelly fraction performance vs target. DD-aware haircut effectiveness (variant_8b vs variant_8). Concurrency cap utilization.

### CLI + entry

```bash
python -m scripts.phases.phase_3.prompt_4_leverage_analysis [--all] [--strategy_ids ...]
```
Lines 350-354.

### Inputs
- Trade detail JSONs with `leverage`, `net_pnl_pct` columns (lines 70-74)

### Procedure
1. Compute leverage distribution stats: mean, median, std, min/max (lines 77-108)
2. Bucket distribution: `[1x, 2-3x, 3-5x, 5-7x, 7-10x, 10-15x, 15-20x, 20x+]` (lines 111-129)
3. Analyze high-leverage (>10x) trades: win rate, avg PnL, worst trade, max DD (lines 132-188)
4. Compute Pearson correlation: leverage vs outcome (lines 191-212)
5. Re-simulate with fixed 3x, 5x, 10x leverage (lines 215-257)

### Outputs
| Path | Schema |
|---|---|
| `pipeline_state/phase_3/prompt_outputs/leverage_analysis/leverage_{sid}.json` | Per-strategy distribution, buckets, correlation (line 405) |
| `pipeline_state/phase_3/prompt_outputs/leverage_aggregate_analysis.json` | Means/medians across strategies (line 409) |
| `pipeline_state/phase_3/prompt_outputs/leverage_summary_table.csv` | 90×10 (mean_leverage, max_lev, correlation, high_lev_wr, fixed_5x_pnl) (line 430) |

### Why this analysis matters

Validates that **model leverage outperforms fixed leverage** (e.g., `fixed_5x_pnl < model_pnl` indicates adaptive value). High correlation (leverage vs outcome) suggests model over-leverages winners (overfit risk). High-leverage win rate <50% flags drawdown risk. Informs leverage cap settings for production.

### Wall-time
**~30-60 sec/strategy**.

---

## Step 7 — Model Agreement (`prompt_5`)

**Script:** [scripts/phases/phase_3/prompt_5_model_agreement.py](scripts/phases/phase_3/prompt_5_model_agreement.py) (~491 lines)

### What this step does

Cross-model correlation on holdout predictions. **High agreement = ensemble adds nothing; low agreement = real diversity.** Identifies redundant models.

### CLI + entry

```bash
python -m scripts.phases.phase_3.prompt_5_model_agreement [--all] [--strategy_ids ...]
```
Lines 369-374.

### Inputs
- Trade detail JSONs with 11 model score columns (lines 79-99): `xgb_classifier_score`, `lstm2_quality_score`, `tft2_quality_score`, `ca2_signal_quality`, `tabnet_classifier_score`, `lgbm_classifier_score`, `catboost_classifier_score`, `tcn_classifier_score`, `ngb_z_score`, `leverage_prediction`, `metacombiner_raw`

### Procedure
1. Count agreement: how many models score ≥0.5 per trade (lines 117-126)
2. Measure agreement frequency: all_agree%, majority%, half%, disagree% (lines 129-170)
3. Compute win rate by agreement level (lines 173-221)
4. Individual model AUC-ROC + Spearman rank correlation vs outcome (lines 224-266)
5. MetaCombiner calibration: decile-based confidence vs actual win rate (lines 269-313)

### Outputs
| Path | Schema |
|---|---|
| `pipeline_state/phase_3/prompt_outputs/model_agreement/agreement_{sid}.json` | Per-strategy agreement patterns (line 425) |
| `pipeline_state/phase_3/prompt_outputs/model_agreement_aggregate.json` | Mean all_4_agree_pct (line 429) |
| `pipeline_state/phase_3/prompt_outputs/model_agreement_summary_table.csv` | 90×15 (line 457) |

### Why this analysis matters

If `mean all_4_agree_pct > 90%`, models are redundant (drop weaker ones). If `win_rate(all_agree) ≈ win_rate(disagree)`, ensemble adds no value. Low AUC-ROC on individual models flags model failure. High `calibration_error` (confidence vs actual outcome) suggests MetaCombiner misweights constituent models.

### Wall-time
**~30-60 sec/strategy**.

---

## Step 8 — TimesFM Accuracy (`prompt_6`)

**Script:** [scripts/phases/phase_3/prompt_6_timesfm_accuracy.py](scripts/phases/phase_3/prompt_6_timesfm_accuracy.py) (~473 lines)

### What this step does

Realized vs forecasted direction accuracy on holdout. Per-cell calibration check (792 cells from PR-G). Identifies cells that should be ineligible for next cycle.

### CLI + entry

```bash
python -m scripts.phases.phase_3.prompt_6_timesfm_accuracy [--all] [--strategy_ids ...]
```
Lines 350-354.

### Inputs
- Trade detail JSONs with: `timesfm_4h_direction`, `timesfm_12h_direction`, `timesfm_24h_direction`, `timesfm_conviction_4h`, `asset`, `net_pnl_pct` (lines 71-75)

### Procedure
1. Direction accuracy per horizon vs actual outcome (lines 78-123):
   - Compare forecast (≥0.5 → bullish) to actual (PnL > 0)
   - Baseline = 50%; flag if all horizons < 55%
2. Alignment impact: TimesFM forecast vs trade signal agreement (lines 126-208)
3. **Chi-squared test:** significant difference between aligned vs misaligned win rates (line 194)
4. Conviction analysis: quartile win rates by TimesFM confidence (lines 211-249)
5. Per-asset accuracy breakdown (lines 252-285)

### Outputs
| Path | Schema |
|---|---|
| `pipeline_state/phase_3/prompt_outputs/timesfm_accuracy/timesfm_{sid}.json` | Per-strategy direction accuracy, alignment, conviction (line 405) |
| `pipeline_state/phase_3/prompt_outputs/timesfm_accuracy_aggregate.json` | Mean/median 4h/12h/24h accuracy, mean p-value (line 409) |
| `pipeline_state/phase_3/prompt_outputs/timesfm_accuracy_summary_table.csv` | 90×5 (line 427) |

### Why this analysis matters

If `mean_4h_accuracy < 55%` across all horizons, TimesFM forecasts are near-random (disable). If alignment not statistically significant (`p > 0.05`), TimesFM disagreement doesn't hurt outcomes (doesn't help either). Per-asset accuracy identifies assets where TimesFM fails (ineligible for next cycle). Conviction quartile tests whether high confidence actually predicts wins (calibration).

### Wall-time
**~30-60 sec/strategy**.

### Open concerns
- RUNTIME-P3-005 guard added against empty `actual_direction` (lines 118, 280); legacy holdout JSON may omit TimesFM cols entirely (returns 0.0 accuracy)

---

## Audit Closeout — Phase 3 Hardening

**6 of 17 HIGH + 4 Gap fixes from 51-finding closeout target Phase 3 reader:**

| Fix | File:line | Mechanism |
|---|---|---|
| **H17** | `prompt_1_holdout_run.py:607-633` | `_refuse_dev_data` normalize separators + deny-list (raw_pool, exploratory, gp_discovery, pipeline_state/phase_{1,2}, data/signals/phase{1,2}) |
| **H18** | `prompt_1_holdout_run.py:552-587` | `_load_exit_config` hard-fails on missing exit config OR `gate_passed=False` (legacy bypass: `PHASE3_ALLOW_EXIT_FALLBACK=1`) |
| **H19** | `prompt_1_holdout_run.py:163-174` | `get_exit_for_trade` allowlists `{fixed, atr_scaled, sigma_exit, dist_exit}`; raises on `gp`/`pysr` |
| **H20** | `prompt_1_holdout_run.py:888-920` | `verify_package_sha` + `assert_required_keys` UNLESS `DEV_MODE=1` |
| **H22** | `prompt_1_holdout_run.py:647-722` | `assert_exit_configs_sha_pinned` hard-fails on missing manifest OR missing `exit_config_shas` (legacy bypass: `PHASE3_ALLOW_LEGACY_UNPINNED=1`) |
| **Gap 1** | `prompt_1_holdout_run.py:479-520` | sigma_exit raises if `SIGMA_EXIT_ELIGIBLE_FOR_PHASE3=False` OR `SIGMA_EXIT_APPROX != "per_bar_replay"` |
| **Gap 2** | `prompt_1_holdout_run.py:522-550` | dist_exit populates `asset/direction/phase=3/base_times/entry_timestamp/calibration_sidecar` |
| **Gap 6** | `prompt_1_holdout_run.py:647-722` | SHA-pin verification of `exit_selection_config.yaml`, `dist_exit_thresholds.json`, `tfm_calibration_per_cell.json` |

**Tampering detection layers:**
1. **Package-level:** Each package `sha256` (whole-package hash); Phase 3 re-hashes on load
2. **Variant-level:** `variant_tuple_sha` (16-char) for drift detection
3. **Config-level:** `manifest["exit_config_shas"]` pins 3 soft-config files
4. **Schema-level:** Manifest `schema_version` pinned
5. **Filesystem-level:** chmod 0o444 (H13 set by Phase 2)
6. **Audit trail:** Registry logged with experiment_id + timestamp

---

## Operational Follow-ups

1. **Phase 0 H21 re-emit must complete BEFORE Phase 3 launches** — currently chained as JID 8897808 with dependency on `p1_consolidate` (8855940). Without re-emit, feature stores carry 1-bar lookahead leak in `tfm_fp_fr_corr_*` cols.
2. **Phase 2.5 freeze required** — `configs/frozen/phase3_handoff/` must be populated; pre-flight refuses to launch if missing
3. **Run pre-flight first:** `python -m scripts.phases.phase_3.validate_phase3_inputs --verbose`
4. **TimesFM Phase 3 generation:** `python -m scripts.phases.phase_3.generate_timesfm_phase3 --all-assets`
5. **Holdout execution:** SLURM array, 1 strategy per task. Estimated ~3-5 days for ~702 strategies with 30-50 concurrent slots
6. **Analysis prompts run in parallel** after holdout execution completes
7. **🚨 NO RETRAINING / NO TUNING / NO FEATURE CHANGES on holdout** — once Phase 3 launches, results are committed; rerunning with adjustments invalidates the holdout integrity

---

## Appendix A — Critical Refusal Rules (CLAUDE.md)

Phase 3 must REFUSE any request that would:

- ❌ Use trade labels, PnL, or outcome information in Phase 0 pretrained models
- ❌ Modify frozen Phase 0 pretrained weights after they are saved
- ❌ Use later-phase data to improve earlier phases
- ❌ **Inspect or optimize on holdout before the final holdout run**
- ❌ Tune thresholds on holdout
- ❌ Retrain models on holdout
- ❌ Modify frozen logic after holdout results are seen
- ❌ Mix in-sample and out-of-sample predictions dishonestly
- ❌ Silently ignore timestamp/release alignment
- ❌ Use future-aware features, labels, or transforms
- ❌ Present contaminated results as valid
- ❌ Modify immutable snapshot data during normal research
- ❌ Proceed without verifying snapshot integrity
- ❌ Run major experiments without logging them
- ❌ Carry an unbounded number of finalist schemes into Phase 3
- ❌ Choose final schemes solely by peak PF
- ❌ Skip required realism or correction rules when strong claims are being made

When refusing, explain:
1. Which rule would be broken
2. Why that would invalidate or weaken the backtest
3. What the clean alternative is, if one exists

### If holdout fails, it fails

> **CLAUDE.md verbatim:** "If holdout results are poor: do not fix it on holdout. do not quietly rerun with changes. report the raw numbers honestly. preserve holdout integrity."

---

**Document version:** 2026-04-27 (post 51-finding audit closeout + Phase 3 hardening 8/8 fixes shipped).
