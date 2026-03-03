"""
Atomic JSON state persistence utilities.

Provides crash-safe read/write for all bot state files:
  - Writes via temp file + os.rename() (atomic on POSIX)
  - Keeps a .bak backup of the previous version
  - Falls back to .bak if primary file is corrupted
"""

import os
import json
import shutil
import logging
import tempfile
from typing import Any

logger = logging.getLogger("GMXBot.state_io")


def atomic_json_write(filepath: str, data: Any, indent: int = 2) -> None:
    """Write JSON data atomically: write to temp file, then os.rename().

    Creates a .bak backup of the previous file before overwriting.
    """
    dir_name = os.path.dirname(filepath) or "."

    # Create backup of existing file
    if os.path.exists(filepath):
        backup_path = filepath + ".bak"
        try:
            shutil.copy2(filepath, backup_path)
        except Exception as e:
            logger.warning(f"Failed to create backup of {filepath}: {e}")

    # Write to temp file in same directory (required for os.rename atomicity)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp", prefix=".state_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, filepath)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


_SENTINEL = object()

def safe_json_read(filepath: str, default: Any = _SENTINEL) -> Any:
    """Read JSON with fallback to .bak if primary file is corrupted."""
    for path in [filepath, filepath + ".bak"]:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r") as f:
                data = json.load(f)
            if path.endswith(".bak"):
                logger.warning(f"Primary {filepath} corrupted — loaded from backup")
            return data
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"Failed to read {path}: {e}")
            continue
    return default if default is not _SENTINEL else {}
