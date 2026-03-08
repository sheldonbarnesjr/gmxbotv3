"""
Family member data model and environment loader.

Each family member is configured via env vars:
  FAMILY_X_NAME, FAMILY_X_CHAT_ID, FAMILY_X_ENABLED,
  FAMILY_X_PRIVATE_KEY
where X = 1..5.
"""

import os
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any

logger = logging.getLogger("GMXBot.family")

MAX_FAMILY_MEMBERS = 5


@dataclass
class FamilyMember:
    id: int                         # 1-5
    name: str                       # Display name ("Mom", "Dad", etc.)
    chat_id: int                    # Telegram user ID (auth + notifications)
    enabled: bool
    private_key: str                # GMX wallet key (Arbitrum)

    # Runtime state (not from env)
    account: Optional[Any] = None   # eth_account.Account object
    positions: Dict[str, Any] = field(default_factory=dict)
    trade_history: list = field(default_factory=list)

    @property
    def short_address(self) -> str:
        if self.account:
            addr = self.account.address
            return f"{addr[:8]}...{addr[-6:]}"
        return "?"


def load_family_members() -> List[FamilyMember]:
    """Read FAMILY_1_* through FAMILY_5_* from env, return enabled members."""
    members = []
    for i in range(1, MAX_FAMILY_MEMBERS + 1):
        prefix = f"FAMILY_{i}_"
        name = os.getenv(f"{prefix}NAME", "").strip()
        if not name:
            continue

        chat_id_str = os.getenv(f"{prefix}CHAT_ID", "").strip()
        if not chat_id_str:
            logger.warning(f"Family member {i} ({name}): no CHAT_ID set, skipping")
            continue

        private_key = os.getenv(f"{prefix}PRIVATE_KEY", "").strip()
        if not private_key:
            logger.warning(f"Family member {i} ({name}): no PRIVATE_KEY set, skipping")
            continue

        enabled = os.getenv(f"{prefix}ENABLED", "true").strip().lower() in ("true", "1", "yes")

        try:
            chat_id = int(chat_id_str)
        except ValueError:
            logger.warning(f"Family member {i} ({name}): invalid CHAT_ID '{chat_id_str}'")
            continue

        member = FamilyMember(
            id=i,
            name=name,
            chat_id=chat_id,
            enabled=enabled,
            private_key=private_key,
        )
        if enabled:
            members.append(member)
            logger.info(f"Loaded family member {i}: {name} (chat_id={chat_id})")
        else:
            logger.info(f"Family member {i}: {name} — disabled")

    return members
