"""
Global configuration for the Morgan Trades SAR backtester.
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
RISK_PER_TRADE = 0.01            # 1% of account per trade
MAX_POSITION_PCT = 0.40          # max 40% of account in one position
MAX_CONCURRENT_POSITIONS = 5
SLIPPAGE_PCT = 0.001             # 0.1% per trade

# ─── Backtest Period ─────────────────────────────────────────────────────────
START_DATE = "2019-01-01"
END_DATE = "2024-12-31"

# ─── Exit Rules ──────────────────────────────────────────────────────────────
PARTIAL_SELL_PCT = 0.20          # sell 20% at profit target
PARTIAL_SELL_THRESHOLD = 5.0     # 5x risk triggers partial
MAX_HOLD_DAYS = 60               # safety time exit

# ─── Universe Filters ───────────────────────────────────────────────────────
MIN_PRICE = 1.00
MIN_ADR_PCT = 5.0
MIN_DAILY_DOLLAR_VOL = 3_500_000
RS_TOP_PCT = 0.02                # top 2% relative strength

# ─── Breakout Detection ─────────────────────────────────────────────────────
PRIOR_MOVE_PCT = 0.30            # 30% prior move minimum
PRIOR_MOVE_WINDOW = (5, 60)      # lookback range in trading days
CONSOLIDATION_MIN_BARS = 3
VOLUME_DRY_UP_RATIO = 0.60       # pullback vol < 60% of move vol
BREAKOUT_VOL_RATIO = 1.5         # breakout vol > 1.5x 20-day avg

# ─── Smoke Test Tickers ─────────────────────────────────────────────────────
SMOKE_TICKERS = [
    "NVDA", "SMCI", "PLTR", "CELH", "CAVA", "DUOL", "APP", "CRDO",
    "ANET", "DECK", "LLY", "COST", "META", "AVGO", "TOST",
]
