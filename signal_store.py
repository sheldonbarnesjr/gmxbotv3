"""
Persistent Signal Store for GMX V2 Trading Bot.

Archives every parsed signal with a unique ID, links signals to positions,
and tracks rejection reasons. Survives restarts via JSON persistence.
"""

import uuid
import time
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict

from state_io import atomic_json_write, safe_json_read

logger = logging.getLogger("GMXBot.signal_store")

SIGNAL_STORE_FILE = "signal_store.json"
MAX_SIGNALS = 500  # keep last N signals in memory/disk


@dataclass
class StoredSignal:
    signal_id: str
    raw_text: str
    symbol: str
    side: str
    entry_low: float
    entry_high: float
    take_profits: List[Dict]       # [{price, close_pct}, ...]
    stop_loss: float
    leverage: float
    trade_type: str                # "swing" or "scalp"
    timestamp_received: float
    timestamp_executed: Optional[float] = None
    position_id: Optional[str] = None
    status: str = "pending"        # pending / executed / rejected
    rejection_reason: Optional[str] = None
    wallet_id: Optional[int] = None
    source_channel: Optional[str] = None


class SignalStore:
    """Persistent signal archive."""

    def __init__(self):
        self.signals: Dict[str, StoredSignal] = {}
        self._load()

    def _load(self):
        data = safe_json_read(SIGNAL_STORE_FILE, default=[])
        for entry in data:
            try:
                sig = StoredSignal(**entry)
                self.signals[sig.signal_id] = sig
            except Exception as e:
                logger.warning(f"Skipping corrupt signal store entry: {e}")

    def _save(self):
        all_sigs = sorted(
            self.signals.values(),
            key=lambda s: s.timestamp_received,
            reverse=True,
        )
        to_save = all_sigs[:MAX_SIGNALS]
        # Prune in-memory dict to prevent unbounded growth
        if len(self.signals) > MAX_SIGNALS:
            saved_ids = {s.signal_id for s in to_save}
            self.signals = {k: v for k, v in self.signals.items() if k in saved_ids}
        try:
            atomic_json_write(SIGNAL_STORE_FILE, [asdict(s) for s in to_save])
        except Exception as e:
            logger.warning(f"Failed to save signal store: {e}")

    def record_signal(self, signal, raw_text: str, source_channel: str = None) -> str:
        """Record a parsed signal. Returns signal_id."""
        signal_id = str(uuid.uuid4())
        stored = StoredSignal(
            signal_id=signal_id,
            raw_text=raw_text,
            symbol=signal.symbol,
            side=signal.side,
            entry_low=signal.entry_low,
            entry_high=signal.entry_high,
            take_profits=[
                {"price": tp.price, "close_pct": tp.close_pct}
                for tp in signal.take_profits
            ],
            stop_loss=signal.stop_loss,
            leverage=signal.leverage,
            trade_type=getattr(signal, "trade_type", "scalp"),
            timestamp_received=time.time(),
            source_channel=source_channel,
        )
        self.signals[signal_id] = stored
        self._save()
        return signal_id

    def mark_executed(self, signal_id: str, position_id: str, wallet_id: int):
        sig = self.signals.get(signal_id)
        if sig:
            sig.status = "executed"
            sig.timestamp_executed = time.time()
            sig.position_id = position_id
            sig.wallet_id = wallet_id
            self._save()

    def mark_rejected(self, signal_id: str, reason: str):
        sig = self.signals.get(signal_id)
        if sig:
            sig.status = "rejected"
            sig.rejection_reason = reason
            self._save()

    def get_signal_for_position(self, position_id: str) -> Optional[StoredSignal]:
        for sig in self.signals.values():
            if sig.position_id == position_id:
                return sig
        return None

    def get_recent(self, n: int = 10) -> List[StoredSignal]:
        return sorted(
            self.signals.values(),
            key=lambda s: s.timestamp_received,
            reverse=True,
        )[:n]
