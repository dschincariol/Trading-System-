# dev_core/training_guard.py
import time
from typing import Optional, Dict, Any

from engine.risk_state import get_state, set_state, get_state_row


# Allowed modes:
# - enabled     : training permitted
# - disabled    : training blocked (kill switch)
# - maintenance : training blocked (maintenance window)
_ALLOWED = {"enabled", "disabled", "maintenance"}


def training_allowed() -> bool:
    return get_state("training_state", "enabled") == "enabled"


def set_training_state(enabled: bool, reason: Optional[str] = None) -> None:
    # Backwards compatible boolean setter
    set_training_mode("enabled" if enabled else "disabled", reason=reason)


def set_training_mode(mode: str, reason: Optional[str] = None) -> None:
    mode = (mode or "").strip().lower()
    if mode not in _ALLOWED:
        mode = "disabled"
    set_state("training_state", mode)
    if reason is not None:
        set_state("training_reason", str(reason))


def get_training_status() -> Dict[str, Any]:
    # Dashboard-readable status bundle (mode, reason, updated_ts_ms)
    mode, mode_ts = get_state_row("training_state", "enabled")
    reason, reason_ts = get_state_row("training_reason", "")
    ts_ms = max(int(mode_ts or 0), int(reason_ts or 0))
    return {
        "mode": str(mode),
        "allowed": (str(mode) == "enabled"),
        "reason": str(reason),
        "updated_ts_ms": int(ts_ms),
    }
