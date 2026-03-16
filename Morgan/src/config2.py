"""
Morgan Trades SAR Strategy — Configuration (v2)

All quantifiable parameters from the strategy PDF consolidated here.
"""
from pathlib import Path

# ─── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
DATA_CACHE = BASE_DIR / "data_cache"
RESULTS_DIR = BASE_DIR / "results"
DATA_CACHE.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)

# ─── Capital & Risk ─────────────────────────────────────────────────────────
STARTING_CAPITAL = 100_000
RISK_PER_TRADE = 0.01            # 1% of account per trade (hard rule)
MAX_POSITION_PCT = 0.40          # cap position at 40% of account
MAX_CONCURRENT_POSITIONS = 5     # max 5 open at once
SLIPPAGE_PCT = 0.001             # 0.1% slippage per fill

# ─── Backtest Period ────────────────────────────────────────────────────────
START_DATE = "2019-01-01"
END_DATE = "2024-12-31"

# ─── Exit Rules ─────────────────────────────────────────────────────────────
PARTIAL_SELL_PCT = 0.20          # sell 20% at profit target
PARTIAL_SELL_THRESHOLD = 5.0     # 5x risk triggers partial (PDF: 10-30% at 5x+)
MAX_HOLD_DAYS = 90               # safety time exit (swing trades can last weeks)

# ─── Universe Filters (PDF Section 9) ───────────────────────────────────────
MIN_PRICE = 1.00                 # > $1.00 — avoid penny stocks
MIN_ADR_PCT = 5.0                # ADR% > 5%
MIN_DAILY_DOLLAR_VOL = 3_500_000 # avg daily $ volume > $3.5M
RS_TOP_PCT = 0.02                # top 2% relative strength

# ─── Breakout Detection (PDF Section 4.1 + 9) ──────────────────────────────
PRIOR_MOVE_PCT = 0.30            # >= 30% gain over multiple days/weeks
PRIOR_MOVE_WINDOW = (5, 60)      # lookback range in trading days
CONSOLIDATION_MIN_BARS = 3       # minimum bars in consolidation
CONSOLIDATION_MAX_BARS = 20      # max bars to look back for consolidation

# Volume rules (PDF: volume drying up, then breakout on HIGH VOLUME)
VOLUME_CONTRACTION_RATIO = 0.70  # pullback vol < 70% of 20-day avg
BREAKOUT_VOL_RATIO = 1.50        # breakout vol > 150% of 20-day avg

# ─── Entry Filters ──────────────────────────────────────────────────────────
MAX_GAP_DOWN_PCT = 0.02          # skip if gap down > 2%
MAX_GAP_UP_PCT = 0.10            # skip if gap up > 10%
MAX_STOP_WIDTH_PCT = 0.08        # skip if stop > 8% from entry

# ─── Smoke Test Tickers ────────────────────────────────────────────────────
SMOKE_TICKERS = [
    "NVDA", "SMCI", "PLTR", "CELH", "CAVA", "DUOL", "APP", "CRDO",
    "ANET", "DECK", "LLY", "COST", "META", "AVGO", "TOST",
]
