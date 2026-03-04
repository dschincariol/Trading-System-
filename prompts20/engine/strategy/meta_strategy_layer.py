# engine/strategy/meta_strategy_layer.py
"""
Meta Strategy Layer

trade_pipeline_job imports compute_allocations().
Keep deterministic + side-effect free.

This hook must exist because trade_pipeline_job imports it.
"""

from typing import Dict, Any


def compute_allocations(*, window_days: int, ts_ms: int, con=None) -> Dict[str, Any]:
    # Safe default: no allocation overrides.
    # This preserves pipeline structure without changing execution behavior.
    try:
        _ = int(window_days)
        _ = int(ts_ms)
    except Exception:
        pass
    return {}
