"""
Configuration loader for GMX V2 Telegram Trading Bot.

Reads .env via dotenv, parses all env vars into typed fields,
and exposes a Config dataclass + load_config() function.

No side effects besides config parsing.
"""

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Set, Union

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from web3 import Web3


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Parsing helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int = 0) -> int:
    try:
        return int(_env(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float = 0.0) -> float:
    try:
        return float(_env(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    return _env(key, str(default)).strip().lower() in ("true", "1", "yes")


def _env_list(key: str, default: str = "") -> List[str]:
    """Split comma-separated env var into a list of stripped, non-empty strings."""
    return [s.strip() for s in _env(key, default).split(",") if s.strip()]


def _parse_chat_id(raw: str):
    """Parse a Telegram chat ID from string.

    Returns:
        int — for numeric chat IDs (e.g. "-1001234567890")
        str — for username handles (e.g. "@username") or "me"
        0   — if empty/unparseable
    """
    raw = raw.strip()
    if not raw:
        return 0
    # "me" is a Telethon shorthand for saved-messages
    if raw.lower() == "me":
        return "me"
    # Username handles (Telethon accepts these directly)
    if raw.startswith("@"):
        return raw
    try:
        return int(raw)
    except ValueError:
        # Could be a channel name without @
        if raw.isalpha() or ("_" in raw and raw.replace("_", "").isalnum()):
            return raw
        return 0


def parse_telegram_channels(raw: str) -> List[str]:
    """Parse TELEGRAM_CHANNELS robustly.

    Accepts:
      - @username      → kept as-is
      - -1001234567890 → kept as-is (full supergroup/channel ID)
      - 1234567890     → normalised to "-1001234567890" (bare numeric missing -100 prefix)
      - username       → prefixed with @

    Returns a list of channel identifiers ready for Telethon.
    """
    channels: List[str] = []
    for ch in raw.split(","):
        ch = ch.strip()
        if not ch:
            continue

        # Try as numeric ID
        try:
            num = int(ch)
            if num < 0:
                channels.append(ch)
            elif len(str(num)) >= 10:
                channels.append(f"-100{num}")
            else:
                channels.append(ch)
            continue
        except ValueError:
            pass

        # String — @username or bare username
        if ch.startswith("@"):
            channels.append(ch)
        else:
            channels.append(f"@{ch}")

    return channels


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Static constants (not configurable via .env)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ALLOWED_SYMBOLS: Set[str] = {"BTC", "ETH", "SOL", "LINK"}

CHAINLINK_FEEDS: Dict[str, str] = {
    "BTC":  "0x6ce185860a4963106506C203335A2910413708e9",
    "ETH":  "0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612",
    "SOL":  "0x24ceA4b8ce57cdA5058b924B9B9987992450590c",
    "LINK": "0x86E53CF1B870786351Da77A57575e79CB55812CB",
}

CHAINLINK_ABI = [
    {
        "name": "latestRoundData", "type": "function", "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
    },
    {
        "name": "decimals", "type": "function", "stateMutability": "view",
        "inputs": [], "outputs": [{"type": "uint8"}],
    },
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Config dataclass
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class Config:
    """All bot configuration in one immutable place."""

    # ── Telegram ──
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_session: str = "gmx_advanced_session"
    telegram_channels: List[str] = field(default_factory=list)
    notify_chat: Any = 0  # int (chat ID), str (@username), or 0 (disabled)
    admin_chat: Any = 0   # int (chat ID), str (@username), or 0 (disabled)
    admin_usernames: List[str] = field(default_factory=list)

    # ── Bot API (separate from Telethon user session) ──
    telegram_bot_token: str = ""
    bot_admin_chat_id: str = ""

    # ── VIP Promo ──
    vip_group_chat_id: str = ""
    salesbot_username: str = ""

    # ── Network & Web3 ──
    network: str = "arbitrum"
    rpc_url: str = "https://arb1.arbitrum.io/rpc"
    private_key: str = ""
    private_key_2: str = ""
    private_key_3: str = ""
    private_key_4: str = ""

    # ── GMX V2 Addresses ──
    exchange_router: str = ""
    order_vault: str = ""
    default_market: str = ""
    collateral_token: str = ""
    markets: Dict[str, str] = field(default_factory=dict)

    # ── Trading ──
    min_leverage: float = 5.0
    max_leverage: float = 10.0
    max_position_usd: float = 50000.0
    min_position_usd: float = 2.0
    portfolio_pct: float = 0.20
    free_balance_after: int = 2      # After N open trades, size from free USDC only
    slippage_bps: int = 30
    execution_fee_wei: int = 0
    max_price_deviation: float = 0.05

    # ── Safety ──
    require_sl: bool = True
    require_tp: bool = True
    dry_run: bool = True

    # ── Prices ──
    price_max_age_s: int = 15
    price_update_interval: float = 10.0

    # ── Heartbeat ──
    heartbeat_interval: int = 30
    halt_on_price_stale: int = 120
    auto_resume_after: int = 300

    # ── Bitunix ──
    bitunix_api_key: str = ""
    bitunix_secret_key: str = ""
    bitunix_margin_mode: str = "ISOLATION"  # "ISOLATION" or "CROSS"
    bitunix_deposit_address: str = ""  # Arbitrum USDC deposit address for Bitunix

    # ── Exchange Mode ──
    # "gmx" = GMX only, "bitunix" = Bitunix only, "mirror" = both execute same trades
    exchange_mode: str = "gmx"

    # ── Logging ──
    log_level: str = "INFO"



def load_config() -> Config:
    """Read .env / environment variables and return a fully populated Config."""

    raw_channels = _env("TELEGRAM_CHANNELS", "")
    channels = parse_telegram_channels(raw_channels)

    admin_usernames = [
        u.strip().lstrip("@").lower()
        for u in _env("ADMIN_USERNAMES", "").split(",")
        if u.strip()
    ]

    default_market = _env("GMX_V2_MARKET", "").strip()
    markets = {
        "BTC":  _env("GMX_V2_MARKET_BTC",  _env("GMX_V2_MARKET", "0x47c031236e19d024b42f8ae6780e44a573170703")).strip(),
        "ETH":  _env("GMX_V2_MARKET_ETH",  "0x70d95587d40A2caf56bd97485aB3Eec10Bee6336").strip(),
        "SOL":  _env("GMX_V2_MARKET_SOL",  "0x09400D9DB990D5ed3f35D7be61DfAEB900Af03C9").strip(),
        "LINK": _env("GMX_V2_MARKET_LINK", "0x7f1fa204bb700853D36994DA19F830b6Ad18455C").strip(),
    }

    rpc_url = _env("ARBITRUM_RPC_URL") or _env("RPC_URL", "https://arb1.arbitrum.io/rpc")

    cfg = Config(
        # Telegram
        telegram_api_id=_env_int("TELEGRAM_API_ID", 0),
        telegram_api_hash=_env("TELEGRAM_API_HASH", ""),
        telegram_session=_env("TELEGRAM_SESSION", "gmx_advanced_session"),
        telegram_channels=channels,
        notify_chat=_parse_chat_id(_env("NOTIFY_CHAT", "")),
        admin_chat=_parse_chat_id(_env("ADMIN_CHAT", "")),
        admin_usernames=admin_usernames,

        # Bot API
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN", ""),
        bot_admin_chat_id=_env("ADMIN_CHAT_ID", ""),

        # VIP Promo
        vip_group_chat_id=_env("VIP_GROUP_CHAT_ID", ""),
        salesbot_username=_env("SALESBOT_USERNAME", ""),

        # Network
        network=_env("NETWORK", "arbitrum").lower(),
        rpc_url=rpc_url,
        private_key=_env("PRIVATE_KEY", ""),
        private_key_2=_env("PRIVATE_KEY_2", ""),
        private_key_3=_env("PRIVATE_KEY_3", ""),
        private_key_4=_env("PRIVATE_KEY_4", ""),

        # GMX V2
        exchange_router=_env("GMX_V2_EXCHANGE_ROUTER", "").strip(),
        order_vault=_env("GMX_V2_ORDER_VAULT", "").strip(),
        default_market=default_market,
        collateral_token=_env("GMX_V2_COLLATERAL_TOKEN", "").strip(),
        markets=markets,

        # Trading
        min_leverage=_env_float("MIN_LEVERAGE", 5.0),
        max_leverage=_env_float("MAX_LEVERAGE", 10.0),
        max_position_usd=_env_float("MAX_POSITION_USD", 50000.0),
        min_position_usd=_env_float("MIN_POSITION_USD", 2.0),
        portfolio_pct=_env_float("PORTFOLIO_PCT", 0.20),
        free_balance_after=_env_int("FREE_BALANCE_AFTER", 2),
        slippage_bps=_env_int("SLIPPAGE_BPS", 30),
        execution_fee_wei=_env_int("GMX_V2_EXECUTION_FEE_WEI", int(Web3.to_wei(0.0002, "ether"))),
        max_price_deviation=_env_float("MAX_PRICE_DEVIATION", 0.05),

        # Safety
        require_sl=_env_bool("REQUIRE_SL", True),
        require_tp=_env_bool("REQUIRE_TP", True),
        dry_run=_env_bool("DRY_RUN", True),

        # Prices
        price_max_age_s=_env_int("PRICE_MAX_AGE_S", 15),
        price_update_interval=_env_float("PRICE_UPDATE_INTERVAL", 10.0),

        # Heartbeat
        heartbeat_interval=_env_int("HEARTBEAT_INTERVAL", 30),
        halt_on_price_stale=_env_int("HALT_ON_PRICE_STALE", 120),
        auto_resume_after=_env_int("AUTO_RESUME_AFTER", 300),

        # Bitunix
        bitunix_api_key=_env("BITUNIX_API_KEY", ""),
        bitunix_secret_key=_env("BITUNIX_SECRET_KEY", ""),
        bitunix_margin_mode=(_env("BITUNIX_MARGIN_MODE", "") or _env("MARGIN_MODE", "ISOLATION")).upper(),
        bitunix_deposit_address=_env("BITUNIX_DEPOSIT_ADDRESS", ""),

        # Exchange mode
        exchange_mode=_env("EXCHANGE_MODE", "gmx").lower(),

        # Logging
        log_level=_env("LOG_LEVEL", "INFO").upper(),

    )

    # ── Post-load validation ──
    import logging as _logging
    _log = _logging.getLogger("GMXBot.config")

    missing_addrs = []
    if not cfg.exchange_router:
        missing_addrs.append("GMX_V2_EXCHANGE_ROUTER")
    if not cfg.order_vault:
        missing_addrs.append("GMX_V2_ORDER_VAULT")
    if not cfg.collateral_token:
        missing_addrs.append("GMX_V2_COLLATERAL_TOKEN")
    if missing_addrs:
        _log.error(f"Missing required GMX addresses: {', '.join(missing_addrs)}. Set them in .env")

    if cfg.min_leverage > cfg.max_leverage:
        _log.warning(f"min_leverage ({cfg.min_leverage}) > max_leverage ({cfg.max_leverage}) — swapping")
        cfg.min_leverage, cfg.max_leverage = cfg.max_leverage, cfg.min_leverage

    if cfg.min_position_usd > cfg.max_position_usd:
        _log.warning(f"min_position_usd > max_position_usd — swapping")
        cfg.min_position_usd, cfg.max_position_usd = cfg.max_position_usd, cfg.min_position_usd

    return cfg
