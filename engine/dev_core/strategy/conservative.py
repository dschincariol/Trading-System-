# dev_core/strategies/conservative.py
"""
Conservative variant:
- stricter thresholds (conf, abs(z))
- smaller gross cap (local), still bounded by portfolio per-symbol caps
- fewer positions (optional override)

Interface:
  build_desired(alerts, now_ms) -> dict desired[sym] = {...}
"""

import os
from typing import Dict, List

from engine.dev_core import portfolio as P

NAME = "conservative"

MIN_CONF = float(os.environ.get("STRAT_CONSERVATIVE_MIN_CONF", "0.70"))
MIN_ABS_Z = float(os.environ.get("STRAT_CONSERVATIVE_MIN_ABS_Z", "1.60"))
MAX_POS = int(os.environ.get("STRAT_CONSERVATIVE_MAX_POSITIONS", "2"))
GROSS_CAP = float(os.environ.get("STRAT_CONSERVATIVE_GROSS_CAP", "0.70"))
SCORE_NORM = float(os.environ.get("STRAT_CONSERVATIVE_SCORE_NORM", str(P.PORTFOLIO_SCORE_NORM)))

def _clamp(x, lo, hi):
    return max(float(lo), min(float(hi), float(x)))

def build_desired(alerts: List[Dict], now_ms: int) -> Dict[str, Dict]:
    # local gating
    filtered = []
    for a in alerts or []:
        try:
            z = float(a.get("expected_z", 0.0))
            c = float(a.get("confidence", 0.0))
            if c < float(MIN_CONF):
                continue
            if abs(z) < float(MIN_ABS_Z):
                continue
            filtered.append(a)
        except Exception:
            continue

    best = {}
    for a in filtered:
        sym = a["symbol"]
        z = float(a["expected_z"])
        conf = float(a["confidence"])
        score = P._score_from_alert(z, conf, a.get("severity"))
        cur = best.get(sym)
        if (cur is None) or (score > float(cur.get("_score", 0.0))):
            b = dict(a)
            b["_score"] = float(score)
            best[sym] = b

    candidates = sorted(best.values(), key=lambda a: float(a.get("_score", 0.0)), reverse=True)
    candidates = candidates[: max(1, int(MAX_POS))]

    desired = {}
    for a in candidates:
        sym = a["symbol"]
        z = float(a["expected_z"])
        conf = float(a["confidence"])
        score = float(a["_score"])

        # weight formula like baseline but with local caps
        w = (float(score) / float(SCORE_NORM)) * float(GROSS_CAP)
        w = _clamp(w, 0.0, P._symbol_cap(sym))
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
                "min_conf": float(MIN_CONF),
                "min_abs_z": float(MIN_ABS_Z),
            },
            "explain_json": a.get("explain_json") or "{}",
            "_strategy": NAME,
            "_now_ms": int(now_ms),
        }

    # gross normalize to MIN(GROSS_CAP, portfolio gross cap)
    gross_cap = min(float(GROSS_CAP), float(P.PORTFOLIO_GROSS_CAP))
    gross = sum(abs(float(v["weight"])) for v in desired.values())
    if gross > float(gross_cap) and gross > 1e-9:
        scale = float(gross_cap) / float(gross)
        for sym in list(desired.keys()):
            desired[sym]["weight"] = float(desired[sym]["weight"]) * float(scale)

    return desired
