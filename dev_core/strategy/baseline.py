# dev_core/strategies/baseline.py
"""
Baseline strategy extracted from dev_core.portfolio compute_rebalance() logic.

Interface:
  build_desired(alerts, now_ms) -> dict desired[sym] = {...}
"""

from typing import Dict, List

from dev_core import portfolio as P

NAME = "baseline"

def build_desired(alerts: List[Dict], now_ms: int) -> Dict[str, Dict]:
    # Use existing portfolio gating + scoring helpers
    best = P._pick_best_per_symbol(alerts)

    candidates = sorted(best.values(), key=lambda a: float(a.get("_score", 0.0)), reverse=True)
    candidates = candidates[: max(1, int(P.PORTFOLIO_MAX_POSITIONS))]

    desired = {}
    for a in candidates:
        sym = a["symbol"]
        z = float(a["expected_z"])
        conf = float(a["confidence"])
        score = float(a["_score"])
        w = P._desired_weight(score, sym)
        side = "LONG" if z > 0 else "SHORT"
        desired[sym] = {
            "symbol": sym,
            "side": side,
            "weight": float(w),
            "source_alert_id": int(a["id"]),
            "reason": {
                "event_title": a.get("event_title", ""),
                "severity": a.get("severity", ""),
                "horizon_s": a.get("horizon_s", 0),
                "expected_z": float(z),
                "confidence": float(conf),
                "score": float(score),
            },
            "explain_json": a.get("explain_json") or "{}",
            "_strategy": NAME,
            "_now_ms": int(now_ms),
        }

    # gross normalize to portfolio gross cap
    gross = sum(abs(float(v["weight"])) for v in desired.values())
    if gross > float(P.PORTFOLIO_GROSS_CAP) and gross > 1e-9:
        scale = float(P.PORTFOLIO_GROSS_CAP) / float(gross)
        for sym in list(desired.keys()):
            desired[sym]["weight"] = float(desired[sym]["weight"]) * float(scale)

    return desired
