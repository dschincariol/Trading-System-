# dev_core/regime_size.py
"""
Regime-aware sizing throttle.

Uses model_v2.get_current_regime() (your function takes no args in portfolio_rebalance).
Env:
  PORTFOLIO_REGIME_SIZE_ENABLE=1
  PORTFOLIO_REGIME_MULT_LOW=1.10
  PORTFOLIO_REGIME_MULT_MID=1.00
  PORTFOLIO_REGIME_MULT_HIGH=0.70
"""

import os
from typing import Tuple

from dev_core.model_v2 import get_current_regime

USE = os.environ.get("PORTFOLIO_REGIME_SIZE_ENABLE", "1") == "1"
M_LOW = float(os.environ.get("PORTFOLIO_REGIME_MULT_LOW", "1.10"))
M_MID = float(os.environ.get("PORTFOLIO_REGIME_MULT_MID", "1.00"))
M_HIGH = float(os.environ.get("PORTFOLIO_REGIME_MULT_HIGH", "0.70"))


def regime_multiplier() -> Tuple[str, float]:
    if not USE:
        return "MID", 1.0
    reg = str(get_current_regime() or "MID").upper().strip()
    if reg == "LOW":
        return reg, float(M_LOW)
    if reg == "HIGH":
        return reg, float(M_HIGH)
    return "MID", float(M_MID)
