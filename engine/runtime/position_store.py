# engine/runtime/position_store.py

from typing import Dict


# ------------------------------------------------------------
# Minimal PnL snapshot provider
# Replace internals later with real broker / portfolio logic
# ------------------------------------------------------------

def get_pnl_snapshot() -> Dict[str, float]:
    """
    Returns:
        {
            "total": float,
            "unrealized": float,
            "realized": float
        }
    """

    # TODO: Replace with real portfolio state logic
    # Safe zero default prevents dashboard crashes

    return {
        "total": 0.0,
        "unrealized": 0.0,
        "realized": 0.0,
    }
