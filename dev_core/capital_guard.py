# dev_core/capital_guard.py
import os
import time
from dev_core.drawdown_state import get_current_drawdown
from dev_core.risk_state import get_state, set_state

# thresholds
MAX_DRAWDOWN = float(os.environ.get("CAPITAL_STOP_DRAWDOWN", "0.25"))  # 25%
COOLDOWN_DAYS = int(os.environ.get("CAPITAL_COOLDOWN_DAYS", "5"))


def trading_allowed(con=None) -> bool:
    state = get_state("trading_state", "enabled")
    if state != "enabled":
        return False

    dd = get_current_drawdown(con)
    if dd >= MAX_DRAWDOWN:
        set_state("trading_state", "stopped")
        set_state("stop_reason", f"drawdown={dd:.2%}")
        set_state("stop_ts_ms", str(int(time.time() * 1000)))
        return False

    return True


def maybe_release_cooldown(con=None):
    """
    Re-enable trading after cooldown days AND drawdown improved.
    """
    import time

    state = get_state("trading_state", "enabled")
    if state != "stopped":
        return

    ts = int(get_state("stop_ts_ms", "0") or "0")
    if ts <= 0:
        return

    days = (time.time() * 1000 - ts) / (86400 * 1000)
    if days < COOLDOWN_DAYS:
        return

    dd = get_current_drawdown(con)
    if dd < MAX_DRAWDOWN * 0.75:
        set_state("trading_state", "enabled")
        set_state("stop_reason", "")
