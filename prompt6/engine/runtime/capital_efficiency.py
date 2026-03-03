# engine/runtime/capital_efficiency.py
import time
from typing import Dict, Any


def _now_ms() -> int:
    return int(time.time() * 1000)


def capital_efficiency_snapshot(strategy_key: str, metrics: Dict[str, Any]) -> Dict[str, Any]:
    """
    Produces a capital efficiency score + scaling multiplier.
    Wire metrics from your execution analytics / decay sharpe / drawdown.
    """
    dd = float(metrics.get("drawdown", 0.0) or 0.0)
    sharpe = float(metrics.get("decay_sharpe", 0.0) or 0.0)
    slip = float(metrics.get("slippage_pct", 0.0) or 0.0)

    # conservative institutional heuristic baseline
    score = 0.0
    score += max(-2.0, min(2.0, sharpe))
    score -= min(2.0, abs(dd) * 10.0)
    score -= min(2.0, abs(slip) * 5.0)

    # map score -> multiplier
    mult = 1.0
    if score < -1.0:
        mult = 0.25
    elif score < 0.0:
        mult = 0.5
    elif score > 1.0:
        mult = 1.25

    return {
        "ok": True,
        "ts_ms": _now_ms(),
        "strategy": strategy_key,
        "score": score,
        "multiplier": mult,
        "inputs": {"drawdown": dd, "decay_sharpe": sharpe, "slippage_pct": slip},
        "reasons": [],
    }
